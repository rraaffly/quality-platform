"""Public web inputs deliberately contain no filesystem paths or actor overrides."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality_platform.config.models import AgentModels
from quality_platform.services.scopes import ScopeProposal

SafeID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_models: AgentModels
    max_concurrency: int = Field(ge=1, le=16)
    test_case_style: Literal["detailed", "gherkin", "both"]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "create_project",
        "create_workflow",
        "import",
        "run",
        "resume",
        "answer",
        "scope",
        "baseline",
        "submit",
        "decide",
        "revision",
        "apply",
        "scope_proposal",
    ]
    project_id: SafeID
    workflow_id: SafeID | None = None
    name: str = Field(default="", max_length=200)
    goal: str = Field(default="", max_length=10000)
    settings_id: SafeID | None = None
    instruction: str = Field(default="", max_length=30000)
    request_id: SafeID | None = None
    style: Literal["detailed", "gherkin", "both"] | None = None
    target_id: SafeID | None = None
    baseline_id: SafeID | None = None
    state_hash: str = Field(default="", max_length=64)
    action: Literal["APPROVE", "REJECT", "MERGE", "SPLIT"] = "APPROVE"
    replacements: tuple[ScopeProposal, ...] = ()
    merge_with_ids: tuple[SafeID, ...] = ()
    author: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=20000)
    source: str = Field(default="", max_length=1000)
    draft_id: SafeID | None = None
    reviewed_diff_hash: str = Field(default="", max_length=64)
    confirm_cost: bool = False

    @model_validator(mode="after")
    def required_inputs(self) -> "Command":
        if self.kind == "create_project":
            if not self.name.strip():
                raise ValueError("Project name is required")
            return self
        if self.workflow_id is None:
            raise ValueError("Workflow is required")
        if self.kind == "create_workflow" and (not self.goal.strip() or not self.settings_id):
            raise ValueError("Goal and settings version are required")
        if self.kind == "run" and (not self.request_id or not self.instruction.strip()):
            raise ValueError("Instruction and request ID are required")
        if self.kind == "resume" and not self.request_id:
            raise ValueError("Original request ID is required")
        if self.kind in {"run", "resume", "revision", "scope_proposal"} and not self.confirm_cost:
            raise ValueError("Explicit confirmation of paid model usage is required")
        if self.kind in {"revision", "scope_proposal"} and not self.instruction.strip():
            raise ValueError("Instruction is required")
        if (
            self.kind in {"scope", "baseline", "submit", "decide", "revision", "answer"}
            and not self.target_id
        ):
            raise ValueError("Target is required")
        if self.kind == "submit" and not self.baseline_id:
            raise ValueError("Approved baseline is required")
        if (
            self.kind in {"scope", "baseline", "submit", "decide", "apply", "answer"}
            and not self.state_hash
        ):
            raise ValueError("Reviewed state hash is required")
        if self.kind == "answer" and not all(
            value.strip() for value in (self.author, self.text, self.source)
        ):
            raise ValueError("Stakeholder, answer and source are required")
        if self.kind == "apply" and (not self.draft_id or not self.reviewed_diff_hash):
            raise ValueError("Draft and reviewed diff hash are required")
        return self
