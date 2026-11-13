"""Single writer consuming durable commands, without depending on HTTP or Typer."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy.exc import NoResultFound

from quality_platform.application import WorkflowApplication, create_project, create_workflow
from quality_platform.documents.ingestion import ingest_markdown
from quality_platform.domain.models import StakeholderAnswer
from quality_platform.evaluation.models import BenchmarkReport
from quality_platform.services.revisions import RevisionDraft
from quality_platform.services.scopes import ScopeAction, ScopeDecision, propose_scope_edit
from quality_platform.storage.checkpoints import create_checkpoint_saver
from quality_platform.storage.database import open_database
from quality_platform.storage.ownership import writer_lock
from quality_platform.storage.repositories import QaRepositories
from quality_platform.web.control import Conflict, Control
from quality_platform.web.evaluation import EvaluationImportCommand, EvaluationStore
from quality_platform.web.models import Command
from quality_platform.web.queries import workflow_view


def confined(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PermissionError("Path escapes managed directory")
    return resolved


async def dispatch(
    control: Control, repo: QaRepositories, job: dict[str, Any], *, model_factory: Any = None
) -> Any:
    payload = json.loads(job["payload"])
    if payload.get("kind") == "evaluation_import_report":
        request = EvaluationImportCommand.model_validate(payload)
        repo.read_project_record(request.project_id, request.project_id)
        staged = confined(
            control.state,
            control.state / "evaluation" / "staged" / f"{request.report_hash}.json",
        )
        raw = staged.read_bytes()
        if hashlib.sha256(raw).hexdigest() != request.report_hash:
            raise ValueError("Staged report changed")
        report = BenchmarkReport.model_validate_json(raw)
        identity = EvaluationStore(control.state).save("benchmark-report", report)
        summary = report.model_dump(
            mode="json",
            include={"run_id", "dataset_version", "decision", "readiness_labels", "candidate", "rubric_id", "rubric_version", "overall_precision", "overall_recall", "eligible_count", "excluded_count", "case_scores", "metric_variance", "total_cost_usd", "input_fingerprint", "output_fingerprint"},
        )
        with control.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO reports VALUES (?,?,?)",
                (identity, request.project_id, json.dumps(summary, sort_keys=True)),
            )
        return {"report_id": identity, "run_id": report.run_id, "decision": report.decision.value}
    uploads = payload.pop("uploads", [])
    command = Command.model_validate(payload)
    config = (
        control.config(command.settings_id)
        if command.kind == "create_workflow"
        else control.config()
    )
    project, workflow, actor = command.project_id, command.workflow_id, job["actor"]
    if command.kind == "create_project":
        root = confined(config.workspace_root, config.workspace_root / project)
        root.mkdir(parents=True, exist_ok=True)
        try:
            existing = repo.read_project_record(project, project)
        except NoResultFound:
            existing = create_project(repo, project, command.name, root)
        if existing.root.resolve() != root or existing.name != command.name:
            raise Conflict("Project ID already belongs to another project")
        return existing.model_dump(mode="json")
    repo.read_project_record(project, project)
    assert workflow is not None
    if command.kind == "create_workflow":
        try:
            existing_workflow = repo.read_workflow(project, workflow)
        except NoResultFound:
            existing_workflow = create_workflow(repo, project, workflow, command.goal)
        if existing_workflow.project_id != project or existing_workflow.goal != command.goal:
            raise Conflict("Workflow ID already belongs to another workflow")
        from quality_platform.workflows.execution import configure_execution

        configure_execution(repo, project, workflow, config, [])
        return repo.read_workflow(project, workflow).model_dump(mode="json")
    current = workflow_view(repo, project, workflow)
    # Replays of non-model commands are conservative: never apply a decision to newer data.
    if command.state_hash and command.state_hash != current["state_hash"]:
        raise Conflict(
            "Reviewed state changed. Inspect current versions and submit a new decision."
        )
    if command.kind == "import":
        project_root = repo.read_project_record(project, project).root
        target_directory = confined(
            project_root, project_root / ".quality-platform" / "uploads" / job["id"]
        )
        target_directory.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        for index, upload in enumerate(uploads):
            source = confined(control.state, control.state / "uploads" / f"{upload['hash']}.md")
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != upload["hash"]:
                raise ValueError("Staged upload changed")
            name = upload["name"]
            if "/" in name or "\\" in name or not name.lower().endswith(".md"):
                raise ValueError("Invalid source filename")
            destination = confined(project_root, target_directory / str(index) / name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() != raw:
                raise ValueError("Existing upload differs")
            destination.write_bytes(raw)
            files.append(destination)
        if not files:
            raise ValueError("Upload at least one Markdown file")
        sources = ingest_markdown(
            project, files, repo, config.workspace_root, idempotency_key=job["id"]
        )
        item = repo.read_workflow(project, workflow)
        repo.configure_workflow(
            project,
            workflow,
            source_snapshot_ids=tuple(
                dict.fromkeys((*item.source_snapshot_ids, *(source.id for source in sources)))
            ),
        )
        return {"source_ids": [source.id for source in sources]}
    async with create_checkpoint_saver(
        config.database_path.with_suffix(".checkpoints.sqlite")
    ) as saver:
        app = WorkflowApplication(repo, project, workflow, saver, model_factory=model_factory)
        execution = app.execution
        if command.kind == "run":
            results = await app.run(
                command.instruction, str(command.request_id), style=command.style
            )
            return [item.model_dump(mode="json") for item in results]
        if command.kind == "resume":
            return [
                item.model_dump(mode="json") for item in await app.resume(str(command.request_id))
            ]
        if command.kind == "answer":
            from quality_platform.services.clarifications import ClarificationService

            result = ClarificationService(
                repo, project_id=project, workflow_id=workflow
            ).record_stakeholder_answer(
                str(command.target_id),
                StakeholderAnswer(
                    author=command.author,
                    text=command.text,
                    source=command.source,
                    recorded_by=actor,
                ),
            )
            return result.model_dump(mode="json")
        if command.kind == "scope":
            return execution.scopes.review_scope(
                str(command.target_id),
                ScopeDecision(
                    action=ScopeAction(command.action),
                    actor=actor,
                    human_reviewed=True,
                    replacements=command.replacements,
                    merge_with_ids=command.merge_with_ids,
                ),
            ).model_dump(mode="json")
        if command.kind == "baseline":
            baseline_result = app.approve_baseline(str(command.target_id), actor)
            if not baseline_result.approved:
                raise PermissionError("Baseline is blocked")
            return baseline_result.model_dump(mode="json")
        if command.kind == "submit":
            return (
                await app.submit_review(str(command.target_id), str(command.baseline_id))
            ).model_dump(mode="json")
        if command.kind == "decide":
            if command.action not in {"APPROVE", "REJECT"}:
                raise ValueError("Approve or reject this artifact")
            artifact = execution.artifact(str(command.target_id))
            decision = (
                await app.decide_review(str(command.target_id), command.action == "APPROVE", actor)
            ).model_dump(mode="json")
            return {**decision, "artifact_version": artifact.version}
        if command.kind == "revision":
            return (
                await app.prepare_revision(command.instruction, str(command.target_id))
            ).model_dump(mode="json")
        if command.kind == "apply":
            draft_job = control.job(str(command.draft_id))
            draft_command = Command.model_validate_json(draft_job["payload"])
            if (
                draft_job["status"] != "SUCCEEDED"
                or draft_command.kind != "revision"
                or draft_command.project_id != project
                or draft_command.workflow_id != workflow
            ):
                raise PermissionError("Draft is outside this workflow")
            draft = RevisionDraft.model_validate_json(draft_job["result"])
            execution.artifact(draft.artifact_id)
            return app.commit_revision(draft, command.reviewed_diff_hash, actor).model_dump(
                mode="json"
            )
        if command.kind == "scope_proposal":
            return (
                await propose_scope_edit(execution.agent_context(), command.instruction)
            ).model_dump(mode="json")
    raise ValueError("Unsupported command")


async def process_one(control: Control, repo: QaRepositories, *, model_factory: Any = None) -> bool:
    job = control.claim()
    if job is None:
        return False
    try:
        result = await dispatch(control, repo, job, model_factory=model_factory)
        outcomes = result if isinstance(result, list) else [result]
        status = (
            "BLOCKED"
            if any(
                isinstance(item, dict) and item.get("status") in {"BLOCKED", "FAILED"}
                for item in outcomes
            )
            else "SUCCEEDED"
        )
        control.finish(job["id"], status, result)
    except (Conflict, PermissionError):
        control.finish(
            job["id"],
            "BLOCKED",
            error="Policy or reviewed-version conflict. Reload workflow and inspect prerequisites.",
        )
    except Exception:  # noqa: BLE001 -- isolate failed jobs without exposing provider secrets
        # Provider exceptions may contain request bodies or credentials. No raw exception in UI/logs.
        control.finish(
            job["id"],
            "FAILED",
            error="Command failed validation, data integrity, or provider invocation. Inspect configuration and source evidence before explicit retry.",
        )
    return True


async def run_worker(state: Path, *, once: bool = False, model_factory: Any = None) -> None:
    control = Control(state)
    config = control.config()
    with writer_lock(config.database_path):
        engine = open_database(config.database_path)
        control.recover()
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                control.heartbeat()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3)
                except TimeoutError:
                    pass

        pulse = asyncio.create_task(heartbeat())
        try:
            repo = QaRepositories(engine)
            while True:
                worked = await process_one(control, repo, model_factory=model_factory)
                if once:
                    break
                if not worked:
                    await asyncio.sleep(0.5)
        finally:
            stop.set()
            await pulse
            with control.connect() as db:
                db.execute("DELETE FROM runtime")
            engine.dispose()
