"""HTTP security boundaries use the real app, SQLite sessions and worker."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from quality_platform.config.models import AppConfig, LocalConfig
from quality_platform.storage.database import open_database
from quality_platform.web.app import MAX_FILE, create_app
from quality_platform.web.cli import app as cli
from quality_platform.web.control import DEFAULT_PASSWORD, Control
from quality_platform.web.queries import reader
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
    guest = client.get("/api/session").json()
    assert guest["actor"] is None and guest["csrf"] == client.cookies["qa_csrf"]
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
    assert client.get("/api/jobs").json()["jobs"][0]["id"] == first.json()["job_id"]
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
    settings = client.get("/api/settings")
    assert "never-display-this-key" not in settings.text
    assert settings.json()["credentials"]["openai"] is True
    assert "database_path" not in settings.text
    assert settings.json()["workspace_root"] == str(control.config().workspace_root)
    assert "workspace_root" not in settings.json()["settings"]
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
    assert str(control.config().workspace_root) not in client.get("/api/jobs").text

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


def test_react_shell_and_json_project_projection_do_not_render_untrusted_html(local: Any) -> None:
    client, control = local
    login(client)
    response = client.post(
        "/api/commands",
        json={
            "kind": "create_project",
            "project_id": "P1",
            "name": '<script>alert("xss")</script>',
        },
        headers={"X-CSRF-Token": client.cookies["qa_csrf"], "Idempotency-Key": "project"},
    )
    assert response.status_code == 202
    asyncio.run(run_worker(control.state, once=True))
    page = client.get("/workspace")
    assert page.status_code == 200
    assert '<script>alert("xss")</script>' not in page.text
    assert (
        '<script>alert("xss")</script>' in client.get("/api/projects").json()["projects"][0]["name"]
    )
    assert "database_path" not in client.get("/api/projects").text
    assert client.get("/static/htmx.min.js").status_code == 404
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
    result = client.post(
        "/api/evaluation/jobs",
        data={"kind": "register_report", "project_id": "P1"},
        files={"file": ("report.json", report.read_bytes(), "application/json")},
        headers={**headers, "Accept": "application/json", "Idempotency-Key": "report-upload"},
    )
    assert result.status_code == 202, result.text
    asyncio.run(run_worker(control.state, once=True))
    page = client.get("/api/reports")
    assert page.status_code == 200
    report = page.json()["reports"][0]["report"]
    assert report["run_id"] == "offline-example" and report["decision"] == "REVIEW_REQUIRED"
    assert "overall_precision" in report and "ILLUSTRATIVE_GATES" in report["readiness_labels"]
    assert client.post("/reports", headers=headers).status_code == 405
    with control.connect() as db:
        db.execute("UPDATE sessions SET expires=0")
    assert client.get("/api/jobs/anything").status_code == 401


def test_initial_password_account_changes_and_remote_guard(tmp_path: Path) -> None:
    state = tmp_path / "state"
    control = Control(state)
    control.initialize(
        LocalConfig(database_path=tmp_path / "domain.sqlite", workspace_root=tmp_path / "workspace")
    )
    with pytest.raises(ValueError, match="initial password"):
        create_app(state, public_origin="https://qa.example.com")
    app = create_app(state, testing=True)
    with TestClient(app, client=("198.51.100.7", 443)) as remote:
        assert (
            remote.get("/api/session", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        )
    with TestClient(app) as client, TestClient(app) as other:
        tokens = []
        for browser in (client, other):
            guest = browser.get("/api/session").json()
            assert guest["default_password"] is True
            response = browser.post(
                "/login",
                json={"password": DEFAULT_PASSWORD},
                headers={"X-CSRF-Token": guest["csrf"]},
            )
            assert response.status_code == 200
            tokens.append(browser.cookies["qa_session"])
        headers = {"X-CSRF-Token": client.cookies["qa_csrf"]}
        values = {"display_name": "New owner", "current_password": DEFAULT_PASSWORD}
        assert client.post("/api/account", json=values).status_code == 403
        assert (
            client.post(
                "/api/account", json={**values, "current_password": "wrong"}, headers=headers
            ).status_code
            == 403
        )
        for password in (DEFAULT_PASSWORD, "elevenchars"):
            rejected = client.post(
                "/api/account", json={**values, "new_password": password}, headers=headers
            )
            assert rejected.status_code == 422
        assert client.get("/api/settings").json()["account"]["display_name"] == "Owner"
        saved = client.post("/api/account", json=values, headers=headers)
        assert saved.json() == {"logged_out": False}
        session = client.get("/api/session").json()
        assert session["display_name"] == "New owner" and session["actor"] == "Owner"
        assert session["default_password"] is True
        assert all(control.session(token) for token in tokens)
        replacement = "twelve-chars"
        rotated = client.post(
            "/api/account", json={**values, "new_password": replacement}, headers=headers
        )
        assert rotated.json() == {"logged_out": True}
        assert all(control.session(token) is None for token in tokens)
        assert other.get("/api/settings").status_code == 401
        assert client.get("/api/settings").status_code == 401
        guest = client.get("/api/session").json()
        assert guest["default_password"] is False
        headers = {"X-CSRF-Token": guest["csrf"]}
        assert (
            client.post("/login", json={"password": DEFAULT_PASSWORD}, headers=headers).status_code
            == 401
        )
        assert (
            client.post("/login", json={"password": replacement}, headers=headers).status_code
            == 200
        )
        settings = client.get("/api/settings")
        assert settings.json()["account"] == {
            "display_name": "New owner",
            "default_password": False,
        }
        assert replacement not in settings.text and "password_hash" not in settings.text
    with TestClient(
        create_app(state, public_origin="https://qa.example.com"),
        base_url="https://qa.example.com",
        client=("198.51.100.7", 443),
    ) as remote:
        guest = remote.get("/api/session").json()
        response = remote.post(
            "/login", json={"password": replacement}, headers={"X-CSRF-Token": guest["csrf"]}
        )
        assert response.status_code == 200
        assert remote.get("/api/settings").status_code == 200


def test_workspace_changes_only_affect_new_projects(local: Any, tmp_path: Path) -> None:
    client, control = local
    headers = login(client)
    initial = control.settings_id()
    original_workspace = control.config().workspace_root
    command = {"kind": "create_project", "project_id": "old", "name": "Original project"}
    first = client.post("/api/commands", json=command, headers=headers)
    assert first.status_code == 202
    asyncio.run(run_worker(control.state, once=True))
    assert control.job(first.json()["job_id"])["status"] == "SUCCEEDED"
    for value in ("", "relative/path", str(control.path)):
        assert (
            client.post("/api/workspace", json={"path": value}, headers=headers).status_code == 422
        )
        assert control.settings_id() == initial
    new_workspace = tmp_path / "new workspace"
    headers["Idempotency-Key"] = "workspace"
    changed = client.post("/api/workspace", json={"path": str(new_workspace)}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert new_workspace.is_dir()
    duplicate = client.post("/api/workspace", json={"path": str(new_workspace)}, headers=headers)
    assert duplicate.json() == changed.json()
    assert control.config(initial).workspace_root == original_workspace
    assert client.get("/api/settings").json()["workspace_root"] == str(new_workspace)
    assert (
        client.post(
            "/api/workspace", json={"path": str(tmp_path / "different")}, headers=headers
        ).status_code
        == 409
    )
    for project in ("old", "new"):
        headers["Idempotency-Key"] = project
        submitted = client.post(
            "/api/commands",
            json={
                "kind": "create_project",
                "project_id": project,
                "name": "Original project" if project == "old" else "New project",
            },
            headers=headers,
        )
        assert submitted.status_code == 202
        asyncio.run(run_worker(control.state, once=True))
        assert control.job(submitted.json()["job_id"])["status"] == "SUCCEEDED"
        headers["Idempotency-Key"] = project + "-workflow"
        workflow = client.post(
            "/api/commands",
            json={
                "kind": "create_workflow",
                "project_id": project,
                "workflow_id": project + "-review",
                "goal": "Review",
                "settings_id": changed.json()["settings_id"],
            },
            headers=headers,
        )
        assert workflow.status_code == 202
        asyncio.run(run_worker(control.state, once=True))
        assert control.job(workflow.json()["job_id"])["status"] == "SUCCEEDED"
        headers["Idempotency-Key"] = project + "-import"
        upload = client.post(
            f"/api/projects/{project}/workflows/{project}-review/sources",
            files={
                "files": (
                    "requirements.md",
                    b"# Login\nUsers sign in with a password.\n",
                    "text/markdown",
                )
            },
            headers={**headers, "Accept": "application/json"},
        )
        assert upload.status_code == 202, upload.text
        asyncio.run(run_worker(control.state, once=True))
        assert control.job(upload.json()["job_id"])["status"] == "SUCCEEDED"
        with reader(control.config().database_path) as repo:
            record = repo.read_project_record(project, project)
            expected = original_workspace if project == "old" else new_workspace
            assert record.root == expected / project
            pinned = [
                json.loads(event.payload)
                for event in repo.list_decisions(project)
                if event.event_type == "workflow_config"
            ]
            assert pinned[0]["workspace_root"] == str(expected)
        source = client.get(f"/api/projects/{project}/workflows/{project}-review").json()[
            "sources"
        ][0]
        assert source["name"] == "requirements.md"
    assert not (new_workspace / "old").exists()
    assert (original_workspace / "old").is_dir()
