from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from quality_platform.domain.models import SourceReference
from quality_platform.llm.registry import AgentRole


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TaskType(StrEnum):
    ANALYST = "ANALYST"
    PLANNER = "PLANNER"
    POLICY = "POLICY"


class ReleaseDecision(StrEnum):
    PASS = "PASS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"


class ExpectedItem(EvalRecord):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_references: tuple[SourceReference, ...] = ()
    semantic_alternatives: tuple[str, ...] = ()


class GoldReview(EvalRecord):
    reviewer: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    reviewed_at: AwareDatetime


class GoldRecord(EvalRecord):
    status: GoldStatus
    expected_items: tuple[ExpectedItem, ...]
    forbidden_actions: tuple[str, ...] = ()
    expected_workflow_state: str | None = None
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None
    review_history: tuple[GoldReview, ...] = ()

    @model_validator(mode="after")
    def approval_has_human_record(self) -> "GoldRecord":
        if self.status == GoldStatus.APPROVED and (
            not self.approved_by or self.approved_at is None
        ):
            raise ValueError("approved gold requires human reviewer and timestamp")
        return self


class BenchmarkCase(EvalRecord):
    case_id: str = Field(min_length=1)
    task_type: TaskType
    input_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    severity: Severity
    origin: Literal["synthetic", "real_world"] = "synthetic"
    gold: GoldRecord


class Dataset(EvalRecord):
    version: str = Field(min_length=1)
    rubric_id: str = "qa-mvp"
    rubric_version: str = Field(min_length=1)
    cases: tuple[BenchmarkCase, ...]

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> "Dataset":
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate benchmark case ID")
        return self


class Rubric(EvalRecord):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str


class ObservedItem(EvalRecord):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_references: tuple[SourceReference, ...] = ()


class EvaluationOutput(EvalRecord):
    observed_items: tuple[ObservedItem, ...] = ()
    actions: tuple[str, ...] = ()
    workflow_state: str | None = None
    raw_text: str | None = Field(default=None, exclude=True, repr=False)


class CaseScore(EvalRecord):
    case_id: str
    eligible: bool
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    evidence_accuracy: float = Field(ge=0, le=1)
    unsupported_source_count: int = Field(ge=0)
    critical_violation: bool
    findings: tuple[str, ...] = ()


class GoldRequirement(EvalRecord):
    id: str = Field(min_length=1)
    source_references: tuple[SourceReference, ...] = Field(min_length=1)
    semantic_alternatives: tuple[str, ...] = ()


class ExtractionScore(EvalRecord):
    matched_requirement_ids: tuple[str, ...]
    missed_requirement_ids: tuple[str, ...]
    unexpected_requirement_ids: tuple[str, ...]
    review_required_requirement_ids: tuple[str, ...] = ()
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)


class GoldCase(EvalRecord):
    case_id: str = Field(min_length=1)
    requirements: tuple[GoldRequirement, ...]


class Artifact(EvalRecord):
    id: str = Field(min_length=1)
    requirement_ids: tuple[str, ...]
    source_references: tuple[SourceReference, ...] = ()


class TraceabilityScore(EvalRecord):
    requirement_coverage: float = Field(ge=0, le=1)
    evidence_accuracy: float = Field(ge=0, le=1)
    missing_requirement_ids: tuple[str, ...]
    unexpected_requirement_ids: tuple[str, ...]
    unsupported_source_count: int = Field(ge=0)
    review_required: bool


class ModelUsage(EvalRecord):
    role: AgentRole
    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)


class CandidateSnapshot(EvalRecord):
    candidate_id: str = Field(min_length=1)
    commit_sha: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    model_snapshot_id: str = Field(min_length=1)
    prompt_snapshot_id: str = Field(min_length=1)


class BenchmarkReport(EvalRecord):
    run_id: str = Field(min_length=1)
    generated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    dataset_version: str
    rubric_id: str
    rubric_version: str
    candidate: CandidateSnapshot
    usage: tuple[ModelUsage, ...] = ()
    total_cost_usd: float = Field(default=0, ge=0)
    case_scores: tuple[CaseScore, ...]
    eligible_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    overall_precision: float = Field(ge=0, le=1)
    overall_recall: float = Field(ge=0, le=1)
    decision: ReleaseDecision
