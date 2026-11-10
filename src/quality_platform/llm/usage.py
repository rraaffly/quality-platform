from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field

from quality_platform.config.models import ModelSpec
from quality_platform.llm.registry import AgentRole


class UsageRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: AgentRole
    provider: str
    model_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


def capture_usage(role: AgentRole, spec: ModelSpec, message: AIMessage) -> UsageRecord:
    metadata = message.usage_metadata
    input_tokens = metadata.get("input_tokens", 0) if metadata is not None else 0
    output_tokens = metadata.get("output_tokens", 0) if metadata is not None else 0
    total_tokens = (
        metadata.get("total_tokens", input_tokens + output_tokens)
        if metadata is not None
        else input_tokens + output_tokens
    )
    return UsageRecord(
        role=role,
        provider=spec.provider,
        model_id=spec.model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )
