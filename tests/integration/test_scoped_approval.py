import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from quality_platform.domain.models import (
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
from quality_platform.services.approvals import ApprovalService, BaselineSelection
from quality_platform.services.scopes import ScopeAction, ScopeDecision, ScopeProposal, ScopeService
from quality_platform.storage.checkpoints import create_checkpoint_saver
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.gates import HumanResponse, ReviewTarget
from quality_platform.workflows.graph import WorkflowRuntime, WorkflowServices


def prepared(tmp_path: Path) -> tuple[WorkflowServices, ApprovalService]:
    repo = QaRepositories(open_database(tmp_path / "domain.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Project", root=tmp_path))
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Approve plan",
            thread_id="T1",
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
            id="R1",
            version=1,
            statement="Users can log in.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R1-v1",
    )
    scope_service = ScopeService(repo, project_id="P1", workflow_id="W1")
    scope_service.create_batch(
        [ScopeProposal(id="S1", journey="Login", requirement_ids=("R1",))],
        known_requirement_ids={"R1"},
    )
    scope_service.review_scope(
        "S1", ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    )
    approvals = ApprovalService(repo, scope_service, project_id="P1", workflow_id="W1")
    baseline_result = approvals.approve_scope_baseline(
        "S1",
        BaselineSelection(
            requirement_versions={"R1": 1},
            source_snapshot_ids=("BRD",),
            human_reviewed=True,
        ),
        actor="QA",
    )
    assert baseline_result.baseline is not None
    baseline_id = baseline_result.baseline.id
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
    services = WorkflowServices(
        repo=repo,
        project_id="P1",
        target=ReviewTarget(id="PLAN-1", version=1, scope_id="S1", baseline_id=baseline_id),
        approval_service=approvals,
    )
    return services, approvals


@pytest.mark.asyncio
async def test_graph_interrupt_uses_live_scoped_approval_service(tmp_path: Path) -> None:
    services, _approvals = prepared(tmp_path)
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        runtime = WorkflowRuntime(services, saver)
        waiting = await runtime.run_workflow("W1")
        assert waiting.status == "WAITING_FOR_REVIEW"
        approved = await runtime.resume_workflow("W1", HumanResponse(approved=True, actor="QA"))
        assert approved.status == "APPROVED"
        assert services.repo.read_artifact_version("P1", "PLAN-1").status == ArtifactStatus.APPROVED
        assert (
            len(
                [
                    event
                    for event in services.repo.list_decisions("P1")
                    if event.event_type == "approval"
                ]
            )
            == 1
        )


@pytest.mark.asyncio
async def test_stale_baseline_blocks_before_human_interrupt(tmp_path: Path) -> None:
    services, _approvals = prepared(tmp_path)
    services.repo.save_requirement_version(
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
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        result = await WorkflowRuntime(services, saver).run_workflow("W1")
        assert result.status == "BLOCKED"
        assert any("stale" in reason for reason in result.reasons)
    assert not [
        event for event in services.repo.list_decisions("P1") if event.event_type == "approval"
    ]


@pytest.mark.asyncio
async def test_new_artifact_in_same_scope_receives_its_own_interrupt(tmp_path: Path) -> None:
    services, _approvals = prepared(tmp_path)
    baseline_id = services.target.baseline_id
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        first = WorkflowRuntime(services, saver)
        assert (await first.run_workflow("W1")).status == "WAITING_FOR_REVIEW"
        assert (
            await first.resume_workflow("W1", HumanResponse(approved=True, actor="QA"))
        ).status == "APPROVED"
        services.repo.save_artifact_version(
            "P1",
            ArtifactVersion(
                id="PLAN-2",
                kind=ArtifactKind.TEST_PLAN,
                scope_id="S1",
                version=1,
                content_hash="second-plan-hash",
                requirement_baseline_id=baseline_id,
                status=ArtifactStatus.READY_FOR_REVIEW,
            ),
            idempotency_key="plan-2-ready",
        )
        second = WorkflowRuntime(
            replace(
                services,
                target=ReviewTarget(id="PLAN-2", version=1, scope_id="S1", baseline_id=baseline_id),
            ),
            saver,
        )
        assert (await second.run_workflow("W1")).status == "WAITING_FOR_REVIEW"


def test_graph_rejects_approval_service_for_different_project(tmp_path: Path) -> None:
    services, approvals = prepared(tmp_path)
    with pytest.raises(ValueError, match="approval service"):
        WorkflowServices(
            repo=services.repo,
            project_id="P2",
            target=services.target,
            approval_service=approvals,
        )
