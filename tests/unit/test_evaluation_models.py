from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from quality_platform.evaluation.models import (
    BenchmarkCase,
    ExpectedItem,
    GoldRecord,
    GoldStatus,
    Severity,
    TaskType,
)
from quality_platform.evaluation.registry import load_dataset


def test_approved_gold_requires_named_human_and_timestamp() -> None:
    with pytest.raises(ValidationError):
        GoldRecord(status=GoldStatus.APPROVED, expected_items=())
    gold = GoldRecord(
        status=GoldStatus.APPROVED,
        approved_by="QA Engineer",
        approved_at=datetime.now(UTC),
        expected_items=(),
    )
    assert gold.approved_by == "QA Engineer"


def test_benchmark_case_preserves_gold_separately_from_input_ids() -> None:
    case = BenchmarkCase(
        case_id="conflict-1",
        task_type=TaskType.ANALYST,
        input_snapshot_ids=("BRD-1", "SRS-1"),
        severity=Severity.CRITICAL,
        gold=GoldRecord(
            status=GoldStatus.PENDING_REVIEW,
            expected_items=(ExpectedItem(id="conflict-3-vs-5", kind="CONFLICT"),),
        ),
    )
    assert case.gold.status == GoldStatus.PENDING_REVIEW
    assert case.input_snapshot_ids == ("BRD-1", "SRS-1")


def test_load_dataset_joins_synthetic_cases_and_unapproved_gold() -> None:
    root = Path(__file__).parents[2] / "benchmarks"
    dataset = load_dataset("v0.1", root)
    assert {case.case_id for case in dataset.cases} == {
        "conflict",
        "missing_rule",
        "shared_scope",
        "approval_bypass",
        "targeted_revision",
    }
    assert all(case.gold.status == GoldStatus.PENDING_REVIEW for case in dataset.cases)
    assert dataset.rubric_version == "v0.1"


def test_dataset_version_cannot_escape_benchmark_root() -> None:
    root = Path(__file__).parents[2] / "benchmarks"
    with pytest.raises(ValueError, match="version"):
        load_dataset("../../secrets", root)
