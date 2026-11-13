"""Public HTTP/worker contracts; every model call is a deterministic offline fake."""

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quality_platform.agents.analyst import RequirementAnalysisDraft
from quality_platform.agents.orchestrator import ProposedTask, TaskAction, TaskProposal
from quality_platform.agents.planner import TestPlanDraft
from quality_platform.config.models import AppConfig
from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    Requirement,
    RequirementReadiness,
    SourceReference,
)
from quality_platform.domain.schemas import TestCase, TestCaseSet, TestStep
from quality_platform.services.scopes import ScopeProposal, ScopeProposalSet
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories
from quality_platform.web.app import create_app
from quality_platform.web.control import Control
from quality_platform.web.worker import dispatch, run_worker

PASSWORD = "offline-test-password-only"


class WebJourney:
    def __init__(self, root: Path):
        self.config = AppConfig.model_validate(
            {
                "database_path": str(root / "qa.sqlite"),
                "workspace_root": str(root / "workspace"),
                "agent_models": {
                    role: {"provider": "openai", "model_id": f"offline-{role}"}
                    for role in ("orchestrator", "analyst", "planner", "evaluator")
                },
            }
        )
        self.state = root / "state"
        self.control = Control(self.state)
        self.control.initialize(self.config, "qa-owner", PASSWORD)
        self.engine = open_database(self.config.database_path)
        self.repo = QaRepositories(self.engine)
        self.client = TestClient(create_app(self.state, testing=True))
        self.calls: list[str] = []
        self.fail_cases = False
        self.login()

    def login(self) -> None:
        self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "owner": "qa-owner",
                "password": PASSWORD,
                "csrf": self.client.cookies["qa_csrf"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text

    def headers(self, key: str = "test-command") -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies["qa_csrf"], "Idempotency-Key": key}

    def submit(self, kind: str, key: str | None = None, **values: Any) -> dict[str, Any]:
        command = {"kind": kind, "project_id": "P1", **values}
        if kind != "create_project":
            command.setdefault("workflow_id", "W1")
        if kind in {"run", "resume", "revision", "scope_proposal"}:
            command.setdefault("confirm_cost", True)
        if kind in {"scope", "baseline", "submit", "decide", "answer", "apply"}:
            command.setdefault("state_hash", self.view()["state_hash"])
        response = self.client.post(
            "/api/commands",
            json=command,
            headers=self.headers(key or f"{kind}-{len(self.control.jobs())}"),
        )
        assert response.status_code == 202, response.text
        return response.json()

    def process(self, identity: str, *, factory: Any = None) -> dict[str, Any]:
        asyncio.run(run_worker(self.state, once=True, model_factory=factory or self.factory))
        return self.client.get(f"/api/jobs/{identity}").json()

    def perform(self, kind: str, **values: Any) -> dict[str, Any]:
        job = self.submit(kind, **values)
        result = self.process(job["job_id"])
        assert result["status"] == "SUCCEEDED", result
        return result

    def setup_workflow(self) -> None:
        self.perform("create_project", name="Customer authentication")
        self.perform(
            "create_workflow", goal="Review login and MFA", settings_id=self.control.settings_id()
        )

    def upload(self) -> dict[str, Any]:
        response = self.client.post(
            "/api/projects/P1/workflows/W1/sources",
            files=[
                (
                    "files",
                    (
                        "brd.md",
                        b"# BRD\n\nThe account locks after 3 failed attempts.\n",
                        "text/markdown",
                    ),
                ),
                (
                    "files",
                    (
                        "srs.md",
                        b"# SRS\n\nThe account locks after 5 failed attempts.\n",
                        "text/markdown",
                    ),
                ),
                ("files", ("prd.md", b"# PRD\n\nUsers can enable TOTP.\n", "text/markdown")),
            ],
            headers={**self.headers("upload"), "Accept": "application/json"},
            follow_redirects=False,
        )
        assert response.status_code == 202, response.text
        identity = response.headers["Location"].split("/")[-1]
        return self.process(identity)

    def view(self) -> dict[str, Any]:
        response = self.client.get("/api/projects/P1/workflows/W1")
        assert response.status_code == 200, response.text
        return response.json()

    def factory(self, spec: Any) -> Any:
        journey = self

        class Model:
            def with_structured_output(self, schema: Any) -> Any:
                class Structured:
                    async def ainvoke(self, messages: Any) -> Any:
                        journey.calls.append(schema.__name__)
                        if schema is TaskProposal:
                            return TaskProposal(
                                project_id="P1",
                                workflow_id="W1",
                                tasks=tuple(
                                    ProposedTask(
                                        id=identity,
                                        action=action,
                                        project_id="P1",
                                        workflow_id="W1",
                                        target_id=target,
                                        dependency_ids=dependencies,
                                    )
                                    for identity, action, target, dependencies in (
                                        ("analysis", TaskAction.ANALYZE_CONFLICTS, None, ()),
                                        ("plan", TaskAction.DRAFT_TEST_PLAN, "ALL", ("analysis",)),
                                        (
                                            "cases",
                                            TaskAction.DRAFT_TEST_CASES,
                                            "ALL",
                                            ("analysis",),
                                        ),
                                    )
                                ),
                            )
                        source_ids = journey.repo.read_workflow("P1", "W1").source_snapshot_ids
                        refs = tuple(
                            SourceReference(document_id=identity, start_line=3, end_line=3)
                            for identity in source_ids
                        )
                        if schema is RequirementAnalysisDraft:
                            return RequirementAnalysisDraft(
                                requirements=(
                                    Requirement(
                                        id="R-LOCK",
                                        version=1,
                                        statement="Lockout threshold is unresolved.",
                                        source_references=refs[:2],
                                        readiness=RequirementReadiness.BLOCKED,
                                    ),
                                    Requirement(
                                        id="R-MFA",
                                        version=1,
                                        statement="Users can enable TOTP.",
                                        source_references=(refs[2],),
                                        source_quotes=("Users can enable TOTP.",),
                                        readiness=RequirementReadiness.VALIDATED,
                                    ),
                                ),
                                clarifications=(
                                    Clarification(
                                        id="C-LOCK",
                                        issue_type=ClarificationIssueType.CROSS_DOCUMENT_CONFLICT,
                                        question="Lock after 3 or 5 attempts?",
                                        source_references=refs[:2],
                                        affected_requirement_ids=("R-LOCK",),
                                    ),
                                ),
                            )
                        if schema is ScopeProposalSet:
                            return ScopeProposalSet(
                                proposals=(
                                    ScopeProposal(
                                        id="LOGIN",
                                        journey="Account lockout",
                                        requirement_ids=("R-LOCK",),
                                    ),
                                    ScopeProposal(
                                        id="MFA",
                                        journey="TOTP enrollment",
                                        requirement_ids=("R-MFA",),
                                    ),
                                )
                            )
                        if schema is TestPlanDraft:
                            return TestPlanDraft(
                                scope_id="MFA",
                                objective="Verify TOTP enrollment",
                                requirement_ids=("R-MFA",),
                                in_scope=("TOTP",),
                                test_types=("FUNCTIONAL",),
                                entry_criteria=("Evidence reviewed",),
                                exit_criteria=("Enrollment covered",),
                            )
                        if schema is TestCaseSet:
                            if journey.fail_cases:
                                journey.fail_cases = False
                                raise RuntimeError(
                                    "offline provider failure with secret that must not leak"
                                )
                            return TestCaseSet(
                                cases=tuple(
                                    TestCase(
                                        id=identity,
                                        title=f"Enroll TOTP {identity}",
                                        intent="Verify enrollment",
                                        requirement_ids=("R-MFA",),
                                        scenario="Enable TOTP",
                                        steps=(
                                            TestStep(
                                                action="Enable TOTP",
                                                expected_result="Users can enable TOTP.",
                                                expected_result_sources=(refs[2],),
                                            ),
                                        ),
                                    )
                                    for identity in ("TC-011", "TC-012")
                                )
                            )
                        if schema is TestCase:
                            artifact = next(
                                item
                                for item in journey.view()["artifacts"]
                                if item["kind"] == "TEST_CASES"
                            )
                            root = journey.repo.read_project_record("P1", "P1").root
                            data = TestCaseSet.model_validate_json(
                                (
                                    root
                                    / "artifacts"
                                    / artifact["id"]
                                    / f"v{artifact['version']}"
                                    / "test-cases.json"
                                ).read_text()
                            )
                            return data.cases[1].model_copy(
                                update={"title": "Enroll with a valid authenticator"}
                            )
                        raise AssertionError(f"Unexpected schema {schema}")

                return Structured()

        return Model()


@pytest.fixture
def web(tmp_path: Path) -> Any:
    journey = WebJourney(tmp_path)
    try:
        yield journey
    finally:
        journey.client.close()
        journey.engine.dispose()


def test_http_workflow_preserves_gates_evidence_versions_and_targeted_diff(web: WebJourney) -> None:
    web.setup_workflow()
    assert web.upload()["status"] == "SUCCEEDED"
    workflow = web.client.get("/api/projects/P1/workflows/W1")
    assert workflow.json()["snapshot"]["agent_models"]["analyst"]["model_id"] == "offline-analyst"
    assert str(web.config.workspace_root) not in workflow.text
    source = workflow.json()["sources"][0]["id"]
    evidence = web.client.get(f"/api/projects/P1/workflows/W1/sources/{source}")
    assert evidence.status_code == 200 and "account locks" in evidence.json()["content"]
    run = web.submit(
        "run", request_id="initial", instruction="Analyze and draft eligible plans and cases"
    )
    initial = web.process(run["job_id"])
    assert initial["status"] == "BLOCKED"  # Lockout is blocked, independent MFA drafts still exist.
    view = web.view()
    assert len(view["artifacts"]) == 2
    assert {item["readiness"] for item in view["requirements"]} == {"VALIDATED", "BLOCKED"}
    assert all(item["status"] == "DRAFT" for item in view["artifacts"])
    blocked = web.submit("scope", target_id="LOGIN")
    assert web.process(blocked["job_id"])["status"] in {"FAILED", "BLOCKED"}
    web.perform("scope", target_id="MFA")
    web.perform("baseline", target_id="MFA")
    baseline = web.view()["baselines"][0]["id"]
    for item in web.view()["artifacts"]:
        before = web.submit("decide", target_id=item["id"])
        assert web.process(before["job_id"])["status"] == "BLOCKED"
        web.perform("submit", target_id=item["id"], baseline_id=baseline)
        ready = web.view()
        approval = web.submit("decide", target_id=item["id"], key=f"approve-{item['id']}")
        assert web.process(approval["job_id"])["status"] == "SUCCEEDED"
        # An accepted duplicate returns the same job even after its write changed the state.
        duplicate = web.submit(
            "decide",
            target_id=item["id"],
            state_hash=ready["state_hash"],
            key=f"approve-{item['id']}",
        )
        assert duplicate["job_id"] == approval["job_id"]
    case_artifact = next(item for item in web.view()["artifacts"] if item["kind"] == "TEST_CASES")
    detail = web.client.get(
        f"/api/projects/P1/workflows/W1/artifacts/{case_artifact['id']}/versions/{case_artifact['version']}"
    )
    assert (
        detail.status_code == 200
        and detail.json()["item"]["content_hash"] == case_artifact["content_hash"]
    )
    assert "json" in detail.json()["content"]
    original = web.client.get(
        f"/api/projects/P1/workflows/W1/artifacts/{case_artifact['id']}/versions/{case_artifact['version']}/download"
    ).json()
    draft = web.perform(
        "revision", target_id=case_artifact["id"], instruction="Revise TC-012 title"
    )
    review = web.client.get(f"/api/jobs/{draft['id']}/review")
    assert review.status_code == 200 and review.json()["diff"] and review.json()["current_hash"]
    assert web.client.get(f"/jobs/{draft['id']}").status_code == 200
    web.perform("apply", draft_id=draft["id"], reviewed_diff_hash=draft["result"]["diff_hash"])
    latest = next(item for item in web.view()["artifacts"] if item["id"] == case_artifact["id"])
    assert latest["version"] == case_artifact["version"] + 1
    assert latest["status"] == "DRAFT"
    revised = web.client.get(
        f"/api/projects/P1/workflows/W1/artifacts/{latest['id']}/versions/{latest['version']}/download"
    ).json()
    assert original["cases"][0] == revised["cases"][0]
    assert revised["cases"][1]["title"] == "Enroll with a valid authenticator"
    assert (
        original
        == web.client.get(
            f"/api/projects/P1/workflows/W1/artifacts/{case_artifact['id']}/versions/{case_artifact['version']}/download"
        ).json()
    )
    for format in ("detailed", "gherkin"):
        response = web.client.get(
            f"/api/projects/P1/workflows/W1/artifacts/{latest['id']}/versions/{latest['version']}/download?format={format}"
        )
        assert response.status_code == 200 and "TC-012" in response.text
    count = len(web.calls)
    for page in ("/projects", "/settings", "/jobs", "/reports", "/projects/P1/workflows/W1"):
        assert web.client.get(page).status_code == 200
    assert len(web.calls) == count


def test_crash_after_publication_recovers_same_request_and_import_without_duplicates(
    web: WebJourney,
) -> None:
    web.setup_workflow()
    imported = web.upload()
    identities = web.view()["workflow"]["source_snapshot_ids"]
    # Simulate death after import commit and before job finalization.
    with web.control.connect() as db:
        db.execute("UPDATE jobs SET status='RUNNING',result=NULL WHERE id=?", (imported["id"],))
    web.control.recover()
    assert web.control.job(imported["id"])["status"] == "INTERRUPTED"
    assert web.control.claim() is None
    web.control.retry(imported["id"])
    assert web.process(imported["id"])["status"] == "SUCCEEDED"
    assert web.view()["workflow"]["source_snapshot_ids"] == identities
    run = web.submit(
        "run", request_id="recover", instruction="Analyze and draft eligible plans and cases"
    )
    job = web.control.claim()
    assert job is not None
    asyncio.run(dispatch(web.control, web.repo, job, model_factory=web.factory))
    published = web.view()["artifacts"]
    calls = len(web.calls)
    web.control.recover()
    web.control.retry(run["job_id"])
    replay = web.process(run["job_id"])
    assert replay["status"] == "BLOCKED"
    assert web.view()["artifacts"] == published
    assert len(web.calls) == calls


def test_settings_new_version_does_not_change_existing_workflow_and_stale_review_fails(
    web: WebJourney,
) -> None:
    web.setup_workflow()
    old = web.view()["workflow"]["model_config_snapshot_id"]
    settings = web.config.model_dump(mode="json", exclude={"workspace_root", "database_path"})
    settings["agent_models"]["planner"]["model_id"] = "another-planner"
    response = web.client.post("/api/settings", json=settings, headers=web.headers())
    assert response.status_code == 201
    assert (
        web.client.post("/api/settings", json=settings, headers=web.headers()).json()
        == response.json()
    )
    altered = {**settings, "max_concurrency": 2}
    assert web.client.post("/api/settings", json=altered, headers=web.headers()).status_code == 409
    assert web.view()["workflow"]["model_config_snapshot_id"] == old
    assert web.control.config().agent_models.planner.model_id == "another-planner"
    assert web.upload()["status"] == "SUCCEEDED"
    web.process(
        web.submit(
            "run", request_id="analysis", instruction="Analyze and draft eligible plans and cases"
        )["job_id"]
    )
    stale = web.view()["state_hash"]
    web.perform("scope", target_id="MFA")
    response = web.client.post(
        "/api/commands",
        json={
            "kind": "baseline",
            "project_id": "P1",
            "workflow_id": "W1",
            "target_id": "MFA",
            "state_hash": stale,
        },
        headers=web.headers("old-page"),
    )
    assert response.status_code == 409
    assert web.view()["baselines"] == []


def test_answer_attribution_rejection_checkpoint_and_explicit_resume(web: WebJourney) -> None:
    web.setup_workflow()
    web.upload()
    web.fail_cases = True
    first = web.process(
        web.submit(
            "run", request_id="retry", instruction="Analyze and draft eligible plans and cases"
        )["job_id"]
    )
    assert first["status"] == "FAILED"
    assert len(web.view()["artifacts"]) == 1
    original_calls = len(web.calls)
    resumed = web.process(web.submit("resume", request_id="retry")["job_id"])
    assert resumed["status"] == "BLOCKED"
    assert len(web.view()["artifacts"]) == 2
    assert web.calls[original_calls:] == ["TestCaseSet"]
    web.perform(
        "answer",
        target_id="C-LOCK",
        author="Product Owner",
        source="Meeting decision",
        text="Keep the lockout question open until SRS is reviewed.",
    )
    question = web.view()["clarifications"][0]
    assert question["status"] == "ANSWERED"  # Attribution alone must not resolve the conflict.
    assert question["answers"][-1]["author"] == "Product Owner"
    assert question["answers"][-1]["recorded_by"] == "qa-owner"
    web.perform("scope", target_id="MFA")
    web.perform("baseline", target_id="MFA")
    case = next(item for item in web.view()["artifacts"] if item["kind"] == "TEST_CASES")
    web.perform("submit", target_id=case["id"], baseline_id=web.view()["baselines"][0]["id"])
    rejected = web.perform("decide", target_id=case["id"], action="REJECT")
    assert rejected["result"]["status"] == "REJECTED"
    # A fresh HTTP job cannot turn a completed rejection checkpoint into approval.
    repeated = web.perform("decide", target_id=case["id"])
    assert repeated["result"]["status"] == "REJECTED"
    assert not any(
        event.event_type == "approval" and event.record_id == case["id"]
        for event in web.repo.list_decisions("P1")
    )
    review = web.client.get(
        f"/api/projects/P1/workflows/W1/artifacts/{case['id']}/versions/{rejected['result']['artifact_version']}"
    )
    assert review.json()["rejected_by"] == "qa-owner"
    assert review.json()["item"]["version"] == rejected["result"]["artifact_version"]


def test_download_confinement_integrity_and_cross_workflow_membership(
    web: WebJourney, tmp_path: Path
) -> None:
    web.setup_workflow()
    web.upload()
    web.process(
        web.submit(
            "run", request_id="generate", instruction="Analyze and draft eligible plans and cases"
        )["job_id"]
    )
    web.perform(
        "create_workflow",
        workflow_id="W2",
        goal="Different workflow",
        settings_id=web.control.settings_id(),
    )
    case = next(item for item in web.view()["artifacts"] if item["kind"] == "TEST_CASES")
    path = (
        f"/api/projects/P1/workflows/W1/artifacts/{case['id']}/versions/{case['version']}/download"
    )
    assert web.client.get(path.replace("/W1/", "/W2/")).status_code == 403
    assert web.client.get(path + "?format=../../server.json").status_code == 422
    root = web.repo.read_project_record("P1", "P1").root
    canonical = root / "artifacts" / case["id"] / f"v{case['version']}" / "test-cases.json"
    original = canonical.read_bytes()
    canonical.write_bytes(original + b" ")
    assert web.client.get(path).status_code == 422
    canonical.unlink()
    escaped = tmp_path / "outside.json"
    escaped.write_bytes(original)
    canonical.symlink_to(escaped)
    assert web.client.get(path).status_code == 403


def test_stopped_installation_backup_restores_queue_checkpoints_and_artifacts(
    web: WebJourney, tmp_path: Path
) -> None:
    web.setup_workflow()
    web.upload()
    run = web.submit(
        "run", request_id="backup", instruction="Analyze and draft eligible plans and cases"
    )
    web.process(run["job_id"])
    before = web.view()
    queued = web.submit("resume", request_id="backup")
    web.engine.dispose()
    # No server/worker/writer runs during this copy; restore preserves the original paths.
    backup = tmp_path.parent / (tmp_path.name + "-backup")
    shutil.copytree(tmp_path, backup)
    shutil.rmtree(tmp_path)
    shutil.copytree(backup, tmp_path)
    web.control = Control(web.state)
    web.engine = open_database(web.config.database_path)
    web.repo = QaRepositories(web.engine)
    assert web.view() == before
    assert web.control.job(queued["job_id"])["status"] == "QUEUED"
    count = len(web.calls)
    assert web.process(queued["job_id"])["status"] == "BLOCKED"
    assert len(web.calls) == count
    case = next(item for item in before["artifacts"] if item["kind"] == "TEST_CASES")
    response = web.client.get(
        f"/api/projects/P1/workflows/W1/artifacts/{case['id']}/versions/{case['version']}/download"
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["cases"]] == ["TC-011", "TC-012"]
