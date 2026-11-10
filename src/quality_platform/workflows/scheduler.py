import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from quality_platform.workflows.graph import RunResult


@dataclass(frozen=True)
class ScheduledTask:
    id: str
    workflow_id: str
    scope_id: str
    run: Callable[[], Awaitable[RunResult]]
    blocked_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.workflow_id or not self.scope_id:
            raise ValueError("scheduled task identity fields are required")


class TaskOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    thread_id: str | None = None
    checkpoint_ns: str | None = None
    reasons: tuple[str, ...] = ()


class SchedulerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str = Field(min_length=1)
    max_concurrency: int = Field(ge=1)
    peak_concurrency: int = Field(ge=0)
    outcomes: tuple[TaskOutcome, ...]


class WorkflowScheduler:
    def __init__(self, tasks: Iterable[ScheduledTask]) -> None:
        self.tasks = tuple(tasks)
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate scheduled task ID")

    async def run_eligible_tasks(
        self, workflow_id: str, *, max_concurrency: int
    ) -> SchedulerResult:
        if not workflow_id:
            raise ValueError("workflow ID is required")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        selected = tuple(task for task in self.tasks if task.workflow_id == workflow_id)
        semaphore = asyncio.Semaphore(max_concurrency)
        outcomes: dict[str, TaskOutcome] = {}
        active = 0
        peak = 0

        async def execute(task: ScheduledTask) -> None:
            nonlocal active, peak
            async with semaphore:
                active += 1
                peak = max(peak, active)
                try:
                    result = await task.run()
                except Exception as error:  # noqa: BLE001 - one scope must not cancel its peers
                    outcomes[task.id] = TaskOutcome(
                        task_id=task.id,
                        scope_id=task.scope_id,
                        status="FAILED",
                        reasons=(f"{type(error).__name__}: {error}",),
                    )
                else:
                    outcomes[task.id] = TaskOutcome(
                        task_id=task.id,
                        scope_id=task.scope_id,
                        status=result.status,
                        thread_id=result.thread_id,
                        checkpoint_ns=result.checkpoint_ns,
                        reasons=result.reasons,
                    )
                finally:
                    active -= 1

        async with asyncio.TaskGroup() as group:
            for task in selected:
                if task.blocked_reasons:
                    outcomes[task.id] = TaskOutcome(
                        task_id=task.id,
                        scope_id=task.scope_id,
                        status="BLOCKED",
                        reasons=task.blocked_reasons,
                    )
                    continue
                group.create_task(execute(task), name=f"qa-scope:{task.id}")

        return SchedulerResult(
            workflow_id=workflow_id,
            max_concurrency=max_concurrency,
            peak_concurrency=peak,
            outcomes=tuple(outcomes[task.id] for task in selected),
        )
