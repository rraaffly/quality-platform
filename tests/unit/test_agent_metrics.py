from datetime import UTC, datetime

import pytest

from quality_platform.domain.models import Requirement, SourceReference
from quality_platform.evaluation.metrics import (
    evaluate_case,
    score_extraction,
    score_traceability,
)
from quality_platform.evaluation.models import (
    Artifact,
    BenchmarkCase,
    EvaluationOutput,
    ExpectedItem,
    GoldCase,
    GoldRecord,
    GoldRequirement,
    GoldStatus,
    ObservedItem,
    Severity,
    TaskType,
)


def source(document_id: str, line: int = 1) -> SourceReference:
    return SourceReference(document_id=document_id, start_line=line, end_line=line)


def observed_requirement(requirement_id: str, reference: SourceReference) -> Requirement:
    return Requirement(
        id=requirement_id,
        version=1,
        statement=requirement_id,
        source_references=(reference,),
    )


def test_extraction_precision_and_recall_count_matches_misses_and_extras() -> None:
    brd = source("BRD-1", 3)
    gold = [
        GoldRequirement(id="REQ-1", source_references=(brd,)),
        GoldRequirement(id="REQ-2", source_references=(source("BRD-1", 4),)),
    ]

    score = score_extraction(
        gold,
        [observed_requirement("REQ-1", brd), observed_requirement("REQ-EXTRA", brd)],
    )

    assert score.matched_requirement_ids == ("REQ-1",)
    assert score.missed_requirement_ids == ("REQ-2",)
    assert score.unexpected_requirement_ids == ("REQ-EXTRA",)
    assert score.precision == pytest.approx(0.5)
    assert score.recall == pytest.approx(0.5)


def test_semantic_alternative_is_matched_but_flagged_for_review() -> None:
    reference = source("SRS-1", 8)
    score = score_extraction(
        [
            GoldRequirement(
                id="REQ-LOCKOUT",
                source_references=(reference,),
                semantic_alternatives=("ACCOUNT-LOCK",),
            )
        ],
        [observed_requirement("ACCOUNT-LOCK", reference)],
    )

    assert score.precision == 1
    assert score.recall == 1
    assert score.review_required_requirement_ids == ("REQ-LOCKOUT",)


def test_traceability_scores_requirement_coverage_and_evidence() -> None:
    first = source("BRD-1", 3)
    second = source("SRS-1", 7)
    score = score_traceability(
        GoldCase(
            case_id="traceability",
            requirements=(
                GoldRequirement(id="REQ-1", source_references=(first,)),
                GoldRequirement(id="REQ-2", source_references=(second,)),
            ),
        ),
        Artifact(
            id="PLAN-1",
            requirement_ids=("REQ-1", "REQ-2"),
            source_references=(first, source("SRS-OTHER", 99)),
        ),
    )

    assert score.requirement_coverage == 1
    assert score.evidence_accuracy == pytest.approx(0.5)
    assert score.unsupported_source_count == 1
    assert score.review_required is True


def test_conflict_is_scored_as_one_case_with_all_required_evidence() -> None:
    brd = source("BRD-1", 3)
    srs = source("SRS-1", 3)
    case = BenchmarkCase(
        case_id="login-attempt-conflict",
        task_type=TaskType.ANALYST,
        input_snapshot_ids=("BRD-1", "SRS-1"),
        severity=Severity.CRITICAL,
        gold=GoldRecord(
            status=GoldStatus.APPROVED,
            approved_by="QA reviewer",
            approved_at=datetime.now(UTC),
            expected_items=(
                ExpectedItem(
                    id="three-vs-five",
                    kind="CONFLICT",
                    source_references=(brd, srs),
                ),
            ),
        ),
    )

    score = evaluate_case(
        case,
        EvaluationOutput(
            observed_items=(
                ObservedItem(
                    id="three-vs-five",
                    kind="CONFLICT",
                    source_references=(brd, srs),
                ),
            )
        ),
    )

    assert score.case_id == "login-attempt-conflict"
    assert score.recall == 1
    assert score.evidence_accuracy == 1
