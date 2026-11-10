from datetime import UTC, datetime
from pathlib import Path

import pytest

from quality_platform.evaluation.models import (
    BenchmarkReport,
    CandidateSnapshot,
    CaseScore,
    ReleaseDecision,
)
from quality_platform.evaluation.regression import (
    BaselineApproval,
    BaselineApprovalStatus,
    QualityThresholds,
    ThresholdStatus,
    decide_regression,
    load_quality_thresholds,
    promote_baseline,
)


def report(
    run_id: str,
    *,
    precision: float,
    recall: float,
    evidence: float,
    critical: bool = False,
    dataset: str = "v0.1",
    rubric: str = "v0.1",
    decision: ReleaseDecision = ReleaseDecision.PASS,
) -> BenchmarkReport:
    return BenchmarkReport(
        run_id=run_id,
        dataset_version=dataset,
        rubric_id="qa-mvp",
        rubric_version=rubric,
        candidate=CandidateSnapshot(
            candidate_id=run_id,
            commit_sha=f"commit-{run_id}",
            config_hash=f"config-{run_id}",
            model_snapshot_id=f"model-{run_id}",
            prompt_snapshot_id=f"prompt-{run_id}",
        ),
        case_scores=(
            CaseScore(
                case_id="case-1",
                eligible=True,
                precision=precision,
                recall=recall,
                evidence_accuracy=evidence,
                unsupported_source_count=0,
                critical_violation=critical,
            ),
        ),
        eligible_count=1,
        excluded_count=0,
        overall_precision=precision,
        overall_recall=recall,
        decision=decision,
    )


def calibrated_thresholds(**updates: float) -> QualityThresholds:
    values: dict[str, object] = {
        "version": "v1",
        "status": ThresholdStatus.CALIBRATED,
        "minimum_precision": 0.9,
        "minimum_recall": 0.9,
        "minimum_evidence_accuracy": 0.9,
        "maximum_precision_regression": 0.05,
        "maximum_recall_regression": 0.05,
        "maximum_evidence_regression": 0.05,
    }
    values.update(updates)
    return QualityThresholds.model_validate(values)


def test_critical_violation_blocks_even_when_candidate_average_is_better() -> None:
    baseline = report("baseline", precision=0.91, recall=0.91, evidence=0.91)
    candidate = report("candidate", precision=1, recall=1, evidence=1, critical=True)

    result = decide_regression(candidate, baseline, calibrated_thresholds())

    assert result.decision == ReleaseDecision.BLOCKED
    assert result.critical_hard_gate_failed is True
    assert "critical" in " ".join(result.reasons).lower()


def test_calibrated_threshold_failure_blocks_release() -> None:
    baseline = report("baseline", precision=0.95, recall=0.95, evidence=0.95)
    candidate = report("candidate", precision=0.95, recall=0.89, evidence=0.95)

    result = decide_regression(candidate, baseline, calibrated_thresholds())

    assert result.decision == ReleaseDecision.BLOCKED
    assert result.threshold_failures == ("recall",)


def test_dual_baseline_blocks_material_regression_even_above_minimum() -> None:
    baseline = report("baseline", precision=0.99, recall=0.99, evidence=0.99)
    candidate = report("candidate", precision=0.92, recall=0.99, evidence=0.99)

    result = decide_regression(candidate, baseline, calibrated_thresholds())

    assert result.decision == ReleaseDecision.BLOCKED
    assert result.baseline_regressions == ("precision",)


def test_approved_exception_is_permitted_only_for_noncritical_failure() -> None:
    baseline = report("baseline", precision=0.95, recall=0.95, evidence=0.95)
    exception = report(
        "exception",
        precision=0.95,
        recall=0.89,
        evidence=0.95,
        decision=ReleaseDecision.APPROVED_EXCEPTION,
    )
    critical = exception.model_copy(
        update={
            "case_scores": (
                exception.case_scores[0].model_copy(update={"critical_violation": True}),
            )
        }
    )

    permitted = decide_regression(exception, baseline, calibrated_thresholds())
    denied = decide_regression(critical, baseline, calibrated_thresholds())

    assert permitted.decision == ReleaseDecision.APPROVED_EXCEPTION
    assert denied.decision == ReleaseDecision.BLOCKED


@pytest.mark.parametrize(
    ("candidate", "reason_fragment"),
    [
        (
            report(
                "dataset-change",
                precision=1,
                recall=1,
                evidence=1,
                dataset="v0.2",
            ),
            "dataset",
        ),
        (
            report(
                "rubric-change",
                precision=1,
                recall=1,
                evidence=1,
                rubric="v0.2",
            ),
            "rubric",
        ),
    ],
)
def test_changed_dataset_or_rubric_demands_comparator_reevaluation(
    candidate: BenchmarkReport, reason_fragment: str
) -> None:
    baseline = report("baseline", precision=1, recall=1, evidence=1)

    result = decide_regression(candidate, baseline, calibrated_thresholds())

    assert result.decision == ReleaseDecision.REVIEW_REQUIRED
    assert result.requires_reevaluation is True
    assert reason_fragment in " ".join(result.reasons).lower()


def test_version_change_does_not_downgrade_a_critical_block() -> None:
    baseline = report("baseline", precision=1, recall=1, evidence=1)
    candidate = report(
        "candidate",
        precision=1,
        recall=1,
        evidence=1,
        critical=True,
        dataset="v0.2",
    )

    result = decide_regression(candidate, baseline, calibrated_thresholds())

    assert result.decision == ReleaseDecision.BLOCKED
    assert result.critical_hard_gate_failed is True
    assert result.requires_reevaluation is True


def test_baseline_changes_only_for_matching_human_approval() -> None:
    current = report("baseline", precision=0.95, recall=0.95, evidence=0.95)
    candidate = report("candidate", precision=0.96, recall=0.96, evidence=0.96)
    rejected = BaselineApproval(
        candidate_run_id="candidate",
        status=BaselineApprovalStatus.REJECTED,
        reviewer="QA lead",
        reviewed_at=datetime.now(UTC),
        reviewed_evidence=("regression-decision:PASS",),
    )
    approved = rejected.model_copy(update={"status": BaselineApprovalStatus.APPROVED})

    assert promote_baseline(current, candidate, rejected) is current
    assert promote_baseline(current, candidate, approved) is candidate
    with pytest.raises(ValueError, match="candidate run"):
        promote_baseline(
            current,
            candidate,
            approved.model_copy(update={"candidate_run_id": "different-run"}),
        )


def test_critical_approved_exception_cannot_be_promoted() -> None:
    current = report("baseline", precision=1, recall=1, evidence=1)
    candidate = report(
        "candidate",
        precision=1,
        recall=1,
        evidence=1,
        critical=True,
        decision=ReleaseDecision.APPROVED_EXCEPTION,
    )
    approval = BaselineApproval(
        candidate_run_id="candidate",
        status=BaselineApprovalStatus.APPROVED,
        reviewer="QA lead",
        reviewed_at=datetime.now(UTC),
        reviewed_evidence=("exception:approved",),
    )

    with pytest.raises(ValueError, match="critical"):
        promote_baseline(current, candidate, approval)


def test_illustrative_targets_prompt_review_but_do_not_claim_calibrated_block() -> None:
    path = Path(__file__).parents[2] / "benchmarks" / "rubrics" / "quality-gates.yaml"
    thresholds = load_quality_thresholds(path)
    baseline = report("baseline", precision=0.99, recall=0.99, evidence=0.99)
    candidate = report("candidate", precision=0.5, recall=0.5, evidence=0.5)

    result = decide_regression(candidate, baseline, thresholds)

    assert thresholds.status == ThresholdStatus.ILLUSTRATIVE
    assert result.decision == ReleaseDecision.REVIEW_REQUIRED
    assert result.threshold_failures == ("precision", "recall", "evidence_accuracy")
