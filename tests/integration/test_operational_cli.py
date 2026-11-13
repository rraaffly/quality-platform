"""Installed-style operational commands must not depend on the current directory."""

from pathlib import Path

from typer.testing import CliRunner

from quality_platform.web.control import Control
from quality_platform.web.launcher import app, data_root


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
