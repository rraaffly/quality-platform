"""Local server setup and independent worker processes, including native Windows."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from quality_platform.config.loader import load_config
from quality_platform.storage.database import open_database
from quality_platform.storage.ownership import writer_lock
from quality_platform.web.app import create_app
from quality_platform.web.control import Control
from quality_platform.web.worker import run_worker

app = typer.Typer(help="Quality Platform local web interface and single writer worker")


@app.command()
def init(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    state: Annotated[Path, typer.Option()],
    owner: Annotated[str, typer.Option()],
) -> None:
    """Bootstrap private state; password is requested interactively, never a CLI argument."""
    values = load_config(config)
    if (state / "control.sqlite").exists():
        raise typer.BadParameter("Web state already initialized")
    password = typer.prompt(
        "Owner password (at least 12 characters)", hide_input=True, confirmation_prompt=True
    )
    with writer_lock(values.database_path):
        engine = open_database(values.database_path)
        engine.dispose()
        Control(state).initialize(values, owner, password)
    typer.echo("Web initialized. Start the worker and server in separate terminals.")


@app.command()
def worker(state: Annotated[Path, typer.Option(exists=True, file_okay=False)]) -> None:
    """Process queued commands; keep this process running separately from HTTP."""
    asyncio.run(run_worker(state))


@app.command()
def serve(
    state: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    public_origin: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Serve locally; remote binding requires an explicit HTTPS reverse-proxy origin."""
    if host not in {"127.0.0.1", "localhost", "::1"} and not public_origin:
        raise typer.BadParameter(
            "Remote binding requires --public-origin https://your-host and TLS termination"
        )
    web = create_app(state, public_origin=public_origin)
    # No trust in arbitrary forwarded headers; a configured origin supplies secure cookie policy.
    uvicorn.run(web, host=host, port=port, proxy_headers=False, access_log=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
