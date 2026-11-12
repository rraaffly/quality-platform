"""HTTP security boundaries use the real app, SQLite sessions and worker."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from quality_platform.config.models import AppConfig
from quality_platform.storage.database import open_database
from quality_platform.web.app import MAX_FILE, create_app
from quality_platform.web.cli import app as cli
from quality_platform.web.control import Control
from quality_platform.web.worker import run_worker


@pytest.fixture
def local(tmp_path: Path) -> Any:
    config = AppConfig.model_validate(
        {
            "database_path": str(tmp_path / "qa.sqlite"),
            "workspace_root": str(tmp_path / "workspace"),
            "agent_models": {
                role: {"provider": "openai", "model_id": "offline-model"}
                for role in ("orchestrator", "analyst", "planner", "evaluator")
            },
        }
    )
    state = tmp_path / "web"
    control = Control(state)
    control.initialize(config, "owner", "private-test-password")
    open_database(config.database_path).dispose()
    with TestClient(create_app(state, testing=True)) as client:
        yield client, control


def login(client: TestClient) -> dict[str, str]:
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
    return {"X-CSRF-Token": client.cookies["qa_csrf"], "Idempotency-Key": "command"}


def test_login_session_rotation_expiry_logout_csrf_origin_and_rate_limit(local: Any) -> None:
    client, control = local
    assert client.get("/api/jobs/not-owned").status_code == 401
    assert client.get("/projects", follow_redirects=False).status_code == 303
    client.get("/login")
    old = client.cookies["qa_session"]
    csrf = client.cookies["qa_csrf"]
    client.get("/favicon.ico")
    assert client.cookies["qa_csrf"] == csrf
    assert (
        client.post(
            "/login", data={"owner": "owner", "password": "private-test-password"}
        ).status_code
        == 403
    )
    headers = login(client)
    assert client.cookies["qa_session"] != old
    assert control.session(old) is None
    command = {"kind": "create_project", "project_id": "P1", "name": "Project"}
    assert client.post("/api/commands", json=command).status_code == 403
    assert (
        client.post(
            "/api/commands", json=command, headers={**headers, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/commands", json={**command, "actor": "administrator"}, headers=headers
        ).status_code
        == 422
    )
    first = client.post("/api/commands", json=command, headers=headers)
    duplicate = client.post("/api/commands", json=command, headers=headers)
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    assert (
        client.post(
            "/api/commands", json={**command, "name": "Different"}, headers=headers
        ).status_code
        == 409
    )
    assert client.get("/projects", headers={"Host": "evil.test"}).status_code == 400
    assert client.post("/logout", headers=headers, follow_redirects=False).status_code == 303
    assert client.get("/api/jobs/anything").status_code == 401
    client.get("/login")
    csrf = client.cookies["qa_csrf"]
    for _ in range(8):
        assert (
            client.post(
                "/login", data={"owner": "owner", "password": "wrong", "csrf": csrf}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/login", data={"owner": "owner", "password": "private-test-password", "csrf": csrf}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "filename,raw,status",
    [
        ("../escape.md", b"# no", 422),
        ("dir\\escape.md", b"# no", 422),
        ("document.pdf", b"text", 422),
        ("binary.md", b"\xff", 422),
        ("binary.md", b"a\x00b", 422),
        ("too-big.md", b"a" * (MAX_FILE + 1), 413),
    ],
)
def test_upload_validation_does_not_enqueue_or_call_a_model(
    local: Any, filename: str, raw: bytes, status: int
) -> None:
    client, control = local
    headers = login(client)
    for command in (
        {"kind": "create_project", "project_id": "P1", "name": "Project"},
        {
            "kind": "create_workflow",
            "project_id": "P1",
            "workflow_id": "W1",
            "goal": "Goal",
            "settings_id": control.settings_id(),
        },
    ):
        response = client.post("/api/commands", json=command, headers=headers)
        assert response.status_code == 202
        asyncio.run(run_worker(control.state, once=True))
    count = len(control.jobs())
    response = client.post(
        "/api/projects/P1/workflows/W1/sources",
        files={"files": (filename, raw, "text/markdown")},
        headers=headers,
    )
    assert response.status_code == status, response.text
    assert len(control.jobs()) == count


def test_model_confirmation_secret_redaction_and_cross_project_sources(
    local: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, control = local
    headers = login(client)
    monkeypatch.setenv("OPENAI_API_KEY", "never-display-this-key")
    assert "never-display-this-key" not in client.get("/settings").text
    assert "Configured" in client.get("/settings").text
    for command in (
        {"kind": "create_project", "project_id": "P1", "name": "Project"},
        {
            "kind": "create_workflow",
            "project_id": "P1",
            "workflow_id": "W1",
            "goal": "Goal",
            "settings_id": control.settings_id(),
        },
    ):
        assert client.post("/api/commands", json=command, headers=headers).status_code == 202
        asyncio.run(run_worker(control.state, once=True))
    payload = {
        "kind": "run",
        "project_id": "P1",
        "workflow_id": "W1",
        "request_id": "r1",
        "instruction": "Analyze",
    }
    assert client.post("/api/commands", json=payload, headers=headers).status_code == 422
    assert client.get("/api/projects/P2/workflows/W1").status_code == 403
    assert client.get("/projects/P1/workflows/W1/sources/not-a-source").status_code == 403
    assert (
        client.post("/api/settings", json={"api_key": "forbidden"}, headers=headers).status_code
        == 422
    )
    response = client.post("/api/commands", json={**payload, "confirm_cost": True}, headers=headers)
    assert response.status_code == 202

    def unavailable(spec: Any) -> Any:
        raise RuntimeError("never-display-this-key")

    asyncio.run(run_worker(control.state, once=True, model_factory=unavailable))
    job = client.get(response.json()["status_url"])
    assert job.json()["status"] == "FAILED"
    assert "never-display-this-key" not in job.text
    response = client.get(f"/jobs/{response.json()['job_id']}")
    assert response.status_code == 200 and "never-display-this-key" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_html_forms_work_without_javascript_and_do_not_render_untrusted_html(local: Any) -> None:
    client, control = local
    login(client)
    response = client.post(
        "/commands",
        data={
            "csrf": client.cookies["qa_csrf"],
            "command_key": "form",
            "kind": "create_project",
            "project_id": "P1",
            "name": '<script>alert("xss")</script>',
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    asyncio.run(run_worker(control.state, once=True))
    page = client.get("/projects")
    assert '<script>alert("xss")</script>' not in page.text
    assert "&lt;script&gt;" in page.text
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/../control.sqlite").status_code in {404, 403}


def test_remote_binding_requires_https_and_init_never_accepts_password_argument(local: Any) -> None:
    _, control = local
    result = CliRunner().invoke(cli, ["serve", "--state", str(control.state), "--host", "0.0.0.0"])
    assert result.exit_code != 0 and "public-origin" in result.output
    for origin in (
        "http://qa.example.com",
        "https://user:pass@qa.example.com",
        "https://qa.example.com/path",
    ):
        with pytest.raises(ValueError):
            create_app(control.state, public_origin=origin)
    assert "--password" not in CliRunner().invoke(cli, ["init", "--help"]).output
    with TestClient(
        create_app(control.state, public_origin="https://qa.example.com"),
        base_url="https://qa.example.com",
    ) as client:
        response = client.get("/login")
        assert all(
            "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
            for cookie in response.headers.get_list("set-cookie")
        )


def test_expired_session_and_read_only_report_registration(local: Any, tmp_path: Path) -> None:
    client, control = local
    headers = login(client)
    response = client.post(
        "/api/commands",
        json={"kind": "create_project", "project_id": "P1", "name": "Demo"},
        headers=headers,
    )
    assert response.status_code == 202
    asyncio.run(run_worker(control.state, once=True))
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "run_id": "offline-example",
                "dataset_version": "synthetic-v1",
                "rubric_id": "qa",
                "rubric_version": "v1",
                "candidate": {
                    "candidate_id": "offline",
                    "commit_sha": "local",
                    "config_hash": "config",
                    "model_snapshot_id": "fake",
                    "prompt_snapshot_id": "prompt",
                },
                "case_scores": [],
                "eligible_count": 0,
                "excluded_count": 1,
                "overall_precision": 0.0,
                "overall_recall": 0.0,
                "decision": "REVIEW_REQUIRED",
                "readiness_labels": ["ILLUSTRATIVE_GATES"],
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        [
            "register-report",
            "--state",
            str(control.state),
            "--project-id",
            "P1",
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    page = client.get("/reports")
    assert "offline-example" in page.text and "REVIEW_REQUIRED" in page.text
    assert "overall_precision" in page.text and "ILLUSTRATIVE_GATES" in page.text
    assert client.post("/reports", headers=headers).status_code == 405
    with control.connect() as db:
        db.execute("UPDATE sessions SET expires=0")
    assert client.get("/api/jobs/anything").status_code == 401
