from copy import deepcopy
from pathlib import Path

from quality_platform.domain.models import (
    ArtifactKind,
    ArtifactStaleness,
    ArtifactVersion,
    ProjectRecord,
    Requirement,
    SourceReference,
)
from quality_platform.domain.schemas import TestCase, TestStep
from quality_platform.services.impact import (
    ArtifactDependency,
    RequirementChange,
    SemanticDependency,
    detect_impact,
)
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


def test_requirement_change_marks_traced_and_uncertain_impact_without_editing_cases() -> None:
    cases = [
        TestCase(
            id="TC-012",
            title="Five failed attempts lock the account",
            intent="Verify the configured lockout threshold",
            requirement_ids=("R-LOCKOUT",),
            scenario="Five failed attempts",
            steps=(TestStep(action="Fail login five times", expected_result="Account locks"),),
        ),
        TestCase(
            id="TC-020",
            title="Reset password",
            intent="Verify password reset",
            requirement_ids=("R-RESET",),
            scenario="Reset password",
            steps=(TestStep(action="Reset password", expected_result="Password changes"),),
        ),
    ]
    unchanged = deepcopy(cases)
    change = RequirementChange(
        project_id="P1",
        requirement_id="R-LOCKOUT",
        from_version=1,
        to_version=2,
        before_statement="The account locks after 5 failed attempts.",
        after_statement="The account locks after 3 failed attempts.",
        traced_artifacts=(
            ArtifactDependency(
                project_id="P1",
                artifact_id="CASES-LOGIN",
                scope_id="LOGIN",
                requirement_ids=("R-LOCKOUT",),
            ),
        ),
        semantic_dependencies=(
            SemanticDependency(
                project_id="P1",
                artifact_id="PLAN-SECURITY",
                scope_id="SECURITY",
                reason="Security plan discusses lockout thresholds without a canonical trace.",
                confidence=0.62,
            ),
        ),
    )

    report = detect_impact(change)

    assert report.changed_requirement_version == 2
    assert report.staleness == ArtifactStaleness.POTENTIALLY_STALE
    assert report.affected_artifact_ids == ("CASES-LOGIN", "PLAN-SECURITY")
    assert report.affected_scope_ids == ("LOGIN", "SECURITY")
    direct = next(item for item in report.findings if item.artifact_id == "CASES-LOGIN")
    uncertain = next(item for item in report.findings if item.artifact_id == "PLAN-SECURITY")
    assert direct.uncertain is False
    assert direct.confidence == 1
    assert uncertain.uncertain is True
    assert uncertain.confidence == 0.62
    assert "5" in report.reasoning and "3" in report.reasoning
    assert cases == unchanged


def test_cross_project_impact_candidate_is_rejected() -> None:
    change = RequirementChange(
        project_id="P1",
        requirement_id="R-1",
        from_version=1,
        to_version=2,
        before_statement="Old",
        after_statement="New",
        traced_artifacts=(
            ArtifactDependency(
                project_id="P2",
                artifact_id="SECRET-P2",
                scope_id="PRIVATE",
                requirement_ids=("R-1",),
            ),
        ),
    )

    try:
        detect_impact(change)
    except ValueError as error:
        assert "cross-project" in str(error)
    else:
        raise AssertionError("cross-project impact data must be rejected")


def test_automatic_impact_uses_trace_of_published_version_not_partial_next_version(
    tmp_path: Path,
) -> None:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Login", root=tmp_path))
    requirement = Requirement(
        id="R1",
        version=1,
        statement="Original rule",
        source_references=(SourceReference(document_id="BRD", start_line=1, end_line=1),),
    )
    repo.save_requirement_version("P1", requirement, idempotency_key="rule1")
    repo.append_baseline("P1", "B1", '{"requirement_versions": {"R1": 1}}')
    artifact = ArtifactVersion(
        id="CASES",
        kind=ArtifactKind.TEST_CASES,
        scope_id="LOGIN",
        version=1,
        content_hash="original",
        requirement_baseline_id="B1",
    )
    repo.save_artifact_trace("P1", artifact, {"R1": 1})
    repo.save_artifact_version("P1", artifact, idempotency_key="artifact1")
    repo.save_requirement_version(
        "P1",
        requirement.model_copy(update={"version": 2, "statement": "Revised rule"}),
        idempotency_key="rule2",
    )
    assert (
        repo.read_artifact_version("P1", "CASES").staleness == ArtifactStaleness.POTENTIALLY_STALE
    )
    revised = artifact.model_copy(update={"version": 2, "content_hash": "revised"})
    repo.save_artifact_trace("P1", revised, {"R1": 2})
    repo.save_artifact_version("P1", revised, idempotency_key="artifact2")
    assert repo.read_artifact_version("P1", "CASES").staleness == ArtifactStaleness.CURRENT
    # A trace can be durable before its artifact if publication is interrupted.
    repo.save_artifact_trace("P1", revised.model_copy(update={"version": 3}), {"R1": 3})
    repo.save_requirement_version(
        "P1",
        requirement.model_copy(update={"version": 3, "statement": "Third rule"}),
        idempotency_key="rule3",
    )
    assert (
        repo.read_artifact_version("P1", "CASES").staleness == ArtifactStaleness.POTENTIALLY_STALE
    )
