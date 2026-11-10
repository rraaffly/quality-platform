from datetime import UTC, datetime

import pytest

from quality_platform.evaluation.calibration import Review, calibrate_evaluator


def review(
    case_id: str,
    rating: int,
    *,
    reviewer: str,
    critical: bool = False,
    blind: bool = True,
    model_snapshot_id: str | None = None,
    uncertainty: float = 0,
) -> Review:
    return Review(
        case_id=case_id,
        rating=rating,
        critical=critical,
        reviewer=reviewer,
        reviewed_at=datetime.now(UTC),
        blind=blind,
        model_snapshot_id=model_snapshot_id,
        uncertainty=uncertainty,
    )


def test_calibration_computes_agreement_weighted_kappa_mae_and_critical_rate() -> None:
    ai = [
        review("C1", 1, reviewer="evaluator", critical=True, model_snapshot_id="model-v1"),
        review("C2", 3, reviewer="evaluator", critical=True, model_snapshot_id="model-v1"),
        review("C3", 3, reviewer="evaluator", model_snapshot_id="model-v1"),
        review("C4", 5, reviewer="evaluator", model_snapshot_id="model-v1"),
    ]
    human = [
        review("C1", 1, reviewer="QA-1", critical=True, uncertainty=0.1),
        review("C2", 2, reviewer="QA-1", uncertainty=0.2),
        review("C3", 3, reviewer="QA-2", uncertainty=0.1),
        review("C4", 4, reviewer="QA-2", critical=True, uncertainty=0.2),
    ]

    result = calibrate_evaluator(ai, human)

    assert result.sample_size == 4
    assert result.exact_agreement == pytest.approx(0.5)
    assert result.weighted_kappa == pytest.approx(6 / 7)
    assert result.mean_absolute_error == pytest.approx(0.5)
    assert result.critical_disagreement_rate == pytest.approx(0.5)
    assert result.mean_human_uncertainty == pytest.approx(0.15)
    assert result.small_sample is True
    assert result.evaluator_model_snapshot_id == "model-v1"
    assert result.human_reviewers == ("QA-1", "QA-2")


def test_empty_calibration_is_explicit_instead_of_dividing_by_zero() -> None:
    result = calibrate_evaluator([], [])

    assert result.sample_size == 0
    assert result.exact_agreement is None
    assert result.weighted_kappa is None
    assert result.mean_absolute_error is None
    assert result.critical_disagreement_rate is None
    assert result.mean_human_uncertainty is None
    assert result.small_sample is True


def test_calibration_rejects_unpaired_or_nonblind_reviews() -> None:
    ai = [review("C1", 3, reviewer="evaluator", model_snapshot_id="model-v1")]
    unmatched_human = [review("C2", 3, reviewer="QA")]
    visible_human = [review("C1", 3, reviewer="QA", blind=False)]

    with pytest.raises(ValueError, match="same case IDs"):
        calibrate_evaluator(ai, unmatched_human)
    with pytest.raises(ValueError, match="blind"):
        calibrate_evaluator(ai, visible_human)


def test_calibration_rejects_mixed_evaluator_model_snapshots() -> None:
    ai = [
        review("C1", 3, reviewer="evaluator", model_snapshot_id="model-v1"),
        review("C2", 3, reviewer="evaluator", model_snapshot_id="model-v2"),
    ]
    human = [review("C1", 3, reviewer="QA"), review("C2", 3, reviewer="QA")]

    with pytest.raises(ValueError, match="model snapshot"):
        calibrate_evaluator(ai, human)


def test_calibration_requires_a_reproducible_evaluator_model_snapshot() -> None:
    ai = [review("C1", 3, reviewer="evaluator")]
    human = [review("C1", 3, reviewer="QA")]

    with pytest.raises(ValueError, match="model snapshot"):
        calibrate_evaluator(ai, human)
