import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quality_platform.domain.models import SourceReference
from quality_platform.evaluation.metrics import evaluate_case
from quality_platform.evaluation.models import (
    BenchmarkCase,
    CandidateSnapshot,
    Dataset,
    EvaluationOutput,
    ExpectedItem,
    GoldRecord,
    GoldStatus,
    ObservedItem,
    Severity,
    TaskType,
)
from quality_platform.evaluation.registry import load_dataset
from quality_platform.evaluation.reporting import write_report
from quality_platform.evaluation.runner import run_offline_suite


def approved_case(case_id: str, *, severity: Severity = Severity.MEDIUM) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        task_type=TaskType.ANALYST,
        input_snapshot_ids=("BRD-1",),
        severity=severity,
        gold=GoldRecord(
            status=GoldStatus.APPROVED,
            approved_by="QA Engineer",
            approved_at=datetime.now(UTC),
            expected_items=(
                ExpectedItem(
                    id="REQ-1",
                    kind="REQUIREMENT",
                    semantic_definition="The user can sign in.",
                    source_references=(
                        SourceReference(document_id="BRD-1", start_line=2, end_line=2),
                    ),
                ),
            ),
            forbidden_actions=("approve_without_review",),
        ),
    )


def candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id="local-test",
        commit_sha="abc123",
        config_hash="config-1",
        model_snapshot_id="fake-models",
        prompt_snapshot_id="none",
    )


def test_only_approved_gold_contributes_to_release_metrics() -> None:
    approved = approved_case("approved")
    draft = approved_case("draft").model_copy(
        update={"gold": GoldRecord(status=GoldStatus.PENDING_REVIEW, expected_items=())}
    )
    seen: list[str] = []

    def handler(case: BenchmarkCase) -> EvaluationOutput:
        seen.append(case.case_id)
        return EvaluationOutput(
            observed_items=(
                ObservedItem(
                    id="REQ-1",
                    kind="REQUIREMENT",
                    content="The user can sign in.",
                    source_references=(
                        SourceReference(document_id="BRD-1", start_line=2, end_line=2),
                    ),
                ),
            )
        )

    report = run_offline_suite(
        Dataset(version="v0.1", rubric_version="v0.1", cases=(approved, draft)),
        handler,
        candidate=candidate(),
    )
    assert seen == ["approved"]
    assert report.eligible_count == 1
    assert report.excluded_count == 1
    assert report.overall_recall == 1


def test_unsupported_source_reference_is_penalized() -> None:
    result = evaluate_case(
        approved_case("wrong-source"),
        EvaluationOutput(
            observed_items=(
                ObservedItem(
                    id="REQ-1",
                    kind="REQUIREMENT",
                    content="The user can sign in.",
                    source_references=(
                        SourceReference(document_id="SRS-9", start_line=99, end_line=99),
                    ),
                ),
            )
        ),
    )
    assert result.unsupported_source_count == 1
    assert result.evidence_accuracy == 0


@pytest.mark.parametrize("severity", list(Severity))
def test_critical_policy_bypass_blocks_even_with_good_average(severity: Severity) -> None:
    good = approved_case("good")
    critical = approved_case("critical", severity=severity)

    def handler(case: BenchmarkCase) -> EvaluationOutput:
        return EvaluationOutput(
            observed_items=(
                ObservedItem(
                    id="REQ-1",
                    kind="REQUIREMENT",
                    content="The user can sign in.",
                    source_references=(
                        SourceReference(document_id="BRD-1", start_line=2, end_line=2),
                    ),
                ),
            ),
            actions=("approve_without_review",) if case.case_id == "critical" else (),
        )

    report = run_offline_suite(
        Dataset(version="v0.1", rubric_version="v0.1", cases=(good, critical)),
        handler,
        candidate=candidate(),
    )
    assert report.overall_recall == 1
    assert report.decision == "BLOCKED"


def test_report_records_versions_and_candidate_without_private_input(tmp_path: Path) -> None:
    private_marker = "PRIVATE PRD CONTENT DO NOT LOG"
    report = run_offline_suite(
        Dataset(version="v0.1", rubric_version="v0.1", cases=(approved_case("report"),)),
        lambda _case: EvaluationOutput(
            observed_items=(ObservedItem(id=private_marker, kind="UNKNOWN"),),
            raw_text=private_marker,
        ),
        candidate=candidate(),
    )
    json_path, markdown_path = write_report(report, tmp_path)
    json_text = json_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert report.dataset_version == "v0.1"
    assert report.rubric_version == "v0.1"
    assert report.candidate.commit_sha == "abc123"
    assert private_marker not in json_text + markdown_text
    assert json.loads(json_text)["candidate"]["commit_sha"] == "abc123"
    assert "report" in markdown_text


def test_missing_expected_item_requires_review() -> None:
    report = run_offline_suite(
        Dataset(version="v0.1", rubric_version="v0.1", cases=(approved_case("missed"),)),
        lambda _case: EvaluationOutput(),
        candidate=candidate(),
    )
    assert report.overall_recall == 0
    assert report.decision == "REVIEW_REQUIRED"


def test_bundled_pending_gold_remains_excluded_from_release_metrics() -> None:
    dataset = load_dataset("v0.1", Path(__file__).parents[2] / "benchmarks")
    seen: list[str] = []

    def handler(case: BenchmarkCase) -> EvaluationOutput:
        seen.append(case.case_id)
        return EvaluationOutput()

    report = run_offline_suite(dataset, handler, candidate=candidate())
    assert seen == ["conflict", "missing_rule"]
    assert report.eligible_count == 2
    assert report.excluded_count == 3
    assert report.decision == "REVIEW_REQUIRED"
