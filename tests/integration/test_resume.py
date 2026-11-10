import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from quality_platform.domain.models import (
    ApprovalRecord,
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersion,
    ProjectRecord,
    Requirement,
    RequirementReadiness,
    SourceDocument,
    SourceReference,
    WorkflowRecord,
)
from quality_platform.domain.policies import Decision
from quality_platform.services.approvals import ApprovalService, BaselineSelection
from quality_platform.services.scopes import ScopeAction, ScopeDecision, ScopeProposal, ScopeService
from quality_platform.storage.checkpoints import create_checkpoint_saver
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.gates import HumanResponse, ReviewTarget
from quality_platform.workflows.graph import WorkflowRuntime, WorkflowServices


def setup_services(tmp_path: Path, *, workflow_id: str = "W1") -> WorkflowServices:
    repo = QaRepositories(open_database(tmp_path / "domain.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Project One", root=tmp_path))
    repo.create_workflow(
        WorkflowRecord(
            id=workflow_id,
            project_id="P1",
            goal="Review plan",
            thread_id=f"T-{workflow_id}",
            source_snapshot_ids=("BRD",),
        )
    )
    snapshot = tmp_path / "brd.md"
    snapshot.write_text("# BRD\n\nUsers can log in.\n", encoding="utf-8")
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
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="REQ-1",
            version=1,
            statement="Users can log in.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="REQ-1-v1",
    )
    scopes = ScopeService(repo, project_id="P1", workflow_id=workflow_id)
    scopes.create_batch(
        [ScopeProposal(id="S1", journey="Login", requirement_ids=("REQ-1",))],
        known_requirement_ids={"REQ-1"},
    )
    scopes.review_scope(
        "S1", ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    )
    approvals = ApprovalService(repo, scopes, project_id="P1", workflow_id=workflow_id)
    result = approvals.approve_scope_baseline(
        "S1",
        BaselineSelection(
            requirement_versions={"REQ-1": 1},
            source_snapshot_ids=("BRD",),
            human_reviewed=True,
        ),
        actor="QA",
    )
    assert result.baseline is not None
    baseline_id = result.baseline.id
    repo.save_artifact_version(
        "P1",
        ArtifactVersion(
            id="PLAN-1",
            kind=ArtifactKind.TEST_PLAN,
            scope_id="S1",
            version=1,
            content_hash="plan-hash",
            requirement_baseline_id=baseline_id,
            status=ArtifactStatus.READY_FOR_REVIEW,
        ),
        idempotency_key="plan-ready",
    )
    return WorkflowServices(
        repo=repo,
        project_id="P1",
        target=ReviewTarget(id="PLAN-1", version=1, scope_id="S1", baseline_id=baseline_id),
        approval_service=approvals,
    )


def approvals(repo: QaRepositories) -> list[ApprovalRecord]:
    return [
        ApprovalRecord.model_validate_json(event.payload)
        for event in repo.list_decisions("P1")
        if event.event_type == "approval"
    ]


class CrashAfterAuditWrite(QaRepositories):
    def __init__(self, repo: QaRepositories) -> None:
        super().__init__(repo.engine)
        self.crashed = False

    def commit_artifact_approval(
        self,
        project_id: str,
        artifact: ArtifactVersion,
        approval: ApprovalRecord,
        *,
        validate_eligibility: Callable[[], Decision],
    ) -> ArtifactVersion:
        committed = super().commit_artifact_approval(
            project_id,
            artifact,
            approval,
            validate_eligibility=validate_eligibility,
        )
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("crash after audit write")
        return committed


@pytest.mark.asyncio
async def test_interrupt_survives_recreated_graph_and_approval_is_idempotent(
    tmp_path: Path,
) -> None:
    services = setup_services(tmp_path)
    path = tmp_path / "checkpoints.sqlite"
    async with create_checkpoint_saver(path) as saver:
        runtime = WorkflowRuntime(services, saver)
        waiting = await runtime.run_workflow("W1")
        assert waiting.status == "WAITING_FOR_REVIEW"
        assert waiting.thread_id.startswith("T-W1:S1:PLAN-1:1:")

    async with create_checkpoint_saver(path) as saver:
        runtime = WorkflowRuntime(services, saver)
        response = HumanResponse(approved=True, actor="QA Engineer")
        approved = await runtime.resume_workflow("W1", response)
        assert approved.status == "APPROVED"
        assert (await runtime.resume_workflow("W1", response)).status == "APPROVED"
    assert len(approvals(services.repo)) == 1


@pytest.mark.asyncio
async def test_wrong_or_not_started_thread_cannot_receive_approval(tmp_path: Path) -> None:
    services = setup_services(tmp_path)
    services.repo.create_workflow(
        WorkflowRecord(id="W2", project_id="P1", goal="Other", thread_id="T-W2")
    )
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        runtime = WorkflowRuntime(services, saver)
        await runtime.run_workflow("W1")
        with pytest.raises(ValueError, match="workflow"):
            await runtime.resume_workflow("W2", HumanResponse(approved=True, actor="QA Engineer"))
    assert approvals(services.repo) == []


@pytest.mark.asyncio
async def test_rejected_review_creates_no_approval(tmp_path: Path) -> None:
    services = setup_services(tmp_path)
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        runtime = WorkflowRuntime(services, saver)
        await runtime.run_workflow("W1")
        result = await runtime.resume_workflow(
            "W1", HumanResponse(approved=False, actor="QA Engineer")
        )
        assert result.status == "REJECTED"
    assert approvals(services.repo) == []


@pytest.mark.asyncio
async def test_recovery_after_audit_write_does_not_duplicate_approval(tmp_path: Path) -> None:
    services = setup_services(tmp_path)
    path = tmp_path / "checkpoints.sqlite"
    crash_repo = CrashAfterAuditWrite(services.repo)
    crash_scopes = ScopeService(crash_repo, project_id="P1", workflow_id="W1")
    crash_approvals = ApprovalService(crash_repo, crash_scopes, project_id="P1", workflow_id="W1")
    async with create_checkpoint_saver(path) as saver:
        runtime = WorkflowRuntime(
            replace(services, repo=crash_repo, approval_service=crash_approvals), saver
        )
        await runtime.run_workflow("W1")
        with pytest.raises(RuntimeError, match="crash after audit write"):
            await runtime.resume_workflow("W1", HumanResponse(approved=True, actor="QA Engineer"))
    assert len(approvals(services.repo)) == 1
    async with create_checkpoint_saver(path) as saver:
        recovered = await WorkflowRuntime(services, saver).run_workflow("W1")
        assert recovered.status == "APPROVED"
    assert len(approvals(services.repo)) == 1


@pytest.mark.asyncio
async def test_read_grant_does_not_allow_approving_other_project_workflow(tmp_path: Path) -> None:
    services = setup_services(tmp_path)
    services.repo.create_project(ProjectRecord(id="P2", name="Project Two", root=tmp_path))
    services.repo.create_workflow(
        WorkflowRecord(id="W2", project_id="P2", goal="Other project", thread_id="T-W2")
    )
    services.repo.grant_cross_project_read("P2", "P1")
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        runtime = WorkflowRuntime(services, saver)
        with pytest.raises(PermissionError, match="cross-project"):
            await runtime.run_workflow("W2")
    assert services.repo.list_decisions("P2") == []


@pytest.mark.asyncio
async def test_human_yes_cannot_override_requirement_approval_gate(tmp_path: Path) -> None:
    services = setup_services(tmp_path)
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        runtime = WorkflowRuntime(services, saver)
        await runtime.run_workflow("W1")
        services.repo.save_requirement_version(
            "P1",
            Requirement(
                id="REQ-1",
                version=2,
                statement="Changed login rule.",
                source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
                readiness=RequirementReadiness.VALIDATED,
            ),
            idempotency_key="REQ-1-v2",
        )
        result = await runtime.resume_workflow(
            "W1", HumanResponse(approved=True, actor="QA Engineer")
        )
        assert result.status == "BLOCKED"
        assert any("stale" in reason for reason in result.reasons)
    assert approvals(services.repo) == []
