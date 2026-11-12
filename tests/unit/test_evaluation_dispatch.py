import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from quality_platform.agents.analyst import RequirementAnalysisDraft
from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.domain.models import Requirement, RequirementReadiness, SourceReference
from quality_platform.domain.schemas import TestCase, TestCaseSet, TestStep
from quality_platform.evaluation.judge import JudgeDraft
from quality_platform.evaluation.models import (
    BenchmarkCase,
    Dataset,
    ExpectedItem,
    GoldRecord,
    GoldStatus,
    MetricReview,
    PlannerReview,
    SemanticJudgment,
    Severity,
    TaskType,
)
from quality_platform.evaluation.runner import _LiveBenchmarkConfig, _run_configured_benchmark
from quality_platform.evaluation.workflow_evidence import (
    attach_workflow_evidence,
    capture_workflow_suite,
)
from quality_platform.llm.registry import AgentRole


@pytest.mark.asyncio
@pytest.mark.parametrize("invented", [False, True])
@pytest.mark.parametrize("flagged", [False, True])
async def test_full_dispatch_runs_real_analyst_and_planner_contracts_with_static_fake_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invented: bool,
    flagged: bool,
) -> None:
    source = "# Login\n\nThe user is signed in after submitting valid credentials.\n"
    fixture_root = tmp_path / "benchmarks" / "datasets" / "synthetic"
    fixture_root.mkdir(parents=True)
    (fixture_root / "brd.md").write_text(source)
    reference = SourceReference(document_id="brd", start_line=3, end_line=3)
    requirement = Requirement(
        id="R-LOGIN",
        version=1,
        statement=source.splitlines()[2],
        source_references=(reference,),
        source_quotes=(source.splitlines()[2],),
        readiness=RequirementReadiness.VALIDATED,
    )
    designed = TestCase(
        id="TC-1",
        title="Login",
        intent="Validate login",
        scenario="Valid credentials",
        requirement_ids=("R-LOGIN",),
        steps=(
            TestStep(
                action="Submit valid credentials",
                expected_result="The user is signed in.",
                expected_result_sources=(reference,),
            ),
        ),
    )
    cases = tuple(
        BenchmarkCase(
            case_id=task.value.lower(),
            task_type=task,
            input_snapshot_ids=("brd",),
            input_snapshot_hashes={"brd": hashlib.sha256(source.encode()).hexdigest()},
            severity=Severity.HIGH,
            planner_requirements=(requirement,) if task == TaskType.PLANNER else (),
            gold=GoldRecord(
                status=GoldStatus.APPROVED,
                approved_by="test-only QA",
                approved_at=datetime.now(UTC),
                expected_items=(
                    ExpectedItem(
                        id="expected",
                        kind="SCENARIO" if task == TaskType.PLANNER else "REQUIREMENT",
                        semantic_definition=designed.model_dump_json()
                        if task == TaskType.PLANNER
                        else requirement.statement,
                        source_references=(reference,),
                    ),
                ),
            ),
        )
        for task in (TaskType.ANALYST, TaskType.PLANNER)
    )
    policy_test = "tests.unit.test_policies::test_final_approval_requires_every_guard"
    policy_case = BenchmarkCase(
        case_id="approval-policy",
        task_type=TaskType.POLICY,
        input_snapshot_ids=("brd",),
        severity=Severity.CRITICAL,
        policy_test_ids=(policy_test,),
        gold=GoldRecord(
            status=GoldStatus.APPROVED,
            approved_by="test-only QA",
            approved_at=datetime.now(UTC),
            expected_items=(),
        ),
    )
    dataset = Dataset(version="v1", rubric_version="v1", cases=(*cases, policy_case))
    config = AppConfig(
        database_path=tmp_path / "unused.sqlite",
        workspace_root=tmp_path,
        agent_models=AgentModels(
            **{
                role.value: ModelSpec(provider="openai", model_id=f"fake-{role.value}")
                for role in AgentRole
            }
        ),
    )
    calls: list[str] = []
    schemas: list[object] = []

    class FakeModel:
        def __init__(self, model_id):
            self.model_id = model_id

        def with_structured_output(self, schema, *, include_raw):
            assert include_raw
            schemas.append(schema)
            self.schema = schema
            return self

        async def ainvoke(self, messages):
            calls.append(self.model_id)
            parsed: object
            if self.schema is RequirementAnalysisDraft:
                parsed = RequirementAnalysisDraft(requirements=(requirement,), clarifications=())
            elif self.schema is TestCaseSet:
                assert "Validated requirements" in str(messages[0].content)
                item = designed
                if invented:
                    item = item.model_copy(
                        update={
                            "steps": (
                                TestStep(
                                    action="Login",
                                    expected_result="The account locks for 30 minutes.",
                                    expected_result_sources=(reference,),
                                ),
                            )
                        }
                    )
                parsed = TestCaseSet(cases=(item,))
            else:
                assert self.schema is JudgeDraft
                data = json.loads(messages[1].content)
                planner_review = None
                if data["task_type"] == "PLANNER":
                    assert all(
                        "gherkin" in item["semantic_context"]
                        and "detailed" in item["semantic_context"]
                        for item in data["observed"]
                    )
                    planner_review = PlannerReview(
                        **{
                            name: MetricReview(
                                score=0 if name == "redundancy" else 1,
                                review_required=flagged,
                                uncertainty=0,
                                rationale="Fake contract review, not live evidence",
                            )
                            for name in PlannerReview.model_fields
                        }
                    )
                parsed = JudgeDraft(
                    rating=5,
                    critical=False,
                    uncertainty=0,
                    planner_review=planner_review,
                    judgments=tuple(
                        SemanticJudgment(
                            expected_item_id=expected["id"],
                            observed_item_id=observed["id"],
                            equivalent=expected["semantic_definition"] == observed["content"],
                            review_required=False,
                            uncertainty=0,
                        )
                        for expected in data["expected"]
                        for observed in data["observed"]
                    ),
                )
            return {
                "parsed": parsed,
                "raw": AIMessage(
                    content="",
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                ),
            }

    monkeypatch.setattr("quality_platform.evaluation.runner._repository_root", lambda: tmp_path)
    monkeypatch.setattr("quality_platform.evaluation.runner._commit_sha", lambda _root: "fake-sha")
    monkeypatch.setattr("quality_platform.evaluation.runner.load_config", lambda _path: config)
    monkeypatch.setattr("quality_platform.evaluation.runner.load_dataset", lambda *_args: dataset)
    monkeypatch.setattr(
        "quality_platform.evaluation.runner.create_chat_model",
        lambda spec: FakeModel(spec.model_id),
    )
    app_config = tmp_path / "config.yaml"
    app_config.write_text("fake-config")
    monkeypatch.setenv("QUALITY_PLATFORM_CONFIG", str(app_config))
    for key, value in {
        "QA_TIER3_BUDGET_CONFIRMED": "yes",
        "QA_TIER3_MAX_COST_USD": "1",
        "QA_TIER3_INPUT_USD_PER_MILLION": "1",
        "QA_TIER3_OUTPUT_USD_PER_MILLION": "1",
        "QA_TIER3_ANALYST_INPUT_USD_PER_MILLION": "1",
        "QA_TIER3_ANALYST_OUTPUT_USD_PER_MILLION": "1",
        "QA_TIER3_PLANNER_INPUT_USD_PER_MILLION": "1",
        "QA_TIER3_PLANNER_OUTPUT_USD_PER_MILLION": "1",
        "QA_TIER3_EVALUATOR_INPUT_USD_PER_MILLION": "2",
        "QA_TIER3_EVALUATOR_OUTPUT_USD_PER_MILLION": "2",
    }.items():
        monkeypatch.setenv(key, value)
    live = _LiveBenchmarkConfig(
        candidate_id="fake",
        prompt_snapshot_id="fake",
        representative_case_ids=("analyst",),
        max_budget_usd=1,
        max_case_cost_usd=0.1,
    )

    async def run():
        return await _run_configured_benchmark(
            "v1",
            AgentRole.ANALYST,
            max_cases=None,
            config=live,
            config_raw=b"fake",
            environment_prefix="QA_TIER3",
            budget=None,
            full_dataset=True,
        )

    if invented:
        with pytest.raises(ValueError, match="unsupported expected result"):
            await run()
    else:
        report = await run()
        assert report.overall_precision == report.overall_recall == 1
        assert report.decision == "REVIEW_REQUIRED"
        assert report.readiness_labels == ("POLICY_SUITE_EVIDENCE_MISSING",)
        assert report.policy_suite_tests == {"approval-policy": (policy_test,)}
        assert report.eligible_count == 2 and report.excluded_count == 0
        assert report.case_scores[1].planner_metrics == {
            "requirement_coverage": 1,
            "scenario_coverage": 1,
            "schema_validation": 1,
            "expected_result_correctness": 1,
            "executability": 1,
            "redundancy": 0,
            "detailed_gherkin_agreement": 1,
        }
        assert bool(report.case_scores[1].findings) == flagged
        assert report.evaluated_case_ids == ("analyst", "planner")
        assert len(report.evaluator_assessments) == 2
        assert {item.role for item in report.usage} == {
            AgentRole.ANALYST,
            AgentRole.PLANNER,
            AgentRole.EVALUATOR,
        }
        assert report.total_cost_usd == pytest.approx(0.00009)
        assert calls == ["fake-analyst", "fake-evaluator", "fake-planner", "fake-evaluator"]
        assert schemas == [RequirementAnalysisDraft, JudgeDraft, TestCaseSet, JudgeDraft]
        if not flagged:
            evidence = capture_workflow_suite(report, tmp_path / "mixed-task.junit.xml")
            attached = attach_workflow_evidence(report, evidence)
            assert attached.decision == "PASS"
            assert attached.policy_case_results == {"approval-policy": "PASS"}
            assert not attached.readiness_labels
