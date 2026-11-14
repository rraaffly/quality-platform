"""User-facing operational commands for the local Web application."""

import os
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from quality_platform.config.models import AgentModels, LocalConfig
from quality_platform.evaluation.reporting import _restrict_private_path
from quality_platform.storage.database import open_database
from quality_platform.storage.ownership import writer_lock
from quality_platform.web.control import DEFAULT_PASSWORD, Control
from quality_platform.web.processes import start
from quality_platform.web.processes import status as process_status
from quality_platform.web.processes import stop as process_stop

app = typer.Typer(help="Set up and open the Quality Platform local dashboard")
config_app = typer.Typer(help="Configure models and the workspace for new projects")
app.add_typer(config_app, name="config")


@app.callback()
def root() -> None:
    """Quality Platform local operations."""


def data_root() -> Path:
    return (Path.home() / ".quality-platform").resolve()


@app.command()
def setup() -> None:
    """Initialize private local data without model or account questions."""
    root = data_root()
    state = root / "state"
    if (state / "control.sqlite").exists():
        typer.echo(f"Quality Platform is already initialized at {root}")
        return
    root.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(root, 0o700)
    values = LocalConfig(
        database_path=root / "domain.sqlite", workspace_root=root / "workspace", max_concurrency=1
    )
    values.workspace_root.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(values.workspace_root, 0o700)
    local_config = root / "config.yaml"
    with local_config.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(values.model_dump(mode="json"), stream, sort_keys=True)
    _restrict_private_path(local_config, 0o600)
    with writer_lock(values.database_path):
        engine = open_database(values.database_path)
        engine.dispose()
        Control(state).initialize(values)
    _restrict_private_path(state, 0o700)
    typer.echo(f"Quality Platform initialized at {root}")
    typer.echo(f"Initial owner: Owner. Initial password: {DEFAULT_PASSWORD}")
    typer.echo("Change the initial password in Web Settings before enabling remote access.")
    typer.echo("Configure models in Web Settings or run quality-platform config model.")


@config_app.command("model")
def configure_model() -> None:
    """Choose a provider and model for each agent without contacting providers."""
    control = Control(data_root() / "state")
    if not control.path.is_file():
        raise typer.BadParameter("Run quality-platform setup first")
    current = control.local_config()
    models: dict[str, dict[str, str]] = {}
    for role in ("orchestrator", "analyst", "planner", "evaluator"):
        existing = getattr(current.agent_models, role) if current.agent_models else None
        models[role] = {
            "provider": typer.prompt(
                f"{role} provider", default=existing.provider if existing else "openai"
            )
            .strip()
            .lower(),
            "model_id": typer.prompt(
                f"{role} model ID", default=existing.model_id if existing else None
            ).strip(),
        }
    try:
        configured = AgentModels.model_validate(models)
    except ValidationError as error:
        raise typer.BadParameter(
            "Choose a supported provider and nonempty model ID for every agent"
        ) from error
    control.save_settings(
        {
            **current.model_dump(mode="json", exclude={"database_path", "workspace_root"}),
            "agent_models": configured.model_dump(mode="json"),
        }
    )
    typer.echo("Model settings saved. Existing workflows keep their pinned configuration.")
    keys = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY",
    }
    providers = {value["provider"] for value in models.values()}
    missing = [keys[provider] for provider in sorted(providers) if not os.getenv(keys[provider])]
    if missing:
        typer.echo("Set provider environment variables before live runs: " + ", ".join(missing))


@config_app.command("workspace")
def configure_workspace(
    path: Annotated[Path, typer.Argument(help="Workspace for new projects")],
) -> None:
    """Set the workspace for new projects without moving existing projects."""
    control = Control(data_root() / "state")
    if not control.path.is_file():
        raise typer.BadParameter("Run quality-platform setup first")
    try:
        control.save_workspace(str(path.expanduser().resolve()))
    except (ValueError, OSError) as error:
        raise typer.BadParameter("Workspace must be a writable directory") from error
    typer.echo(f"Workspace for new projects: {control.local_config().workspace_root}")
    typer.echo("Existing projects remain at their original locations.")


def main() -> None:
    app()


def state_path(state: Path | None) -> Path:
    return state.resolve() if state is not None else data_root() / "state"


@app.command()
def dashboard(
    state: Annotated[Path | None, typer.Option(help="Existing state directory")] = None,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    no_open: Annotated[bool, typer.Option(help="Print the URL without opening a browser")] = False,
) -> None:
    """Start the local Web app in the background and open it in a browser."""
    url = start(state_path(state), port)
    typer.echo(f"Quality Platform ready at {url}")
    if not no_open:
        webbrowser.open(url)


@app.command()
def status(state: Annotated[Path | None, typer.Option()] = None) -> None:
    """Show local server and worker status."""
    result = process_status(state_path(state))
    typer.echo(f"{result['state']}: {result.get('url', '')} {result.get('logs', '')}".strip())


@app.command()
def stop(state: Annotated[Path | None, typer.Option()] = None) -> None:
    """Stop only the recorded local server and worker."""
    stopped = process_stop(state_path(state))
    typer.echo("Quality Platform stopped" if stopped else "Quality Platform was not running")


if __name__ == "__main__":
    main()
