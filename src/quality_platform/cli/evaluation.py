"""Register on the existing benchmark Typer; all commands below are offline."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from filelock import FileLock
from pydantic import TypeAdapter

from quality_platform.evaluation.calibration import Review
from quality_platform.evaluation.models import (
    BenchmarkReport,
    EvalRecord,
    ProductivityEvidence,
    ReleaseDecision,
    WorkflowSuiteEvidence,
)
from quality_platform.evaluation.regression import (
    BaselineApproval,
    decide_regression,
    load_quality_thresholds,
)
from quality_platform.evaluation.release import (
    ApprovedBaseline,
    CalibrationEvidence,
    IndependentReviews,
    ReleaseReview,
    create_calibration,
    digest,
    load_baseline,
    review_release,
    validate_independent_reviews,
)
from quality_platform.evaluation.reporting import _atomic_write, validate_review_packet
from quality_platform.evaluation.workflow_evidence import (
    attach_workflow_evidence,
    capture_workflow_suite,
    validate_workflow_evidence,
)

File = Annotated[Path, typer.Option(exists=True, dir_okay=False)]


def _read[T: EvalRecord](path: Path, schema: type[T]) -> T:
    return schema.model_validate_json(path.read_text(encoding="utf-8"))


def _record(path: Path, record: EvalRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a signed-off review or disguise a different decision as the same record.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(record.model_dump_json(indent=2) + "\n")
    typer.echo(f"{path}: sha256:{digest(record)}")


def register_evaluation_commands(benchmark: typer.Typer) -> None:
    @benchmark.command("capture-workflow-evidence")
    def capture_workflow_evidence(
        candidate: File,
        output: Annotated[Path, typer.Option()],
    ) -> None:
        """Actually run the fixed offline workflow/policy suites and record JUnit/code evidence."""
        if output.exists():
            raise typer.BadParameter("evidence already exists; use a new output path")
        report = _read(candidate, BenchmarkReport)
        evidence = capture_workflow_suite(report, output.with_suffix(".junit.xml"))
        _record(output, evidence)
        typer.echo(
            f"{validate_workflow_evidence(report, evidence).value}: {len(evidence.test_results)} tests executed"
        )

    @benchmark.command("attach-workflow-evidence")
    def attach_workflow(
        candidate: File,
        evidence: File,
        output: Annotated[Path, typer.Option()],
    ) -> None:
        """Bind executed results to this report; failed policy suites make it BLOCKED."""
        report = attach_workflow_evidence(
            _read(candidate, BenchmarkReport), _read(evidence, WorkflowSuiteEvidence)
        )
        _record(output, report)
        typer.echo(report.decision.value)

    @benchmark.command("evidence-hashes")
    def evidence_hashes(
        candidate: File,
        thresholds: File,
        human_reviews: File,
        calibration: File,
    ) -> None:
        """Print canonical evidence identities for a human-authored approval, not an approval."""
        report = _read(candidate, BenchmarkReport)
        for label, record in (
            ("report", report),
            ("thresholds", load_quality_thresholds(thresholds)),
            ("human-review", _read(human_reviews, IndependentReviews)),
            ("calibration", _read(calibration, CalibrationEvidence)),
        ):
            typer.echo(f"{label}:{digest(record)}")
        if report.workflow_suite_evidence is not None:
            typer.echo(f"workflow-suite:{digest(report.workflow_suite_evidence)}")

    @benchmark.command("record-productivity")
    def record_productivity(
        candidate: File,
        evidence: File,
        output: Annotated[Path, typer.Option()],
    ) -> None:
        """Attach accepted equivalent-task timings, including review/rework, without approving."""
        report = _read(candidate, BenchmarkReport)
        productivity = _read(evidence, ProductivityEvidence)
        result = report.model_copy(
            update={
                "productivity_evidence": productivity,
                "active_qa_minutes": productivity.active_qa_minutes,
                "manual_baseline_minutes": productivity.manual_baseline_minutes,
                "readiness_labels": tuple(
                    label
                    for label in report.readiness_labels
                    if label != "PRODUCTIVITY_EVIDENCE_MISSING"
                ),
            }
        )
        _record(output, result)

    @benchmark.command("capture-human-review")
    def capture_human_review(
        candidate: File,
        ratings: File,
        output: Annotated[Path, typer.Option()],
        confirm_independent: Annotated[bool, typer.Option()] = False,
        confirm_all_repeats_reviewed: Annotated[bool, typer.Option()] = False,
    ) -> None:
        """Capture human ratings before disclosing evaluator ratings (never prints AI scores)."""
        if not confirm_independent:
            raise typer.BadParameter("--confirm-independent is required for blind human ratings")
        report = _read(candidate, BenchmarkReport)
        validate_review_packet(report)
        if report.repeat_count > 1 and not confirm_all_repeats_reviewed:
            raise typer.BadParameter(
                "--confirm-all-repeats-reviewed is required for multirun ratings"
            )
        reviews = TypeAdapter(tuple[Review, ...]).validate_python(json.loads(ratings.read_text()))
        human = IndependentReviews(
            candidate_run_id=report.run_id,
            input_fingerprint=report.input_fingerprint,
            output_fingerprint=report.output_fingerprint,
            reviews=reviews,
            reviewed_repeat_run_ids=tuple(item.run_id for item in report.repeat_results),
        )
        validate_independent_reviews(report, human)
        _record(output, human)

    @benchmark.command("calibrate")
    def calibrate(
        candidate: File,
        human_reviews: File,
        output: Annotated[Path, typer.Option()],
    ) -> None:
        """Reveal AI ratings only after independent human ratings have been captured."""
        evidence = create_calibration(
            _read(candidate, BenchmarkReport), _read(human_reviews, IndependentReviews)
        )
        _record(output, evidence)
        typer.echo(evidence.calibration.model_dump_json(indent=2))

    @benchmark.command("compare")
    def compare(
        candidate: File,
        thresholds: File,
        baseline_directory: Annotated[Path, typer.Option()],
        output: Annotated[Path, typer.Option()],
    ) -> None:
        """Compare against latest approved and minimum gates; never updates the baseline."""
        baseline = load_baseline(baseline_directory)
        if baseline is None:
            raise typer.BadParameter(
                "latest approved baseline missing; use release-review --bootstrap"
            )
        result = decide_regression(
            _read(candidate, BenchmarkReport), baseline.report, load_quality_thresholds(thresholds)
        )
        _record(output, result)
        typer.echo(result.decision.value)

    @benchmark.command("release-review")
    def release_review(
        candidate: File,
        thresholds: File,
        human_reviews: File,
        calibration: File,
        approval: File,
        baseline_directory: Annotated[Path, typer.Option()],
        output: Annotated[Path, typer.Option()],
        bootstrap: Annotated[bool, typer.Option()] = False,
    ) -> None:
        """Record an explicit human release decision bound to all exact evidence hashes."""
        result = review_release(
            _read(candidate, BenchmarkReport),
            load_baseline(baseline_directory),
            load_quality_thresholds(thresholds),
            _read(human_reviews, IndependentReviews),
            _read(calibration, CalibrationEvidence),
            _read(approval, BaselineApproval),
            bootstrap=bootstrap,
        )
        _record(output, result)
        typer.echo(result.decision.value)
        for reason in (*result.regression.reasons, *result.reasons):
            typer.echo(reason)

    @benchmark.command("promote-baseline")
    def promote(
        candidate: File,
        thresholds: File,
        human_reviews: File,
        calibration: File,
        release: File,
        baseline_directory: Annotated[Path, typer.Option()],
        confirm_promotion: Annotated[bool, typer.Option()] = False,
    ) -> None:
        """Revalidate release evidence and atomically advance latest approved, explicitly."""
        if not confirm_promotion:
            raise typer.BadParameter("--confirm-promotion is required")
        report = _read(candidate, BenchmarkReport)
        previous = _read(release, ReleaseReview)
        baseline_directory.mkdir(parents=True, exist_ok=True)
        with FileLock(baseline_directory / ".promotion.lock"):
            baseline = load_baseline(baseline_directory)
            if previous.baseline_digest != (digest(baseline.report) if baseline else None):
                raise typer.BadParameter("latest approved baseline changed; repeat release review")
            verified = review_release(
                report,
                baseline,
                load_quality_thresholds(thresholds),
                _read(human_reviews, IndependentReviews),
                _read(calibration, CalibrationEvidence),
                previous.approval,
                bootstrap=baseline is None,
            )
            if verified != previous or verified.decision not in {
                ReleaseDecision.PASS,
                ReleaseDecision.APPROVED_EXCEPTION,
            }:
                raise typer.BadParameter("release evidence does not permit baseline promotion")
            approved = ApprovedBaseline(report=report, release=verified)
            history = baseline_directory / "history"
            history.mkdir(exist_ok=True)
            # A hash filename avoids path traversal through imported run IDs.
            _record(history / f"{digest(report)}.json", approved)
            _atomic_write(
                baseline_directory / "latest-approved.json", approved.model_dump_json(indent=2)
            )
        typer.echo(f"Promoted {report.run_id} at {datetime.now(UTC).isoformat()}")
