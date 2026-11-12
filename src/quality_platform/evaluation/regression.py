from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import AwareDatetime, Field

from quality_platform.evaluation.models import BenchmarkReport, EvalRecord, ReleaseDecision


class ThresholdStatus(StrEnum):
    ILLUSTRATIVE = "ILLUSTRATIVE"
    CALIBRATED = "CALIBRATED"


class QualityThresholds(EvalRecord):
    version: str = Field(min_length=1)
    status: ThresholdStatus
    minimum_precision: float = Field(ge=0, le=1)
    minimum_recall: float = Field(ge=0, le=1)
    minimum_evidence_accuracy: float = Field(ge=0, le=1)
    maximum_precision_regression: float = Field(ge=0, le=1)
    maximum_recall_regression: float = Field(ge=0, le=1)
    maximum_evidence_regression: float = Field(ge=0, le=1)


class RegressionDecision(EvalRecord):
    decision: ReleaseDecision
    candidate_run_id: str
    baseline_run_id: str
    threshold_version: str
    critical_hard_gate_failed: bool
    threshold_failures: tuple[str, ...]
    baseline_regressions: tuple[str, ...]
    requires_reevaluation: bool
    reasons: tuple[str, ...]


class BaselineApprovalStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BaselineApproval(EvalRecord):
    candidate_run_id: str = Field(min_length=1)
    status: BaselineApprovalStatus
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    reviewed_evidence: tuple[str, ...] = Field(min_length=1)


def decide_regression(
    candidate: BenchmarkReport,
    latest_approved: BenchmarkReport,
    thresholds: QualityThresholds,
) -> RegressionDecision:
    critical_failure = any(score.critical_violation for score in candidate.case_scores)
    candidate_blocked = candidate.decision == ReleaseDecision.BLOCKED
    version_reasons: list[str] = []
    if candidate.dataset_version != latest_approved.dataset_version:
        version_reasons.append("dataset version changed; re-evaluate the approved comparator")
    if (
        candidate.rubric_id != latest_approved.rubric_id
        or candidate.rubric_version != latest_approved.rubric_version
    ):
        version_reasons.append("rubric version changed; re-evaluate the approved comparator")
    if candidate.evaluated_case_ids != latest_approved.evaluated_case_ids:
        version_reasons.append("evaluated case set changed; re-evaluate the approved comparator")
    if candidate.input_fingerprint != latest_approved.input_fingerprint:
        version_reasons.append(
            "benchmark input fingerprint changed; re-evaluate the approved comparator"
        )
    if version_reasons:
        if critical_failure:
            version_reasons.insert(0, "critical hard-gate violation cannot be averaged or excepted")
        return RegressionDecision(
            decision=(
                ReleaseDecision.BLOCKED
                if critical_failure or candidate_blocked
                else ReleaseDecision.REVIEW_REQUIRED
            ),
            candidate_run_id=candidate.run_id,
            baseline_run_id=latest_approved.run_id,
            threshold_version=thresholds.version,
            critical_hard_gate_failed=critical_failure,
            threshold_failures=(),
            baseline_regressions=(),
            requires_reevaluation=True,
            reasons=tuple(version_reasons),
        )

    candidate_evidence = _evidence_accuracy(candidate)
    baseline_evidence = _evidence_accuracy(latest_approved)
    candidate_metrics = {
        "precision": candidate.overall_precision,
        "recall": candidate.overall_recall,
        "evidence_accuracy": candidate_evidence,
    }
    minimums = {
        "precision": thresholds.minimum_precision,
        "recall": thresholds.minimum_recall,
        "evidence_accuracy": thresholds.minimum_evidence_accuracy,
    }
    baseline_metrics = {
        "precision": latest_approved.overall_precision,
        "recall": latest_approved.overall_recall,
        "evidence_accuracy": baseline_evidence,
    }
    tolerances = {
        "precision": thresholds.maximum_precision_regression,
        "recall": thresholds.maximum_recall_regression,
        "evidence_accuracy": thresholds.maximum_evidence_regression,
    }
    threshold_failures = tuple(
        name for name, value in candidate_metrics.items() if value < minimums[name]
    )
    baseline_regressions = tuple(
        name
        for name, value in candidate_metrics.items()
        if value < baseline_metrics[name] - tolerances[name]
    )
    reasons: list[str] = []
    if critical_failure:
        reasons.append("critical hard-gate violation cannot be averaged or excepted")
    if threshold_failures:
        reasons.append("minimum quality threshold failed: " + ", ".join(threshold_failures))
    if baseline_regressions:
        reasons.append("latest approved baseline regressed: " + ", ".join(baseline_regressions))

    if critical_failure or candidate_blocked:
        decision = ReleaseDecision.BLOCKED
    elif candidate.decision == ReleaseDecision.APPROVED_EXCEPTION:
        decision = ReleaseDecision.APPROVED_EXCEPTION
        reasons.append("noncritical exception was explicitly approved")
    elif threshold_failures or baseline_regressions:
        decision = (
            ReleaseDecision.BLOCKED
            if thresholds.status == ThresholdStatus.CALIBRATED
            else ReleaseDecision.REVIEW_REQUIRED
        )
        if thresholds.status == ThresholdStatus.ILLUSTRATIVE:
            reasons.append(
                "illustrative targets require review but are not calibrated release gates"
            )
    elif candidate.decision == ReleaseDecision.REVIEW_REQUIRED:
        decision = ReleaseDecision.REVIEW_REQUIRED
        reasons.append("candidate evaluation requires review")
    else:
        decision = ReleaseDecision.PASS

    return RegressionDecision(
        decision=decision,
        candidate_run_id=candidate.run_id,
        baseline_run_id=latest_approved.run_id,
        threshold_version=thresholds.version,
        critical_hard_gate_failed=critical_failure,
        threshold_failures=threshold_failures,
        baseline_regressions=baseline_regressions,
        requires_reevaluation=False,
        reasons=tuple(reasons),
    )


def promote_baseline(
    current: BenchmarkReport,
    candidate: BenchmarkReport,
    approval: BaselineApproval,
    *,
    regression: RegressionDecision,
) -> BenchmarkReport:
    if approval.candidate_run_id != candidate.run_id:
        raise ValueError("approval does not match the candidate run")
    if approval.status != BaselineApprovalStatus.APPROVED:
        return current
    if any(score.critical_violation for score in candidate.case_scores):
        raise ValueError("critical violation cannot be promoted as an exception")
    if (
        regression.candidate_run_id != candidate.run_id
        or regression.baseline_run_id != current.run_id
    ):
        raise ValueError("regression decision does not match candidate and baseline runs")
    if regression.requires_reevaluation or regression.decision not in {
        ReleaseDecision.PASS,
        ReleaseDecision.APPROVED_EXCEPTION,
    }:
        raise ValueError("regression decision does not permit baseline promotion")
    evidence = f"regression-decision:{regression.decision.value}"
    if evidence not in approval.reviewed_evidence:
        raise ValueError("approval evidence does not include the regression decision")
    if candidate.decision not in {
        ReleaseDecision.PASS,
        ReleaseDecision.APPROVED_EXCEPTION,
    }:
        raise ValueError("only a passing or approved-exception release can become baseline")
    if (
        candidate.dataset_version != current.dataset_version
        or candidate.rubric_id != current.rubric_id
        or candidate.rubric_version != current.rubric_version
    ):
        raise ValueError(
            "candidate and baseline versions differ; comparator re-evaluation required"
        )
    return candidate


def load_quality_thresholds(path: Path) -> QualityThresholds:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("quality thresholds must be a YAML mapping")
    return QualityThresholds.model_validate(loaded)


def _evidence_accuracy(report: BenchmarkReport) -> float:
    eligible = tuple(score for score in report.case_scores if score.eligible)
    if not eligible:
        return 0.0
    return sum(score.evidence_accuracy for score in eligible) / len(eligible)
