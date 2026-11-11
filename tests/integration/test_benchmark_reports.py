import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from typer.testing import CliRunner

from quality_platform.cli.app import app
from quality_platform.domain.models import SourceReference
from quality_platform.evaluation.models import (
    BenchmarkCase,
    BenchmarkReport,
    CandidateSnapshot,
    Dataset,
    EvaluationOutput,
    ExpectedItem,
    GoldRecord,
    GoldStatus,
    ModelUsage,
    ObservedItem,
    ReleaseDecision,
    Severity,
    TaskType,
)
from quality_platform.evaluation.reporting import write_report
from quality_platform.evaluation.runner import run_offline_suite
from quality_platform.llm.registry import AgentRole


def candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id="tier2-candidate",
        commit_sha="abc123",
        config_hash="config-sha256",
        model_snapshot_id="models-sha256",
        prompt_snapshot_id="prompt-v3",
    )


def approved_case(
    case_id: str, *, origin: Literal["synthetic", "real_world"] = "synthetic"
) -> BenchmarkCase:
    reference = SourceReference(document_id="BRD-1", start_line=3, end_line=3)
    return BenchmarkCase(
        case_id=case_id,
        task_type=TaskType.ANALYST,
        input_snapshot_ids=("BRD-1",),
        severity=Severity.HIGH,
        origin=origin,
        gold=GoldRecord(
            status=GoldStatus.APPROVED,
            approved_by="QA reviewer",
            approved_at=datetime.now(UTC),
            expected_items=(
                ExpectedItem(id="REQ-1", kind="REQUIREMENT", source_references=(reference,)),
            ),
        ),
    )


def test_report_records_reproducible_metadata_cost_and_masks_private_input(
    tmp_path: Path,
) -> None:
    private_marker = "PRIVATE CUSTOMER PRD: acquisition target"
    case = approved_case("metadata")
    report = run_offline_suite(
        Dataset(version="v0.2", rubric_version="v0.4", cases=(case,)),
        lambda _case: EvaluationOutput(
            observed_items=(
                ObservedItem(
                    id="REQ-1",
                    kind="REQUIREMENT",
                    source_references=case.gold.expected_items[0].source_references,
                ),
            ),
            raw_text=private_marker,
        ),
        candidate=candidate(),
        usage=(
            ModelUsage(
                role=AgentRole.ANALYST,
                provider="openai",
                model_id="model-a",
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                cost_usd=0.0042,
            ),
            ModelUsage(
                role=AgentRole.EVALUATOR,
                provider="anthropic",
                model_id="model-b",
                input_tokens=50,
                output_tokens=10,
                total_tokens=60,
                cost_usd=0.0021,
            ),
        ),
    )

    json_path, markdown_path = write_report(report, tmp_path)
    json_text = json_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")
    payload = json.loads(json_text)

    assert payload["dataset_version"] == "v0.2"
    assert payload["rubric_version"] == "v0.4"
    assert payload["candidate"] == {
        "candidate_id": "tier2-candidate",
        "commit_sha": "abc123",
        "config_hash": "config-sha256",
        "model_snapshot_id": "models-sha256",
        "prompt_snapshot_id": "prompt-v3",
    }
    assert {item["provider"] for item in payload["usage"]} == {"openai", "anthropic"}
    assert payload["total_cost_usd"] == 0.0063
    assert "Cost (USD): 0.006300" in markdown_text
    assert private_marker not in json_text + markdown_text


def test_unapproved_real_world_case_is_excluded_without_invoking_handler() -> None:
    approved = approved_case("synthetic-approved")
    pending_real = approved_case("real-pending", origin="real_world").model_copy(
        update={"gold": GoldRecord(status=GoldStatus.PENDING_REVIEW, expected_items=())}
    )
    seen: list[str] = []

    def handler(case: BenchmarkCase) -> EvaluationOutput:
        seen.append(case.case_id)
        return EvaluationOutput()

    report = run_offline_suite(
        Dataset(version="v0.2", rubric_version="v0.2", cases=(approved, pending_real)),
        handler,
        candidate=candidate(),
    )

    assert seen == ["synthetic-approved"]
    assert report.eligible_count == 1
    assert report.excluded_count == 1


def test_ordinary_benchmark_command_never_invokes_live_runner(monkeypatch) -> None:
    invoked = False

    async def forbidden_live_run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("live benchmark must be explicit")

    monkeypatch.setattr("quality_platform.cli.commands.run_live_benchmark", forbidden_live_run)
    result = CliRunner().invoke(app, ["benchmark", "run", "--dataset-version", "v0.1"])

    assert result.exit_code != 0
    assert invoked is False
    assert "--tier" in result.output


def test_explicit_tier2_command_writes_fake_report(tmp_path: Path, monkeypatch) -> None:
    report = run_offline_suite(
        Dataset(version="v0.1", rubric_version="v0.1", cases=()),
        lambda _case: EvaluationOutput(),
        candidate=candidate(),
    ).model_copy(update={"decision": ReleaseDecision.REVIEW_REQUIRED})

    async def fake_live_run(
        dataset_version: str, agent_role: AgentRole, max_cases: int | None
    ) -> BenchmarkReport:
        assert dataset_version == "v0.1"
        assert agent_role == AgentRole.ANALYST
        assert max_cases == 2
        return report

    monkeypatch.setattr("quality_platform.cli.commands.run_live_benchmark", fake_live_run)
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "run",
            "--tier",
            "2",
            "--dataset-version",
            "v0.1",
            "--agent-role",
            "analyst",
            "--max-cases",
            "2",
            "--output-directory",
            str(tmp_path),
            "--confirm-budget",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "REVIEW_REQUIRED" in result.output
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert len(list(tmp_path.glob("*.md"))) == 1
