from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from quality_platform.config.models import AppConfig, ModelSpec
from quality_platform.llm.registry import AgentRole, ModelRegistry, snapshot_models


def configuration(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "database_path": tmp_path / "qa.sqlite",
            "workspace_root": tmp_path,
            "agent_models": {
                role: {"provider": "openai", "model_id": f"configured-{role}"}
                for role in ("orchestrator", "analyst", "planner", "evaluator")
            },
        }
    )


def test_all_four_agents_have_exactly_one_static_model(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    registry = ModelRegistry(config)
    assert {registry.get(role).model_id for role in AgentRole} == {
        "configured-orchestrator",
        "configured-analyst",
        "configured-planner",
        "configured-evaluator",
    }
    with pytest.raises(ValueError):
        registry.get(cast(AgentRole, "unknown"))


def test_unknown_provider_missing_agent_and_task_override_are_rejected(tmp_path: Path) -> None:
    raw = configuration(tmp_path).model_dump()
    raw["agent_models"]["analyst"]["provider"] = "unknown"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)
    raw = configuration(tmp_path).model_dump()
    raw["agent_models"]["analyst"]["model_id"] = "  "
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)
    raw = configuration(tmp_path).model_dump()
    del raw["agent_models"]["planner"]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)
    raw = configuration(tmp_path).model_dump()
    raw["task_model_overrides"] = {"analysis": "other-model"}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_snapshot_and_registry_do_not_change_after_config_edit(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    snapshot = snapshot_models(config)
    registry = ModelRegistry(config)
    config.agent_models.analyst = ModelSpec(provider="google", model_id="changed-model")
    assert snapshot.get(AgentRole.ANALYST).model_id == "configured-analyst"
    assert registry.get(AgentRole.ANALYST).model_id == "configured-analyst"
    assert "API_KEY" not in snapshot.model_dump_json()
