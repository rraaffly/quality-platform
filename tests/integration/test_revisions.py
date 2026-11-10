import hashlib
from pathlib import Path

import pytest

from quality_platform.domain.models import (
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersion,
    ProjectRecord,
)
from quality_platform.domain.schemas import TestCase, TestStep
from quality_platform.rendering.test_cases import write_test_case_artifacts
from quality_platform.services.revisions import RevisionApproval, RevisionService
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


def make_case(case_id: str, priority: str = "MEDIUM") -> TestCase:
    return TestCase(
        id=case_id,
        title=f"Case {case_id}",
        intent=f"Verify {case_id}",
        requirement_ids=("R-LOGIN",),
        scenario=f"Scenario {case_id}",
        priority=priority,
        steps=(TestStep(action="Act", expected_result="Observe"),),
    )


def prepared(tmp_path: Path) -> tuple[RevisionService, QaRepositories, list[TestCase]]:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Project", root=tmp_path))
    cases = [make_case("TC-011"), make_case("TC-012"), make_case("TC-013")]
    directory = tmp_path / "P1" / "artifacts" / "CASES-1" / "v1"
    json_path = write_test_case_artifacts(directory, cases, "detailed")["json"]
    repo.save_artifact_version(
        "P1",
        ArtifactVersion(
            id="CASES-1",
            kind=ArtifactKind.TEST_CASES,
            scope_id="LOGIN",
            version=1,
            content_hash=hashlib.sha256(json_path.read_bytes()).hexdigest(),
            requirement_baseline_id="BASE-1",
            status=ArtifactStatus.READY_FOR_REVIEW,
        ),
        idempotency_key="cases-v1",
    )

    def revise(case: TestCase, instruction: str) -> TestCase:
        assert case.id == "TC-012"
        assert "CRITICAL" in instruction
        return case.model_copy(update={"priority": "CRITICAL"})

    return RevisionService(repo, project_id="P1", workspace=tmp_path, reviser=revise), repo, cases


def test_targeted_revision_leaves_every_other_case_unchanged(tmp_path: Path) -> None:
    service, _repo, original = prepared(tmp_path)

    draft = service.prepare_revision(
        "Change only TC-012 priority to CRITICAL.",
        "CASES-1",
    )

    before = {case.id: case for case in original}
    after = {case.id: case for case in draft.after.cases}
    assert draft.target_case_ids == ("TC-012",)
    assert draft.manifest.changed_ids == ("TC-012",)
    assert after["TC-011"] == before["TC-011"]
    assert after["TC-013"] == before["TC-013"]
    assert after["TC-012"].priority == "CRITICAL"
    assert not service.artifact_path("CASES-1", 2).exists()


def test_revision_requires_review_of_the_exact_diff_before_commit(tmp_path: Path) -> None:
    service, repo, _original = prepared(tmp_path)
    draft = service.prepare_revision("Change only TC-012 priority to CRITICAL.", "CASES-1")

    with pytest.raises(PermissionError, match="reviewed diff"):
        service.commit_revision(
            draft,
            RevisionApproval(
                approved=True,
                actor="QA",
                reviewed_diff_hash="not-the-draft-diff",
            ),
        )
    assert not service.artifact_path("CASES-1", 2).exists()
    assert repo.read_artifact_version("P1", "CASES-1").version == 1

    committed = service.commit_revision(
        draft,
        RevisionApproval(
            approved=True,
            actor="QA",
            reviewed_diff_hash=draft.diff_hash,
        ),
    )

    assert committed.version == 2
    assert committed.status == ArtifactStatus.DRAFT
    assert service.artifact_path("CASES-1", 2).exists()
    assert repo.read_artifact_version("P1", "CASES-1").version == 2
    assert service.load_cases("CASES-1", 1).cases[1].priority == "MEDIUM"
    assert service.load_cases("CASES-1", 2).cases[1].priority == "CRITICAL"


def test_replayed_revision_approval_is_idempotent(tmp_path: Path) -> None:
    service, repo, _original = prepared(tmp_path)
    draft = service.prepare_revision("Change only TC-012 priority to CRITICAL.", "CASES-1")
    approval = RevisionApproval(
        approved=True,
        actor="QA",
        reviewed_diff_hash=draft.diff_hash,
    )

    first = service.commit_revision(draft, approval)
    replay = service.commit_revision(draft, approval)

    assert replay == first
    assert replay.version == 2
    artifact_events = [
        event for event in repo.list_decisions("P1") if event.event_type == "artifact"
    ]
    assert len(artifact_events) == 2
