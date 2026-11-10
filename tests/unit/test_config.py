from pathlib import Path

import pytest
from pydantic import ValidationError

from quality_platform.config.loader import load_config
from quality_platform.config.models import AppConfig


def valid_config(tmp_path: Path) -> dict:
    return {
        "database_path": str(tmp_path / "qa.sqlite"),
        "workspace_root": str(tmp_path),
        "agent_models": {
            role: {"provider": "openai", "model_id": f"example-{role}"}
            for role in ("orchestrator", "analyst", "planner", "evaluator")
        },
    }


def test_config_rejects_unknown_provider_and_missing_agent(tmp_path: Path) -> None:
    config = valid_config(tmp_path)
    config["agent_models"]["analyst"]["provider"] = "unknown"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config)

    config = valid_config(tmp_path)
    del config["agent_models"]["planner"]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config)


def test_config_rejects_per_task_override(tmp_path: Path) -> None:
    config = valid_config(tmp_path)
    config["task_model_overrides"] = {"requirement_analysis": "other-model"}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config)


def test_config_rejects_blank_model_and_credentials(tmp_path: Path) -> None:
    config = valid_config(tmp_path)
    config["agent_models"]["analyst"]["model_id"] = " "
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config)

    config = valid_config(tmp_path)
    config["agent_models"]["analyst"]["api_key"] = "secret"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config)


def test_load_config_reads_yaml_with_default_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: qa.sqlite\nworkspace_root: .\nagent_models:\n"
        + "".join(
            f"  {role}:\n    provider: openai\n    model_id: example-{role}\n"
            for role in ("orchestrator", "analyst", "planner", "evaluator")
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.max_concurrency == 3
    assert config.database_path == tmp_path / "qa.sqlite"
    assert config.workspace_root == tmp_path
