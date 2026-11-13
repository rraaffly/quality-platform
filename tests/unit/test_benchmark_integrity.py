import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from quality_platform.agents.analyst import RequirementAnalysisDraft
from quality_platform.config.models import ModelSpec
from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    Requirement,
    RequirementReadiness,
    SourceReference,
)
from quality_platform.evaluation.models import (
    BenchmarkCase,
    Dataset,
    GoldRecord,
    GoldStatus,
    Severity,
    TaskType,
)
from quality_platform.evaluation.runner import (
    LiveBenchmarkConfigurationError,
    _BudgetLedger,
    _case_documents,
    _invoke_live_case,
    _select_live_cases,
)
from quality_platform.llm.registry import AgentRole


def benchmark_case(snapshot_id: str, digest: str) -> BenchmarkCase:
    return BenchmarkCase(
        case_id="safe-input",
        task_type=TaskType.ANALYST,
        input_snapshot_ids=(snapshot_id,),
        input_snapshot_hashes={snapshot_id: digest},
        severity=Severity.HIGH,
        gold=GoldRecord(
            status=GoldStatus.APPROVED,
            approved_by="QA",
            approved_at=datetime.now(UTC),
            expected_items=(),
        ),
    )


def test_benchmark_snapshot_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    synthetic = root / "benchmarks" / "datasets" / "synthetic"
    synthetic.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("private", encoding="utf-8")
    try:
        (synthetic / "escaped.md").symlink_to(secret)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        pytest.skip("symlink creation requires Windows Developer Mode or privilege")
    case = benchmark_case("escaped", hashlib.sha256(secret.read_bytes()).hexdigest())

    with pytest.raises(LiveBenchmarkConfigurationError, match="outside benchmark root"):
        _case_documents(root, case)


def test_benchmark_snapshot_hash_is_verified_before_prompting(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    synthetic = root / "benchmarks" / "datasets" / "synthetic"
    synthetic.mkdir(parents=True)
    (synthetic / "case.md").write_text("mutated", encoding="utf-8")

    with pytest.raises(LiveBenchmarkConfigurationError, match="hash mismatch"):
        _case_documents(root, benchmark_case("case", "0" * 64))


def test_budget_ledger_reserves_before_spend_and_is_shared_across_repeats() -> None:
    budget = _BudgetLedger(limit_usd=1.0)
    budget.reserve(0.6)

    with pytest.raises(RuntimeError, match="before provider call"):
        budget.reserve(0.6)

    budget.record(0.5, reserved_usd=0.6)
    assert budget.spent_usd == pytest.approx(0.5)


def test_full_tier_selects_every_approved_case_and_refuses_unsupported_roles() -> None:
    analyst = benchmark_case("analyst-input", "a" * 64)
    planner = analyst.model_copy(update={"case_id": "planner", "task_type": TaskType.PLANNER})
    pending = analyst.model_copy(
        update={
            "case_id": "pending",
            "gold": GoldRecord(status=GoldStatus.PENDING_REVIEW, expected_items=()),
        }
    )
    dataset = Dataset(version="v0.1", rubric_version="v0.1", cases=(analyst, pending))

    assert _select_live_cases(
        dataset,
        AgentRole.ANALYST,
        representative_case_ids=(),
        full_dataset=True,
    ) == (analyst,)

    with pytest.raises(LiveBenchmarkConfigurationError, match="planner"):
        _select_live_cases(
            dataset.model_copy(update={"cases": (analyst, planner, pending)}),
            AgentRole.ANALYST,
            representative_case_ids=(),
            full_dataset=True,
        )


@pytest.mark.asyncio
async def test_live_analyst_uses_production_schema_validation_and_semantic_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    fixture_root = root / "benchmarks" / "datasets" / "synthetic" / "analyst"
    fixture_root.mkdir(parents=True)
    brd = "# Login\n\nThe account locks after 3 failed attempts.\n"
    srs = "# Login\n\nThe account locks after 5 failed attempts.\n"
    (fixture_root / "brd.md").write_text(brd, encoding="utf-8")
    (fixture_root / "srs.md").write_text(srs, encoding="utf-8")
    brd_ref = SourceReference(document_id="brd", start_line=3, end_line=3)
    srs_ref = SourceReference(document_id="srs", start_line=3, end_line=3)
    blocked = Requirement(
        id="REQ-LOCKOUT",
        version=1,
        statement="The lockout threshold conflicts.",
        source_references=(brd_ref, srs_ref),
        readiness=RequirementReadiness.BLOCKED,
    )
    question = Clarification(
        id="C-LOCKOUT",
        issue_type=ClarificationIssueType.CROSS_DOCUMENT_CONFLICT,
        question="Which threshold applies?",
        source_references=(brd_ref, srs_ref),
        affected_requirement_ids=(blocked.id,),
    )
    draft = RequirementAnalysisDraft(requirements=(blocked,), clarifications=(question,))
    seen_schema: list[object] = []

    class FakeStructured:
        async def ainvoke(self, _messages: object) -> dict[str, object]:
            return {
                "parsed": draft,
                "raw": AIMessage(
                    content="",
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                ),
            }

    class FakeModel:
        def with_structured_output(self, schema: object, *, include_raw: bool) -> FakeStructured:
            assert include_raw is True
            seen_schema.append(schema)
            return FakeStructured()

    class FakeRegistry:
        def get(self, role: AgentRole) -> ModelSpec:
            assert role == AgentRole.ANALYST
            return ModelSpec(provider="openai", model_id="fake")

    monkeypatch.setattr(
        "quality_platform.evaluation.runner.create_chat_model", lambda _spec: FakeModel()
    )
    case = benchmark_case("brd", hashlib.sha256(brd.encode()).hexdigest()).model_copy(
        update={
            "case_id": "conflict",
            "input_snapshot_ids": ("brd", "srs"),
            "input_snapshot_hashes": {
                "brd": hashlib.sha256(brd.encode()).hexdigest(),
                "srs": hashlib.sha256(srs.encode()).hexdigest(),
            },
        }
    )

    output, usage = await _invoke_live_case(
        case,
        AgentRole.ANALYST,
        FakeRegistry(),  # type: ignore[arg-type]
        root,
        input_rate=1.0,
        output_rate=1.0,
    )

    assert seen_schema == [RequirementAnalysisDraft]
    assert output.observed_items[0].kind == "CONFLICT"
    assert output.observed_items[0].id == "C-LOCKOUT"
    assert usage.total_tokens == 15
