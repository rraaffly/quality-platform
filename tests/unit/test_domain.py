from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from quality_platform.domain.models import (
    ApprovalRecord,
    ArtifactKind,
    ArtifactStaleness,
    ArtifactStatus,
    ArtifactVersion,
    Clarification,
    ClarificationIssueType,
    ClarificationStatus,
    Requirement,
    RequirementReadiness,
    SourceReference,
    StakeholderAnswer,
)
from quality_platform.domain.schemas import TestCase, TestCaseSet, TestStep


def test_requirement_keeps_source_and_approval_separate() -> None:
    requirement = Requirement(
        id="REQ-1",
        version=1,
        statement="Transfer limit is 5 million",
        source_references=(SourceReference(document_id="BRD-1", start_line=12, end_line=14),),
        readiness=RequirementReadiness.VALIDATED,
    )
    assert requirement.approval_id is None
    assert requirement.source_references[0].start_line == 12


def test_approved_artifact_version_cannot_be_mutated() -> None:
    artifact = ArtifactVersion(
        id="PLAN-1",
        kind=ArtifactKind.TEST_PLAN,
        scope_id="SCOPE-1",
        version=1,
        content_hash="sha256:abc",
        requirement_baseline_id="BASE-1",
        status=ArtifactStatus.APPROVED,
    )
    with pytest.raises(ValidationError):
        artifact.content_hash = "sha256:changed"


def test_staleness_is_independent_of_approval_status() -> None:
    artifact = ArtifactVersion(
        id="PLAN-1",
        kind=ArtifactKind.TEST_PLAN,
        scope_id="SCOPE-1",
        version=1,
        content_hash="sha256:abc",
        requirement_baseline_id="BASE-1",
        status=ArtifactStatus.APPROVED,
        staleness=ArtifactStaleness.POTENTIALLY_STALE,
    )
    assert artifact.status == "APPROVED"
    assert artifact.staleness == "POTENTIALLY_STALE"


def test_duplicate_case_id_fails_canonical_validation() -> None:
    case = TestCase(
        id="TC-012",
        title="Successful transfer",
        requirement_ids=("REQ-1",),
        steps=(TestStep(action="Submit", expected_result="Success"),),
    )
    with pytest.raises(ValidationError, match="duplicate"):
        TestCaseSet(cases=(case, case))


def test_clarification_and_approval_record_keep_history() -> None:
    answer = StakeholderAnswer(author="Product Owner", text="Five attempts", recorded_by="QA")
    clarification = Clarification(
        id="CL-1",
        issue_type=ClarificationIssueType.CONTRADICTION,
        question="Three or five attempts?",
        source_references=(SourceReference(document_id="BRD-1", start_line=2, end_line=2),),
        answers=(answer,),
        status=ClarificationStatus.ANSWERED,
    )
    approval = ApprovalRecord(
        id="APR-1",
        actor="QA",
        target_id="PLAN-1",
        target_version=1,
        scope_id="SCOPE-1",
        baseline_id="BASE-1",
        reviewed_evidence=("diff-1",),
        approved_at=datetime.now(UTC),
    )
    assert clarification.answers[0].author == "Product Owner"
    assert approval.target_version == 1
