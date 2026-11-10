import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quality_platform.domain.models import (
    ApprovalRecord,
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersion,
    Clarification,
    ClarificationIssueType,
    ProjectRecord,
    Requirement,
    RequirementReadiness,
    SourceDocument,
    SourceReference,
    WorkflowRecord,
)
from quality_platform.domain.policies import Decision
from quality_platform.services.approvals import (
    ApprovalInput,
    ApprovalService,
    BaselineSelection,
)
from quality_platform.services.clarifications import ClarificationService
from quality_platform.services.scopes import ScopeAction, ScopeDecision, ScopeProposal, ScopeService
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


def setup(tmp_path: Path) -> tuple[QaRepositories, ScopeService, ApprovalService]:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Project", root=tmp_path))
    repo.create_workflow(
        WorkflowRecord(
            id="W1", project_id="P1", goal="Review", thread_id="T1", source_snapshot_ids=("BRD",)
        )
    )
    snapshot = tmp_path / "brd.md"
    snapshot.write_text("# BRD\n\nApproved rule.\n", encoding="utf-8")
    repo.save_source_document(
        SourceDocument(
            id="BRD",
            project_id="P1",
            input_type="BRD",
            content_hash=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            source_path=snapshot,
            snapshot_path=snapshot,
            line_count=3,
        )
    )
    scope_service = ScopeService(repo, project_id="P1", workflow_id="W1")
    scope_service.create_batch(
        [ScopeProposal(id="S1", journey="Login", requirement_ids=("R1",))],
        known_requirement_ids={"R1"},
    )
    scope_service.review_scope(
        "S1", ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    )
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R1",
            version=1,
            statement="Approved rule.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R1-v1",
    )
    return (
        repo,
        scope_service,
        ApprovalService(repo, scope_service, project_id="P1", workflow_id="W1"),
    )


def selection(**overrides: object) -> BaselineSelection:
    raw: dict[str, object] = {
        "requirement_versions": {"R1": 1},
        "source_snapshot_ids": ("BRD",),
        "human_reviewed": True,
    }
    raw.update(overrides)
    return BaselineSelection.model_validate(raw)


def ready_artifact(baseline_id: str) -> ArtifactVersion:
    return ArtifactVersion(
        id="PLAN-1",
        kind=ArtifactKind.TEST_PLAN,
        scope_id="S1",
        version=1,
        content_hash="plan-content-hash",
        requirement_baseline_id=baseline_id,
        status=ArtifactStatus.READY_FOR_REVIEW,
    )


def test_validated_unapproved_requirement_can_draft_but_not_finalize(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    assert service.authorize_draft("S1", {"R1": 1}).allowed
    repo.save_artifact_version("P1", ready_artifact("NOT-APPROVED"), idempotency_key="plan-ready")
    decision = service.authorize_artifact_review("PLAN-1", "NOT-APPROVED")
    assert not decision.allowed
    assert any("baseline" in reason for reason in decision.reasons)


def test_scope_baseline_records_exact_versions_and_replay_is_idempotent(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    first = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert first.approved
    assert first.baseline is not None
    assert first.baseline.requirement_versions == {"R1": 1}
    assert first.baseline.source_snapshot_ids == ("BRD",)
    second = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert second.baseline == first.baseline
    assert (
        len([event for event in repo.list_decisions("P1") if event.event_type == "baseline"]) == 1
    )


def test_baseline_requires_human_review_and_current_source_snapshot(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    assert not service.approve_scope_baseline(
        "S1", selection(human_reviewed=False), actor="QA"
    ).approved
    assert not service.approve_scope_baseline(
        "S1", selection(source_snapshot_ids=("OTHER",)), actor="QA"
    ).approved
    source = repo.read_source_document("P1", "BRD")
    source.snapshot_path.write_text("tampered", encoding="utf-8")
    assert not service.approve_scope_baseline("S1", selection(), actor="QA").approved


def test_transitive_or_uncertain_external_dependency_denies_baseline(tmp_path: Path) -> None:
    repo, scope_service, service = setup(tmp_path)
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R1",
            version=2,
            statement="Approved rule with dependency.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            dependency_ids=("R2",),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R1-v2",
    )
    assert not service.approve_scope_baseline(
        "S1", selection(requirement_versions={"R1": 2}), actor="QA"
    ).approved
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R2",
            version=1,
            statement="External dependency.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R2-v1",
    )
    assert not service.approve_scope_baseline(
        "S1", selection(requirement_versions={"R1": 2, "R2": 1}), actor="QA"
    ).approved
    scope_service.create_batch(
        [ScopeProposal(id="S2", journey="External", requirement_ids=("R2",))],
        known_requirement_ids={"R2"},
    )
    assert not service.approve_scope_baseline(
        "S1", selection(requirement_versions={"R1": 2, "R2": 1}), actor="QA"
    ).approved


def test_final_artifact_approval_is_atomic_idempotent_and_historical(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    baseline_result = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert baseline_result.baseline is not None
    baseline = baseline_result.baseline
    repo.save_artifact_version("P1", ready_artifact(baseline.id), idempotency_key="plan-ready")
    assert service.authorize_artifact_review("PLAN-1", baseline.id).allowed
    human = ApprovalInput(
        approved=True,
        actor="QA",
        artifact_version=1,
        scope_id="S1",
        baseline_id=baseline.id,
        content_hash="plan-content-hash",
        reviewed_evidence=("plan diff",),
    )
    approved = service.commit_artifact_approval("PLAN-1", human)
    assert approved.status == ArtifactStatus.APPROVED
    assert approved.approval_id is not None
    assert service.commit_artifact_approval("PLAN-1", human) == approved
    approvals = [event for event in repo.list_decisions("P1") if event.event_type == "approval"]
    assert len(approvals) == 1
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R1",
            version=2,
            statement="Changed rule.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R1-v2",
    )
    assert not service.authorize_artifact_review("PLAN-1", baseline.id).allowed
    assert [
        event.payload for event in repo.list_decisions("P1") if event.event_type == "approval"
    ] == [approvals[0].payload]


def test_wrong_target_or_rejected_human_decision_cannot_commit(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    baseline_result = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert baseline_result.baseline is not None
    baseline_id = baseline_result.baseline.id
    repo.save_artifact_version("P1", ready_artifact(baseline_id), idempotency_key="plan-ready")
    with pytest.raises(ValueError, match="target"):
        service.commit_artifact_approval(
            "PLAN-1",
            ApprovalInput(
                approved=True,
                actor="QA",
                artifact_version=2,
                scope_id="S1",
                baseline_id=baseline_id,
                content_hash="plan-content-hash",
            ),
        )
    with pytest.raises(PermissionError, match="human"):
        service.commit_artifact_approval(
            "PLAN-1",
            ApprovalInput(
                approved=False,
                actor="QA",
                artifact_version=1,
                scope_id="S1",
                baseline_id=baseline_id,
                content_hash="plan-content-hash",
            ),
        )
    assert not [event for event in repo.list_decisions("P1") if event.event_type == "approval"]


def test_out_of_range_requirement_reference_cannot_enter_approved_baseline(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R1",
            version=2,
            statement="Unverifiable rule.",
            source_references=(SourceReference(document_id="BRD", start_line=99, end_line=99),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R1-v2",
    )
    result = service.approve_scope_baseline(
        "S1", selection(requirement_versions={"R1": 2}), actor="QA"
    )
    assert not result.approved
    assert any("source reference" in reason for reason in result.reasons)


def test_final_approval_requires_reviewed_evidence(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    result = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert result.baseline is not None
    repo.save_artifact_version(
        "P1", ready_artifact(result.baseline.id), idempotency_key="plan-ready"
    )
    with pytest.raises(ValueError, match="reviewed evidence"):
        service.commit_artifact_approval(
            "PLAN-1",
            ApprovalInput(
                approved=True,
                actor="QA",
                artifact_version=1,
                scope_id="S1",
                baseline_id=result.baseline.id,
                content_hash="plan-content-hash",
            ),
        )
    assert not [event for event in repo.list_decisions("P1") if event.event_type == "approval"]


def test_new_clarification_after_scope_review_blocks_baseline_and_artifact(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    baseline_result = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert baseline_result.baseline is not None
    repo.save_artifact_version(
        "P1", ready_artifact(baseline_result.baseline.id), idempotency_key="plan-ready"
    )
    ClarificationService(repo, project_id="P1", workflow_id="W1").create_batch(
        [
            Clarification(
                id="C-LATE",
                issue_type=ClarificationIssueType.MISSING_RULE,
                question="What happens after lockout?",
                source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
                affected_requirement_ids=("R1",),
            )
        ]
    )
    assert not service.approve_scope_baseline("S1", selection(), actor="QA").approved
    decision = service.authorize_artifact_review("PLAN-1", baseline_result.baseline.id)
    assert not decision.allowed
    assert any("clarification" in reason for reason in decision.reasons)


def test_ready_artifact_content_is_immutable_within_version(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    baseline_result = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert baseline_result.baseline is not None
    original = ready_artifact(baseline_result.baseline.id)
    repo.save_artifact_version("P1", original, idempotency_key="plan-ready")
    with pytest.raises(ValueError, match="content"):
        repo.save_artifact_version(
            "P1",
            original.model_copy(update={"content_hash": "different-content"}),
            idempotency_key="swap",
        )
    assert repo.read_artifact_version("P1", "PLAN-1").content_hash == original.content_hash


def test_requirement_change_between_service_check_and_commit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _scopes, service = setup(tmp_path)
    baseline_result = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert baseline_result.baseline is not None
    baseline_id = baseline_result.baseline.id
    repo.save_artifact_version("P1", ready_artifact(baseline_id), idempotency_key="plan-ready")
    original_commit = repo.commit_artifact_approval

    def race_commit(
        project_id: str,
        artifact: ArtifactVersion,
        approval: ApprovalRecord,
        *,
        validate_eligibility: Callable[[], Decision],
    ) -> ArtifactVersion:
        repo.save_requirement_version(
            "P1",
            Requirement(
                id="R1",
                version=2,
                statement="Changed rule.",
                source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
                readiness=RequirementReadiness.VALIDATED,
            ),
            idempotency_key="R1-v2",
        )
        return original_commit(
            project_id, artifact, approval, validate_eligibility=validate_eligibility
        )

    monkeypatch.setattr(repo, "commit_artifact_approval", race_commit)
    with pytest.raises(PermissionError, match="stale"):
        service.commit_artifact_approval(
            "PLAN-1",
            ApprovalInput(
                approved=True,
                actor="QA",
                artifact_version=1,
                scope_id="S1",
                baseline_id=baseline_id,
                content_hash="plan-content-hash",
                reviewed_evidence=("plan diff",),
            ),
        )
    assert not [event for event in repo.list_decisions("P1") if event.event_type == "approval"]


def test_duplicate_commit_replays_persisted_target_despite_new_timestamp(tmp_path: Path) -> None:
    repo, _scopes, service = setup(tmp_path)
    baseline_result = service.approve_scope_baseline("S1", selection(), actor="QA")
    assert baseline_result.baseline is not None
    baseline_id = baseline_result.baseline.id
    repo.save_artifact_version("P1", ready_artifact(baseline_id), idempotency_key="plan-ready")
    approved = service.commit_artifact_approval(
        "PLAN-1",
        ApprovalInput(
            approved=True,
            actor="QA",
            artifact_version=1,
            scope_id="S1",
            baseline_id=baseline_id,
            content_hash="plan-content-hash",
            reviewed_evidence=("plan diff",),
        ),
    )
    approval_event = next(
        event for event in repo.list_decisions("P1") if event.event_type == "approval"
    )
    prior = ApprovalRecord.model_validate_json(approval_event.payload)
    retry = prior.model_copy(update={"approved_at": datetime.now(UTC) + timedelta(seconds=1)})
    replayed = repo.commit_artifact_approval(
        "P1",
        approved,
        retry,
        validate_eligibility=lambda: Decision(allowed=False, reasons=["stale now"]),
    )
    assert replayed == approved
    assert (
        len([event for event in repo.list_decisions("P1") if event.event_type == "approval"]) == 1
    )


def test_new_clarification_on_external_dependency_blocks_baseline_and_artifact(
    tmp_path: Path,
) -> None:
    repo, scopes, service = setup(tmp_path)
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R1",
            version=2,
            statement="Rule requiring R2.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            dependency_ids=("R2",),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R1-v2",
    )
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R2",
            version=1,
            statement="External rule.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R2-v1",
    )
    scopes.create_batch(
        [ScopeProposal(id="S2", journey="External", requirement_ids=("R2",))],
        known_requirement_ids={"R2"},
    )
    scopes.review_scope(
        "S2", ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    )
    selected = selection(requirement_versions={"R1": 2, "R2": 1})
    baseline_result = service.approve_scope_baseline("S1", selected, actor="QA")
    assert baseline_result.baseline is not None
    repo.save_artifact_version(
        "P1", ready_artifact(baseline_result.baseline.id), idempotency_key="plan-ready"
    )
    ClarificationService(repo, project_id="P1", workflow_id="W1").create_batch(
        [
            Clarification(
                id="C-EXTERNAL",
                issue_type=ClarificationIssueType.MISSING_RULE,
                question="What is the external rule detail?",
                source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
                affected_requirement_ids=("R2",),
            )
        ]
    )
    assert not service.approve_scope_baseline("S1", selected, actor="QA").approved
    decision = service.authorize_artifact_review("PLAN-1", baseline_result.baseline.id)
    assert not decision.allowed
    assert any("C-EXTERNAL" in reason for reason in decision.reasons)
