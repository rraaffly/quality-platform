import typer

from quality_platform.cli.commands import register_commands

app = typer.Typer(help="QA Orchestrator CLI")
register_commands(app)
