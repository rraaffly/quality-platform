"""Transport-independent workflow orchestration shared by CLI and workers.

Callers own the repository, checkpoint saver, and database writer lock lifetime.
"""

from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from quality_platform.config.models import AppConfig
from quality_platform.domain.models import (
    ArtifactVersion,
    ProjectRecord,
    WorkflowRecord,
    WorkflowStatus,
)
from quality_platform.rendering.test_cases import RenderingStyle, write_test_case_artifacts
from quality_platform.services.approvals import ApprovalResult, BaselineSelection
from quality_platform.services.revisions import RevisionApproval, RevisionDraft, RevisionService
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.execution import (
    IntentPlan,
    TaskResult,
    WorkflowExecution,
    configure_execution,
)
from quality_platform.workflows.gates import HumanResponse, ReviewTarget
from quality_platform.workflows.graph import RunResult, WorkflowRuntime, WorkflowServices


def create_project(repo: QaRepositories, project_id: str, name: str, root: Path) -> ProjectRecord:
    return repo.create_project(ProjectRecord(id=project_id, name=name, root=root.resolve()))


def create_workflow(
    repo: QaRepositories,
    project_id: str,
    workflow_id: str,
    goal: str,
    config: AppConfig | None = None,
    sources: list[Path] | None = None,
    *,
    thread_id: str | None = None,
) -> WorkflowRecord:
    if sources and config is None:
        raise ValueError("--config is required with --source")
    created = repo.create_workflow(
        WorkflowRecord(
            id=workflow_id,
            project_id=project_id,
            goal=goal,
            thread_id=thread_id or f"thread-{workflow_id}",
            status=WorkflowStatus.ACTIVE,
        )
    )
    if config is not None:
        configure_execution(repo, project_id, workflow_id, config, sources or [])
    return created


class WorkflowApplication:
    execution: WorkflowExecution

    def __init__(
        self,
        repo: QaRepositories,
        project_id: str,
        workflow_id: str,
        saver: AsyncSqliteSaver,
        *,
        model_factory: Any = None,
    ) -> None:
        self.execution = WorkflowExecution(
            repo, project_id, workflow_id, saver, model_factory=model_factory
        )

    async def run(
        self, instruction: str, request_id: str, *, style: RenderingStyle | None = None
    ) -> list[TaskResult]:
        return await self.execution.run(instruction, request_id, style=style)

    async def resume(self, request_id: str) -> list[TaskResult]:
        plans = (
            IntentPlan.model_validate_json(payload)
            for payload in self.execution.events("task_plan")
        )
        plan = next((item for item in plans if item.request_id == request_id), None)
        if plan is None:
            raise ValueError(
                "request plan not saved; rerun workflow run with the original instruction"
            )
        return await self.run(plan.instruction, request_id, style=plan.rendering_style)

    def approve_baseline(
        self, scope_id: str, actor: str, selection: BaselineSelection | None = None
    ) -> ApprovalResult:
        execution = self.execution
        if selection is None:
            selection = BaselineSelection(
                requirement_versions=execution.scope_versions(scope_id),
                source_snapshot_ids=execution.repo.read_workflow(
                    execution.project_id, execution.workflow_id
                ).source_snapshot_ids,
                human_reviewed=True,
            )
        return execution.approvals.approve_scope_baseline(scope_id, selection, actor)

    async def submit_review(self, artifact_id: str, baseline_id: str) -> RunResult:
        execution = self.execution
        item = execution.prepare_review(artifact_id, baseline_id)
        return await self._review_runtime(item).run_workflow(execution.workflow_id)

    def _review_runtime(self, item: ArtifactVersion) -> WorkflowRuntime:
        execution = self.execution
        return WorkflowRuntime(
            WorkflowServices(
                execution.repo,
                execution.project_id,
                ReviewTarget(
                    id=item.id,
                    version=item.version,
                    scope_id=item.scope_id,
                    baseline_id=item.requirement_baseline_id,
                ),
                execution.approvals,
            ),
            execution.saver,
        )

    async def decide_review(self, artifact_id: str, approved: bool, actor: str) -> RunResult:
        item = self.execution.artifact(artifact_id)
        if item.status != "READY_FOR_REVIEW":
            raise PermissionError("Artifact must be submitted for review first")
        return await self._review_runtime(item).resume_workflow(
            self.execution.workflow_id, HumanResponse(approved=approved, actor=actor)
        )

    async def prepare_revision(self, instruction: str, artifact_id: str) -> RevisionDraft:
        return await self.execution.prepare_revision(instruction, artifact_id)

    def commit_revision(
        self, draft: RevisionDraft, reviewed_diff_hash: str, actor: str
    ) -> ArtifactVersion:
        from quality_platform.agents.planner import planning_requirements, validate_test_cases

        execution = self.execution
        repo, project_id, workflow_id = (
            execution.repo,
            execution.project_id,
            execution.workflow_id,
        )
        artifact = execution.artifact(draft.artifact_id)
        versions = execution.scope_versions(artifact.scope_id)
        if (
            draft.requirement_versions != versions
            or draft.source_snapshot_ids
            != repo.read_workflow(project_id, workflow_id).source_snapshot_ids
        ):
            raise ValueError("revision source baseline changed; prepare and review a new diff")
        eligible, _ = planning_requirements(execution.agent_context(), artifact.scope_id)
        selected = tuple(
            requirement
            for requirement in eligible
            if requirement.id in {key for case in draft.after.cases for key in case.requirement_ids}
        )
        validate_test_cases(list(draft.after.cases), selected)
        service = RevisionService(
            repo, project_id=project_id, workspace=execution.config.workspace_root
        )
        result = service.commit_revision(
            draft,
            RevisionApproval(approved=True, actor=actor, reviewed_diff_hash=reviewed_diff_hash),
        )
        repo.save_artifact_trace(project_id, result, versions, workflow_id=workflow_id)
        write_test_case_artifacts(
            execution.artifact_directory(result.id, result.version),
            list(draft.after.cases),
            execution.config.test_case_style,
        )
        return result
