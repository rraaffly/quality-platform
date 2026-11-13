import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.evaluation.models import (
    BenchmarkCase,
    BenchmarkReport,
    CandidateSnapshot,
    Dataset,
    EvaluationOutput,
    EvaluatorAssessment,
    ExpectedItem,
    GoldRecord,
    GoldStatus,
    ObservedItem,
    ReleaseDecision,
    Severity,
    TaskType,
)
from quality_platform.evaluation.reporting import write_report
from quality_platform.evaluation.runner import run_offline_suite
from quality_platform.storage.database import open_database
from quality_platform.web.app import create_app
from quality_platform.web.control import Control
from quality_platform.web.evaluation import EvaluationStore, register_report
from quality_platform.web.worker import run_worker


@pytest.fixture
def web(tmp_path: Path):
    model = ModelSpec(provider="openai", model_id="fake-offline")
    config = AppConfig(
        database_path=tmp_path / "domain.sqlite",
        workspace_root=tmp_path / "workspace",
        agent_models=AgentModels(orchestrator=model, analyst=model, planner=model, evaluator=model),
    )
    control = Control(tmp_path / "state")
    control.initialize(config, "owner", "private-test-password")
    open_database(config.database_path).dispose()
    with TestClient(create_app(control.state, testing=True)) as client:
        client.get("/login")
        response = client.post(
            "/login",
            data={
                "owner": "owner",
                "password": "private-test-password",
                "csrf": client.cookies["qa_csrf"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        headers = {
            "X-CSRF-Token": client.cookies["qa_csrf"],
            "Idempotency-Key": "report-1",
            "Accept": "application/json",
        }
        yield client, control, headers


def test_report_upload_requires_auth_project_and_idempotent_content(web) -> None:
    client, control, headers = web
    report = BenchmarkReport(
        run_id="run-1",
        dataset_version="v0.1",
        rubric_id="v0.1",
        rubric_version="v0.1",
        candidate=CandidateSnapshot(
            candidate_id="candidate",
            commit_sha="abc",
            config_hash="abc",
            model_snapshot_id="abc",
            prompt_snapshot_id="abc",
        ),
        case_scores=(),
        eligible_count=0,
        excluded_count=0,
        overall_precision=0,
        overall_recall=0,
        decision=ReleaseDecision.REVIEW_REQUIRED,
    )
    payload = report.model_dump_json().encode()
    client.post(
        "/api/commands",
        json={"kind": "create_project", "project_id": "P1", "name": "Project"},
        headers={**headers, "Idempotency-Key": "project"},
    )
    asyncio.run(run_worker(control.state, once=True))
    endpoint = "/api/evaluation/jobs"
    form = {"kind": "register_report", "project_id": "P1"}
    files = {"file": ("report.json", payload, "application/json")}
    assert client.post(endpoint, data=form, files=files).status_code == 403
    assert (
        client.post(
            endpoint, data={**form, "project_id": "P2"}, files=files, headers=headers
        ).status_code
        == 404
    )
    first = client.post(endpoint, data=form, files=files, headers=headers)
    assert first.status_code == 202, first.text
    duplicate = client.post(endpoint, data=form, files=files, headers=headers)
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    changed = client.post(
        endpoint,
        data=form,
        files={"file": ("report.json", payload + b" ", "application/json")},
        headers=headers,
    )
    assert changed.status_code == 409
    asyncio.run(run_worker(control.state, once=True))
    result = client.get(f"/api/evaluation/jobs/{first.json()['job_id']}")
    assert result.status_code == 200
    assert result.json()["status"] == "SUCCEEDED"
    assert result.json()["result"]["run_id"] == "run-1"
    assert "review_packet_path" not in result.text
    assert client.get("/api/reports").json()["reports"][0]["report"]["run_id"] == "run-1"


def test_evaluation_store_rejects_changed_record_and_path_like_identity(tmp_path: Path) -> None:
    store = EvaluationStore(tmp_path)
    record = CandidateSnapshot(
        candidate_id="candidate",
        commit_sha="abc",
        config_hash="abc",
        model_snapshot_id="abc",
        prompt_snapshot_id="abc",
    )
    identity = store.save("candidate", record)
    assert store.save("candidate", record) == identity
    assert store.load(identity, CandidateSnapshot) == record
    with pytest.raises(ValueError, match="Invalid evaluation record ID"):
        store.load("../outside", CandidateSnapshot)
    path = store.root / f"{identity}.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.load(identity, CandidateSnapshot)


def test_benchmark_run_requires_explicit_budget_and_queues_exact_settings(web) -> None:
    client, control, headers = web
    project = client.post(
        "/api/commands",
        json={"kind": "create_project", "project_id": "P1", "name": "Project"},
        headers={**headers, "Idempotency-Key": "project"},
    )
    assert project.status_code == 202
    asyncio.run(run_worker(control.state, once=True))
    body = {
        "kind": "run_benchmark",
        "project_id": "P1",
        "settings_id": control.settings_id(),
        "tier": 2,
        "dataset_version": "v0.1",
        "agent_role": "analyst",
        "max_cost_usd": 5.0,
        "repeats": 1,
        "rates": {
            role: {"input_usd_per_million": 1.0, "output_usd_per_million": 1.0}
            for role in ("analyst", "planner", "evaluator")
        },
        "confirm_cost": False,
    }
    endpoint = "/api/evaluation/jobs"
    assert client.post(endpoint, json=body, headers=headers).status_code == 422
    assert len(control.jobs()) == 1
    accepted = client.post(endpoint, json={**body, "confirm_cost": True}, headers=headers)
    assert accepted.status_code == 202, accepted.text
    duplicate = client.post(endpoint, json={**body, "confirm_cost": True}, headers=headers)
    assert duplicate.json()["job_id"] == accepted.json()["job_id"]
    assert (
        client.post(
            endpoint, json={**body, "confirm_cost": True, "max_cost_usd": 4.0}, headers=headers
        ).status_code
        == 409
    )
    stored = control.job(accepted.json()["job_id"])
    assert "fake-offline" not in stored["payload"]
    assert "OPENAI_API_KEY" not in stored["payload"]


def test_benchmark_worker_persists_once_and_restores_environment(web, monkeypatch) -> None:
    client, control, headers = web
    client.post(
        "/api/commands",
        json={"kind": "create_project", "project_id": "P1", "name": "Project"},
        headers={**headers, "Idempotency-Key": "project"},
    )
    asyncio.run(run_worker(control.state, once=True))
    body = {
        "kind": "run_benchmark",
        "project_id": "P1",
        "settings_id": control.settings_id(),
        "tier": 2,
        "dataset_version": "v0.1",
        "agent_role": "analyst",
        "max_cost_usd": 5.0,
        "repeats": 1,
        "rates": {
            role: {"input_usd_per_million": 1.0, "output_usd_per_million": 2.0}
            for role in ("analyst", "planner", "evaluator")
        },
        "confirm_cost": True,
    }
    queued = client.post("/api/evaluation/jobs", json=body, headers=headers)
    assert queued.status_code == 202
    calls = []

    async def fake_run(version, role, max_cases):
        calls.append((version, role.value, max_cases, os.environ["QA_TIER2_MAX_COST_USD"]))
        assert os.environ["QA_TIER2_BUDGET_CONFIRMED"] == "yes"
        assert os.environ["QA_TIER2_EVALUATOR_INPUT_USD_PER_MILLION"] == "1.0"
        assert Path(os.environ["QUALITY_PLATFORM_CONFIG"]).is_file()
        return BenchmarkReport(
            run_id="web-run",
            dataset_version=version,
            rubric_id="v0.1",
            rubric_version="v0.1",
            candidate=CandidateSnapshot(
                candidate_id="candidate",
                commit_sha="abc",
                config_hash="abc",
                model_snapshot_id="abc",
                prompt_snapshot_id="abc",
            ),
            case_scores=(),
            eligible_count=0,
            excluded_count=0,
            overall_precision=0,
            overall_recall=0,
            decision=ReleaseDecision.REVIEW_REQUIRED,
        )

    monkeypatch.setattr(
        "quality_platform.web.evaluation.run_live_benchmark", fake_run, raising=False
    )
    asyncio.run(run_worker(control.state, once=True))
    result = client.get(f"/api/evaluation/jobs/{queued.json()['job_id']}")
    assert result.json()["status"] == "SUCCEEDED"
    assert result.json()["result"]["run_id"] == "web-run"
    assert len(client.get("/api/reports").json()["reports"]) == 1
    assert calls == [("v0.1", "analyst", None, "5.0")]
    assert "QA_TIER2_BUDGET_CONFIRMED" not in os.environ
    with control.connect() as db:
        db.execute("UPDATE jobs SET status='INTERRUPTED' WHERE id=?", (queued.json()["job_id"],))
    control.retry(queued.json()["job_id"])
    asyncio.run(run_worker(control.state, once=True))
    assert calls == [("v0.1", "analyst", None, "5.0")]


def test_blind_web_review_precedes_calibration(web) -> None:
    client, control, headers = web
    client.post(
        "/api/commands",
        json={"kind": "create_project", "project_id": "P1", "name": "Project"},
        headers={**headers, "Idempotency-Key": "project"},
    )
    asyncio.run(run_worker(control.state, once=True))
    case = BenchmarkCase(
        case_id="case-1",
        task_type=TaskType.ANALYST,
        input_snapshot_ids=("doc",),
        severity=Severity.HIGH,
        gold=GoldRecord(
            status=GoldStatus.APPROVED,
            approved_by="QA",
            approved_at=datetime.now(UTC),
            expected_items=(
                ExpectedItem(
                    id="R1", kind="REQUIREMENT", semantic_definition="A user can sign in."
                ),
            ),
        ),
    )
    output = EvaluationOutput(
        observed_items=(ObservedItem(id="R1", kind="REQUIREMENT", content="A user can sign in."),),
        evaluator_assessment=EvaluatorAssessment(
            case_id="case-1",
            model_snapshot_id="fake-evaluator",
            rating=5,
            critical=False,
            uncertainty=0,
            judgments=(),
        ),
    )
    report = run_offline_suite(
        Dataset(version="v1", rubric_version="v1", cases=(case,)),
        lambda _case: output,
        candidate=CandidateSnapshot(
            candidate_id="fake",
            commit_sha="abc",
            config_hash="abc",
            model_snapshot_id="abc",
            prompt_snapshot_id="abc",
        ),
    )
    report_path, _ = write_report(report, control.state / "evaluation" / "runs")
    persisted = BenchmarkReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    report_id = register_report(control, "P1", persisted, source="web")["report_id"]
    packet = client.get(f"/api/projects/P1/evaluation/{report_id}/review-packet")
    assert packet.status_code == 200
    assert "A user can sign in." in packet.text
    assert "evaluator_assessment" not in packet.text
    review = {
        "case_id": "case-1",
        "rating": 5,
        "critical": False,
        "reviewer": "QA",
        "reviewed_at": datetime.now(UTC).isoformat(),
        "blind": True,
        "model_snapshot_id": None,
        "uncertainty": 0,
    }
    action = {
        "kind": "evaluation_action",
        "action": "capture_review",
        "project_id": "P1",
        "report_id": report_id,
        "reviews": [review],
        "confirm_independent": True,
    }
    queued = client.post(
        "/api/evaluation/jobs", json=action, headers={**headers, "Idempotency-Key": "review"}
    )
    assert queued.status_code == 202, queued.text
    asyncio.run(run_worker(control.state, once=True))
    human_id = client.get(f"/api/evaluation/jobs/{queued.json()['job_id']}").json()["result"][
        "human_id"
    ]
    calibrated = client.post(
        "/api/evaluation/jobs",
        json={
            "kind": "evaluation_action",
            "action": "calibrate",
            "project_id": "P1",
            "report_id": report_id,
            "human_id": human_id,
        },
        headers={**headers, "Idempotency-Key": "calibrate"},
    )
    assert calibrated.status_code == 202
    asyncio.run(run_worker(control.state, once=True))
    result = client.get(f"/api/evaluation/jobs/{calibrated.json()['job_id']}").json()
    assert result["status"] == "SUCCEEDED"
    assert result["result"]["sample_size"] == 1
