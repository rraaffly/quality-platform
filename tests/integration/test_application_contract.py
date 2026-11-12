"""Contracts for callers sharing orchestration and database ownership."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from quality_platform.agents.orchestrator import ProposedTask, TaskAction, TaskProposal
from quality_platform.application import WorkflowApplication, create_project, create_workflow
from quality_platform.cli import commands
from quality_platform.cli import workflow as workflow_commands
from quality_platform.cli.app import app
from quality_platform.cli.workflow import import_sources, run_workflow
from quality_platform.config.models import AppConfig
from quality_platform.services.approvals import BaselineSelection
from quality_platform.storage.checkpoints import create_checkpoint_saver
from quality_platform.storage.database import open_database, projects
from quality_platform.storage.ownership import open_read_only_database, writer_lock
from quality_platform.storage.repositories import QaRepositories


def test_competing_cli_process_cannot_initialize_database(tmp_path: Path) -> None:
    database = tmp_path / "domain.sqlite"
    with writer_lock(database):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "from quality_platform.cli.commands import create_project; "
                    "import sys; create_project(Path(sys.argv[1]), 'P1', 'Project', Path(sys.argv[2]))"
                ),
                str(database),
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode != 0
        assert "database writer already active" in result.stderr
        assert not database.exists()
    # Release must allow the next CLI writer, without stale-lock cleanup.
    commands.create_project(database, "P1", "Project", tmp_path)
    assert database.exists()


@pytest.mark.parametrize(
    "command,arguments",
    [
        (commands.create_project, {"project_id": "P1", "name": "Project", "root": Path(".")}),
        (commands.start_workflow, {"workflow_id": "W1", "goal": "Goal"}),
        (commands.resume_workflow, {"workflow_id": "W1"}),
        (
            commands.answer_clarification,
            {
                "workflow_id": "W1",
                "clarification_id": "C1",
                "author": "QA",
                "text": "Answer",
                "recorded_by": "QA",
                "source": "stakeholder",
            },
        ),
        (
            commands.review_scope,
            {
                "workflow_id": "W1",
                "scope_id": "S1",
                "action": "approve",
                "actor": "QA",
            },
        ),
        (commands.review_artifact, {"workflow_id": "W1", "artifact_id": "A1", "actor": "QA"}),
        (
            run_workflow,
            {
                "workflow_id": "W1",
                "instruction": "Impact",
                "request_id": "R1",
                "checkpoint_database": Path("unused.sqlite"),
            },
        ),
        (import_sources, {"workflow_id": "W1", "source": [], "config": Path("unused.yaml")}),
        (
            workflow_commands.approve_baseline,
            {
                "workflow_id": "W1",
                "scope_id": "S1",
                "actor": "QA",
            },
        ),
        (
            workflow_commands.submit_review,
            {
                "workflow_id": "W1",
                "artifact_id": "A1",
                "baseline_id": "B1",
                "checkpoint_database": Path("unused.sqlite"),
            },
        ),
        (
            workflow_commands.propose_edit,
            {
                "workflow_id": "W1",
                "instruction": "Split scope",
                "output": Path("unused.json"),
            },
        ),
        (
            workflow_commands.apply_edit,
            {
                "workflow_id": "W1",
                "proposal": Path("unused.json"),
                "actor": "QA",
            },
        ),
        (
            workflow_commands.propose_revision,
            {
                "workflow_id": "W1",
                "artifact_id": "A1",
                "instruction": "Revise TC-1",
                "output": Path("unused.json"),
            },
        ),
        (
            workflow_commands.commit_revision,
            {
                "workflow_id": "W1",
                "draft": Path("unused.json"),
                "reviewed_diff_hash": "hash",
                "actor": "QA",
            },
        ),
    ],
)
def test_direct_mutations_lock_before_opening_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: Any, arguments: dict[str, Any]
) -> None:
    database = tmp_path / "domain.sqlite"

    def forbidden_open(_database: Path) -> None:
        pytest.fail("writer must acquire ownership before open_database")

    monkeypatch.setattr(commands, "open_database", forbidden_open)
    with writer_lock(database), pytest.raises(RuntimeError, match="writer already active"):
        command(database=database, **arguments)
    assert not database.exists()


def test_readers_work_during_writer_ownership_and_cannot_write(tmp_path: Path) -> None:
    database = tmp_path / "domain ?# space.sqlite"
    commands.create_project(database, "P1", "Project", tmp_path)
    with writer_lock(database):
        selected = CliRunner().invoke(
            app,
            [
                "project",
                "select",
                "--database",
                str(database),
                "--project-id",
                "P1",
            ],
        )
        assert selected.exit_code == 0, selected.output
        assert commands._selection_path(database).exists()
        listed = CliRunner().invoke(app, ["project", "list", "--database", str(database)])
        assert listed.exit_code == 0, listed.output
        assert "P1" in listed.output
        with (
            commands._repository(database, read_only=True) as repo,
            pytest.raises(OperationalError, match="readonly"),
            repo.engine.begin() as connection,
        ):
            connection.execute(projects.delete())


def test_read_only_open_never_creates_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "domain.sqlite"
    engine = open_read_only_database(database)
    try:
        with pytest.raises(OperationalError), engine.connect():
            pass
        assert not database.parent.exists()
    finally:
        engine.dispose()


def test_application_creation_and_saved_request_are_shared_with_cli(tmp_path: Path) -> None:
    database = tmp_path / "domain.sqlite"
    checkpoints = tmp_path / "checkpoints.sqlite"
    engine = open_database(database)
    repo = QaRepositories(engine)
    config = AppConfig.model_validate(
        {
            "database_path": database,
            "workspace_root": tmp_path,
            "agent_models": {
                role: {"provider": "openai", "model_id": "offline"}
                for role in ("orchestrator", "analyst", "planner", "evaluator")
            },
        }
    )
    calls = 0

    class FakeModel:
        def with_structured_output(self, schema: type[Any]) -> Any:
            assert schema is TaskProposal
            return self

        async def ainvoke(self, messages: Any) -> TaskProposal:
            nonlocal calls
            calls += 1
            return TaskProposal(
                project_id="P1",
                workflow_id="W1",
                tasks=(
                    ProposedTask(
                        id="impact",
                        action=TaskAction.DETECT_IMPACT,
                        project_id="P1",
                        workflow_id="W1",
                    ),
                ),
            )

    async def execute() -> list[dict[str, Any]]:
        async with create_checkpoint_saver(checkpoints) as saver:
            application = WorkflowApplication(
                repo, "P1", "W1", saver, model_factory=lambda spec: FakeModel()
            )
            assert application.execution.context.model_factory is not None
            result = await application.run("Detect impact", "request-1", style="gherkin")
            assert result[0].status == "COMPLETED"
            assert await application.resume("request-1") == result
            with pytest.raises(ValueError, match="request plan not saved"):
                await application.resume("unknown")
            with pytest.raises(ValueError, match="another instruction"):
                await application.run("Different intent", "request-1", style="gherkin")
            # A service denial is a typed result, not a transport-specific exception.
            denied = application.approve_baseline(
                "missing-scope",
                "QA",
                BaselineSelection(
                    requirement_versions={"missing": 1},
                    source_snapshot_ids=("missing",),
                    human_reviewed=True,
                ),
            )
            assert not denied.approved
            assert denied.reasons
            return [item.model_dump(mode="json") for item in result]

    try:
        created = create_project(repo, "P1", "Project", tmp_path / "P1" / ".." / "P1")
        assert created.root == (tmp_path / "P1").resolve()
        with pytest.raises(ValueError, match="config is required"):
            create_workflow(repo, "P1", "invalid", "Goal", sources=[Path("unused.md")])
        assert repo.list_workflows("P1") == []
        workflow = create_workflow(repo, "P1", "W1", "Goal", config)
        assert workflow.thread_id == "thread-W1"
        assert workflow.status == "ACTIVE"
        with writer_lock(database):
            # Workers call application directly while retaining lifetime ownership.
            expected = asyncio.run(execute())
        resumed = CliRunner().invoke(
            app,
            [
                "workflow",
                "resume",
                "--database",
                str(database),
                "--project-id",
                "P1",
                "--workflow-id",
                "W1",
                "--checkpoint-database",
                str(checkpoints),
                "--request-id",
                "request-1",
            ],
        )
        assert resumed.exit_code == 0, resumed.output
        assert json.loads(resumed.output) == expected
        assert calls == 1
    finally:
        engine.dispose()
