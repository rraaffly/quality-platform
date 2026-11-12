"""File-backed explicit release controls; no command silently approves or promotes."""

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, Field

from quality_platform.evaluation.calibration import CalibrationReport, Review, calibrate_evaluator
from quality_platform.evaluation.models import (
    BenchmarkReport,
    EvalRecord,
    EvaluatorAssessment,
    PlannerReview,
    ReleaseDecision,
)
from quality_platform.evaluation.regression import (
    BaselineApproval,
    BaselineApprovalStatus,
    QualityThresholds,
    RegressionDecision,
    ThresholdStatus,
    decide_regression,
)
from quality_platform.evaluation.reporting import validate_review_packet
from quality_platform.evaluation.workflow_evidence import validate_workflow_evidence


def digest(record: EvalRecord) -> str:
    return hashlib.sha256(record.model_dump_json().encode()).hexdigest()


class IndependentReviews(EvalRecord):
    candidate_run_id: str
    input_fingerprint: str = Field(min_length=1)
    output_fingerprint: str = Field(min_length=1)
    captured_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    reviews: tuple[Review, ...] = Field(min_length=1)
    reviewed_repeat_run_ids: tuple[str, ...] = ()


class CalibrationEvidence(EvalRecord):
    candidate_run_id: str
    report_digest: str
    human_reviews_digest: str
    ai_revealed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    calibration: CalibrationReport
    disagreement_case_ids: tuple[str, ...]
    sampling_method: str = "one-independent-case"
    repeat_run_ids: tuple[str, ...] = ()


class ReleaseReview(EvalRecord):
    candidate_run_id: str
    report_digest: str
    baseline_run_id: str | None
    baseline_digest: str | None
    thresholds_digest: str
    human_reviews_digest: str
    calibration_digest: str
    approval: BaselineApproval
    regression: RegressionDecision
    decision: ReleaseDecision
    reasons: tuple[str, ...]


class ApprovedBaseline(EvalRecord):
    report: BenchmarkReport
    release: ReleaseReview


def load_baseline(directory: Path) -> ApprovedBaseline | None:
    path = directory / "latest-approved.json"
    if not path.exists():
        return None
    baseline = ApprovedBaseline.model_validate_json(path.read_text())
    if (
        baseline.release.report_digest != digest(baseline.report)
        or baseline.release.candidate_run_id != baseline.report.run_id
        or baseline.release.approval.status != BaselineApprovalStatus.APPROVED
        or baseline.release.decision
        not in {ReleaseDecision.PASS, ReleaseDecision.APPROVED_EXCEPTION}
    ):
        raise ValueError("latest approved baseline has inconsistent approval evidence")
    return baseline


def validate_independent_reviews(report: BenchmarkReport, human: IndependentReviews) -> None:
    if (
        human.candidate_run_id != report.run_id
        or human.input_fingerprint != report.input_fingerprint
        or human.output_fingerprint != report.output_fingerprint
    ):
        raise ValueError("human review must identify this run and exact inputs/outputs")
    if len({review.case_id for review in human.reviews}) != len(human.reviews):
        raise ValueError("duplicate independent review case ID")
    if {review.case_id for review in human.reviews} != set(report.evaluated_case_ids):
        raise ValueError("independent human review must cover every evaluated case")
    if report.repeat_count > 1 and human.reviewed_repeat_run_ids != tuple(
        item.run_id for item in report.repeat_results
    ):
        raise ValueError("human review must explicitly cover the complete repeat run manifest")
    if any(
        not review.blind
        or review.model_snapshot_id is not None
        or review.reviewer.casefold() == "evaluator"
        or review.reviewed_at > human.captured_at
        for review in human.reviews
    ):
        raise ValueError("human ratings must be independently captured blind, not model ratings")


def create_calibration(report: BenchmarkReport, human: IndependentReviews) -> CalibrationEvidence:
    validate_independent_reviews(report, human)
    grouped: dict[str, list[EvaluatorAssessment]] = defaultdict(list)
    if report.repeat_count > 1:
        if (
            len(report.repeat_results) != report.repeat_count
            or len({item.run_id for item in report.repeat_results}) != report.repeat_count
        ):
            raise ValueError("multirun calibration requires unique, complete repeat provenance")
        flattened = tuple(
            assessment
            for repeat in report.repeat_results
            for assessment in repeat.evaluator_assessments
        )
        if flattened != report.evaluator_assessments:
            raise ValueError("aggregate evaluator evidence differs from per-repeat provenance")
        for repeat in report.repeat_results:
            if len(repeat.evaluator_assessments) != len(report.evaluated_case_ids) or {
                item.case_id for item in repeat.evaluator_assessments
            } != set(report.evaluated_case_ids):
                raise ValueError("every repeat must cover each evaluated case exactly once")
    for item in report.evaluator_assessments:
        grouped[item.case_id].append(item)
    if set(grouped) != set(report.evaluated_case_ids) or any(
        len(items) != report.repeat_count for items in grouped.values()
    ):
        raise ValueError("calibration needs one evaluator assessment per case per repeat")
    if len({item.model_snapshot_id for item in report.evaluator_assessments}) != 1:
        raise ValueError("calibration requires one static evaluator snapshot")
    # Repeats are a cluster, not new independent cases: conservative worst-case evidence
    # is compared to one human rating after reviewing every output in that case cluster.
    ai = [
        Review(
            case_id=case_id,
            rating=min(item.rating for item in items),
            critical=any(item.critical for item in items),
            reviewer="evaluator",
            reviewed_at=max(item.reviewed_at for item in items),
            blind=True,
            model_snapshot_id=items[0].model_snapshot_id,
            uncertainty=max(item.uncertainty for item in items),
        )
        for case_id, items in sorted(grouped.items())
    ]
    calibration = calibrate_evaluator(ai, list(human.reviews))
    human_by_case = {item.case_id: item for item in human.reviews}
    return CalibrationEvidence(
        candidate_run_id=report.run_id,
        report_digest=digest(report),
        human_reviews_digest=digest(human),
        calibration=calibration,
        sampling_method="case-cluster-worst-rating-any-critical"
        if report.repeat_count > 1
        else "one-independent-case",
        repeat_run_ids=tuple(item.run_id for item in report.repeat_results),
        disagreement_case_ids=tuple(
            case_id
            for case_id, items in sorted(grouped.items())
            if any(
                item.rating != human_by_case[case_id].rating
                or item.critical != human_by_case[case_id].critical
                for item in items
            )
        ),
    )


def review_release(
    report: BenchmarkReport,
    baseline: ApprovedBaseline | None,
    thresholds: QualityThresholds,
    human: IndependentReviews,
    calibration: CalibrationEvidence,
    approval: BaselineApproval,
    *,
    bootstrap: bool = False,
) -> ReleaseReview:
    validate_independent_reviews(report, human)
    if baseline is None and not bootstrap:
        raise ValueError("latest approved baseline missing; explicit --bootstrap required")
    if baseline is not None and bootstrap:
        raise ValueError("bootstrap cannot replace an existing approved baseline")
    if approval.candidate_run_id != report.run_id:
        raise ValueError("approval belongs to another candidate run")
    if (
        approval.reviewed_at < human.captured_at
        or approval.reviewed_at < calibration.ai_revealed_at
    ):
        raise ValueError(
            "release approval must follow independent ratings and calibration disclosure"
        )
    reasons: list[str] = []
    # A human may adjudicate an AI paraphrase, but cannot invent absent gold definitions,
    # repair unsupported citations, or waive a critical violation via an ordinary approval.
    adjudicable = {
        "AI semantic match requires independent human adjudication",
        "evaluator flagged semantic uncertainty or review",
        "evaluator flagged critical behavior or uncertainty; human review required",
        *(f"Planner metric {name} requires human review" for name in PlannerReview.model_fields),
    }
    reviewed = set(approval.reviewed_evidence)
    effective = report
    if (
        report.decision == ReleaseDecision.REVIEW_REQUIRED
        and report.case_scores
        and not report.readiness_labels
    ) and all(
        not score.critical_violation
        and min(score.precision, score.recall, score.evidence_accuracy) == 1
        and set(score.findings).issubset(adjudicable)
        and (not score.findings or f"case-adjudication:{score.case_id}" in reviewed)
        for score in report.case_scores
    ):
        effective = report.model_copy(update={"decision": ReleaseDecision.PASS})
    comparator = baseline.report if baseline else effective
    regression = decide_regression(effective, comparator, thresholds)
    decision = regression.decision
    if report.decision == ReleaseDecision.BLOCKED or any(
        score.critical_violation for score in report.case_scores
    ):
        decision = ReleaseDecision.BLOCKED
    try:
        validate_review_packet(report)
    except ValueError as error:
        reasons.append(str(error))
    if report.workflow_suite_evidence is None:
        reasons.append("E2E_WORKFLOW_EVIDENCE_MISSING")
    else:
        try:
            workflow_decision = validate_workflow_evidence(report, report.workflow_suite_evidence)
        except ValueError as error:
            reasons.append(str(error))
        else:
            if workflow_decision == ReleaseDecision.BLOCKED:
                decision = ReleaseDecision.BLOCKED
                reasons.append("critical offline workflow/policy suite failed")
            elif workflow_decision != ReleaseDecision.PASS:
                reasons.append("offline workflow suite did not fully execute and pass")
    if thresholds.status != ThresholdStatus.CALIBRATED:
        reasons.append("quality thresholds are not calibrated")
    if not report.case_scores or not report.evaluated_case_ids or not report.input_fingerprint:
        reasons.append("benchmark evidence is empty or unversioned")
    if report.readiness_labels:
        reasons.extend(report.readiness_labels)
    if approval.status != BaselineApprovalStatus.APPROVED:
        reasons.append("human release approval not granted")
    if any(item.critical or item.uncertainty > 0 or item.rating < 4 for item in human.reviews):
        reasons.append("independent human review found unresolved quality concerns")
    if calibration.ai_revealed_at < human.captured_at:
        reasons.append("AI ratings were disclosed before human ratings were captured")
    if calibration.calibration.small_sample and "small-sample:acknowledged" not in reviewed:
        reasons.append("small evaluator calibration sample requires explicit human acknowledgment")
    if (
        report.decision == ReleaseDecision.APPROVED_EXCEPTION
        and "exception:approved" not in reviewed
    ):
        reasons.append("release exception requires explicit human exception evidence")
    snapshots = {item.model_snapshot_id for item in report.evaluator_assessments}
    if snapshots != {calibration.calibration.evaluator_model_snapshot_id}:
        reasons.append("calibration does not cover the static evaluator snapshot")
    if calibration.candidate_run_id == report.run_id:
        # Recompute imported evidence instead of trusting edited aggregate calibration scores.
        expected = create_calibration(report, human)
        if calibration.calibration != expected.calibration or (
            calibration.report_digest != digest(report)
            or calibration.human_reviews_digest != digest(human)
            or calibration.disagreement_case_ids != expected.disagreement_case_ids
            or calibration.sampling_method != expected.sampling_method
            or calibration.repeat_run_ids != expected.repeat_run_ids
        ):
            raise ValueError("calibration is not bound to this report and independent reviews")
    else:
        reasons.append("calibration must be supplied for this reviewed run")
    unresolved = set(calibration.disagreement_case_ids).difference(
        value.removeprefix("case-adjudication:")
        for value in reviewed
        if value.startswith("case-adjudication:")
    )
    if unresolved:
        reasons.append("human/AI disagreements require explicit case adjudication")
    required_evidence = {
        f"report:{digest(report)}",
        f"human-review:{digest(human)}",
        f"calibration:{digest(calibration)}",
        f"thresholds:{digest(thresholds)}",
        f"regression-decision:{regression.decision.value}",
    }
    if report.workflow_suite_evidence is not None:
        required_evidence.add(f"workflow-suite:{digest(report.workflow_suite_evidence)}")
    if not required_evidence.issubset(reviewed):
        reasons.append("approval does not reference all exact release evidence")
    if reasons and decision != ReleaseDecision.BLOCKED:
        decision = ReleaseDecision.REVIEW_REQUIRED
    return ReleaseReview(
        candidate_run_id=report.run_id,
        report_digest=digest(report),
        baseline_run_id=baseline.report.run_id if baseline else None,
        baseline_digest=digest(baseline.report) if baseline else None,
        thresholds_digest=digest(thresholds),
        human_reviews_digest=digest(human),
        calibration_digest=digest(calibration),
        approval=approval,
        regression=regression,
        decision=decision,
        reasons=tuple(reasons),
    )
