import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from quality_platform.config.models import AppConfig, ModelSpec


class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    ANALYST = "analyst"
    PLANNER = "planner"
    EVALUATOR = "evaluator"


class RoleModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: AgentRole
    provider: str
    model_id: str


class ModelSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    models: tuple[RoleModel, ...]

    def get(self, role: AgentRole) -> RoleModel:
        for model in self.models:
            if model.role == role:
                return model
        raise ValueError(f"no model configured for role: {role}")


def snapshot_models(config: AppConfig) -> ModelSnapshot:
    models = tuple(
        RoleModel(
            role=role,
            provider=getattr(config.agent_models, role.value).provider,
            model_id=getattr(config.agent_models, role.value).model_id,
        )
        for role in AgentRole
    )
    canonical = json.dumps([model.model_dump(mode="json") for model in models], sort_keys=True)
    return ModelSnapshot(id=hashlib.sha256(canonical.encode("utf-8")).hexdigest(), models=models)


class ModelRegistry:
    def __init__(self, config: AppConfig) -> None:
        self.snapshot = snapshot_models(config)

    def get(self, agent_role: AgentRole) -> ModelSpec:
        role = AgentRole(agent_role)
        model = self.snapshot.get(role)
        return ModelSpec.model_validate({"provider": model.provider, "model_id": model.model_id})
