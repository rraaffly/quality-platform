"""User-facing operational commands for the local Web application."""

import os
from pathlib import Path
from typing import Annotated

import platformdirs
import typer
import yaml

from quality_platform.config.loader import load_config
from quality_platform.config.models import AppConfig
from quality_platform.evaluation.reporting import _restrict_private_path
from quality_platform.storage.database import open_database
from quality_platform.storage.ownership import writer_lock
from quality_platform.web.control import Control

app = typer.Typer(help="Set up and open the Quality Platform local dashboard")


@app.callback()
def root() -> None:
    """Quality Platform local operations."""


def data_root(override: Path | None) -> Path:
    return (override or platformdirs.user_data_path("QualityPlatform", appauthor=False)).resolve()


def _interactive_config(root: Path) -> AppConfig:
    models: dict[str, dict[str, str]] = {}
    for role in ("orchestrator", "analyst", "planner", "evaluator"):
        models[role] = {
            "provider": typer.prompt(f"{role} provider", default="openai").strip().lower(),
            "model_id": typer.prompt(f"{role} model ID").strip(),
        }
    return AppConfig.model_validate(
        {
            "database_path": root / "domain.sqlite",
            "workspace_root": root / "workspace",
            "max_concurrency": 1,
            "test_case_style": "both",
            "agent_models": models,
        }
    )


@app.command()
def setup(
    data_dir: Annotated[Path | None, typer.Option(help="Private user data directory")] = None,
    config: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    owner: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Initialize owner credentials, model settings and private local state once."""
    root = data_root(data_dir)
    state = root / "state"
    if (state / "control.sqlite").exists():
        typer.echo(f"Quality Platform is already initialized at {root}")
        return
    root.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(root, 0o700)
    local_config = root / "config.yaml"
    if config is not None:
        values = load_config(config)
    elif local_config.exists():
        values = load_config(local_config)
    else:
        values = _interactive_config(root)
    selected_owner = owner or typer.prompt("Owner name", default="qa")
    password = typer.prompt(
        "Owner password (at least 12 characters)", hide_input=True, confirmation_prompt=True
    )
    if len(password) < 12:
        raise typer.BadParameter("Owner password must have at least 12 characters")
    values.workspace_root.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(values.workspace_root, 0o700)
    contents = yaml.safe_dump(values.model_dump(mode="json"), sort_keys=True)
    if local_config.exists():
        if local_config.read_text(encoding="utf-8") != contents:
            raise typer.BadParameter("Existing private config differs; choose a new data directory")
    else:
        with local_config.open("x", encoding="utf-8") as stream:
            stream.write(contents)
    _restrict_private_path(local_config, 0o600)
    with writer_lock(values.database_path):
        engine = open_database(values.database_path)
        engine.dispose()
        Control(state).initialize(values, selected_owner, password)
    _restrict_private_path(state, 0o700)
    keys = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY",
    }
    providers = {
        getattr(values.agent_models, role).provider
        for role in ("orchestrator", "analyst", "planner", "evaluator")
    }
    missing = [keys[provider] for provider in sorted(providers) if not os.getenv(keys[provider])]
    typer.echo(f"Quality Platform initialized at {root}")
    if missing:
        typer.echo("Set provider environment variables before live runs: " + ", ".join(missing))


def main() -> None:
    app()
