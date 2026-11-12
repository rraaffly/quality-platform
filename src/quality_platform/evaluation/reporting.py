import hashlib
import json
import os
import tempfile
from pathlib import Path

from quality_platform.evaluation.models import BenchmarkReport, EvalRecord, EvaluationOutput


class ReviewPacket(EvalRecord):
    candidate_run_id: str
    input_fingerprint: str
    repeat_run_ids: tuple[str, ...]
    outputs: tuple[dict[str, EvaluationOutput], ...]


def fingerprint_outputs(outputs: dict[str, EvaluationOutput]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                case_id: output.model_dump(mode="json", exclude={"evaluator_assessment"})
                for case_id, output in sorted(outputs.items())
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def validate_review_packet(report: BenchmarkReport) -> ReviewPacket:
    if not report.review_packet_path or not report.review_packet_sha256:
        raise ValueError("private candidate review packet missing; rerun the benchmark")
    path = Path(report.review_packet_path)
    if (
        not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != report.review_packet_sha256
    ):
        raise ValueError("private candidate review packet missing or changed")
    packet = ReviewPacket.model_validate_json(path.read_text(encoding="utf-8"))
    fingerprints = [fingerprint_outputs(outputs) for outputs in packet.outputs]
    fingerprint = (
        hashlib.sha256(json.dumps(fingerprints).encode()).hexdigest()
        if report.repeat_results
        else fingerprints[0]
        if len(fingerprints) == 1
        else ""
    )
    if (
        packet.candidate_run_id != report.run_id
        or packet.input_fingerprint != report.input_fingerprint
        or packet.repeat_run_ids != tuple(item.run_id for item in report.repeat_results)
        or len(packet.outputs) != report.repeat_count
        or any(set(outputs) != set(report.evaluated_case_ids) for outputs in packet.outputs)
        or fingerprint != report.output_fingerprint
        or any(
            output.evaluator_assessment is not None
            for outputs in packet.outputs
            for output in outputs.values()
        )
    ):
        raise ValueError("private review packet does not match candidate outputs/repeats")
    return packet


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
    if report.review_outputs:
        private_directory = directory / ".private"
        private_directory.mkdir(mode=0o700, exist_ok=True)
        packet = ReviewPacket(
            candidate_run_id=report.run_id,
            input_fingerprint=report.input_fingerprint,
            repeat_run_ids=tuple(item.run_id for item in report.repeat_results),
            outputs=tuple(
                {
                    case_id: output.model_copy(
                        update={"evaluator_assessment": None, "raw_text": None}
                    )
                    for case_id, output in outputs.items()
                }
                for outputs in report.review_outputs
            ),
        )
        packet_path = (private_directory / f"{report.run_id}.review.json").resolve()
        _atomic_write(packet_path, packet.model_dump_json(indent=2))
        report = report.model_copy(
            update={
                "review_packet_path": str(packet_path),
                "review_packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            }
        )
        validate_review_packet(report)
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
        f"- Source fingerprint: {report.code_fingerprint or 'unrecorded'}",
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
    for score in report.case_scores:
        if score.planner_metrics:
            rows.extend(["", f"## Planner metrics: {score.case_id}", ""])
            rows.extend(
                f"- {name}: {value:.3f}" for name, value in sorted(score.planner_metrics.items())
            )
        rows.extend(f"- Review finding ({score.case_id}): {finding}" for finding in score.findings)
        if score.observed_actions or score.observed_workflow_state is not None:
            rows.append(
                f"- Observed policy ({score.case_id}): actions={score.observed_actions}; state={score.observed_workflow_state}"
            )
    if report.policy_suite_tests:
        rows.extend(["", "## Deterministic POLICY cases (not model semantic scores)", ""])
        rows.extend(
            f"- {case_id}: {report.policy_case_results.get(case_id, 'EVIDENCE_MISSING')}; tests={', '.join(ids)}"
            for case_id, ids in sorted(report.policy_suite_tests.items())
        )
    if report.repeat_results:
        rows.extend(
            [
                "",
                "| Repeat run | Case | Precision | Recall | Evidence | Decision |",
                "| --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        rows.extend(
            f"| {repeat.run_id} | {score.case_id} | {score.precision:.3f} | "
            f"{score.recall:.3f} | {score.evidence_accuracy:.3f} | {repeat.decision} |"
            for repeat in report.repeat_results
            for score in repeat.case_scores
        )
    if report.metric_variance:
        rows.extend(["", "## Multirun population variance", ""])
        rows.extend(
            f"- {metric}: {value:.6f}" for metric, value in sorted(report.metric_variance.items())
        )
    if report.workflow_suite_evidence is not None:
        workflow = report.workflow_suite_evidence
        rows.extend(
            [
                "",
                "## Executed offline workflow/policy evidence",
                "",
                f"- Source fingerprint: {workflow.source_fingerprint}",
                f"- Commit: {workflow.commit_sha}",
                f"- Pytest exit code: {workflow.exit_code}",
                f"- Tests recorded: {len(workflow.test_results)}",
                f"- JUnit SHA-256: {workflow.junit_sha256}",
                "- Suites: " + ", ".join(workflow.suites),
            ]
        )
    if report.productivity_evidence is not None:
        evidence = report.productivity_evidence
        rows.extend(
            [
                "",
                "## Equivalent-task productivity evidence",
                "",
                f"- Equivalent task: {evidence.equivalent_task_id}",
                f"- Acceptance criteria: {evidence.acceptance_criteria_id}",
                f"- Manual / assisted execution: {evidence.manual_task_id} / {evidence.assisted_task_id}",
                f"- Accepted artifacts: {', '.join(evidence.accepted_artifact_ids)}",
                f"- Accepted by: {evidence.accepted_by}",
                f"- Human review / rework (minutes): {evidence.human_review_minutes:.3f} / {evidence.rework_minutes:.3f}",
                f"- Active QA time reduction: {1 - evidence.active_qa_minutes / evidence.manual_baseline_minutes:.1%}",
                f"- Cost per accepted artifact (USD): {report.total_cost_usd / len(evidence.accepted_artifact_ids):.6f}",
            ]
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
