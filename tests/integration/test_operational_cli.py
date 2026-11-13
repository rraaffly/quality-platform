"""Installed-style operational commands must not depend on the current directory."""

import json
import os
import socket
from pathlib import Path

import psutil
import pytest
from typer.testing import CliRunner

from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.web.control import Control
from quality_platform.web.launcher import app, data_root
from quality_platform.web.processes import start, status, stop


def test_setup_from_another_directory_preserves_existing_owner(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "source config.yaml"
    config.write_text(
        """database_path: ./domain.sqlite
workspace_root: ./workspace
max_concurrency: 1
agent_models:
  orchestrator: {provider: openai, model_id: offline}
  analyst: {provider: openai, model_id: offline}
  planner: {provider: openai, model_id: offline}
  evaluator: {provider: openai, model_id: offline}
""",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    root = tmp_path / "my data"
    first = CliRunner().invoke(
        app,
        ["setup", "--data-dir", str(root), "--config", str(config), "--owner", "qa"],
        input="private-test-password\nprivate-test-password\n",
    )
    assert first.exit_code == 0, first.output
    control = Control(root / "state")
    assert control.login("test", "qa", "private-test-password")
    assert control.config().database_path == (tmp_path / "domain.sqlite").resolve()
    assert (root / "config.yaml").is_file()
    assert (tmp_path / "workspace").is_dir()
    second = CliRunner().invoke(app, ["setup", "--data-dir", str(root)])
    assert second.exit_code == 0
    assert "already initialized" in second.output
    assert control.login("test", "qa", "private-test-password")


def test_default_data_root_is_platform_user_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "platformdirs.user_data_path", lambda name, appauthor: tmp_path / name
    )
    assert data_root(None) == (tmp_path / "QualityPlatform").resolve()


def test_setup_accepts_password_from_stdin_for_automation(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "database_path: ./domain.sqlite\nworkspace_root: ./workspace\n"
        "agent_models:\n"
        "  orchestrator: {provider: openai, model_id: offline}\n"
        "  analyst: {provider: openai, model_id: offline}\n"
        "  planner: {provider: openai, model_id: offline}\n"
        "  evaluator: {provider: openai, model_id: offline}\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["setup", "--data-dir", str(tmp_path / "data"), "--config", str(config),
         "--owner", "qa", "--password-stdin"],
        input="private-test-password\n",
    )
    assert result.exit_code == 0, result.output


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
        json.dumps({
            "port": 8000,
            "worker": {"pid": own.pid, "created": 0.0},
            "server": {"pid": own.pid, "created": 0.0},
        })
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
    state = _ready_state(tmp_path)
    port = _free_port()
    opened: list[str] = []
    monkeypatch.setattr("quality_platform.web.launcher.webbrowser.open", opened.append)
    elsewhere = tmp_path / "another cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    try:
        launched = CliRunner().invoke(
            app, ["dashboard", "--data-dir", str(tmp_path), "--port", str(port)]
        )
        assert launched.exit_code == 0, launched.output
        assert opened == [f"http://127.0.0.1:{port}"]
        current = CliRunner().invoke(app, ["status", "--data-dir", str(tmp_path)])
        assert current.exit_code == 0 and "RUNNING" in current.output
        stopped = CliRunner().invoke(app, ["stop", "--data-dir", str(tmp_path)])
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
