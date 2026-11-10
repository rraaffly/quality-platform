from dataclasses import replace
from pathlib import Path

import pytest

from quality_platform.domain.models import (
    ApprovalRecord,
    Clarification,
    ProjectRecord,
    WorkflowRecord,
)
from quality_platform.domain.policies import PolicyContext
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
            id=workflow_id, project_id="P1", goal="Review plan", thread_id=f"T-{workflow_id}"
        )
    )
    return WorkflowServices(
        repo=repo,
        project_id="P1",
        target=ReviewTarget(id="PLAN-1", version=1, scope_id="S1", baseline_id="B1"),
        policy_context=PolicyContext(
            human_reviewed=True,
            scope_approved=True,
            requirement_versions={"REQ-1": 1},
            approved_requirement_versions={"REQ-1": 1},
            dependencies_resolved=True,
            source_baseline_consistent=True,
        ),
    )


class CrashAfterAuditWrite(QaRepositories):
    def __init__(self, repo: QaRepositories) -> None:
        super().__init__(repo.engine)
        self.crashed = False

    def append_decision(
        self,
        project_id: str,
        event: ApprovalRecord | Clarification,
        *,
        idempotency_key: str,
        actor_project_id: str | None = None,
    ) -> int:
        sequence = super().append_decision(
            project_id, event, idempotency_key=idempotency_key, actor_project_id=actor_project_id
        )
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("crash after audit write")
        return sequence


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
        assert waiting.thread_id == "T-W1:S1"

    async with create_checkpoint_saver(path) as saver:
        runtime = WorkflowRuntime(services, saver)
        response = HumanResponse(approved=True, actor="QA Engineer")
        approved = await runtime.resume_workflow("W1", response)
        assert approved.status == "APPROVED"
        assert (await runtime.resume_workflow("W1", response)).status == "APPROVED"
    assert len(services.repo.list_decisions("P1")) == 1


@pytest.mark.asyncio
async def test_wrong_or_not_started_thread_cannot_receive_approval(tmp_path: Path) -> None:
    services = setup_services(tmp_path)
    services.repo.create_workflow(
        WorkflowRecord(id="W2", project_id="P1", goal="Other", thread_id="T-W2")
    )
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        runtime = WorkflowRuntime(services, saver)
        await runtime.run_workflow("W1")
        with pytest.raises(ValueError, match="not waiting"):
            await runtime.resume_workflow("W2", HumanResponse(approved=True, actor="QA Engineer"))
    assert services.repo.list_decisions("P1") == []


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
    assert services.repo.list_decisions("P1") == []


@pytest.mark.asyncio
async def test_recovery_after_audit_write_does_not_duplicate_approval(tmp_path: Path) -> None:
    services = setup_services(tmp_path)
    path = tmp_path / "checkpoints.sqlite"
    async with create_checkpoint_saver(path) as saver:
        runtime = WorkflowRuntime(
            replace(services, repo=CrashAfterAuditWrite(services.repo)), saver
        )
        await runtime.run_workflow("W1")
        with pytest.raises(RuntimeError, match="crash after audit write"):
            await runtime.resume_workflow("W1", HumanResponse(approved=True, actor="QA Engineer"))
    assert len(services.repo.list_decisions("P1")) == 1
    async with create_checkpoint_saver(path) as saver:
        recovered = await WorkflowRuntime(services, saver).run_workflow("W1")
        assert recovered.status == "APPROVED"
    assert len(services.repo.list_decisions("P1")) == 1


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
    services = replace(
        services,
        policy_context=services.policy_context.model_copy(
            update={"approved_requirement_versions": {}}
        ),
    )
    async with create_checkpoint_saver(tmp_path / "checkpoints.sqlite") as saver:
        runtime = WorkflowRuntime(services, saver)
        await runtime.run_workflow("W1")
        result = await runtime.resume_workflow(
            "W1", HumanResponse(approved=True, actor="QA Engineer")
        )
        assert result.status == "BLOCKED"
        assert any("REQ-1" in reason for reason in result.reasons)
    assert services.repo.list_decisions("P1") == []
