from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict

from quality_platform.domain.models import WorkflowRecord
from quality_platform.services.approvals import ApprovalInput, ApprovalService
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.gates import HumanResponse, ReviewTarget
from quality_platform.workflows.state import WorkflowState


@dataclass(frozen=True)
class WorkflowServices:
    repo: QaRepositories
    project_id: str
    target: ReviewTarget
    approval_service: ApprovalService

    def __post_init__(self) -> None:
        if self.approval_service.project_id != self.project_id:
            raise ValueError("approval service project does not match runtime")
        if self.approval_service.repo.engine is not self.repo.engine:
            raise ValueError("approval service repository does not match runtime")


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
        current = services.repo.read_artifact_version(state["project_id"], state["target_id"])
        if (
            current.version != state["target_version"]
            or current.scope_id != state["scope_id"]
            or current.requirement_baseline_id != state["baseline_id"]
            or current.content_hash != state["content_hash"]
        ):
            return {"status": "BLOCKED", "reasons": ["artifact review target changed"]}
        eligibility = services.approval_service.authorize_artifact_review(
            state["target_id"], state["baseline_id"]
        )
        if not eligibility.allowed:
            return {"status": "BLOCKED", "reasons": eligibility.reasons}
        raw_response = interrupt(
            {
                "workflow_id": state["workflow_id"],
                "target_id": state["target_id"],
                "target_version": state["target_version"],
                "scope_id": state["scope_id"],
                "baseline_id": state["baseline_id"],
                "content_hash": state["content_hash"],
            }
        )
        response = HumanResponse.model_validate(raw_response)
        if not response.approved:
            return {"status": "REJECTED"}
        eligibility = services.approval_service.authorize_artifact_review(
            state["target_id"], state["baseline_id"]
        )
        if not eligibility.allowed:
            return {"status": "BLOCKED", "reasons": eligibility.reasons}
        return {"status": "REVIEW_APPROVED", "actor": response.actor}

    def commit(state: WorkflowState) -> dict[str, object]:
        approved = services.approval_service.commit_artifact_approval(
            state["target_id"],
            ApprovalInput(
                approved=True,
                actor=state["actor"],
                artifact_version=state["target_version"],
                scope_id=state["scope_id"],
                baseline_id=state["baseline_id"],
                content_hash=state["content_hash"],
                reviewed_evidence=(
                    f"artifact:{state['target_id']}:{state['target_version']}:{state['content_hash']}",
                    f"baseline:{state['baseline_id']}",
                ),
            ),
        )
        return {"status": "APPROVED", "approval_id": approved.approval_id}

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
        if self.services.approval_service.workflow_id != workflow.id:
            raise ValueError("approval service workflow does not match runtime")
        return workflow

    def _thread_id(self, workflow: WorkflowRecord) -> str:
        target = self.services.target
        return (
            f"{workflow.thread_id}:{target.scope_id}:{target.id}:"
            f"{target.version}:{target.baseline_id}"
        )

    def _config(self, workflow: WorkflowRecord) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": self._thread_id(workflow),
            }
        }

    def _checkpoint_ns(self, workflow: WorkflowRecord) -> str:
        return f"{workflow.project_id}:{workflow.id}:{self.services.target.scope_id}"

    def _result(
        self, workflow: WorkflowRecord, state: Mapping[str, Any], waiting: bool
    ) -> RunResult:
        status = "WAITING_FOR_REVIEW" if waiting else state["status"]
        return RunResult(
            status=status,
            thread_id=self._thread_id(workflow),
            checkpoint_ns=self._checkpoint_ns(workflow),
            approval_id=state.get("approval_id"),
            reasons=tuple(state.get("reasons", [])),
        )

    async def run_workflow(self, workflow_id: str) -> RunResult:
        workflow = self._workflow(workflow_id)
        config = self._config(workflow)
        snapshot = await self.graph.aget_state(config)
        if snapshot.values:
            current = self.services.repo.read_artifact_version(
                self.services.project_id, self.services.target.id
            )
            if (
                snapshot.values["target_id"] != self.services.target.id
                or snapshot.values["target_version"] != self.services.target.version
                or snapshot.values["scope_id"] != self.services.target.scope_id
                or snapshot.values["baseline_id"] != self.services.target.baseline_id
                or snapshot.values["content_hash"] != current.content_hash
            ):
                raise ValueError("review checkpoint target changed")
            if snapshot.next and snapshot.next != ("review",):
                await self.graph.ainvoke(None, config, durability="sync")
                snapshot = await self.graph.aget_state(config)
            return self._result(workflow, snapshot.values, snapshot.next == ("review",))
        target = self.services.target
        artifact = self.services.repo.read_artifact_version(self.services.project_id, target.id)
        state: WorkflowState = {
            "workflow_id": workflow.id,
            "project_id": workflow.project_id,
            "scope_id": target.scope_id,
            "target_id": target.id,
            "target_version": target.version,
            "baseline_id": target.baseline_id,
            "content_hash": artifact.content_hash,
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
