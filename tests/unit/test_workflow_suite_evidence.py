from datetime import UTC, datetime
from pathlib import Path

import pytest

from quality_platform.evaluation.calibration import Review
from quality_platform.evaluation.models import (
    BenchmarkReport,
    CandidateSnapshot,
    CaseScore,
    EvaluatorAssessment,
    ReleaseDecision,
)
from quality_platform.evaluation.regression import (
    BaselineApproval,
    BaselineApprovalStatus,
    QualityThresholds,
    ThresholdStatus,
)
from quality_platform.evaluation.release import (
    IndependentReviews,
    create_calibration,
    digest,
    review_release,
)
from quality_platform.evaluation.workflow_evidence import (
    attach_workflow_evidence,
    capture_workflow_suite,
    code_fingerprint,
    validate_workflow_evidence,
)


def report(root: Path) -> BenchmarkReport:
    return BenchmarkReport(
        run_id="test-only",
        dataset_version="v1",
        rubric_id="test",
        rubric_version="v1",
        candidate=CandidateSnapshot(
            candidate_id="test-only",
            commit_sha="fake",
            config_hash="fake",
            model_snapshot_id="fake",
            prompt_snapshot_id="fake",
        ),
        case_scores=(
            CaseScore(
                case_id="policy",
                eligible=True,
                precision=1,
                recall=1,
                evidence_accuracy=1,
                unsupported_source_count=0,
                critical_violation=False,
            ),
        ),
        eligible_count=1,
        excluded_count=0,
        overall_precision=1,
        overall_recall=1,
        decision=ReleaseDecision.REVIEW_REQUIRED,
        input_fingerprint="fake-input",
        output_fingerprint="fake-output",
        evaluated_case_ids=("policy",),
        readiness_labels=("E2E_WORKFLOW_EVIDENCE_MISSING",),
        code_fingerprint=code_fingerprint(root),
        evaluator_assessments=(
            EvaluatorAssessment(
                case_id="policy",
                model_snapshot_id="fake-evaluator",
                rating=5,
                critical=False,
                uncertainty=0,
                judgments=(),
            ),
        ),
    )


@pytest.mark.parametrize("failure", [False, True])
def test_actually_executed_pytest_evidence_pass_or_failure_binds_release_and_rejects_staleness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    # Disposable mini-repository tests the recorder with a real pytest child, including a
    # deliberate policy failure. It is not evidence that the production workflow passed.
    suite = tmp_path / "tests" / "e2e" / "test_policy.py"
    suite.parent.mkdir(parents=True)
    suite.write_text(f"def test_approval_policy():\n    assert {not failure}\n")
    monkeypatch.setattr(
        "quality_platform.evaluation.workflow_evidence.repository_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "quality_platform.evaluation.workflow_evidence.WORKFLOW_POLICY_SUITES",
        ("tests/e2e/test_policy.py",),
    )
    candidate = report(tmp_path)
    candidate = candidate.model_copy(
        update={
            "policy_suite_tests": {
                "approval-policy": ("tests.e2e.test_policy::test_approval_policy",)
            },
        }
    )
    evidence = capture_workflow_suite(candidate, tmp_path / "executed.junit.xml")
    expected = ReleaseDecision.BLOCKED if failure else ReleaseDecision.PASS
    assert validate_workflow_evidence(candidate, evidence) == expected, evidence.test_results
    assert len(evidence.test_results) == 1
    assert evidence.test_results[0].outcome == ("failed" if failure else "passed")
    attached = attach_workflow_evidence(candidate, evidence)
    assert attached.decision == expected
    assert "E2E_WORKFLOW_EVIDENCE_MISSING" not in attached.readiness_labels
    assert attached.workflow_suite_evidence is not None
    assert attached.policy_case_results == {"approval-policy": expected}

    human = IndependentReviews(
        candidate_run_id=attached.run_id,
        input_fingerprint=attached.input_fingerprint,
        output_fingerprint=attached.output_fingerprint,
        reviews=(
            Review(
                case_id="policy",
                rating=5,
                critical=False,
                reviewer="test-only human",
                reviewed_at=datetime.now(UTC),
                blind=True,
                uncertainty=0,
            ),
        ),
    )
    calibration = create_calibration(attached, human)
    approval = BaselineApproval(
        candidate_run_id=attached.run_id,
        status=BaselineApprovalStatus.APPROVED,
        reviewer="test-only release human",
        reviewed_at=datetime.now(UTC),
        reviewed_evidence=("test-only-not-real-approval",),
    )
    thresholds = QualityThresholds(
        version="test-only",
        status=ThresholdStatus.CALIBRATED,
        minimum_precision=0.9,
        minimum_recall=0.9,
        minimum_evidence_accuracy=0.9,
        maximum_precision_regression=0,
        maximum_recall_regression=0,
        maximum_evidence_regression=0,
    )
    release = review_release(
        attached, None, thresholds, human, calibration, approval, bootstrap=True
    )
    if failure:
        assert release.decision == ReleaseDecision.BLOCKED
        assert "critical offline workflow/policy suite failed" in release.reasons
    else:
        assert release.decision == ReleaseDecision.REVIEW_REQUIRED
        assert "approval does not reference all exact release evidence" in release.reasons
        # The private packet's own checksum and identity checks have separate tests.
        monkeypatch.setattr(
            "quality_platform.evaluation.release.validate_review_packet", lambda _report: None
        )
        approved = BaselineApproval(
            candidate_run_id=attached.run_id,
            status=BaselineApprovalStatus.APPROVED,
            reviewer="test-only release human",
            reviewed_at=datetime.now(UTC),
            reviewed_evidence=(
                f"report:{digest(attached)}",
                f"human-review:{digest(human)}",
                f"calibration:{digest(calibration)}",
                f"thresholds:{digest(thresholds)}",
                f"workflow-suite:{digest(evidence)}",
                "regression-decision:PASS",
                "small-sample:acknowledged",
            ),
        )
        assert (
            review_release(
                attached, None, thresholds, human, calibration, approved, bootstrap=True
            ).decision
            == ReleaseDecision.PASS
        )

    with pytest.raises(ValueError, match="different benchmark"):
        validate_workflow_evidence(candidate.model_copy(update={"run_id": "other"}), evidence)
    junit = Path(evidence.junit_path)
    original_junit = junit.read_bytes()
    junit.write_bytes(original_junit + b"\n<!-- changed evidence -->\n")
    with pytest.raises(ValueError, match="JUnit evidence is missing or changed"):
        validate_workflow_evidence(attached, evidence)
    junit.write_bytes(original_junit)
    suite.write_text("def test_approval_policy():\n    assert True  # changed source\n")
    with pytest.raises(ValueError, match="stale"):
        validate_workflow_evidence(attached, evidence)
    with pytest.raises(ValueError, match="stale"):
        capture_workflow_suite(candidate, tmp_path / "stale.junit.xml")


def test_executed_but_skipped_workflow_suite_cannot_clear_missing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = tmp_path / "tests" / "e2e" / "test_policy.py"
    suite.parent.mkdir(parents=True)
    suite.write_text(
        'import pytest\n@pytest.mark.skip(reason="not exercised")\n'
        "def test_approval_policy():\n    assert True\n"
    )
    monkeypatch.setattr(
        "quality_platform.evaluation.workflow_evidence.repository_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "quality_platform.evaluation.workflow_evidence.WORKFLOW_POLICY_SUITES",
        ("tests/e2e/test_policy.py",),
    )
    candidate = report(tmp_path)
    evidence = capture_workflow_suite(candidate, tmp_path / "skipped.junit.xml")
    assert evidence.exit_code == 0
    assert evidence.test_results[0].outcome == "skipped"
    assert validate_workflow_evidence(candidate, evidence) == ReleaseDecision.REVIEW_REQUIRED
    attached = attach_workflow_evidence(candidate, evidence)
    assert attached.decision == ReleaseDecision.REVIEW_REQUIRED
    assert "E2E_WORKFLOW_EVIDENCE_MISSING" in attached.readiness_labels


def test_capture_without_source_tests_cannot_claim_workflow_evidence(tmp_path: Path) -> None:
    candidate = report(tmp_path)
    junit = tmp_path / "missing-source.junit.xml"
    with pytest.raises(ValueError, match="requires the source checkout"):
        capture_workflow_suite(candidate, junit, root=tmp_path)
    assert not junit.exists()
