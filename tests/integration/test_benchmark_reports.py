import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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
    Severity,
    TaskType,
)
from quality_platform.evaluation.reporting import validate_review_packet, write_report
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
    private_output = "Private candidate requirement for independent QA review"
    case = approved_case("metadata")
    report = run_offline_suite(
        Dataset(version="v0.2", rubric_version="v0.4", cases=(case,)),
        lambda _case: EvaluationOutput(
            observed_items=(
                ObservedItem(
                    id="REQ-1",
                    kind="REQUIREMENT",
                    content=private_output,
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
    assert private_output not in json_text + markdown_text
    persisted = BenchmarkReport.model_validate_json(json_text)
    packet = validate_review_packet(persisted)
    assert packet.outputs[0]["metadata"].observed_items[0].content == private_output
    assert persisted.review_packet_path is not None
    packet_path = Path(persisted.review_packet_path)
    assert private_marker not in packet_path.read_text()
    if os.name == "nt":
        for private_path in (packet_path.parent, packet_path):
            acl = subprocess.check_output(["icacls", str(private_path)], text=True)
            assert "(I)" not in acl
    else:
        assert packet_path.stat().st_mode & 0o777 == 0o600
        assert packet_path.parent.stat().st_mode & 0o777 == 0o700


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
