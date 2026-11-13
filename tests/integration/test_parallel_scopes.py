import asyncio
import hashlib
from collections.abc import Awaitable, Callable
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
from quality_platform.workflows.graph import RunResult, WorkflowRuntime, WorkflowServices
from quality_platform.workflows.scheduler import ScheduledTask, WorkflowScheduler


def result(task_id: str, scope_id: str, status: str = "ADVANCED") -> RunResult:
    return RunResult(
        status=status,
        thread_id=f"thread:{task_id}",
        checkpoint_ns=f"workflow:{scope_id}",
    )


@pytest.mark.asyncio
async def test_independent_scopes_respect_limit_and_blocked_scope_does_not_halt_others() -> None:
    active = 0
    peak = 0
    two_running = asyncio.Event()
    release = asyncio.Event()
    blocked_called = False

    def runner(task_id: str, scope_id: str) -> Callable[[], Awaitable[RunResult]]:
        async def run() -> RunResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_running.set()
            await release.wait()
            active -= 1
            return result(task_id, scope_id)

        return run

    async def blocked_runner() -> RunResult:
        nonlocal blocked_called
        blocked_called = True
        return result("blocked", "S-BLOCKED")

    scheduler = WorkflowScheduler(
        (
            ScheduledTask("A", "W1", "S1", runner("A", "S1")),
            ScheduledTask("B", "W1", "S2", runner("B", "S2")),
            ScheduledTask("C", "W1", "S3", runner("C", "S3")),
            ScheduledTask(
                "BLOCKED",
                "W1",
                "S-BLOCKED",
                blocked_runner,
                blocked_reasons=("open clarification",),
            ),
        )
    )

    running = asyncio.create_task(scheduler.run_eligible_tasks("W1", max_concurrency=2))
    await asyncio.wait_for(two_running.wait(), timeout=1)
    assert peak == 2
    release.set()
    observed = await running

    assert observed.peak_concurrency == 2
    assert {item.scope_id: item.status for item in observed.outcomes} == {
        "S1": "ADVANCED",
        "S2": "ADVANCED",
        "S3": "ADVANCED",
        "S-BLOCKED": "BLOCKED",
    }
    assert blocked_called is False


@pytest.mark.asyncio
async def test_scheduler_cancellation_waits_for_scope_tasks_to_stop() -> None:
    started = asyncio.Event()
    stopped: set[str] = set()
    running_count = 0

    def runner(task_id: str, scope_id: str) -> Callable[[], Awaitable[RunResult]]:
        async def run() -> RunResult:
            nonlocal running_count
            running_count += 1
            if running_count == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(task_id)
            return result(task_id, scope_id)

        return run

    scheduler = WorkflowScheduler(
        (
            ScheduledTask("A", "W1", "S1", runner("A", "S1")),
            ScheduledTask("B", "W1", "S2", runner("B", "S2")),
        )
    )
    parent = asyncio.create_task(scheduler.run_eligible_tasks("W1", max_concurrency=2))
    await asyncio.wait_for(started.wait(), timeout=1)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent

    assert stopped == {"A", "B"}
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("qa-scope:")
    ]


def prepared_runtimes(tmp_path: Path) -> tuple[QaRepositories, dict[str, WorkflowServices]]:
    repo = QaRepositories(open_database(tmp_path / "domain.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Project", root=tmp_path))
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Review independent scopes",
            thread_id="T1",
            source_snapshot_ids=("BRD",),
        )
    )
    source = tmp_path / "brd.md"
    source.write_text("# Rules\n\nIndependent rule.\n", encoding="utf-8")
    repo.save_source_document(
        SourceDocument(
            id="BRD",
            project_id="P1",
            input_type="BRD",
            content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            source_path=source,
            snapshot_path=source,
            line_count=3,
        )
    )
    repo.save_requirement_version(
        "P1",
        Requirement(
            id="R1",
            version=1,
            statement="Independent rule.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        idempotency_key="R1-v1",
    )
    scopes = ScopeService(repo, project_id="P1", workflow_id="W1")
    scopes.create_batch(
        [
            ScopeProposal(id="S1", journey="First", requirement_ids=("R1",)),
            ScopeProposal(id="S2", journey="Second", requirement_ids=("R1",)),
        ],
        known_requirement_ids={"R1"},
    )
    services: dict[str, WorkflowServices] = {}
    for scope_id in ("S1", "S2"):
        scopes.review_scope(
            scope_id,
            ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True),
        )
        approvals = ApprovalService(repo, scopes, project_id="P1", workflow_id="W1")
        baseline = approvals.approve_scope_baseline(
            scope_id,
            BaselineSelection(
                requirement_versions={"R1": 1},
                source_snapshot_ids=("BRD",),
                human_reviewed=True,
            ),
            actor="QA",
        ).baseline
        assert baseline is not None
        artifact_id = f"PLAN-{scope_id}"
        repo.save_artifact_version(
            "P1",
            ArtifactVersion(
                id=artifact_id,
                kind=ArtifactKind.TEST_PLAN,
                scope_id=scope_id,
                version=1,
                content_hash=f"hash-{scope_id}",
                requirement_baseline_id=baseline.id,
                status=ArtifactStatus.READY_FOR_REVIEW,
            ),
            idempotency_key=f"artifact-{scope_id}",
        )
        services[scope_id] = WorkflowServices(
            repo=repo,
            project_id="P1",
            target=ReviewTarget(
                id=artifact_id,
                version=1,
                scope_id=scope_id,
                baseline_id=baseline.id,
            ),
            approval_service=approvals,
        )
    return repo, services


def run_runtime(runtime: WorkflowRuntime) -> Callable[[], Awaitable[RunResult]]:
    async def run() -> RunResult:
        return await runtime.run_workflow("W1")

    return run


@pytest.mark.asyncio
async def test_scope_checkpoints_survive_shutdown_and_resume_does_not_double_write(
    tmp_path: Path,
) -> None:
    repo, services = prepared_runtimes(tmp_path)
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    async with create_checkpoint_saver(checkpoint_path) as saver:
        runtimes = {
            scope_id: WorkflowRuntime(service, saver) for scope_id, service in services.items()
        }
        scheduler = WorkflowScheduler(
            tuple(
                ScheduledTask(
                    id=f"review-{scope_id}",
                    workflow_id="W1",
                    scope_id=scope_id,
                    run=run_runtime(runtime),
                )
                for scope_id, runtime in runtimes.items()
            )
        )
        result_set = await scheduler.run_eligible_tasks("W1", max_concurrency=2)
        assert {item.status for item in result_set.outcomes} == {"WAITING_FOR_REVIEW"}, result_set
        assert {item.checkpoint_ns for item in result_set.outcomes} == {
            "P1:W1:S1",
            "P1:W1:S2",
        }

    async with create_checkpoint_saver(checkpoint_path) as saver:
        first = WorkflowRuntime(services["S1"], saver)
        second = WorkflowRuntime(services["S2"], saver)
        response = HumanResponse(approved=True, actor="QA")
        assert (await first.resume_workflow("W1", response)).status == "APPROVED"
        assert (await first.resume_workflow("W1", response)).status == "APPROVED"
        assert (await second.run_workflow("W1")).status == "WAITING_FOR_REVIEW"

    approvals = [event for event in repo.list_decisions("P1") if event.event_type == "approval"]
    assert len(approvals) == 1
