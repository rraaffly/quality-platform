from pathlib import Path

from typer.testing import CliRunner

from quality_platform.cli.app import app
from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    ClarificationStatus,
    ProjectRecord,
    SourceReference,
    WorkflowStatus,
)
from quality_platform.services.clarifications import ClarificationService
from quality_platform.services.scopes import ScopeProposal, ScopeService
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories

runner = CliRunner()


def invoke(*arguments: str) -> str:
    result = runner.invoke(app, list(arguments))
    assert result.exit_code == 0, result.output
    return result.output


def test_switching_projects_preserves_context_and_lists_workflow_status(tmp_path: Path) -> None:
    database = tmp_path / "qa.sqlite"
    for project_id in ("P1", "P2"):
        invoke(
            "project",
            "create",
            "--database",
            str(database),
            "--project-id",
            project_id,
            "--name",
            f"Project {project_id}",
            "--root",
            str(tmp_path / project_id),
        )

    invoke("project", "select", "--database", str(database), "--project-id", "P1")
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--goal",
        "First workflow",
    )
    invoke("project", "select", "--database", str(database), "--project-id", "P2")
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W2",
        "--goal",
        "Second workflow",
    )
    invoke("project", "select", "--database", str(database), "--project-id", "P1")

    output = invoke("workflow", "list", "--database", str(database))
    assert "W1" in output
    assert "ACTIVE" in output
    assert "W2" not in output

    repo = QaRepositories(open_database(database))
    assert repo.read_workflow("P1", "W1").status == WorkflowStatus.ACTIVE
    assert repo.read_workflow("P2", "W2").status == WorkflowStatus.ACTIVE


def test_cli_uses_clarification_and_scope_services_for_selected_project(tmp_path: Path) -> None:
    database = tmp_path / "qa.sqlite"
    repo = QaRepositories(open_database(database))
    repo.create_project(ProjectRecord(id="P1", name="Project", root=tmp_path))
    invoke("project", "select", "--database", str(database), "--project-id", "P1")
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--goal",
        "Review login",
    )
    clarification = Clarification(
        id="C1",
        issue_type=ClarificationIssueType.MISSING_RULE,
        question="How long is the lockout?",
        source_references=(SourceReference(document_id="BRD", start_line=1, end_line=1),),
        affected_requirement_ids=("R1",),
    )
    ClarificationService(repo, project_id="P1", workflow_id="W1").create_batch([clarification])

    listed = invoke(
        "clarification",
        "list",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
    )
    assert "C1" in listed and "OPEN" in listed
    invoke(
        "clarification",
        "answer",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--clarification-id",
        "C1",
        "--author",
        "Product Owner",
        "--text",
        "15 minutes",
        "--recorded-by",
        "QA",
        "--source",
        "review meeting",
    )
    assert (
        ClarificationService(repo, project_id="P1", workflow_id="W1").get("C1").status
        == ClarificationStatus.ANSWERED
    )

    scopes = ScopeService(repo, project_id="P1", workflow_id="W1")
    scopes.create_batch(
        [ScopeProposal(id="S1", journey="Independent MFA", requirement_ids=("R2",))],
        known_requirement_ids={"R2"},
    )
    output = invoke(
        "scope",
        "review",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--scope-id",
        "S1",
        "--action",
        "approve",
        "--actor",
        "QA",
    )
    assert "APPROVED" in output


def test_cli_exposes_complete_mvp_command_surface() -> None:
    root = invoke("--help")
    for group in ("project", "workflow", "clarification", "scope", "artifact", "benchmark"):
        assert group in root
    expected = {
        "project": ("create", "list", "select"),
        "workflow": ("start", "list", "resume"),
        "clarification": ("list", "answer"),
        "scope": ("review",),
        "artifact": ("show", "review"),
        "benchmark": ("run", "report"),
    }
    for group, commands in expected.items():
        output = invoke(group, "--help")
        for command in commands:
            assert command in output
