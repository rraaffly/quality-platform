from collections.abc import Callable
from uuid import uuid4

from quality_platform.evaluation.metrics import evaluate_case
from quality_platform.evaluation.models import (
    BenchmarkCase,
    BenchmarkReport,
    CandidateSnapshot,
    Dataset,
    EvaluationOutput,
    GoldStatus,
    ReleaseDecision,
)


def run_offline_suite(
    dataset: Dataset,
    handler: Callable[[BenchmarkCase], EvaluationOutput],
    *,
    candidate: CandidateSnapshot,
) -> BenchmarkReport:
    approved = tuple(case for case in dataset.cases if case.gold.status == GoldStatus.APPROVED)
    scores = tuple(evaluate_case(case, handler(case)) for case in approved)
    if any(score.critical_violation for score in scores):
        decision = ReleaseDecision.BLOCKED
    elif not scores or any(
        score.precision < 1 or score.recall < 1 or score.evidence_accuracy < 1 or score.findings
        for score in scores
    ):
        decision = ReleaseDecision.REVIEW_REQUIRED
    else:
        decision = ReleaseDecision.PASS
    return BenchmarkReport(
        run_id=str(uuid4()),
        dataset_version=dataset.version,
        rubric_id=dataset.rubric_id,
        rubric_version=dataset.rubric_version,
        candidate=candidate,
        case_scores=scores,
        eligible_count=len(scores),
        excluded_count=len(dataset.cases) - len(scores),
        overall_precision=sum(score.precision for score in scores) / len(scores) if scores else 0,
        overall_recall=sum(score.recall for score in scores) / len(scores) if scores else 0,
        decision=decision,
    )
