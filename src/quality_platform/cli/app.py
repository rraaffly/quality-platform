import typer

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
