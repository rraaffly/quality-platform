from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quality_platform.cli.app import app
from quality_platform.evaluation.models import (
    BenchmarkReport,
    CandidateSnapshot,
    CaseScore,
    ModelUsage,
    ReleaseDecision,
)
from quality_platform.evaluation.readiness import HumanReview, assemble_release_readiness
from quality_platform.evaluation.runner import LiveBenchmarkConfigurationError, run_full_benchmark
from quality_platform.llm.registry import AgentRole


def report(
    run_id: str,
    *,
    recall: float = 1,
    critical: bool = False,
    decision: ReleaseDecision = ReleaseDecision.PASS,
) -> BenchmarkReport:
    return BenchmarkReport(
        run_id=run_id,
        dataset_version="v0.1",
        rubric_id="qa-mvp",
        rubric_version="v0.1",
        candidate=CandidateSnapshot(
            candidate_id="tier3-candidate",
            commit_sha="commit-1",
            config_hash="config-1",
            model_snapshot_id="models-1",
            prompt_snapshot_id="prompt-1",
        ),
        usage=(
            ModelUsage(
                role=AgentRole.ANALYST,
                provider="openai",
                model_id="model-a",
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                cost_usd=0.01,
            ),
        ),
        total_cost_usd=0.01,
        case_scores=(
            CaseScore(
                case_id="conflict",
                eligible=True,
                precision=1,
                recall=recall,
                evidence_accuracy=1,
                unsupported_source_count=0,
                critical_violation=critical,
                findings=() if recall == 1 else ("missed conflict",),
            ),
        ),
        eligible_count=1,
        excluded_count=0,
        overall_precision=1,
        overall_recall=recall,
        decision=decision,
    )


@pytest.mark.asyncio
async def test_tier3_repeats_aggregate_cost_time_and_case_linked_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = iter(
        [
            report("repeat-1"),
            report("repeat-2", recall=0.5, decision=ReleaseDecision.REVIEW_REQUIRED),
        ]
    )

    async def fake_tier3_once(_dataset_version: str) -> BenchmarkReport:
        return next(outputs)

    monkeypatch.setattr("quality_platform.evaluation.runner._run_tier3_once", fake_tier3_once)
    monkeypatch.setenv("QA_TIER3_BUDGET_CONFIRMED", "yes")
    monkeypatch.setenv("QA_TIER3_MAX_COST_USD", "1.0")
    monkeypatch.setenv("QA_TIER3_ACTIVE_QA_MINUTES", "12")
    monkeypatch.setenv("QA_TIER3_MANUAL_BASELINE_MINUTES", "30")

    result = await run_full_benchmark("v0.1", repeats=2)

    assert result.repeat_count == 2
    assert result.total_cost_usd == pytest.approx(0.02)
    assert result.latency_seconds >= 0
    assert result.active_qa_minutes == 12
    assert result.manual_baseline_minutes == 30
    assert result.failure_case_ids == ("conflict",)
    assert result.decision == ReleaseDecision.REVIEW_REQUIRED
    assert "INSUFFICIENT_REAL_CASES" in result.readiness_labels
    assert "UNCALIBRATED_GATES" in result.readiness_labels


@pytest.mark.asyncio
async def test_tier3_never_runs_without_explicit_budget_or_with_unbounded_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QA_TIER3_BUDGET_CONFIRMED", raising=False)
    with pytest.raises(LiveBenchmarkConfigurationError, match="BUDGET_CONFIRMED"):
        await run_full_benchmark("v0.1", repeats=1)

    monkeypatch.setenv("QA_TIER3_BUDGET_CONFIRMED", "yes")
    monkeypatch.setenv("QA_TIER3_MAX_COST_USD", "1.0")
    with pytest.raises(LiveBenchmarkConfigurationError, match="repeats"):
        await run_full_benchmark("v0.1", repeats=6)


def human_review(**updates: object) -> HumanReview:
    values: dict[str, object] = {
        "reviewer": "QA lead",
        "reviewed_at": datetime.now(UTC),
        "approved": True,
        "reviewed_case_ids": ("conflict",),
        "active_qa_minutes": 12,
        "manual_baseline_minutes": 30,
        "thresholds_calibrated": True,
        "approved_real_case_count": 12,
        "minimum_real_case_count": 12,
    }
    values.update(updates)
    return HumanReview.model_validate(values)


def test_release_readiness_never_hides_critical_or_insufficient_evidence() -> None:
    critical = report("critical", critical=True, decision=ReleaseDecision.BLOCKED)
    uncalibrated = report("uncalibrated").model_copy(
        update={"readiness_labels": ("UNCALIBRATED_GATES",)}
    )
    ready = report("ready")

    assert assemble_release_readiness(critical, human_review()) == ReleaseDecision.BLOCKED
    assert (
        assemble_release_readiness(
            uncalibrated,
            human_review(thresholds_calibrated=False),
        )
        == ReleaseDecision.REVIEW_REQUIRED
    )
    assert assemble_release_readiness(ready, human_review()) == ReleaseDecision.PASS


def test_human_review_must_cover_every_failed_case() -> None:
    failed = report("failed", recall=0.5, decision=ReleaseDecision.REVIEW_REQUIRED).model_copy(
        update={"failure_case_ids": ("conflict",)}
    )

    decision = assemble_release_readiness(
        failed,
        human_review(reviewed_case_ids=(), approved=True),
    )

    assert decision == ReleaseDecision.REVIEW_REQUIRED


def test_explicit_tier3_cli_captures_productivity_inputs_and_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = report("tier3-cli").model_copy(
        update={
            "repeat_count": 2,
            "active_qa_minutes": 12,
            "manual_baseline_minutes": 30,
            "readiness_labels": ("INSUFFICIENT_REAL_CASES", "UNCALIBRATED_GATES"),
        }
    )

    async def fake_full_benchmark(dataset_version: str, *, repeats: int) -> BenchmarkReport:
        assert dataset_version == "v0.1"
        assert repeats == 2
        return expected

    monkeypatch.setattr("quality_platform.cli.commands.run_full_benchmark", fake_full_benchmark)
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "run",
            "--tier",
            "3",
            "--dataset-version",
            "v0.1",
            "--output-directory",
            str(tmp_path),
            "--repeats",
            "2",
            "--confirm-budget",
            "--max-cost-usd",
            "1.0",
            "--active-qa-minutes",
            "12",
            "--manual-baseline-minutes",
            "30",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "INSUFFICIENT_REAL_CASES" in result.output
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert len(list(tmp_path.glob("*.md"))) == 1
    markdown = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "- Repeats: 2" in markdown
    assert "- Active QA time (minutes): 12.000" in markdown
    assert "- Manual baseline time (minutes): 30.000" in markdown
    assert "- Readiness labels: INSUFFICIENT_REAL_CASES, UNCALIBRATED_GATES" in markdown
