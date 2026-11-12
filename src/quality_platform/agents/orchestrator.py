from enum import StrEnum

from langchain_core.messages import HumanMessage
from pydantic import Field, model_validator

from quality_platform.agents.analyst import AgentContext
from quality_platform.domain.models import Record
from quality_platform.domain.policies import Decision, PolicyContext, validate_transition
from quality_platform.llm.registry import AgentRole
from quality_platform.llm.structured import generate_structured


class TaskAction(StrEnum):
    ANALYZE_CONFLICTS = "ANALYZE_CONFLICTS"
    DRAFT_TEST_PLAN = "DRAFT_TEST_PLAN"
    DRAFT_TEST_CASES = "DRAFT_TEST_CASES"
    REVISE_ARTIFACT = "REVISE_ARTIFACT"
    DETECT_IMPACT = "DETECT_IMPACT"
    FINALIZE_ARTIFACT = "FINALIZE_ARTIFACT"


class ProposedTask(Record):
    id: str = Field(min_length=1)
    action: TaskAction
    project_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    target_id: str | None = None
    dependency_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_dependencies(self) -> "ProposedTask":
        if self.id in self.dependency_ids:
            raise ValueError("task cannot depend on itself")
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("duplicate task dependency")
        return self


class TaskProposal(Record):
    project_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    tasks: tuple[ProposedTask, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_plan(self) -> "TaskProposal":
        task_ids = {task.id for task in self.tasks}
        if len(task_ids) != len(self.tasks):
            raise ValueError("duplicate proposed task ID")
        for task in self.tasks:
            if task.project_id != self.project_id or task.workflow_id != self.workflow_id:
                raise ValueError("proposed task is outside the proposal context")
            unknown = set(task.dependency_ids).difference(task_ids)
            if unknown:
                raise ValueError("unknown task dependencies: " + ", ".join(sorted(unknown)))
        return self


def _ordered_tasks(tasks: tuple[ProposedTask, ...]) -> tuple[ProposedTask, ...]:
    remaining = {task.id: task for task in tasks}
    completed: set[str] = set()
    ordered: list[ProposedTask] = []
    positions = {task.id: index for index, task in enumerate(tasks)}
    while remaining:
        ready = [
            task for task in remaining.values() if set(task.dependency_ids).issubset(completed)
        ]
        if not ready:
            raise ValueError("proposed task dependencies contain a cycle")
        ready.sort(
            key=lambda task: (
                task.action != TaskAction.ANALYZE_CONFLICTS,
                positions[task.id],
            )
        )
        selected = ready[0]
        ordered.append(selected)
        completed.add(selected.id)
        del remaining[selected.id]
    return tuple(ordered)


async def plan_user_intent(context: AgentContext, user_text: str) -> TaskProposal:
    if not user_text.strip():
        raise ValueError("user intent is required")
    workflow = context.repo.read_workflow(context.project_id, context.workflow_id)
    if workflow.project_id != context.project_id:
        raise PermissionError("workflow belongs to another project")
    from quality_platform.services.scopes import ScopeService

    scopes = ScopeService(
        context.repo, project_id=context.project_id, workflow_id=context.workflow_id
    ).list_scopes()
    scope_ids = {item.id for item in scopes}
    artifacts = [
        event.record_id
        for event in context.repo.list_decisions(context.project_id)
        if event.event_type == "artifact"
        and context.repo.read_artifact_version(context.project_id, event.record_id).scope_id
        in scope_ids
    ]
    eligible_requirements: tuple[str, ...] = ()
    blocked_requirements: tuple[str, ...] = ()
    if context.analysis is not None:
        eligible_requirements = context.analysis.draft_eligible_requirement_ids
        blocked_requirements = tuple(
            requirement.id
            for requirement in context.analysis.requirements
            if requirement.id not in eligible_requirements
        )
    prompt = (
        "Propose a typed task plan only; do not execute tools, change the database, approve "
        "artifacts, or claim human authority. Put independent conflict analysis ahead of draft "
        "work while preserving explicit dependencies. If sources have not been analyzed, make "
        "ANALYZE_CONFLICTS a prerequisite of drafting; use target_id=ALL to draft discovered scopes. "
        "For drafts target a scope; for revision/finalization target an existing artifact ID. "
        "FINALIZE_ARTIFACT requests a human gate, never approval. Use only this active workflow context:\n"
        f"project_id={context.project_id}\n"
        f"workflow_id={workflow.id}\n"
        f"goal={workflow.goal}\n"
        f"source_snapshot_ids={list(workflow.source_snapshot_ids)}\n"
        f"draft_eligible_requirement_ids={list(eligible_requirements)}\n"
        f"blocked_requirement_ids={list(blocked_requirements)}\n"
        f"scope_ids={sorted(scope_ids)}\n"
        f"artifact_ids={sorted(set(artifacts))}\n"
        f"user_intent={user_text}"
    )
    result = await generate_structured(
        AgentRole.ORCHESTRATOR,
        [HumanMessage(content=prompt)],
        TaskProposal,
        registry=context.models,
        factory=context.model_factory,
    )
    proposal = TaskProposal.model_validate(result.model_dump())
    if proposal.project_id != context.project_id or proposal.workflow_id != context.workflow_id:
        raise PermissionError("task proposal is outside the active workflow")
    return proposal.model_copy(update={"tasks": _ordered_tasks(proposal.tasks)})


def authorize_task(proposal: TaskProposal, context: PolicyContext) -> Decision:
    reasons: list[str] = []
    for task in proposal.tasks:
        if task.action != TaskAction.FINALIZE_ARTIFACT:
            continue
        decision = validate_transition("artifact", "READY_FOR_REVIEW", "APPROVED", context)
        for reason in decision.reasons:
            if reason not in reasons:
                reasons.append(reason)
    return Decision(allowed=not reasons, reasons=reasons)
