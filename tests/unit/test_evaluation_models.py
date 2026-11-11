import json
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


def test_load_dataset_preserves_reviewed_analyst_gold_and_pending_other_cases() -> None:
    root = Path(__file__).parents[2] / "benchmarks"
    dataset = load_dataset("v0.1", root)
    assert {case.case_id for case in dataset.cases} == {
        "conflict",
        "missing_rule",
        "shared_scope",
        "approval_bypass",
        "targeted_revision",
    }
    by_id = {case.case_id: case for case in dataset.cases}
    for case_id in ("conflict", "missing_rule"):
        gold = by_id[case_id].gold
        assert gold.status == GoldStatus.APPROVED
        assert gold.approved_by
        assert gold.approved_at is not None
        assert len(gold.review_history) == 1
        assert gold.review_history[0].reviewer == gold.approved_by
        assert gold.review_history[0].decision == "APPROVED"
        assert gold.review_history[0].reviewed_at == gold.approved_at
    assert all(
        by_id[case_id].gold.status == GoldStatus.PENDING_REVIEW
        for case_id in ("shared_scope", "approval_bypass", "targeted_revision")
    )
    assert dataset.rubric_version == "v0.1"


def test_dataset_version_cannot_escape_benchmark_root() -> None:
    root = Path(__file__).parents[2] / "benchmarks"
    with pytest.raises(ValueError, match="version"):
        load_dataset("../../secrets", root)


def test_load_dataset_merges_optional_approved_real_world_manifest(tmp_path: Path) -> None:
    (tmp_path / "datasets" / "synthetic").mkdir(parents=True)
    (tmp_path / "datasets" / "real_world").mkdir(parents=True)
    (tmp_path / "ground_truth" / "real_world").mkdir(parents=True)
    (tmp_path / "rubrics").mkdir()
    (tmp_path / "datasets" / "synthetic" / "v0.1.json").write_text(
        json.dumps({"version": "v0.1", "rubric_version": "v0.1", "cases": []}),
        encoding="utf-8",
    )
    (tmp_path / "ground_truth" / "v0.1.json").write_text(
        json.dumps({"version": "v0.1", "cases": []}), encoding="utf-8"
    )
    (tmp_path / "rubrics" / "v0.1.json").write_text(
        json.dumps({"id": "qa-mvp", "version": "v0.1", "description": "test"}),
        encoding="utf-8",
    )
    (tmp_path / "datasets" / "real_world" / "v0.1.json").write_text(
        json.dumps(
            {
                "version": "v0.1",
                "rubric_version": "v0.1",
                "cases": [
                    {
                        "case_id": "customer-login",
                        "task_type": "ANALYST",
                        "input_snapshot_ids": ["customer-brd"],
                        "input_snapshot_hashes": {"customer-brd": "a" * 64},
                        "severity": "HIGH",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ground_truth" / "real_world" / "v0.1.json").write_text(
        json.dumps(
            {
                "version": "v0.1",
                "cases": [
                    {
                        "case_id": "customer-login",
                        "gold": {
                            "status": "APPROVED",
                            "expected_items": [],
                            "approved_by": "QA lead",
                            "approved_at": "2026-11-11T00:00:00Z",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    dataset = load_dataset("v0.1", tmp_path)

    assert [case.case_id for case in dataset.cases] == ["customer-login"]
    assert dataset.cases[0].origin == "real_world"
    assert dataset.content_fingerprint
