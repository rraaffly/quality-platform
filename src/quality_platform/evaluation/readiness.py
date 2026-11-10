from pydantic import AwareDatetime, Field

from quality_platform.evaluation.models import BenchmarkReport, EvalRecord, ReleaseDecision


class HumanReview(EvalRecord):
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    approved: bool
    reviewed_case_ids: tuple[str, ...]
    active_qa_minutes: float = Field(ge=0)
    manual_baseline_minutes: float = Field(ge=0)
    thresholds_calibrated: bool
    approved_real_case_count: int = Field(ge=0)
    minimum_real_case_count: int = Field(ge=0)


def assemble_release_readiness(report: BenchmarkReport, review: HumanReview) -> ReleaseDecision:
    if report.decision == ReleaseDecision.BLOCKED or any(
        item.critical_violation for item in report.case_scores
    ):
        return ReleaseDecision.BLOCKED
    if not review.approved:
        return ReleaseDecision.REVIEW_REQUIRED
    if not set(report.failure_case_ids).issubset(review.reviewed_case_ids):
        return ReleaseDecision.REVIEW_REQUIRED
    if (
        not review.thresholds_calibrated
        or review.approved_real_case_count < review.minimum_real_case_count
        or report.readiness_labels
    ):
        return ReleaseDecision.REVIEW_REQUIRED
    if report.decision == ReleaseDecision.REVIEW_REQUIRED:
        return ReleaseDecision.REVIEW_REQUIRED
    return report.decision
