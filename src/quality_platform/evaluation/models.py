from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from quality_platform.domain.models import Requirement, SourceReference
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
    APPROVED_EXCEPTION = "APPROVED_EXCEPTION"


class ExpectedItem(EvalRecord):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_references: tuple[SourceReference, ...] = ()
    semantic_alternatives: tuple[str, ...] = ()
    semantic_definition: str | None = Field(default=None, min_length=1)
    accepted_contents: tuple[str, ...] = ()


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
    input_snapshot_hashes: dict[str, str] = Field(default_factory=dict)
    severity: Severity
    origin: Literal["synthetic", "real_world"] = "synthetic"
    gold: GoldRecord
    planner_requirements: tuple[Requirement, ...] = ()
    planner_scope_id: str = "BENCHMARK"
    policy_test_ids: tuple[str, ...] = ()


class Dataset(EvalRecord):
    version: str = Field(min_length=1)
    rubric_id: str = "qa-mvp"
    rubric_version: str = Field(min_length=1)
    cases: tuple[BenchmarkCase, ...]
    content_fingerprint: str = ""

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
    content: str = ""
    semantic_context: str = ""


class SemanticJudgment(EvalRecord):
    expected_item_id: str
    observed_item_id: str
    equivalent: bool
    review_required: bool = True
    uncertainty: float = Field(ge=0, le=1)


class MetricReview(EvalRecord):
    score: float = Field(ge=0, le=1)
    review_required: bool
    uncertainty: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)


class PlannerReview(EvalRecord):
    expected_result_correctness: MetricReview
    executability: MetricReview
    redundancy: MetricReview  # Fraction redundant; lower is better.
    detailed_gherkin_agreement: MetricReview


class EvaluatorAssessment(EvalRecord):
    case_id: str
    model_snapshot_id: str = Field(min_length=1)
    reviewed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    rating: int = Field(ge=1, le=5)
    critical: bool
    uncertainty: float = Field(ge=0, le=1)
    judgments: tuple[SemanticJudgment, ...]
    planner_review: PlannerReview | None = None


class EvaluationOutput(EvalRecord):
    observed_items: tuple[ObservedItem, ...] = ()
    actions: tuple[str, ...] = ()
    workflow_state: str | None = None
    evaluator_assessment: EvaluatorAssessment | None = None
    raw_text: str | None = Field(default=None, exclude=True, repr=False)
    requirement_coverage: float | None = Field(default=None, ge=0, le=1)
    schema_valid: bool | None = None
    policy_observation_complete: bool = True


class CaseScore(EvalRecord):
    case_id: str
    eligible: bool
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    evidence_accuracy: float = Field(ge=0, le=1)
    unsupported_source_count: int = Field(ge=0)
    critical_violation: bool
    findings: tuple[str, ...] = ()
    planner_metrics: dict[str, float] = Field(default_factory=dict)
    observed_actions: tuple[str, ...] = ()
    observed_workflow_state: str | None = None


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


class RepeatResult(EvalRecord):
    run_id: str
    case_scores: tuple[CaseScore, ...]
    overall_precision: float = Field(ge=0, le=1)
    overall_recall: float = Field(ge=0, le=1)
    decision: ReleaseDecision
    evaluator_assessments: tuple[EvaluatorAssessment, ...] = ()


class ProductivityEvidence(EvalRecord):
    equivalent_task_id: str = Field(min_length=1)
    acceptance_criteria_id: str = Field(min_length=1)
    manual_task_id: str = Field(min_length=1)
    assisted_task_id: str = Field(min_length=1)
    accepted_artifact_ids: tuple[str, ...] = Field(min_length=1)
    accepted_by: str = Field(min_length=1)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    active_qa_minutes: float = Field(ge=0)
    manual_baseline_minutes: float = Field(gt=0)
    human_review_minutes: float = Field(ge=0)
    rework_minutes: float = Field(ge=0)

    @model_validator(mode="after")
    def active_time_includes_review_and_rework(self) -> "ProductivityEvidence":
        if self.active_qa_minutes < self.human_review_minutes + self.rework_minutes:
            raise ValueError("active QA time must include human review and rework time")
        if self.manual_task_id == self.assisted_task_id:
            raise ValueError("manual and assisted task executions need distinct identities")
        return self


class SuiteTestResult(EvalRecord):
    test_id: str
    suite: str
    outcome: Literal["passed", "failed", "error", "skipped"]
    duration_seconds: float = Field(ge=0)


class WorkflowSuiteEvidence(EvalRecord):
    benchmark_binding: str
    source_fingerprint: str
    source_manifest: dict[str, str]
    commit_sha: str
    started_at: AwareDatetime
    completed_at: AwareDatetime
    command: tuple[str, ...]
    suites: tuple[str, ...]
    exit_code: int
    test_results: tuple[SuiteTestResult, ...]
    junit_path: str
    junit_sha256: str


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
    repeat_count: int = Field(default=1, ge=1)
    latency_seconds: float = Field(default=0, ge=0)
    active_qa_minutes: float | None = Field(default=None, ge=0)
    manual_baseline_minutes: float | None = Field(default=None, ge=0)
    failure_case_ids: tuple[str, ...] = ()
    readiness_labels: tuple[str, ...] = ()
    input_fingerprint: str = ""
    output_fingerprint: str = ""
    evaluated_case_ids: tuple[str, ...] = ()
    evaluator_assessments: tuple[EvaluatorAssessment, ...] = ()
    repeat_results: tuple[RepeatResult, ...] = ()
    metric_variance: dict[str, float] = Field(default_factory=dict)
    productivity_evidence: ProductivityEvidence | None = None
    code_fingerprint: str = ""
    workflow_suite_evidence: WorkflowSuiteEvidence | None = None
    policy_suite_tests: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    policy_case_results: dict[str, ReleaseDecision] = Field(default_factory=dict)
    review_packet_path: str | None = None
    review_packet_sha256: str | None = None
    # Kept only until the private review packet is written, never in score exports.
    review_outputs: tuple[dict[str, EvaluationOutput], ...] = Field(
        default=(), exclude=True, repr=False
    )
