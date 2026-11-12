"""Durable intent dispatch; agents propose content, domain services authorize writes."""

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import NoResultFound

from quality_platform.agents.analyst import (
    AgentContext,
    RequirementAnalysisDraft,
    analyze_documents,
)
from quality_platform.agents.orchestrator import TaskAction, TaskProposal, plan_user_intent
from quality_platform.agents.planner import create_test_plan, design_test_cases, revise_test_case
from quality_platform.config.models import AppConfig
from quality_platform.documents.ingestion import ingest_markdown
from quality_platform.domain.models import (
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersion,
    Record,
    Requirement,
    SourceDocument,
)
from quality_platform.llm.model_factory import create_chat_model
from quality_platform.llm.registry import ModelRegistry
from quality_platform.rendering.markdown import render_requirement_analysis
from quality_platform.rendering.test_cases import (
    RenderingStyle,
    _atomic_write,
    write_test_case_artifacts,
    write_test_plan_artifact,
)
from quality_platform.services.approvals import ApprovalService
from quality_platform.services.clarifications import ClarificationService
from quality_platform.services.revisions import RevisionDraft, RevisionService
from quality_platform.services.scopes import ScopeService, propose_scopes
from quality_platform.storage.artifacts import publish_source_snapshot
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.gates import ReviewTarget
from quality_platform.workflows.graph import RunResult, WorkflowRuntime, WorkflowServices
from quality_platform.workflows.scheduler import ScheduledTask, WorkflowScheduler


class TaskResult(Record):
    request_id: str
    task_id: str
    target_id: str
    status: str
    artifact_id: str | None = None
    reasons: tuple[str, ...] = ()


class IntentPlan(Record):
    request_id: str
    instruction: str
    rendering_style: RenderingStyle = "both"
    proposal: TaskProposal


class AnalysisRecord(Record):
    request_key: str
    source_snapshot_ids: tuple[str, ...]
    analysis: RequirementAnalysisDraft


class ExecutionState(TypedDict):
    workflow_id: str
    request_id: str
    status: str
    results: list[dict[str, Any]]


def configure_execution(
    repo: QaRepositories,
    project_id: str,
    workflow_id: str,
    config: AppConfig,
    sources: list[Path],
) -> None:
    project = repo.read_project_record(project_id, project_id)
    if not project.root.resolve().is_relative_to(config.workspace_root.resolve()):
        raise ValueError("project root is outside configured workspace")
    if config.database_path.resolve() != Path(str(repo.engine.url.database)).resolve():
        raise ValueError("configuration database differs from the workflow database")
    registry = ModelRegistry(config)
    existing = [
        event
        for event in repo.list_decisions(project_id)
        if event.event_type == "workflow_config" and event.record_id == workflow_id
    ]
    if existing and AppConfig.model_validate_json(existing[-1].payload) != config:
        raise ValueError("workflow configuration is immutable; start a new workflow")
    current = repo.read_workflow(project_id, workflow_id)
    repo.configure_workflow(
        project_id,
        workflow_id,
        source_snapshot_ids=current.source_snapshot_ids,
        model_config_snapshot_id=registry.snapshot.id,
    )
    repo.append_workflow_event(
        project_id, workflow_id, "workflow_config", config.model_dump_json(), key="config"
    )
    documents = ingest_markdown(project_id, sources, repo, config.workspace_root) if sources else []
    repo.configure_workflow(
        project_id,
        workflow_id,
        source_snapshot_ids=(*current.source_snapshot_ids, *(item.id for item in documents)),
    )


class WorkflowExecution:
    def __init__(
        self,
        repo: QaRepositories,
        project_id: str,
        workflow_id: str,
        saver: AsyncSqliteSaver,
        *,
        model_factory: Any = None,
    ) -> None:
        self.repo, self.project_id, self.workflow_id, self.saver = (
            repo,
            project_id,
            workflow_id,
            saver,
        )
        workflow = repo.read_workflow(project_id, workflow_id)
        if workflow.project_id != project_id:
            raise PermissionError("cross-project workflow execution denied")
        configs = self.events("workflow_config")
        if not configs:
            raise ValueError("configure this workflow with --config and import Markdown first")
        self.config = AppConfig.model_validate_json(configs[-1])
        self.rendering_style = self.config.test_case_style
        registry = ModelRegistry(self.config)
        if workflow.model_config_snapshot_id != registry.snapshot.id:
            raise ValueError("workflow model snapshot mismatch")
        self.context = AgentContext(
            project_id, workflow_id, repo, registry, model_factory or create_chat_model
        )
        self.scopes = ScopeService(repo, project_id=project_id, workflow_id=workflow_id)
        self.approvals = ApprovalService(
            repo, self.scopes, project_id=project_id, workflow_id=workflow_id
        )
        self.locks: dict[str, asyncio.Lock] = {}

    def events(self, kind: str) -> list[str]:
        return [
            event.payload
            for event in self.repo.list_decisions(self.project_id)
            if event.event_type == kind and event.record_id == self.workflow_id
        ]

    def analysis(self, *, require_current: bool = True) -> RequirementAnalysisDraft | None:
        records = self.events("analysis")
        if not records:
            return None
        record = AnalysisRecord.model_validate_json(records[-1])
        workflow = self.repo.read_workflow(self.project_id, self.workflow_id)
        changed = record.source_snapshot_ids != workflow.source_snapshot_ids or any(
            self.repo.latest_requirement_version(self.project_id, item.id) != item.version
            for item in record.analysis.requirements
        )
        if changed and not require_current:
            return None
        if changed:
            raise ValueError("source baseline changed; analyze the imported sources first")
        return record.analysis

    def agent_context(self) -> AgentContext:
        return replace(self.context, analysis=self.analysis())

    def append(self, kind: str, item: Record, key: str) -> None:
        self.repo.append_workflow_event(
            self.project_id, self.workflow_id, kind, item.model_dump_json(), key=key
        )

    def artifact(self, artifact_id: str) -> ArtifactVersion:
        item = self.repo.read_artifact_version(self.project_id, artifact_id)
        owners = {
            json.loads(event.payload).get("workflow_id")
            for event in self.repo.list_decisions(self.project_id)
            if event.event_type == "artifact_trace" and event.record_id == artifact_id
        } - {None}
        if owners != {self.workflow_id}:
            raise PermissionError(
                "artifact belongs to another workflow or has no workflow provenance"
            )
        return item

    def artifact_directory(self, artifact_id: str, version: int = 1) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", artifact_id):
            raise ValueError("invalid artifact ID")
        root = self.repo.read_project_record(self.project_id, self.project_id).root.resolve()
        directory = (root / "artifacts" / artifact_id / f"v{version}").resolve()
        if not directory.is_relative_to(root):
            raise ValueError("artifact directory escapes project root")
        return directory

    async def analyze(self, key: str) -> None:
        saved = next(
            (
                AnalysisRecord.model_validate_json(item)
                for item in self.events("analysis")
                if json.loads(item)["request_key"] == key
            ),
            None,
        )
        if saved is not None:
            await self.discover_scopes(saved.analysis, key)
            return
        clarifications = ClarificationService(
            self.repo,
            project_id=self.project_id,
            workflow_id=self.workflow_id,
            models=self.context.models,
            model_factory=self.context.model_factory,
        )
        await clarifications.validate_clarifications(self.workflow_id)
        workflow = self.repo.read_workflow(self.project_id, self.workflow_id)
        source_ids = list(workflow.source_snapshot_ids)
        root = self.repo.read_project_record(self.project_id, self.project_id).root.resolve()
        # Human decisions are immutable source evidence, never silent overrides of old files.
        for question in clarifications.list_clarifications():
            if question.status != "RESOLVED" or not question.resolution or not question.answers:
                continue
            answer = question.answers[-1]
            content = (
                f"# Stakeholder decision {question.id}\n\n{question.resolution}\n\n"
                f"Reported by: {answer.recorded_by}\nStakeholder: {answer.author}\n"
                f"Source: {answer.source}\n"
            ).encode()
            snapshot, digest = publish_source_snapshot(root, content)
            source_id = f"decision-{digest}"
            try:
                self.repo.read_source_document(self.project_id, source_id)
            except NoResultFound:
                self.repo.save_source_document(
                    SourceDocument(
                        id=source_id,
                        project_id=self.project_id,
                        input_type="STAKEHOLDER_DECISION",
                        content_hash=digest,
                        source_path=snapshot,
                        snapshot_path=snapshot,
                        line_count=len(content.decode().splitlines()),
                        headings=(f"Stakeholder decision {question.id}",),
                    )
                )
            if source_id not in source_ids:
                source_ids.append(source_id)
        workflow = self.repo.configure_workflow(
            self.project_id, self.workflow_id, source_snapshot_ids=tuple(source_ids)
        )
        if not source_ids:
            raise ValueError("import at least one Markdown source before analysis")
        draft = await analyze_documents(self.context, source_ids)
        for question in clarifications.list_clarifications():
            if question.status != "RESOLVED" and any(
                item.id in question.affected_requirement_ids and item.readiness == "VALIDATED"
                for item in draft.requirements
            ):
                raise ValueError("unresolved clarification cannot revalidate its requirement")
        previous = {item.id: item for item in self.repo.list_requirements(self.project_id)}
        requirements: list[Requirement] = []
        for item in draft.requirements:
            current = previous.get(item.id)
            if current is not None and not any(
                ref.document_id in source_ids for ref in current.source_references
            ):
                raise ValueError("requirement ID belongs to another source baseline")
            version = 1 if current is None else current.version
            if (
                current
                and item.model_copy(update={"version": version, "approval_id": current.approval_id})
                != current
            ):
                version += 1
            item = item.model_copy(update={"version": version, "approval_id": None})
            if current != item:
                self.repo.save_requirement_version(
                    self.project_id,
                    item,
                    idempotency_key=f"analysis:{self.project_id}:{item.id}:{item.version}",
                )
            requirements.append(item)
        draft = draft.model_copy(update={"requirements": tuple(requirements)})
        existing_questions = {item.id: item for item in clarifications.list_clarifications()}
        clarifications.create_batch(
            [item for item in draft.clarifications if item.id not in existing_questions]
        )
        record = AnalysisRecord(
            request_key=key, source_snapshot_ids=workflow.source_snapshot_ids, analysis=draft
        )
        self.append("analysis", record, key)
        await self.discover_scopes(draft, key)

    async def discover_scopes(self, draft: RequirementAnalysisDraft, key: str) -> None:
        proposals = await propose_scopes(replace(self.context, analysis=draft))
        existing_scopes = {item.id for item in self.scopes.list_scopes()}
        self.scopes.create_batch(
            [item for item in proposals if item.id not in existing_scopes],
            known_requirement_ids={item.id for item in draft.requirements},
        )
        path = self.artifact_directory(f"ANALYSIS-{hashlib.sha256(key.encode()).hexdigest()[:16]}")
        _atomic_write(
            path / "requirement-analysis.md", render_requirement_analysis(draft, self.workflow_id)
        )

    def scope_versions(self, scope_id: str) -> dict[str, int]:
        from quality_platform.domain.dependencies import DependencyResolver

        analysis = self.analysis()
        if analysis is None:
            raise ValueError("analyze sources before drafting artifacts")
        requirements = {item.id: item for item in analysis.requirements}
        report = DependencyResolver(
            scopes={item.id: item for item in self.scopes.list_scopes()}, requirements=requirements
        ).compute_dependency_closure(scope_id, "draft")
        return {key: requirements[key].version for key in report.required_requirement_ids}

    async def draft_artifact(self, action: TaskAction, target: str, key: str) -> str:
        artifact_id = (
            f"{action.value.removeprefix('DRAFT_')}-{hashlib.sha256(key.encode()).hexdigest()[:20]}"
        )
        try:
            item = self.artifact(artifact_id)
            canonical = (
                "test-cases.json" if item.kind == ArtifactKind.TEST_CASES else "test-plan.md"
            )
            if (
                hashlib.sha256(
                    (self.artifact_directory(item.id, item.version) / canonical).read_bytes()
                ).hexdigest()
                != item.content_hash
            ):
                raise ValueError("saved artifact content hash mismatch")
            return artifact_id
        except NoResultFound:
            pass
        context = self.agent_context()
        if context.analysis is None:
            raise ValueError("analysis required before drafting")
        versions = self.scope_versions(target)
        baseline = next(
            (
                json.loads(event.payload)["id"]
                for event in reversed(self.repo.list_decisions(self.project_id))
                if event.event_type == "baseline"
                and json.loads(event.payload)["workflow_id"] == self.workflow_id
                and json.loads(event.payload)["scope_id"] == target
                and json.loads(event.payload)["requirement_versions"] == versions
            ),
            None,
        )
        # Unapproved validated requirements may draft. The Planner enforces dependency eligibility.
        directory = self.artifact_directory(artifact_id)
        if action == TaskAction.DRAFT_TEST_PLAN:
            plan = await create_test_plan(context, target)
            path = write_test_plan_artifact(
                directory, plan, {item.id: item for item in context.analysis.requirements}
            )
            kind = ArtifactKind.TEST_PLAN
        else:
            cases = await design_test_cases(context, target)
            path = write_test_case_artifacts(directory, cases, self.rendering_style)["json"]
            kind = ArtifactKind.TEST_CASES
        artifact = ArtifactVersion(
            id=artifact_id,
            kind=kind,
            scope_id=target,
            version=1,
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            requirement_baseline_id=baseline
            or f"draft-{hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()}",
        )
        self.repo.save_artifact_trace(
            self.project_id, artifact, versions, workflow_id=self.workflow_id
        )
        self.repo.save_artifact_version(
            self.project_id, artifact, idempotency_key=f"draft:{self.project_id}:{key}"
        )
        return artifact.id

    async def prepare_revision(self, instruction: str, artifact_id: str) -> RevisionDraft:
        service = RevisionService(
            self.repo, project_id=self.project_id, workspace=self.config.workspace_root
        )
        artifact = self.artifact(artifact_id)
        mentioned = tuple(dict.fromkeys(re.findall(r"\bTC-[A-Za-z0-9-]+\b", instruction)))
        if len(mentioned) != 1:
            raise ValueError("revision must target exactly one test case ID")
        case = next(
            (
                item
                for item in service.load_cases(artifact_id, artifact.version).cases
                if item.id == mentioned[0]
            ),
            None,
        )
        if case is None:
            raise ValueError("revision target does not exist")
        proposed = await revise_test_case(
            self.agent_context(), artifact.scope_id, case, instruction
        )
        return service.prepare_revision(
            instruction,
            artifact_id,
            proposed_case=proposed,
            requirement_versions=self.scope_versions(artifact.scope_id),
            source_snapshot_ids=self.repo.read_workflow(
                self.project_id, self.workflow_id
            ).source_snapshot_ids,
        )

    def prepare_review(self, artifact_id: str, baseline_id: str) -> ArtifactVersion:
        from quality_platform.services.approvals import ScopeBaseline

        artifact = self.artifact(artifact_id)
        baseline = next(
            (
                ScopeBaseline.model_validate_json(event.payload)
                for event in self.repo.list_decisions(self.project_id)
                if event.event_type == "baseline" and event.record_id == baseline_id
            ),
            None,
        )
        if (
            baseline is None
            or baseline.workflow_id != self.workflow_id
            or baseline.scope_id != artifact.scope_id
        ):
            raise ValueError("approved baseline does not match this artifact/workflow")
        traces = [
            json.loads(event.payload)
            for event in self.repo.list_decisions(self.project_id)
            if event.event_type == "artifact_trace"
            and event.record_id == artifact_id
            and json.loads(event.payload)["version"] == artifact.version
        ]
        if not traces or traces[-1]["requirement_versions"] != baseline.requirement_versions:
            raise ValueError("artifact content was generated from a different requirement baseline")
        if artifact.staleness != "CURRENT":
            raise ValueError(
                "artifact is stale; explicitly revise it against the current requirements"
            )
        if artifact.status == ArtifactStatus.APPROVED:
            return artifact
        if artifact.requirement_baseline_id != baseline_id:
            source = self.artifact_directory(artifact_id, artifact.version)
            canonical = (
                "test-cases.json" if artifact.kind == ArtifactKind.TEST_CASES else "test-plan.md"
            )
            if (
                hashlib.sha256((source / canonical).read_bytes()).hexdigest()
                != artifact.content_hash
            ):
                raise ValueError("artifact content hash mismatch")
            artifact = artifact.model_copy(
                update={
                    "version": artifact.version + 1,
                    "requirement_baseline_id": baseline_id,
                    "status": ArtifactStatus.DRAFT,
                }
            )
            directory = self.artifact_directory(artifact_id, artifact.version)
            for name in (
                "test-cases.json",
                "test-cases.detailed.md",
                "test-cases.gherkin.md",
                "test-plan.md",
            ):
                if (source / name).exists():
                    _atomic_write(directory / name, (source / name).read_text())
            self.repo.save_artifact_trace(
                self.project_id,
                artifact,
                baseline.requirement_versions,
                workflow_id=self.workflow_id,
            )
            self.repo.save_artifact_version(
                self.project_id,
                artifact,
                idempotency_key=f"rebaseline:{self.project_id}:{artifact_id}:{artifact.version}:{baseline_id}",
            )
        ready = artifact.model_copy(update={"status": ArtifactStatus.READY_FOR_REVIEW})
        self.repo.save_artifact_version(
            self.project_id,
            ready,
            idempotency_key=f"ready:{self.project_id}:{artifact_id}:{artifact.version}:{baseline_id}",
        )
        decision = self.approvals.authorize_artifact_review(artifact_id, baseline_id)
        if not decision.allowed:
            raise PermissionError("; ".join(decision.reasons))
        return ready

    async def run(
        self, instruction: str, request_id: str, *, style: RenderingStyle | None = None
    ) -> list[TaskResult]:
        self.rendering_style = style or self.config.test_case_style
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", request_id):
            raise ValueError("invalid request ID")
        workflow = self.repo.read_workflow(self.project_id, self.workflow_id)
        thread_id = f"{self.project_id}:{workflow.thread_id}:intent:{request_id}"
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

        async def plan(state: ExecutionState) -> dict[str, object]:
            existing = [IntentPlan.model_validate_json(item) for item in self.events("task_plan")]
            saved = next((item for item in existing if item.request_id == request_id), None)
            if saved:
                if (
                    saved.instruction != instruction
                    or saved.rendering_style != self.rendering_style
                ):
                    raise ValueError("request ID already belongs to another instruction")
            else:
                proposal = await plan_user_intent(
                    replace(self.context, analysis=self.analysis(require_current=False)),
                    instruction,
                )
                self.append(
                    "task_plan",
                    IntentPlan(
                        request_id=request_id,
                        instruction=instruction,
                        rendering_style=self.rendering_style,
                        proposal=proposal,
                    ),
                    request_id,
                )
            return {"status": "PLANNED"}

        async def dispatch(state: ExecutionState) -> dict[str, object]:
            saved = next(
                IntentPlan.model_validate_json(item)
                for item in self.events("task_plan")
                if json.loads(item)["request_id"] == request_id
            )
            complete = {
                (item.task_id, item.target_id): item
                for payload in self.events("task_result")
                if (item := TaskResult.model_validate_json(payload)).request_id == request_id
            }
            done: set[str] = set()
            pending = list(saved.proposal.tasks)
            all_results: list[TaskResult] = []
            while pending:
                ready = [task for task in pending if set(task.dependency_ids).issubset(done)]
                if not ready:
                    raise ValueError("task dependencies contain a cycle")
                analyses = [task for task in ready if task.action == TaskAction.ANALYZE_CONFLICTS]
                if analyses:
                    ready = analyses[:1]
                scheduled: list[ScheduledTask] = []
                for task in ready:
                    failed_dependency = any(
                        result.status not in {"COMPLETED", "APPROVED"}
                        for result in all_results
                        if result.task_id in task.dependency_ids
                    )
                    if task.action in {TaskAction.DRAFT_TEST_PLAN, TaskAction.DRAFT_TEST_CASES}:
                        targets = (
                            [
                                item.id
                                for item in self.scopes.list_scopes()
                                if item.status != "REJECTED"
                            ]
                            if task.target_id in (None, "ALL")
                            else [task.target_id]
                        )
                        if not targets:
                            targets = ["ALL"]
                    else:
                        targets = [task.target_id or "workflow"]
                    for target in targets:

                        async def execute(
                            task: Any = task,
                            target: str = target,
                            blocked: bool = failed_dependency,
                        ) -> RunResult:
                            prior = complete.get((task.id, target))
                            if prior and prior.status not in {"FAILED", "BLOCKED"}:
                                result = prior
                            else:
                                async with self.locks.setdefault(target, asyncio.Lock()):
                                    result = await self.execute_task(
                                        task.action,
                                        target,
                                        f"{self.workflow_id}:{request_id}:{task.id}:{target}",
                                        instruction,
                                        request_id,
                                        task.id,
                                        blocked,
                                    )
                                if result.status not in {"FAILED", "BLOCKED"}:
                                    self.append(
                                        "task_result", result, f"{request_id}:{task.id}:{target}"
                                    )
                            all_results.append(result)
                            return RunResult(
                                status=result.status,
                                thread_id=thread_id,
                                checkpoint_ns=f"{self.project_id}:{self.workflow_id}:{target}",
                                reasons=result.reasons,
                            )

                        scheduled.append(
                            ScheduledTask(
                                id=f"{task.id}:{target}",
                                workflow_id=self.workflow_id,
                                scope_id=target,
                                run=execute,
                            )
                        )
                scheduled_results = await WorkflowScheduler(scheduled).run_eligible_tasks(
                    self.workflow_id, max_concurrency=self.config.max_concurrency
                )
                if any(item.status == "FAILED" for item in scheduled_results.outcomes):
                    raise RuntimeError(
                        "task execution failed; retry the same request ID to resume unfinished work"
                    )
                done.update(task.id for task in ready)
                pending = [task for task in pending if task not in ready]
            return {
                "status": "COMPLETED",
                "results": [item.model_dump(mode="json") for item in all_results],
            }

        graph: StateGraph[ExecutionState] = StateGraph(ExecutionState)
        graph.add_node("plan", plan)
        graph.add_node("dispatch", dispatch)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "dispatch")
        graph.add_edge("dispatch", END)
        compiled = graph.compile(checkpointer=self.saver)
        state = await compiled.aget_state(config)
        if state.values:
            saved = next(
                (
                    IntentPlan.model_validate_json(item)
                    for item in self.events("task_plan")
                    if json.loads(item)["request_id"] == request_id
                ),
                None,
            )
            if saved and (
                saved.instruction != instruction or saved.rendering_style != self.rendering_style
            ):
                raise ValueError("request ID already belongs to another instruction")
            if state.next:
                await compiled.ainvoke(None, config, durability="sync")
        else:
            await compiled.ainvoke(
                {
                    "workflow_id": self.workflow_id,
                    "request_id": request_id,
                    "status": "PENDING",
                    "results": [],
                },
                config,
                durability="sync",
            )
        state = await compiled.aget_state(config)
        return [TaskResult.model_validate(item) for item in state.values.get("results", [])]

    async def execute_task(
        self,
        action: TaskAction,
        target: str,
        key: str,
        instruction: str,
        request_id: str,
        task_id: str,
        blocked: bool,
    ) -> TaskResult:
        artifact_id = None
        status = "COMPLETED"
        try:
            if blocked:
                raise ValueError("task dependency did not complete")
            if action == TaskAction.ANALYZE_CONFLICTS:
                await self.analyze(key)
            elif action in {TaskAction.DRAFT_TEST_PLAN, TaskAction.DRAFT_TEST_CASES}:
                artifact_id = await self.draft_artifact(action, target, key)
            elif action == TaskAction.REVISE_ARTIFACT:
                draft = await self.prepare_revision(instruction, target)
                self.append("revision_draft", draft, draft.diff_hash)
                artifact_id, status = target, "WAITING_FOR_DIFF_REVIEW"
            elif action == TaskAction.DETECT_IMPACT:
                # Requirement writes have already persisted impact atomically; no automatic revisions.
                status = "COMPLETED"
            elif action == TaskAction.FINALIZE_ARTIFACT:
                artifact = self.artifact(target)
                runtime = WorkflowRuntime(
                    WorkflowServices(
                        self.repo,
                        self.project_id,
                        ReviewTarget(
                            id=artifact.id,
                            version=artifact.version,
                            scope_id=artifact.scope_id,
                            baseline_id=artifact.requirement_baseline_id,
                        ),
                        self.approvals,
                    ),
                    self.saver,
                )
                review = await runtime.run_workflow(self.workflow_id)
                status, artifact_id = review.status, artifact.id
        except (ValueError, PermissionError, NoResultFound) as error:
            return TaskResult(
                request_id=request_id,
                task_id=task_id,
                target_id=target,
                status="BLOCKED",
                reasons=(str(error),),
            )
        except Exception as error:  # noqa: BLE001 - isolate provider failures from other scopes
            # Provider messages can contain private inputs; do not persist exception text.
            return TaskResult(
                request_id=request_id,
                task_id=task_id,
                target_id=target,
                status="FAILED",
                reasons=(type(error).__name__,),
            )
        return TaskResult(
            request_id=request_id,
            task_id=task_id,
            target_id=target,
            status=status,
            artifact_id=artifact_id,
        )
