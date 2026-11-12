import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from quality_platform.cli.evaluation import register_evaluation_commands
from quality_platform.evaluation.calibration import Review
from quality_platform.evaluation.models import (
    BenchmarkCase,
    BenchmarkReport,
    CandidateSnapshot,
    Dataset,
    EvalRecord,
    EvaluationOutput,
    EvaluatorAssessment,
    ExpectedItem,
    GoldRecord,
    GoldStatus,
    ObservedItem,
    ProductivityEvidence,
    ReleaseDecision,
    Severity,
    TaskType,
)
from quality_platform.evaluation.regression import (
    BaselineApproval,
    BaselineApprovalStatus,
    QualityThresholds,
    ThresholdStatus,
)
from quality_platform.evaluation.release import (
    CalibrationEvidence,
    IndependentReviews,
    digest,
    load_baseline,
)
from quality_platform.evaluation.reporting import write_report
from quality_platform.evaluation.runner import run_full_benchmark, run_offline_suite


def fake_report(*, count: int = 30) -> BenchmarkReport:
    cases = tuple(
        BenchmarkCase(
            case_id=f"case-{index}",
            task_type=TaskType.ANALYST,
            input_snapshot_ids=("test-input",),
            severity=Severity.HIGH,
            gold=GoldRecord(
                status=GoldStatus.APPROVED,
                approved_by="test-only QA",
                approved_at=datetime.now(UTC),
                expected_items=(
                    ExpectedItem(
                        id=f"R-{index}",
                        kind="REQUIREMENT",
                        semantic_definition=f"Test rule {index}",
                    ),
                ),
            ),
        )
        for index in range(count)
    )

    def observed(case: BenchmarkCase) -> EvaluationOutput:
        item = case.gold.expected_items[0]
        assert item.semantic_definition is not None
        return EvaluationOutput(
            observed_items=(
                ObservedItem(id=item.id, kind=item.kind, content=item.semantic_definition),
            ),
            evaluator_assessment=EvaluatorAssessment(
                case_id=case.case_id,
                model_snapshot_id="fake-static-evaluator",
                rating=5,
                critical=False,
                uncertainty=0,
                judgments=(),
            ),
        )

    return run_offline_suite(
        Dataset(version="v1", rubric_version="v1", cases=cases),
        observed,
        candidate=CandidateSnapshot(
            candidate_id="test-only",
            commit_sha="fake",
            config_hash="fake-config",
            model_snapshot_id="fake-models",
            prompt_snapshot_id="fake-prompts",
        ),
    )


def write(path: Path, record: EvalRecord) -> Path:
    path.write_text(record.model_dump_json(indent=2))
    return path


def harness() -> tuple[typer.Typer, CliRunner]:
    app = typer.Typer()
    register_evaluation_commands(app)
    return app, CliRunner()


def prepare(tmp_path: Path, report: BenchmarkReport, *, workflow: bool = False):
    app, runner = harness()
    score_path, _ = write_report(report, tmp_path)
    report = BenchmarkReport.model_validate_json(score_path.read_text())
    candidate = write(tmp_path / "candidate.json", report)
    if workflow:
        suite_path = tmp_path / "workflow-evidence.json"
        captured = runner.invoke(
            app,
            [
                "capture-workflow-evidence",
                "--candidate",
                str(candidate),
                "--output",
                str(suite_path),
            ],
        )
        assert captured.exit_code == 0, captured.exception
        candidate = tmp_path / "candidate-with-workflow.json"
        attached = runner.invoke(
            app,
            [
                "attach-workflow-evidence",
                "--candidate",
                str(tmp_path / "candidate.json"),
                "--evidence",
                str(suite_path),
                "--output",
                str(candidate),
            ],
        )
        assert attached.exit_code == 0, attached.exception
        report = BenchmarkReport.model_validate_json(candidate.read_text())
    ratings = [
        Review(
            case_id=case_id,
            rating=5,
            critical=False,
            reviewer="test-only human",
            reviewed_at=datetime.now(UTC),
            blind=True,
            uncertainty=0,
        ).model_dump(mode="json")
        for case_id in report.evaluated_case_ids
    ]
    (tmp_path / "ratings.json").write_text(json.dumps(ratings))
    human_path = tmp_path / "human.json"
    result = runner.invoke(
        app,
        [
            "capture-human-review",
            "--candidate",
            str(candidate),
            "--ratings",
            str(tmp_path / "ratings.json"),
            "--output",
            str(human_path),
            "--confirm-independent",
            *(["--confirm-all-repeats-reviewed"] if report.repeat_count > 1 else []),
        ],
    )
    assert result.exit_code == 0, result.exception
    assert "rating" not in result.output and "agreement" not in result.output
    calibration_path = tmp_path / "calibration.json"
    result = runner.invoke(
        app,
        [
            "calibrate",
            "--candidate",
            str(candidate),
            "--human-reviews",
            str(human_path),
            "--output",
            str(calibration_path),
        ],
    )
    assert result.exit_code == 0, result.exception
    calibration = CalibrationEvidence.model_validate_json(calibration_path.read_text())
    human = IndependentReviews.model_validate_json(human_path.read_text())
    thresholds = QualityThresholds(
        version="test-calibrated",
        status=ThresholdStatus.CALIBRATED,
        minimum_precision=0.95,
        minimum_recall=0.95,
        minimum_evidence_accuracy=0.98,
        maximum_precision_regression=0.01,
        maximum_recall_regression=0.01,
        maximum_evidence_regression=0.01,
    )
    thresholds_path = write(tmp_path / "thresholds.yaml", thresholds)
    approval = BaselineApproval(
        candidate_run_id=report.run_id,
        status=BaselineApprovalStatus.APPROVED,
        reviewer="test-only release QA",
        reviewed_at=datetime.now(UTC),
        reviewed_evidence=(
            f"report:{digest(report)}",
            f"human-review:{digest(human)}",
            f"calibration:{digest(calibration)}",
            f"thresholds:{digest(thresholds)}",
            "regression-decision:PASS",
            *(
                (f"workflow-suite:{digest(report.workflow_suite_evidence)}",)
                if report.workflow_suite_evidence
                else ()
            ),
        ),
    )
    approval_path = write(tmp_path / "approval.json", approval)
    common = [
        "--candidate",
        str(candidate),
        "--human-reviews",
        str(human_path),
        "--calibration",
        str(calibration_path),
        "--thresholds",
        str(thresholds_path),
        "--baseline-directory",
        str(tmp_path / "baselines"),
    ]
    return app, runner, common, approval_path


@pytest.mark.parametrize("repeats", [1, 2])
def test_release_commands_capture_calibrate_approve_then_explicitly_promote_and_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repeats: int,
) -> None:
    report = fake_report()
    if repeats > 1:
        from quality_platform.evaluation import runner as evaluation_runner

        config, raw = evaluation_runner._load_live_config(
            evaluation_runner._repository_root(), "tier3"
        )
        monkeypatch.setattr(
            evaluation_runner,
            "_load_live_config",
            lambda *_args: (config.model_copy(update={"minimum_approved_real_cases": 0}), raw),
        )
        gates = evaluation_runner.load_quality_thresholds(
            Path("benchmarks/rubrics/quality-gates.yaml")
        )
        monkeypatch.setattr(
            evaluation_runner,
            "load_quality_thresholds",
            lambda *_args: gates.model_copy(update={"status": ThresholdStatus.CALIBRATED}),
        )
        repeat_reports = iter(
            report.model_copy(update={"run_id": f"repeat-{index}"}) for index in range(repeats)
        )

        async def fake_task_run(_dataset_version, _budget):
            return next(repeat_reports)

        monkeypatch.setattr(evaluation_runner, "_run_tier3_once", fake_task_run)
        monkeypatch.setenv("QA_TIER3_BUDGET_CONFIRMED", "yes")
        monkeypatch.setenv("QA_TIER3_MAX_COST_USD", "1")
        productivity = ProductivityEvidence(
            equivalent_task_id="test-task",
            acceptance_criteria_id="test-acceptance",
            manual_task_id="manual",
            assisted_task_id="assisted",
            accepted_artifact_ids=("test-artifact",),
            accepted_by="test-only QA",
            evidence_references=("test-only acceptance",),
            active_qa_minutes=10,
            manual_baseline_minutes=20,
            human_review_minutes=2,
            rework_minutes=1,
        )
        path = write(tmp_path / "productivity.json", productivity)
        monkeypatch.setenv("QA_TIER3_PRODUCTIVITY_EVIDENCE", str(path))
        report = asyncio.run(run_full_benchmark("v0.1", repeats=repeats))
        assert report.repeat_count == repeats
        assert all(repeat.evaluator_assessments for repeat in report.repeat_results)
    app, runner, common, approval = prepare(tmp_path, report, workflow=True)
    report = BenchmarkReport.model_validate_json(Path(common[1]).read_text())
    calibration = CalibrationEvidence.model_validate_json(
        (tmp_path / "calibration.json").read_text()
    )
    assert calibration.calibration.sample_size == 30  # Not 30 * repeats.
    if repeats > 1:
        assert calibration.sampling_method == "case-cluster-worst-rating-any-critical"
        assert calibration.repeat_run_ids == tuple(item.run_id for item in report.repeat_results)
    assert report.review_packet_path is not None
    packet = json.loads(Path(report.review_packet_path).read_text())
    assert len(packet["outputs"]) == repeats
    assert "rating" not in json.dumps(packet)
    assert all(
        output["evaluator_assessment"] is None
        for outputs in packet["outputs"]
        for output in outputs.values()
    )
    release_path = tmp_path / "release.json"
    result = runner.invoke(
        app,
        [
            "release-review",
            *common,
            "--approval",
            str(approval),
            "--output",
            str(release_path),
            "--bootstrap",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert "PASS" in result.output
    assert load_baseline(tmp_path / "baselines") is None  # review never promotes implicitly
    denied = runner.invoke(app, ["promote-baseline", *common, "--release", str(release_path)])
    assert denied.exit_code != 0
    assert load_baseline(tmp_path / "baselines") is None
    promoted = runner.invoke(
        app,
        [
            "promote-baseline",
            *common,
            "--release",
            str(release_path),
            "--confirm-promotion",
        ],
    )
    assert promoted.exit_code == 0, promoted.exception
    baseline = load_baseline(tmp_path / "baselines")
    assert baseline is not None
    assert baseline.report == report
    assert len(list((tmp_path / "baselines" / "history").glob("*.json"))) == 1

    regressed = report.model_copy(
        update={
            "run_id": "regressed",
            "overall_recall": 0.5,
            "case_scores": tuple(
                item.model_copy(update={"recall": 0.5}) for item in report.case_scores
            ),
        }
    )
    candidate = write(tmp_path / "regressed.json", regressed)
    compared = runner.invoke(
        app,
        [
            "compare",
            "--candidate",
            str(candidate),
            "--thresholds",
            str(tmp_path / "thresholds.yaml"),
            "--baseline-directory",
            str(tmp_path / "baselines"),
            "--output",
            str(tmp_path / "comparison.json"),
        ],
    )
    assert compared.exit_code == 0, compared.exception
    assert "BLOCKED" in compared.output
    assert load_baseline(tmp_path / "baselines") == baseline


@pytest.mark.parametrize("blocker", ["critical", "small-sample", "missing-semantic", "workflow"])
def test_release_approval_cannot_hide_incomplete_or_critical_evidence(
    tmp_path: Path,
    blocker: str,
) -> None:
    report = fake_report(count=2 if blocker == "small-sample" else 30)
    if blocker == "critical":
        report = report.model_copy(
            update={
                "decision": ReleaseDecision.BLOCKED,
                "case_scores": (
                    report.case_scores[0].model_copy(update={"critical_violation": True}),
                    *report.case_scores[1:],
                ),
            }
        )
    elif blocker == "missing-semantic":
        report = report.model_copy(
            update={
                "decision": ReleaseDecision.REVIEW_REQUIRED,
                "case_scores": (
                    report.case_scores[0].model_copy(
                        update={
                            "precision": 0,
                            "recall": 0,
                            "findings": (
                                "gold semantic definition missing; human gold review required",
                            ),
                        }
                    ),
                    *report.case_scores[1:],
                ),
            }
        )
    elif blocker == "workflow":
        report = report.model_copy(update={"readiness_labels": ("E2E_WORKFLOW_EVIDENCE_MISSING",)})
    app, runner, common, approval = prepare(tmp_path, report)
    release_path = tmp_path / "release.json"
    result = runner.invoke(
        app,
        [
            "release-review",
            *common,
            "--approval",
            str(approval),
            "--output",
            str(release_path),
            "--bootstrap",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert (
        "BLOCKED" in result.output if blocker == "critical" else "REVIEW_REQUIRED" in result.output
    )
    promoted = runner.invoke(
        app,
        [
            "promote-baseline",
            *common,
            "--release",
            str(release_path),
            "--confirm-promotion",
        ],
    )
    assert promoted.exit_code != 0
    assert load_baseline(tmp_path / "baselines") is None


def test_promotion_revalidates_hashes_and_rejects_modified_evidence(tmp_path: Path) -> None:
    report = fake_report()
    app, runner, common, approval = prepare(tmp_path, report, workflow=True)
    release_path = tmp_path / "release.json"
    result = runner.invoke(
        app,
        [
            "release-review",
            *common,
            "--approval",
            str(approval),
            "--output",
            str(release_path),
            "--bootstrap",
        ],
    )
    assert result.exit_code == 0, result.exception
    changed = report.model_copy(
        update={"candidate": report.candidate.model_copy(update={"commit_sha": "changed"})}
    )
    write(Path(common[1]), changed)
    denied = runner.invoke(
        app,
        [
            "promote-baseline",
            *common,
            "--release",
            str(release_path),
            "--confirm-promotion",
        ],
    )
    assert denied.exit_code != 0
    assert load_baseline(tmp_path / "baselines") is None


def test_productivity_command_retains_equivalence_acceptance_and_rework_without_approving(
    tmp_path: Path,
) -> None:
    report = fake_report(count=1).model_copy(
        update={
            "decision": ReleaseDecision.REVIEW_REQUIRED,
            "readiness_labels": ("PRODUCTIVITY_EVIDENCE_MISSING", "E2E_WORKFLOW_EVIDENCE_MISSING"),
        }
    )
    candidate = write(tmp_path / "candidate.json", report)
    productivity = ProductivityEvidence(
        equivalent_task_id="test-task",
        acceptance_criteria_id="test-criteria-v1",
        manual_task_id="manual-1",
        assisted_task_id="assisted-1",
        accepted_artifact_ids=("TCSET-1",),
        accepted_by="test-only QA",
        evidence_references=("test-acceptance-record",),
        active_qa_minutes=12,
        manual_baseline_minutes=30,
        human_review_minutes=4,
        rework_minutes=3,
    )
    evidence_path = write(tmp_path / "evidence.json", productivity)
    output = tmp_path / "with-productivity.json"
    app, runner = harness()
    result = runner.invoke(
        app,
        [
            "record-productivity",
            "--candidate",
            str(candidate),
            "--evidence",
            str(evidence_path),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.exception
    updated = BenchmarkReport.model_validate_json(output.read_text())
    assert updated.productivity_evidence == productivity
    assert updated.active_qa_minutes == 12
    assert updated.manual_baseline_minutes == 30
    assert updated.readiness_labels == ("E2E_WORKFLOW_EVIDENCE_MISSING",)
    assert updated.decision == ReleaseDecision.REVIEW_REQUIRED


@pytest.mark.parametrize("tampering", ["missing", "content", "content-with-updated-hash"])
def test_human_review_requires_original_blind_candidate_outputs(
    tmp_path: Path, tampering: str
) -> None:
    report = fake_report(count=1)
    candidate, _ = write_report(report, tmp_path)
    payload = json.loads(candidate.read_text())
    packet = Path(payload["review_packet_path"])
    if tampering == "missing":
        packet.unlink()
    else:
        packet.write_text(packet.read_text().replace("Test rule 0", "A different rule"))
        if tampering == "content-with-updated-hash":
            payload["review_packet_sha256"] = hashlib.sha256(packet.read_bytes()).hexdigest()
            candidate.write_text(json.dumps(payload))
    ratings = tmp_path / "ratings.json"
    ratings.write_text("[]")
    app, runner = harness()
    output = tmp_path / "human-review.json"
    result = runner.invoke(
        app,
        [
            "capture-human-review",
            "--candidate",
            str(candidate),
            "--ratings",
            str(ratings),
            "--output",
            str(output),
            "--confirm-independent",
        ],
    )
    assert result.exit_code != 0
    assert "packet" in str(result.exception)
    assert not output.exists()
