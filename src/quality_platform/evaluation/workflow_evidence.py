"""Executed offline workflow/policy suite evidence, never inferred from test existence."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from quality_platform.evaluation.models import (
    BenchmarkReport,
    ReleaseDecision,
    SuiteTestResult,
    WorkflowSuiteEvidence,
)
from quality_platform.resources import PACKAGE_ROOT, runtime_root

WORKFLOW_POLICY_SUITES = (
    "tests/e2e/test_web_workflows.py",
    "tests/e2e/test_resume_and_conflict.py",
    "tests/e2e/test_stlc_journey.py",
    "tests/integration/test_parallel_scopes.py",
    "tests/integration/test_scoped_approval.py",
    "tests/integration/test_checkpoints.py",
    "tests/integration/test_resume.py",
    "tests/unit/test_access_control.py",
    "tests/unit/test_policies.py",
)
MISSING_WORKFLOW_EVIDENCE = "E2E_WORKFLOW_EVIDENCE_MISSING"


def repository_root() -> Path:
    return runtime_root()


def source_manifest(root: Path) -> dict[str, str]:
    paths = [root / "pyproject.toml", root / "uv.lock"]
    for directory in (
        "src",
        "tests",
        "benchmarks/configs",
        "benchmarks/rubrics",
        "benchmarks/ground_truth",
        "benchmarks/datasets/synthetic",
    ):
        paths.extend(
            path
            for path in (root / directory).rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".md", ".json", ".yaml", ".toml"}
            and "__pycache__" not in path.parts
        )
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(paths))
        if path.is_file()
    }


def code_fingerprint(root: Path | None = None) -> str:
    selected = root or repository_root()
    if selected == PACKAGE_ROOT / "_data":
        # Hash installed code and resources, not an empty source manifest or .pyc caches.
        manifest = {
            path.relative_to(PACKAGE_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(PACKAGE_ROOT.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
    else:
        manifest = source_manifest(selected)
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def benchmark_binding(report: BenchmarkReport) -> str:
    # Attaching workflow evidence can change readiness/decision, but cannot change the run,
    # semantic scores, outputs, inputs, candidate, productivity or repeat evidence it certifies.
    payload = report.model_dump(
        mode="json",
        exclude={
            "workflow_suite_evidence",
            "readiness_labels",
            "decision",
            "policy_case_results",
        },
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _results(path: Path) -> tuple[SuiteTestResult, ...]:
    root = ElementTree.fromstring(path.read_bytes())
    return tuple(
        SuiteTestResult(
            test_id=f"{item.get('classname', '')}::{item.get('name', '')}",
            suite=item.get("file", "").replace("\\", "/"),
            outcome=(
                "failed"
                if item.find("failure") is not None
                else "error"
                if item.find("error") is not None
                else "skipped"
                if item.find("skipped") is not None
                else "passed"
            ),
            duration_seconds=float(item.get("time", "0")),
        )
        for item in root.iter("testcase")
    )


def capture_workflow_suite(
    report: BenchmarkReport,
    junit_path: Path,
    *,
    root: Path | None = None,
) -> WorkflowSuiteEvidence:
    root = (root or repository_root()).resolve()
    if not all((root / suite).is_file() for suite in WORKFLOW_POLICY_SUITES):
        raise ValueError(
            "Workflow evidence requires the source checkout and its test dependencies; "
            "it cannot be captured from an installed wheel. Run the benchmark and evidence "
            "suite together in the source checkout."
        )
    before = code_fingerprint(root)
    if not report.code_fingerprint or report.code_fingerprint != before:
        raise ValueError("candidate code fingerprint is stale or absent; rerun the benchmark")
    junit_path = junit_path.resolve()
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    if junit_path.exists():
        raise FileExistsError("workflow JUnit evidence already exists; use a new evidence path")
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *WORKFLOW_POLICY_SUITES,
        "-o",
        "junit_family=xunit1",
        f"--junitxml={junit_path}",
    )
    environment = os.environ.copy()
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "QA_TIER2_BUDGET_CONFIRMED",
        "QA_TIER3_BUDGET_CONFIRMED",
        "PYTEST_ADDOPTS",
    ):
        environment.pop(key, None)
    started = datetime.now(UTC)
    result = subprocess.run(
        command,
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    completed = datetime.now(UTC)
    manifest = source_manifest(root)
    after = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if before != after:
        raise ValueError("source changed during the workflow suite; evidence is stale, rerun")
    if not junit_path.is_file():
        raise ValueError("pytest produced no JUnit evidence; no pass can be recorded")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return WorkflowSuiteEvidence(
        benchmark_binding=benchmark_binding(report),
        source_fingerprint=after,
        source_manifest=manifest,
        commit_sha=commit.stdout.strip() if commit.returncode == 0 else "unavailable",
        started_at=started,
        completed_at=completed,
        command=command,
        suites=WORKFLOW_POLICY_SUITES,
        exit_code=result.returncode,
        test_results=_results(junit_path),
        junit_path=str(junit_path),
        junit_sha256=hashlib.sha256(junit_path.read_bytes()).hexdigest(),
    )


def validate_workflow_evidence(
    report: BenchmarkReport,
    evidence: WorkflowSuiteEvidence,
    *,
    root: Path | None = None,
) -> ReleaseDecision:
    root = root or repository_root()
    current_manifest = source_manifest(root)
    current = hashlib.sha256(json.dumps(current_manifest, sort_keys=True).encode()).hexdigest()
    if (
        evidence.source_manifest != current_manifest
        or evidence.source_fingerprint != current
        or report.code_fingerprint != current
    ):
        raise ValueError("workflow suite evidence is stale for the current source fingerprint")
    if evidence.benchmark_binding != benchmark_binding(report):
        raise ValueError("workflow suite evidence belongs to a different benchmark report")
    if evidence.suites != WORKFLOW_POLICY_SUITES:
        raise ValueError("workflow evidence did not execute the required policy suite manifest")
    expected_command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *WORKFLOW_POLICY_SUITES,
        "-o",
        "junit_family=xunit1",
        f"--junitxml={evidence.junit_path}",
    )
    if evidence.command != expected_command or evidence.completed_at < evidence.started_at:
        raise ValueError("workflow suite execution manifest is invalid")
    junit = Path(evidence.junit_path)
    if (
        not junit.is_file()
        or hashlib.sha256(junit.read_bytes()).hexdigest() != evidence.junit_sha256
    ):
        raise ValueError("workflow JUnit evidence is missing or changed")
    if _results(junit) != evidence.test_results:
        raise ValueError("workflow results differ from the executed JUnit evidence")
    if evidence.exit_code == 1 or any(
        item.outcome in {"failed", "error"} for item in evidence.test_results
    ):
        return ReleaseDecision.BLOCKED
    if (
        evidence.exit_code != 0
        or not evidence.test_results
        or any(item.outcome != "passed" for item in evidence.test_results)
        or not set(WORKFLOW_POLICY_SUITES).issubset(item.suite for item in evidence.test_results)
    ):
        return ReleaseDecision.REVIEW_REQUIRED
    executed = {item.test_id for item in evidence.test_results}
    if any(
        not ids or not set(ids).issubset(executed) for ids in report.policy_suite_tests.values()
    ):
        return ReleaseDecision.REVIEW_REQUIRED
    return ReleaseDecision.PASS


def attach_workflow_evidence(
    report: BenchmarkReport,
    evidence: WorkflowSuiteEvidence,
) -> BenchmarkReport:
    outcome = validate_workflow_evidence(report, evidence)
    labels = tuple(
        label
        for label in report.readiness_labels
        if label not in {MISSING_WORKFLOW_EVIDENCE, "POLICY_SUITE_EVIDENCE_MISSING"}
    )
    executed = {item.test_id: item.outcome for item in evidence.test_results}
    policy_results = {
        case_id: ReleaseDecision.BLOCKED
        if any(executed.get(test_id) in {"failed", "error"} for test_id in ids)
        else ReleaseDecision.PASS
        if ids and all(executed.get(test_id) == "passed" for test_id in ids)
        else ReleaseDecision.REVIEW_REQUIRED
        for case_id, ids in report.policy_suite_tests.items()
    }
    if any(result != ReleaseDecision.PASS for result in policy_results.values()):
        labels += ("POLICY_SUITE_EVIDENCE_MISSING",)
    decision = report.decision
    if outcome == ReleaseDecision.BLOCKED:
        decision = ReleaseDecision.BLOCKED
        labels += ("E2E_WORKFLOW_POLICY_FAILED",)
    elif outcome != ReleaseDecision.PASS:
        labels += (MISSING_WORKFLOW_EVIDENCE,)
        if decision != ReleaseDecision.BLOCKED:
            decision = ReleaseDecision.REVIEW_REQUIRED
    elif (
        decision == ReleaseDecision.REVIEW_REQUIRED
        and not labels
        and report.case_scores
        and all(
            not score.findings
            and not score.critical_violation
            and min(
                score.precision,
                score.recall,
                score.evidence_accuracy,
            )
            == 1
            for score in report.case_scores
        )
    ):
        decision = ReleaseDecision.PASS
    return report.model_copy(
        update={
            "workflow_suite_evidence": evidence,
            "readiness_labels": tuple(dict.fromkeys(labels)),
            "decision": decision,
            "policy_case_results": policy_results,
        }
    )
