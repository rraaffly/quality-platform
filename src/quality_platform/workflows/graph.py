from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict

from quality_platform.domain.models import ApprovalRecord, WorkflowRecord
from quality_platform.domain.policies import PolicyContext, validate_transition
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.gates import HumanResponse, ReviewTarget
from quality_platform.workflows.state import WorkflowState


@dataclass(frozen=True)
class WorkflowServices:
    repo: QaRepositories
    project_id: str
    target: ReviewTarget
    policy_context: PolicyContext


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    thread_id: str
    checkpoint_ns: str
    approval_id: str | None = None
    reasons: tuple[str, ...] = ()


def build_graph(
    services: WorkflowServices, saver: AsyncSqliteSaver
) -> CompiledStateGraph[WorkflowState]:
    builder = StateGraph(WorkflowState)

    def review(state: WorkflowState) -> dict[str, object]:
        raw_response = interrupt(
            {
                "workflow_id": state["workflow_id"],
                "target_id": state["target_id"],
                "target_version": state["target_version"],
                "scope_id": state["scope_id"],
                "baseline_id": state["baseline_id"],
            }
        )
        response = HumanResponse.model_validate(raw_response)
        if not response.approved:
            return {"status": "REJECTED"}
        context = services.policy_context.model_copy(update={"human_reviewed": True})
        decision = validate_transition("artifact", "READY_FOR_REVIEW", "APPROVED", context)
        if not decision.allowed:
            return {"status": "BLOCKED", "reasons": decision.reasons}
        return {"status": "REVIEW_APPROVED", "actor": response.actor}

    def commit(state: WorkflowState) -> dict[str, object]:
        approval_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{state['project_id']}:{state['workflow_id']}:{state['scope_id']}:"
                f"{state['target_id']}:{state['target_version']}",
            )
        )
        approval = ApprovalRecord(
            id=approval_id,
            actor=state["actor"],
            target_id=state["target_id"],
            target_version=state["target_version"],
            scope_id=state["scope_id"],
            baseline_id=state["baseline_id"],
            approved_at=datetime.fromisoformat(state["requested_at"]),
        )
        services.repo.append_decision(
            state["project_id"], approval, idempotency_key=f"approval:{approval_id}"
        )
        return {"status": "APPROVED", "approval_id": approval_id}

    def route_after_review(state: WorkflowState) -> str:
        return "commit" if state["status"] == "REVIEW_APPROVED" else END

    builder.add_node("review", review)
    builder.add_node("commit", commit)
    builder.add_edge(START, "review")
    builder.add_conditional_edges("review", route_after_review)
    builder.add_edge("commit", END)
    return builder.compile(checkpointer=saver)


class WorkflowRuntime:
    def __init__(self, services: WorkflowServices, saver: AsyncSqliteSaver) -> None:
        self.services = services
        self.graph = build_graph(services, saver)

    def _workflow(self, workflow_id: str) -> WorkflowRecord:
        workflow = self.services.repo.read_workflow(self.services.project_id, workflow_id)
        if workflow.project_id != self.services.project_id:
            raise PermissionError("cross-project workflow execution denied")
        return workflow

    def _config(self, workflow: WorkflowRecord) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": f"{workflow.thread_id}:{self.services.target.scope_id}",
            }
        }

    def _result(
        self, workflow: WorkflowRecord, state: Mapping[str, Any], waiting: bool
    ) -> RunResult:
        status = "WAITING_FOR_REVIEW" if waiting else state["status"]
        return RunResult(
            status=status,
            thread_id=f"{workflow.thread_id}:{self.services.target.scope_id}",
            checkpoint_ns=f"{workflow.project_id}:{workflow.id}:{self.services.target.scope_id}",
            approval_id=state.get("approval_id"),
            reasons=tuple(state.get("reasons", [])),
        )

    async def run_workflow(self, workflow_id: str) -> RunResult:
        workflow = self._workflow(workflow_id)
        config = self._config(workflow)
        snapshot = await self.graph.aget_state(config)
        if snapshot.values:
            if snapshot.next and snapshot.next != ("review",):
                await self.graph.ainvoke(None, config, durability="sync")
                snapshot = await self.graph.aget_state(config)
            return self._result(workflow, snapshot.values, snapshot.next == ("review",))
        target = self.services.target
        state: WorkflowState = {
            "workflow_id": workflow.id,
            "project_id": workflow.project_id,
            "scope_id": target.scope_id,
            "target_id": target.id,
            "target_version": target.version,
            "baseline_id": target.baseline_id,
            "requested_at": datetime.now(UTC).isoformat(),
            "status": "PENDING",
        }
        await self.graph.ainvoke(state, config, durability="sync")
        snapshot = await self.graph.aget_state(config)
        return self._result(workflow, snapshot.values, snapshot.next == ("review",))

    async def resume_workflow(self, workflow_id: str, response: HumanResponse) -> RunResult:
        workflow = self._workflow(workflow_id)
        config = self._config(workflow)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            raise ValueError("workflow is not waiting for review")
        if not snapshot.next:
            return self._result(workflow, snapshot.values, False)
        if snapshot.next != ("review",):
            await self.graph.ainvoke(None, config, durability="sync")
            snapshot = await self.graph.aget_state(config)
            return self._result(workflow, snapshot.values, snapshot.next == ("review",))
        await self.graph.ainvoke(Command(resume=response.model_dump()), config, durability="sync")
        snapshot = await self.graph.aget_state(config)
        return self._result(workflow, snapshot.values, snapshot.next == ("review",))
