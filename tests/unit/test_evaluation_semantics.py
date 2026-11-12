from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.domain.models import SourceReference
from quality_platform.evaluation.judge import JudgeDraft, review_output
from quality_platform.evaluation.metrics import evaluate_case
from quality_platform.evaluation.models import (
    BenchmarkCase,
    EvaluationOutput,
    EvaluatorAssessment,
    ExpectedItem,
    GoldRecord,
    GoldStatus,
    ObservedItem,
    SemanticJudgment,
    Severity,
    TaskType,
)
from quality_platform.evaluation.registry import load_dataset
from quality_platform.llm.registry import AgentRole, ModelRegistry


def case() -> BenchmarkCase:
    return BenchmarkCase(
        case_id="missing_rule",
        task_type=TaskType.ANALYST,
        input_snapshot_ids=("BRD",),
        severity=Severity.HIGH,
        gold=GoldRecord(
            status=GoldStatus.APPROVED,
            approved_by="test-only QA",
            approved_at=datetime.now(UTC),
            expected_items=(
                ExpectedItem(
                    id="lockout-duration",
                    kind="GAP",
                    semantic_definition="What is the lockout duration?",
                    source_references=(
                        SourceReference(document_id="BRD", start_line=3, end_line=3),
                    ),
                ),
            ),
        ),
    )


def observation(content: str, *, item_id: str = "lockout-duration") -> EvaluationOutput:
    return EvaluationOutput(
        observed_items=(
            ObservedItem(
                id=item_id,
                kind="GAP",
                content=content,
                source_references=case().gold.expected_items[0].source_references,
            ),
        )
    )


def registry(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(
        AppConfig(
            database_path=tmp_path / "qa.sqlite",
            workspace_root=tmp_path,
            agent_models=AgentModels(
                **{
                    role.value: ModelSpec(provider="openai", model_id=f"fake-{role.value}")
                    for role in AgentRole
                }
            ),
        )
    )


@pytest.mark.parametrize("item_id", ["lockout-duration", "MISSING_RULE", "arbitrary"])
def test_button_color_is_not_lockout_duration_even_with_same_kind_id_and_line(item_id: str) -> None:
    score = evaluate_case(case(), observation("What color should the button be?", item_id=item_id))
    assert score.precision == score.recall == 0
    assert score.findings


def test_correct_semantic_content_can_match_a_different_identity() -> None:
    score = evaluate_case(case(), observation("What is the lockout duration?", item_id="C-1"))
    assert score.precision == score.recall == score.evidence_accuracy == 1
    assert not score.findings


def test_legacy_approved_gold_has_no_invented_semantic_approval() -> None:
    root = Path(__file__).parents[2] / "benchmarks"
    legacy = next(
        item for item in load_dataset("v0.1", root).cases if item.case_id == "missing_rule"
    )
    expected = legacy.gold.expected_items[0]
    output = EvaluationOutput(
        observed_items=(
            ObservedItem(
                id=expected.id,
                kind=expected.kind,
                content="What color should the button be?",
                source_references=expected.source_references,
            ),
        )
    )
    score = evaluate_case(legacy, output)
    assert score.precision == score.recall == 0
    assert "gold semantic definition missing" in score.findings[0]
    assert legacy.gold.approved_by == "Rafly Rafsanjani"


def test_ai_paraphrase_always_requires_human_adjudication_even_if_judge_claims_confidence() -> None:
    output = observation("How long does lockout last?")
    assessment = EvaluatorAssessment(
        case_id="missing_rule",
        model_snapshot_id="static-judge",
        rating=5,
        critical=False,
        uncertainty=0,
        judgments=(
            SemanticJudgment(
                expected_item_id="lockout-duration",
                observed_item_id="lockout-duration",
                equivalent=True,
                review_required=False,
                uncertainty=0,
            ),
        ),
    )
    score = evaluate_case(case(), output.model_copy(update={"evaluator_assessment": assessment}))
    assert score.recall == 1
    assert "AI semantic match requires independent human adjudication" in score.findings


@pytest.mark.asyncio
async def test_static_evaluator_receives_full_content_and_can_reject_same_source_wrong_subject(
    tmp_path: Path,
) -> None:
    models = registry(tmp_path)
    calls: list[str] = []

    class FakeModel:
        def with_structured_output(self, schema, *, include_raw):
            assert schema is JudgeDraft
            assert include_raw
            return self

        async def ainvoke(self, messages):
            prompt = str(messages[1].content)
            assert "What color should the button be?" in prompt
            assert "What is the lockout duration?" in prompt
            return {
                "parsed": JudgeDraft(
                    rating=1,
                    critical=False,
                    uncertainty=0,
                    judgments=(
                        SemanticJudgment(
                            expected_item_id="lockout-duration",
                            observed_item_id="lockout-duration",
                            equivalent=False,
                            uncertainty=0,
                        ),
                    ),
                ),
                "raw": AIMessage(
                    content="",
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                ),
            }

    def factory(spec):
        calls.append(spec.model_id)
        return FakeModel()

    reviewed, raw = await review_output(
        case(),
        observation("What color should the button be?"),
        {"BRD": ["", "", "locks"]},
        models,
        factory,
    )
    assert calls == ["fake-evaluator"]
    assert reviewed.evaluator_assessment is not None
    assert evaluate_case(case(), reviewed).recall == 0
    assert raw.usage_metadata is not None
    assert raw.usage_metadata["total_tokens"] == 15
