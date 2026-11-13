import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.evaluation.models import BenchmarkReport, CandidateSnapshot, ReleaseDecision
from quality_platform.storage.database import open_database
from quality_platform.web.app import create_app
from quality_platform.web.control import Control
from quality_platform.web.evaluation import EvaluationStore
from quality_platform.web.worker import run_worker


@pytest.fixture
def web(tmp_path: Path):
    model = ModelSpec(provider="openai", model_id="fake-offline")
    config = AppConfig(
        database_path=tmp_path / "domain.sqlite",
        workspace_root=tmp_path / "workspace",
        agent_models=AgentModels(
            orchestrator=model, analyst=model, planner=model, evaluator=model
        ),
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
        headers = {"X-CSRF-Token": client.cookies["qa_csrf"], "Idempotency-Key": "report-1", "Accept": "application/json"}
        yield client, control, headers


def test_report_upload_requires_auth_project_and_idempotent_content(web) -> None:
    client, control, headers = web
    report = BenchmarkReport(
        run_id="run-1",
        dataset_version="v0.1",
        rubric_id="v0.1",
        rubric_version="v0.1",
        candidate=CandidateSnapshot(
            candidate_id="candidate", commit_sha="abc", config_hash="abc",
            model_snapshot_id="abc", prompt_snapshot_id="abc"
        ),
        case_scores=(), eligible_count=0, excluded_count=0,
        overall_precision=0, overall_recall=0, decision=ReleaseDecision.REVIEW_REQUIRED,
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
    assert client.post(endpoint, data={**form, "project_id": "P2"}, files=files, headers=headers).status_code == 404
    first = client.post(endpoint, data=form, files=files, headers=headers)
    assert first.status_code == 202, first.text
    duplicate = client.post(endpoint, data=form, files=files, headers=headers)
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    changed = client.post(
        endpoint, data=form,
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
        candidate_id="candidate", commit_sha="abc", config_hash="abc",
        model_snapshot_id="abc", prompt_snapshot_id="abc"
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
