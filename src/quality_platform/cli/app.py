from pathlib import Path
from typing import Annotated

import typer

from quality_platform.domain.models import StakeholderAnswer
from quality_platform.services.clarifications import ClarificationService
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories

app = typer.Typer(help="QA Orchestrator CLI")
project = typer.Typer(help="Manage QA projects")
workflow = typer.Typer(help="Manage QA workflows")
benchmark = typer.Typer(help="Run and inspect evaluations")

app.add_typer(project, name="project")
app.add_typer(workflow, name="workflow")
app.add_typer(benchmark, name="benchmark")


@project.callback(invoke_without_command=True)
def project_group() -> None:
    """Project commands arrive with the persistence layer."""


@workflow.callback(invoke_without_command=True)
def workflow_group() -> None:
    """Workflow commands arrive with the graph runtime."""


@benchmark.callback(invoke_without_command=True)
def benchmark_group() -> None:
    """Benchmark commands arrive with the evaluation runner."""


@workflow.command("answer")
def record_workflow_answer(
    database: Annotated[Path, typer.Option(help="Project database path")],
    project_id: Annotated[str, typer.Option()],
    workflow_id: Annotated[str, typer.Option()],
    clarification_id: Annotated[str, typer.Option()],
    author: Annotated[str, typer.Option()],
    text: Annotated[str, typer.Option()],
    recorded_by: Annotated[str, typer.Option()],
    source: Annotated[str, typer.Option()],
) -> None:
    """Record a stakeholder answer for an open clarification."""
    engine = open_database(database)
    try:
        service = ClarificationService(
            QaRepositories(engine), project_id=project_id, workflow_id=workflow_id
        )
        item = service.record_stakeholder_answer(
            clarification_id,
            StakeholderAnswer(author=author, text=text, recorded_by=recorded_by, source=source),
        )
        typer.echo(f"{item.id}: {item.status}")
    finally:
        engine.dispose()
