import os
import tempfile
from pathlib import Path

from quality_platform.evaluation.models import BenchmarkReport


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_report(report: BenchmarkReport, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{report.run_id}.json"
    markdown_path = directory / f"{report.run_id}.md"
    rows = [
        f"# Benchmark run {report.run_id}",
        "",
        f"- Dataset: {report.dataset_version}",
        f"- Rubric: {report.rubric_id} {report.rubric_version}",
        f"- Candidate: {report.candidate.candidate_id} ({report.candidate.commit_sha})",
        f"- Config: {report.candidate.config_hash}",
        f"- Models: {report.candidate.model_snapshot_id}",
        f"- Prompt: {report.candidate.prompt_snapshot_id}",
        f"- Cost (USD): {report.total_cost_usd:.6f}",
        f"- Decision: {report.decision}",
        f"- Eligible cases: {report.eligible_count}",
        f"- Excluded cases: {report.excluded_count}",
        f"- Precision: {report.overall_precision:.3f}",
        f"- Recall: {report.overall_recall:.3f}",
        f"- Repeats: {report.repeat_count}",
        f"- Latency (seconds): {report.latency_seconds:.3f}",
        "- Active QA time (minutes): "
        + (f"{report.active_qa_minutes:.3f}" if report.active_qa_minutes is not None else "N/A"),
        "- Manual baseline time (minutes): "
        + (
            f"{report.manual_baseline_minutes:.3f}"
            if report.manual_baseline_minutes is not None
            else "N/A"
        ),
        "- Failed cases: " + (", ".join(report.failure_case_ids) or "none"),
        "- Readiness labels: " + (", ".join(report.readiness_labels) or "none"),
        "",
        "| Case | Precision | Recall | Evidence | Critical violation |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    rows.extend(
        f"| {score.case_id} | {score.precision:.3f} | {score.recall:.3f} | "
        f"{score.evidence_accuracy:.3f} | {score.critical_violation} |"
        for score in report.case_scores
    )
    if report.usage:
        rows.extend(
            [
                "",
                "| Role | Provider | Model | Input tokens | Output tokens | Cost (USD) |",
                "| --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        rows.extend(
            f"| {item.role} | {item.provider} | {item.model_id} | {item.input_tokens} | "
            f"{item.output_tokens} | {item.cost_usd:.6f} |"
            for item in report.usage
        )
    _atomic_write(json_path, report.model_dump_json(indent=2))
    _atomic_write(markdown_path, "\n".join(rows) + "\n")
    return json_path, markdown_path
