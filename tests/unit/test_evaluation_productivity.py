import pytest
from pydantic import ValidationError

from quality_platform.evaluation.models import ProductivityEvidence


def evidence() -> ProductivityEvidence:
    return ProductivityEvidence(
        equivalent_task_id="login-case-design",
        acceptance_criteria_id="login-acceptance-v1",
        manual_task_id="manual-execution-1",
        assisted_task_id="assisted-execution-1",
        accepted_artifact_ids=("TCSET-v1",),
        accepted_by="test-only QA",
        evidence_references=("human-acceptance-record:test",),
        active_qa_minutes=12,
        manual_baseline_minutes=30,
        human_review_minutes=4,
        rework_minutes=3,
    )


def test_productivity_requires_acceptance_equivalence_and_rework_identity() -> None:
    valid = evidence()
    for missing in (
        "equivalent_task_id",
        "acceptance_criteria_id",
        "accepted_artifact_ids",
        "rework_minutes",
    ):
        payload = valid.model_dump()
        del payload[missing]
        with pytest.raises(ValidationError):
            ProductivityEvidence.model_validate(payload)
    with pytest.raises(ValidationError, match="include human review and rework"):
        ProductivityEvidence.model_validate({**valid.model_dump(), "active_qa_minutes": 1})
