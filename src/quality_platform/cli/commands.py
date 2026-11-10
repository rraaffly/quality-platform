import asyncio
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict

from quality_platform.domain.models import (
    ProjectRecord,
    StakeholderAnswer,
    WorkflowRecord,
    WorkflowStatus,
)
from quality_platform.evaluation.models import BenchmarkReport
from quality_platform.evaluation.reporting import write_report
from quality_platform.evaluation.runner import run_full_benchmark, run_live_benchmark
from quality_platform.llm.registry import AgentRole
from quality_platform.services.approvals import ApprovalInput, ApprovalService
from quality_platform.services.clarifications import ClarificationService
from quality_platform.services.scopes import ScopeAction, ScopeDecision, ScopeService
from quality_platform.storage.checkpoints import create_checkpoint_saver
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.gates import HumanResponse, ReviewTarget
from quality_platform.workflows.graph import WorkflowRuntime, WorkflowServices

project = typer.Typer(help="Manage QA projects")
workflow = typer.Typer(help="Manage QA workflows")
clarification = typer.Typer(help="Manage stakeholder clarifications")
scope = typer.Typer(help="Review business scopes")
artifact = typer.Typer(help="Inspect and review artifacts")
benchmark = typer.Typer(help="Run and inspect evaluations")


class SelectedProject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str


@contextmanager
def _repository(database: Path) -> Iterator[QaRepositories]:
    engine = open_database(database)
    try:
        yield QaRepositories(engine)
    finally:
        engine.dispose()


def _selection_path(database: Path) -> Path:
    resolved = database.resolve()
    return resolved.with_name(f"{resolved.name}.context.json")


def _write_selection(database: Path, selected: SelectedProject) -> None:
    target = _selection_path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(selected.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _selected_project(database: Path, repo: QaRepositories, project_id: str | None) -> str:
    if project_id is None:
        path = _selection_path(database)
        if not path.exists():
            raise typer.BadParameter("select a project first or pass --project-id")
        project_id = SelectedProject.model_validate_json(
            path.read_text(encoding="utf-8")
        ).project_id
    repo.read_project_record(project_id, project_id)
    return project_id


@project.command("create")
def create_project(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    project_id: Annotated[str, typer.Option()],
    name: Annotated[str, typer.Option()],
    root: Annotated[Path, typer.Option()],
) -> None:
    with _repository(database) as repo:
        created = repo.create_project(ProjectRecord(id=project_id, name=name, root=root.resolve()))
    typer.echo(f"{created.id}: {created.name}")


@project.command("list")
def list_projects(database: Annotated[Path, typer.Option(help="Domain database path")]) -> None:
    with _repository(database) as repo:
        items = repo.list_projects()
    for item in items:
        typer.echo(f"{item.id}\t{item.name}\t{item.root}")


@project.command("select")
def select_project(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    project_id: Annotated[str, typer.Option()],
) -> None:
    with _repository(database) as repo:
        selected = repo.read_project_record(project_id, project_id)
    _write_selection(database, SelectedProject(project_id=selected.id))
    typer.echo(f"selected project {selected.id}")


@workflow.command("start")
def start_workflow(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    workflow_id: Annotated[str, typer.Option()],
    goal: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
    thread_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        created = repo.create_workflow(
            WorkflowRecord(
                id=workflow_id,
                project_id=active_project,
                goal=goal,
                thread_id=thread_id or f"thread-{workflow_id}",
                status=WorkflowStatus.ACTIVE,
            )
        )
    typer.echo(f"{created.id}: {created.status}")


@workflow.command("list")
def list_workflows(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        items = repo.list_workflows(active_project)
    for item in items:
        typer.echo(f"{item.id}\t{item.status}\t{item.goal}")


@workflow.command("resume")
def resume_workflow(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    workflow_id: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
    checkpoint_database: Annotated[Path | None, typer.Option()] = None,
    artifact_id: Annotated[str | None, typer.Option()] = None,
    actor: Annotated[str | None, typer.Option()] = None,
    approved: Annotated[bool, typer.Option("--approve/--reject")] = True,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        if artifact_id is not None:
            if checkpoint_database is None or actor is None:
                raise typer.BadParameter(
                    "--checkpoint-database and --actor are required with --artifact-id"
                )
            item = repo.read_artifact_version(active_project, artifact_id)
            scopes = ScopeService(repo, project_id=active_project, workflow_id=workflow_id)
            approvals = ApprovalService(
                repo, scopes, project_id=active_project, workflow_id=workflow_id
            )
            services = WorkflowServices(
                repo=repo,
                project_id=active_project,
                target=ReviewTarget(
                    id=item.id,
                    version=item.version,
                    scope_id=item.scope_id,
                    baseline_id=item.requirement_baseline_id,
                ),
                approval_service=approvals,
            )

            async def resume_checkpoint() -> str:
                async with create_checkpoint_saver(checkpoint_database) as saver:
                    result = await WorkflowRuntime(services, saver).resume_workflow(
                        workflow_id,
                        HumanResponse(approved=approved, actor=actor),
                    )
                    return result.status

            typer.echo(f"{workflow_id}: {asyncio.run(resume_checkpoint())}")
            return
        resumed = repo.update_workflow_status(active_project, workflow_id, WorkflowStatus.ACTIVE)
    typer.echo(f"{resumed.id}: {resumed.status}")


@clarification.command("list")
def list_clarifications(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    workflow_id: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        items = ClarificationService(
            repo, project_id=active_project, workflow_id=workflow_id
        ).list_clarifications()
    for item in items:
        typer.echo(f"{item.id}\t{item.status}\t{item.question}")


@clarification.command("answer")
def answer_clarification(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    workflow_id: Annotated[str, typer.Option()],
    clarification_id: Annotated[str, typer.Option()],
    author: Annotated[str, typer.Option()],
    text: Annotated[str, typer.Option()],
    recorded_by: Annotated[str, typer.Option()],
    source: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        item = ClarificationService(
            repo, project_id=active_project, workflow_id=workflow_id
        ).record_stakeholder_answer(
            clarification_id,
            StakeholderAnswer(
                author=author,
                text=text,
                recorded_by=recorded_by,
                source=source,
            ),
        )
    typer.echo(f"{item.id}: {item.status}")


@scope.command("review")
def review_scope(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    workflow_id: Annotated[str, typer.Option()],
    scope_id: Annotated[str, typer.Option()],
    action: Annotated[str, typer.Option()],
    actor: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        service = ScopeService(repo, project_id=active_project, workflow_id=workflow_id)
        item = service.review_scope(
            scope_id,
            ScopeDecision(
                action=ScopeAction(action.upper()),
                actor=actor,
                human_reviewed=True,
            ),
        )
    typer.echo(f"{item.id}: {item.status}")


@artifact.command("show")
def show_artifact(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    artifact_id: Annotated[str, typer.Option()],
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        item = repo.read_artifact_version(active_project, artifact_id)
    typer.echo(item.model_dump_json(indent=2))


@artifact.command("review")
def review_artifact(
    database: Annotated[Path, typer.Option(help="Domain database path")],
    workflow_id: Annotated[str, typer.Option()],
    artifact_id: Annotated[str, typer.Option()],
    actor: Annotated[str, typer.Option()],
    approved: Annotated[bool, typer.Option("--approve/--reject")] = True,
    project_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _repository(database) as repo:
        active_project = _selected_project(database, repo, project_id)
        item = repo.read_artifact_version(active_project, artifact_id)
        if not approved:
            typer.echo(f"{item.id}: REJECTED")
            return
        scopes = ScopeService(repo, project_id=active_project, workflow_id=workflow_id)
        approvals = ApprovalService(
            repo, scopes, project_id=active_project, workflow_id=workflow_id
        )
        committed = approvals.commit_artifact_approval(
            artifact_id,
            ApprovalInput(
                approved=True,
                actor=actor,
                artifact_version=item.version,
                scope_id=item.scope_id,
                baseline_id=item.requirement_baseline_id,
                content_hash=item.content_hash,
                reviewed_evidence=(
                    f"artifact:{item.id}:{item.version}:{item.content_hash}",
                    f"baseline:{item.requirement_baseline_id}",
                ),
            ),
        )
    typer.echo(f"{committed.id}: {committed.status}")


@benchmark.command("run")
def run_benchmark(
    tier: Annotated[int, typer.Option(help="Cost-bearing benchmark tier")],
    dataset_version: Annotated[str, typer.Option()],
    output_directory: Annotated[Path, typer.Option()],
    agent_role: Annotated[AgentRole, typer.Option()] = AgentRole.ANALYST,
    max_cases: Annotated[int | None, typer.Option()] = None,
    confirm_budget: Annotated[bool, typer.Option("--confirm-budget")] = False,
    max_cost_usd: Annotated[float | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option(help="Application model config")] = None,
    input_usd_per_million: Annotated[float | None, typer.Option()] = None,
    output_usd_per_million: Annotated[float | None, typer.Option()] = None,
    repeats: Annotated[int, typer.Option(help="Tier 3 repeated runs")] = 1,
    active_qa_minutes: Annotated[float | None, typer.Option()] = None,
    manual_baseline_minutes: Annotated[float | None, typer.Option()] = None,
) -> None:
    if tier not in {2, 3}:
        raise typer.BadParameter("only explicit --tier 2 or --tier 3 evaluation is available")
    if not confirm_budget:
        raise typer.BadParameter("--confirm-budget is required for live API evaluation")

    prefix = f"QA_TIER{tier}"
    updates = {
        f"{prefix}_BUDGET_CONFIRMED": "yes",
        f"{prefix}_MAX_COST_USD": None if max_cost_usd is None else str(max_cost_usd),
        "QUALITY_PLATFORM_CONFIG": None if config is None else str(config),
        f"{prefix}_INPUT_USD_PER_MILLION": (
            None if input_usd_per_million is None else str(input_usd_per_million)
        ),
        f"{prefix}_OUTPUT_USD_PER_MILLION": (
            None if output_usd_per_million is None else str(output_usd_per_million)
        ),
        "QA_TIER3_ACTIVE_QA_MINUTES": (
            None if active_qa_minutes is None else str(active_qa_minutes)
        ),
        "QA_TIER3_MANUAL_BASELINE_MINUTES": (
            None if manual_baseline_minutes is None else str(manual_baseline_minutes)
        ),
    }
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is not None:
                os.environ[key] = value
        report = (
            asyncio.run(run_live_benchmark(dataset_version, agent_role, max_cases))
            if tier == 2
            else asyncio.run(run_full_benchmark(dataset_version, repeats=repeats))
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    json_path, markdown_path = write_report(report, output_directory)
    labels = ",".join(report.readiness_labels) or "none"
    typer.echo(f"{report.decision}: {json_path} {markdown_path} labels={labels}")


@benchmark.command("report")
def show_benchmark_report(report: Annotated[Path, typer.Option()]) -> None:
    parsed = BenchmarkReport.model_validate_json(report.read_text(encoding="utf-8"))
    typer.echo(
        json.dumps(
            {
                "run_id": parsed.run_id,
                "dataset_version": parsed.dataset_version,
                "decision": parsed.decision,
            },
            default=str,
            sort_keys=True,
        )
    )


def register_commands(root: typer.Typer) -> None:
    root.add_typer(project, name="project")
    root.add_typer(workflow, name="workflow")
    root.add_typer(clarification, name="clarification")
    root.add_typer(scope, name="scope")
    root.add_typer(artifact, name="artifact")
    root.add_typer(benchmark, name="benchmark")
    workflow.command("answer", hidden=True)(answer_clarification)
