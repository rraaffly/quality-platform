"""Installed-style operational commands must not depend on the current directory."""

import asyncio
import json
import os
import socket
from pathlib import Path

import psutil
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.web.app import create_app
from quality_platform.web.control import DEFAULT_PASSWORD, Control
from quality_platform.web.launcher import app, data_root
from quality_platform.web.processes import start, status, stop
from quality_platform.web.worker import run_worker


def test_setup_from_another_directory_preserves_existing_owner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    root = tmp_path / ".quality-platform"
    first = CliRunner().invoke(app, ["setup"])
    assert first.exit_code == 0, first.output
    control = Control(root / "state")
    assert control.login("test", "Owner", DEFAULT_PASSWORD)
    assert control.local_config().database_path == (root / "domain.sqlite").resolve()
    assert control.local_config().workspace_root == (root / "workspace").resolve()
    assert (root / "config.yaml").is_file()
    assert (root / "workspace").is_dir()
    assert control.update_account(
        "My name", DEFAULT_PASSWORD, "private-test-password", client="test"
    )
    second = CliRunner().invoke(app, ["setup"])
    assert second.exit_code == 0
    assert "already initialized" in second.output
    assert control.login("test", "Owner", "private-test-password")
    assert control.account()["display_name"] == "My name"
    for obsolete in ("--skip-models", "--data-dir", "--owner", "--password-stdin", "--config"):
        assert obsolete not in CliRunner().invoke(app, ["setup", "--help"]).output


def test_default_data_root_is_home_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored"))
    assert data_root() == (tmp_path / ".quality-platform").resolve()


def test_setup_without_models_can_be_completed_from_web_settings(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 0, result.output
    root = tmp_path / ".quality-platform"
    assert "analyst provider" not in result.output
    assert yaml.safe_load((root / "config.yaml").read_text())["agent_models"] is None
    state = root / "state"
    control = Control(state)
    initial = control.settings_id()
    with pytest.raises(ValueError, match="Configure all agent models"):
        control.config()

    with TestClient(create_app(state, testing=True)) as client:
        guest = client.get("/api/session").json()
        logged_in = client.post(
            "/login",
            json={"password": DEFAULT_PASSWORD},
            headers={"X-CSRF-Token": guest["csrf"]},
        )
        assert logged_in.status_code == 200
        headers = {"X-CSRF-Token": logged_in.json()["csrf"], "Idempotency-Key": "create-project"}
        assert client.get("/api/settings").json()["settings"]["agent_models"] is None
        assert client.get("/api/projects").json()["models_configured"] is False
        project = client.post(
            "/api/commands",
            headers=headers,
            json={"kind": "create_project", "project_id": "demo", "name": "Demo"},
        )
        assert project.status_code == 202
        asyncio.run(run_worker(state, once=True))
        assert control.job(project.json()["job_id"])["status"] == "SUCCEEDED"
        workflow = {
            "kind": "create_workflow",
            "project_id": "demo",
            "workflow_id": "login",
            "goal": "Review login",
            "settings_id": initial,
        }
        headers["Idempotency-Key"] = "create-workflow"
        rejected = client.post("/api/commands", json=workflow, headers=headers)
        assert rejected.status_code == 409
        assert "Settings" in rejected.json()["detail"]
        settings = {
            "agent_models": {
                role: {"provider": provider, "model_id": f"selected-{role}"}
                for role, provider in zip(
                    ("orchestrator", "analyst", "planner", "evaluator"),
                    ("openai", "anthropic", "google", "openai"),
                    strict=True,
                )
            },
            "max_concurrency": 2,
            "test_case_style": "gherkin",
        }
        headers["Idempotency-Key"] = "save-settings"
        assert (
            client.post(
                "/api/settings",
                headers=headers,
                json={**settings, "agent_models": None},
            ).status_code
            == 422
        )
        saved = client.post("/api/settings", json=settings, headers=headers)
        assert saved.status_code == 201
        assert client.get("/api/settings").json()["settings"] == settings
        assert client.get("/api/projects").json()["models_configured"] is True
        assert control.local_config(initial).agent_models is None
        assert client.post("/api/commands", json=workflow, headers=headers).status_code == 409
        workflow["settings_id"] = saved.json()["settings_id"]
        headers["Idempotency-Key"] = "configured-workflow"
        accepted = client.post("/api/commands", json=workflow, headers=headers)
        assert accepted.status_code == 202
        asyncio.run(run_worker(state, once=True))
        assert control.job(accepted.json()["job_id"])["status"] == "SUCCEEDED"
        snapshot = client.get("/api/projects/demo/workflows/login").json()["snapshot"]
        assert snapshot["agent_models"] == settings["agent_models"]

    again = CliRunner().invoke(app, ["setup"])
    assert again.exit_code == 0 and "already initialized" in again.output
    assert control.config().agent_models.analyst.model_id == "selected-analyst"


def test_model_and_workspace_config_commands_share_web_settings(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert CliRunner().invoke(app, ["config", "model"]).exit_code != 0
    assert CliRunner().invoke(app, ["setup"]).exit_code == 0
    control = Control(tmp_path / ".quality-platform" / "state")
    workspace = tmp_path / "other workspace"
    changed = CliRunner().invoke(app, ["config", "workspace", str(workspace)])
    assert changed.exit_code == 0, changed.output
    assert control.local_config().workspace_root == workspace.resolve()
    assert workspace.is_dir()
    result = CliRunner().invoke(
        app,
        ["config", "model"],
        input="openai\ncoordinator\nanthropic\nanalyst\ngoogle\nplanner\nopenai\njudge\n",
    )
    assert result.exit_code == 0, result.output
    models = control.config().agent_models
    assert models.orchestrator.model_id == "coordinator"
    assert models.analyst.provider == "anthropic"
    assert models.planner.provider == "google"
    assert models.evaluator.model_id == "judge"
    assert control.config().workspace_root == workspace.resolve()
    settings_id = control.settings_id()
    invalid = CliRunner().invoke(app, ["config", "model"], input="bad\nx\n\nx\n\nx\n\nx\n")
    assert invalid.exit_code != 0
    assert control.settings_id() == settings_id
    not_directory = tmp_path / "file"
    not_directory.write_text("not a workspace")
    assert CliRunner().invoke(app, ["config", "workspace", str(not_directory)]).exit_code != 0
    assert control.config().workspace_root == workspace.resolve()


def _ready_state(tmp_path: Path) -> Path:
    model = ModelSpec(provider="openai", model_id="offline")
    config = AppConfig(
        database_path=tmp_path / "domain.sqlite",
        workspace_root=tmp_path / "workspace",
        agent_models=AgentModels(orchestrator=model, analyst=model, planner=model, evaluator=model),
    )
    state = tmp_path / "state"
    Control(state).initialize(config, "qa", "private-test-password")
    return state


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_dashboard_starts_once_and_stop_preserves_other_processes(tmp_path: Path) -> None:
    state = _ready_state(tmp_path)
    port = _free_port()
    try:
        assert start(state, port) == f"http://127.0.0.1:{port}"
        first = json.loads((state / "local-processes.json").read_text())
        assert status(state)["state"] == "RUNNING"
        assert start(state, port) == f"http://127.0.0.1:{port}"
        assert json.loads((state / "local-processes.json").read_text()) == first
        stopped = stop(state)
        assert stopped is True
        assert status(state)["state"] == "STOPPED"
        assert not (state / "local-processes.json").exists()
    finally:
        if (state / "local-processes.json").exists():
            stop(state)


def test_stop_rejects_reused_pid_without_killing_it(tmp_path: Path) -> None:
    state = _ready_state(tmp_path)
    own = psutil.Process(os.getpid())
    (state / "local-processes.json").write_text(
        json.dumps(
            {
                "port": 8000,
                "worker": {"pid": own.pid, "created": 0.0},
                "server": {"pid": own.pid, "created": 0.0},
            }
        )
    )
    try:
        with pytest.raises(ValueError, match="creation time"):
            stop(state)
        assert own.is_running()
    finally:
        (state / "local-processes.json").unlink()


def test_dashboard_command_opens_browser_after_health_and_reports_status(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    state = _ready_state(tmp_path / ".quality-platform")
    port = _free_port()
    opened: list[str] = []
    monkeypatch.setattr("quality_platform.web.launcher.webbrowser.open", opened.append)
    elsewhere = tmp_path / "another cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    try:
        launched = CliRunner().invoke(app, ["dashboard", "--port", str(port)])
        assert launched.exit_code == 0, launched.output
        assert opened == [f"http://127.0.0.1:{port}"]
        current = CliRunner().invoke(app, ["status"])
        assert current.exit_code == 0 and "RUNNING" in current.output
        stopped = CliRunner().invoke(app, ["stop"])
        assert stopped.exit_code == 0 and "stopped" in stopped.output
    finally:
        if (state / "local-processes.json").exists():
            stop(state)


def test_failed_server_start_cleans_up_recorded_children(tmp_path: Path) -> None:
    state = _ready_state(tmp_path)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        with pytest.raises(RuntimeError, match="logs"):
            start(state, port)
    assert not (state / "local-processes.json").exists()
    assert status(state)["state"] == "STOPPED"
