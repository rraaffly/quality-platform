from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "anthropic", "google"]
    model_id: str

    @field_validator("model_id")
    @classmethod
    def nonempty_model_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model_id must not be blank")
        return value


class AgentModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orchestrator: ModelSpec
    analyst: ModelSpec
    planner: ModelSpec
    evaluator: ModelSpec


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_path: Path
    workspace_root: Path
    max_concurrency: int = Field(default=3, ge=1)
    test_case_style: Literal["detailed", "gherkin", "both"] = "both"
    agent_models: AgentModels
