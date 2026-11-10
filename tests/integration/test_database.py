from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from quality_platform.domain.models import (
    ApprovalRecord,
    Clarification,
    ClarificationIssueType,
    ProjectRecord,
    Requirement,
    SourceReference,
    WorkflowRecord,
)
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


def project(project_id: str, root: Path) -> ProjectRecord:
    return ProjectRecord(id=project_id, name=project_id, root=root)


def requirement() -> Requirement:
    return Requirement(
        id="REQ-1",
        version=1,
        statement="Five login attempts",
        source_references=(SourceReference(document_id="BRD-1", start_line=4, end_line=4),),
    )


def test_projects_and_workflows_survive_reopening_database(tmp_path: Path) -> None:
    path = tmp_path / "qa.sqlite"
    engine = open_database(path)
    repo = QaRepositories(engine)
    repo.create_project(project("P1", tmp_path))
    workflow = WorkflowRecord(id="W1", project_id="P1", goal="Review login", thread_id="T1")
    repo.create_workflow(workflow)
    engine.dispose()

    reopened = QaRepositories(open_database(path))
    assert reopened.read_project_record("P1", "P1").id == "P1"
    assert reopened.read_workflow("P1", "W1") == workflow


def test_foreign_keys_and_unique_ids_are_enforced(tmp_path: Path) -> None:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    with pytest.raises(IntegrityError):
        repo.create_workflow(
            WorkflowRecord(id="W1", project_id="missing", goal="Review", thread_id="T1")
        )

    repo.create_project(project("P1", tmp_path))
    with pytest.raises(IntegrityError):
        repo.create_project(project("P1", tmp_path))


def test_requirement_versions_are_unique_and_idempotent(tmp_path: Path) -> None:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(project("P1", tmp_path))
    first = requirement()
    repo.save_requirement_version("P1", first, idempotency_key="save-1")
    repo.save_requirement_version("P1", first, idempotency_key="save-1")
    assert repo.read_requirement_version("P1", "REQ-1", 1) == first
    with pytest.raises(ValueError, match="idempotency"):
        repo.save_requirement_version(
            "P1", first.model_copy(update={"statement": "Three attempts"}), idempotency_key="save-1"
        )
    with pytest.raises(IntegrityError):
        repo.save_requirement_version("P1", first, idempotency_key="save-2")


def test_approval_and_clarification_audit_is_append_only(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    engine = open_database(tmp_path / "qa.sqlite")
    repo = QaRepositories(engine)
    repo.create_project(project("P1", tmp_path))
    approval = ApprovalRecord(
        id="A1",
        actor="QA",
        target_id="PLAN-1",
        target_version=1,
        scope_id="S1",
        baseline_id="B1",
        approved_at=datetime.now(UTC),
    )
    clarification = Clarification(
        id="C1",
        issue_type=ClarificationIssueType.CONTRADICTION,
        question="Three or five attempts?",
        source_references=(SourceReference(document_id="BRD-1", start_line=4, end_line=4),),
    )
    approval_seq = repo.append_decision("P1", approval, idempotency_key="approval-1")
    assert repo.append_decision("P1", approval, idempotency_key="approval-1") == approval_seq
    repo.append_decision("P1", clarification, idempotency_key="clarification-1")
    assert [event.record_id for event in repo.list_decisions("P1")] == ["A1", "C1"]
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("DELETE FROM audit_events WHERE record_id = 'A1'"))


def test_cross_project_read_requires_grant_and_write_is_forbidden(tmp_path: Path) -> None:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(project("P1", tmp_path))
    repo.create_project(project("P2", tmp_path))
    with pytest.raises(PermissionError):
        repo.read_project_record("P2", "P1")
    repo.grant_cross_project_read("P1", "P2")
    assert repo.read_project_record("P2", "P1").id == "P1"
    with pytest.raises(PermissionError):
        repo.save_requirement_version(
            "P1", requirement(), idempotency_key="cross", actor_project_id="P2"
        )


def test_schema_setup_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "qa.sqlite"
    open_database(path).dispose()
    repo = QaRepositories(open_database(path))
    repo.create_project(project("P1", tmp_path))
    assert repo.read_project_record("P1", "P1").id == "P1"


def test_same_requirement_id_in_two_projects_keeps_versions_isolated(tmp_path: Path) -> None:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(project("P1", tmp_path))
    repo.create_project(project("P2", tmp_path))
    repo.save_requirement_version("P1", requirement(), idempotency_key="p1-req")
    second = requirement().model_copy(update={"statement": "Three login attempts"})
    repo.save_requirement_version("P2", second, idempotency_key="p2-req")

    assert repo.read_requirement_version("P1", "REQ-1", 1) == requirement()
    assert repo.read_requirement_version("P2", "REQ-1", 1) == second
