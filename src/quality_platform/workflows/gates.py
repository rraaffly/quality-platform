from pydantic import BaseModel, ConfigDict, Field


class ReviewTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    scope_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)


class HumanResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approved: bool
    actor: str = Field(min_length=1)
