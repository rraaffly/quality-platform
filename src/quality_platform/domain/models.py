from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequirementReadiness(StrEnum):
    EXTRACTED = "EXTRACTED"
    VALIDATED = "VALIDATED"
    BLOCKED = "BLOCKED"


class ClarificationStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    RESOLVED = "RESOLVED"
    REOPENED = "REOPENED"


class ClarificationIssueType(StrEnum):
    MISSING_RULE = "MISSING_RULE"
    AMBIGUITY = "AMBIGUITY"
    CONTRADICTION = "CONTRADICTION"
    CROSS_DOCUMENT_CONFLICT = "CROSS_DOCUMENT_CONFLICT"


class ScopeStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ArtifactStatus(StrEnum):
    DRAFT = "DRAFT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"


class ArtifactStaleness(StrEnum):
    CURRENT = "CURRENT"
    POTENTIALLY_STALE = "POTENTIALLY_STALE"


class ArtifactKind(StrEnum):
    REQUIREMENT_ANALYSIS = "REQUIREMENT_ANALYSIS"
    TEST_PLAN = "TEST_PLAN"
    TEST_CASES = "TEST_CASES"


class WorkflowStatus(StrEnum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SourceReference(Record):
    document_id: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    heading: str | None = None

    @model_validator(mode="after")
    def valid_line_range(self) -> "SourceReference":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be at least start_line")
        return self


class ProjectRecord(Record):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    root: Path
    model_config_snapshot_id: str | None = None
    cross_project_read_grants: tuple[str, ...] = ()


class SourceDocument(Record):
    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    input_type: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    source_path: Path
    snapshot_path: Path
    line_count: int = Field(ge=0)
    headings: tuple[str, ...] = ()


class Requirement(Record):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    statement: str = Field(min_length=1)
    source_references: tuple[SourceReference, ...] = Field(min_length=1)
    dependency_ids: tuple[str, ...] = ()
    readiness: RequirementReadiness = RequirementReadiness.EXTRACTED
    approval_id: str | None = None


class StakeholderAnswer(Record):
    author: str = Field(min_length=1)
    text: str = Field(min_length=1)
    recorded_by: str = Field(min_length=1)
    source: str | None = None


class Clarification(Record):
    id: str = Field(min_length=1)
    issue_type: ClarificationIssueType
    question: str = Field(min_length=1)
    source_references: tuple[SourceReference, ...] = Field(min_length=1)
    answers: tuple[StakeholderAnswer, ...] = ()
    status: ClarificationStatus = ClarificationStatus.OPEN


class BusinessScope(Record):
    id: str = Field(min_length=1)
    journey: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = ()
    excluded_requirement_ids: tuple[str, ...] = ()
    dependency_scope_ids: tuple[str, ...] = ()
    status: ScopeStatus = ScopeStatus.PROPOSED
    approval_id: str | None = None


class ArtifactVersion(Record):
    id: str = Field(min_length=1)
    kind: ArtifactKind
    scope_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    content_hash: str = Field(min_length=1)
    requirement_baseline_id: str = Field(min_length=1)
    status: ArtifactStatus = ArtifactStatus.DRAFT
    staleness: ArtifactStaleness = ArtifactStaleness.CURRENT
    approval_id: str | None = None


class ApprovalRecord(Record):
    id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    target_version: int = Field(ge=1)
    scope_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    reviewed_evidence: tuple[str, ...] = ()
    approved_at: AwareDatetime


class WorkflowRecord(Record):
    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    source_snapshot_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    status: WorkflowStatus = WorkflowStatus.NEW
    model_config_snapshot_id: str | None = None


class TaskRecord(Record):
    id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    scope_id: str | None = None
    kind: str = Field(min_length=1)
    input_baseline_id: str | None = None
    dependency_ids: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    idempotency_key: str = Field(min_length=1)


class ImpactReport(Record):
    id: str = Field(min_length=1)
    changed_requirement_id: str = Field(min_length=1)
    changed_requirement_version: int = Field(ge=1)
    affected_artifact_ids: tuple[str, ...] = ()
    affected_scope_ids: tuple[str, ...] = ()
    reasoning: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    manual_disposition: str | None = None
    staleness: ArtifactStaleness = ArtifactStaleness.POTENTIALLY_STALE
