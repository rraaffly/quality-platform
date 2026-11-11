from pydantic import AwareDatetime, Field

from quality_platform.evaluation.models import EvalRecord

_MINIMUM_CALIBRATION_SAMPLE = 30
_RATING_SPAN_SQUARED = 16


class Review(EvalRecord):
    case_id: str = Field(min_length=1)
    rating: int = Field(ge=1, le=5)
    critical: bool
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    blind: bool
    model_snapshot_id: str | None = None
    uncertainty: float = Field(ge=0, le=1)


class CalibrationReport(EvalRecord):
    sample_size: int = Field(ge=0)
    exact_agreement: float | None
    weighted_kappa: float | None
    mean_absolute_error: float | None
    critical_disagreement_rate: float | None
    mean_human_uncertainty: float | None
    small_sample: bool
    evaluator_model_snapshot_id: str | None
    human_reviewers: tuple[str, ...]


def calibrate_evaluator(ai_reviews: list[Review], human_reviews: list[Review]) -> CalibrationReport:
    ai_by_case = _reviews_by_case(ai_reviews, "AI")
    human_by_case = _reviews_by_case(human_reviews, "human")
    if set(ai_by_case) != set(human_by_case):
        raise ValueError("AI and human reviews must contain the same case IDs")
    if any(not item.blind for item in (*ai_reviews, *human_reviews)):
        raise ValueError("calibration reviews must be captured blind and independently")

    if any(item.model_snapshot_id is None for item in ai_reviews):
        raise ValueError("AI reviews require an evaluator model snapshot")
    snapshots = {item.model_snapshot_id for item in ai_reviews if item.model_snapshot_id}
    if len(snapshots) > 1:
        raise ValueError("AI reviews must use one static evaluator model snapshot")
    model_snapshot_id = next(iter(snapshots), None)
    case_ids = tuple(sorted(ai_by_case))
    sample_size = len(case_ids)
    reviewers = tuple(sorted({item.reviewer for item in human_reviews}))
    if not sample_size:
        return CalibrationReport(
            sample_size=0,
            exact_agreement=None,
            weighted_kappa=None,
            mean_absolute_error=None,
            critical_disagreement_rate=None,
            mean_human_uncertainty=None,
            small_sample=True,
            evaluator_model_snapshot_id=model_snapshot_id,
            human_reviewers=reviewers,
        )

    pairs = tuple((ai_by_case[case_id], human_by_case[case_id]) for case_id in case_ids)
    exact_agreement = sum(ai.rating == human.rating for ai, human in pairs) / sample_size
    mean_absolute_error = sum(abs(ai.rating - human.rating) for ai, human in pairs) / sample_size
    critical_disagreement_rate = (
        sum(ai.critical != human.critical for ai, human in pairs) / sample_size
    )
    mean_human_uncertainty = sum(human.uncertainty for _, human in pairs) / sample_size
    return CalibrationReport(
        sample_size=sample_size,
        exact_agreement=exact_agreement,
        weighted_kappa=_quadratic_weighted_kappa(pairs),
        mean_absolute_error=mean_absolute_error,
        critical_disagreement_rate=critical_disagreement_rate,
        mean_human_uncertainty=mean_human_uncertainty,
        small_sample=sample_size < _MINIMUM_CALIBRATION_SAMPLE,
        evaluator_model_snapshot_id=model_snapshot_id,
        human_reviewers=reviewers,
    )


def _reviews_by_case(reviews: list[Review], label: str) -> dict[str, Review]:
    by_case = {item.case_id: item for item in reviews}
    if len(by_case) != len(reviews):
        raise ValueError(f"duplicate {label} review case ID")
    return by_case


def _quadratic_weighted_kappa(pairs: tuple[tuple[Review, Review], ...]) -> float:
    sample_size = len(pairs)
    observed_disagreement = (
        sum((ai.rating - human.rating) ** 2 / _RATING_SPAN_SQUARED for ai, human in pairs)
        / sample_size
    )
    ai_frequencies = {
        rating: sum(ai.rating == rating for ai, _ in pairs) / sample_size for rating in range(1, 6)
    }
    human_frequencies = {
        rating: sum(human.rating == rating for _, human in pairs) / sample_size
        for rating in range(1, 6)
    }
    expected_disagreement = sum(
        ai_frequencies[ai_rating]
        * human_frequencies[human_rating]
        * (ai_rating - human_rating) ** 2
        / _RATING_SPAN_SQUARED
        for ai_rating in range(1, 6)
        for human_rating in range(1, 6)
    )
    if expected_disagreement == 0:
        return 1.0 if observed_disagreement == 0 else 0.0
    return 1 - observed_disagreement / expected_disagreement
