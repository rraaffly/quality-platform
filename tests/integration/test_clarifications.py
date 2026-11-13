from pathlib import Path

import pytest

from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    ClarificationStatus,
    ProjectRecord,
    SourceReference,
    StakeholderAnswer,
    WorkflowRecord,
)
from quality_platform.services.clarifications import (
    ClarificationAssessment,
    ClarificationService,
)
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


def setup_repo(tmp_path: Path) -> QaRepositories:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Test", root=tmp_path))
    repo.create_workflow(WorkflowRecord(id="W1", project_id="P1", goal="Login", thread_id="T1"))
    repo.create_workflow(WorkflowRecord(id="W2", project_id="P1", goal="Other", thread_id="T2"))
    return repo


def question(question_id: str) -> Clarification:
    return Clarification(
        id=question_id,
        issue_type=ClarificationIssueType.MISSING_RULE,
        question="How long does lockout last?",
        source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
        affected_requirement_ids=("REQ-LOGIN",),
    )


def answer(text: str) -> StakeholderAnswer:
    return StakeholderAnswer(
        author="Product Owner", text=text, recorded_by="QA", source="review meeting"
    )


@pytest.mark.asyncio
async def test_incomplete_answer_reopens_and_later_explicit_rule_resolves(tmp_path: Path) -> None:
    repo = setup_repo(tmp_path)
    calls: list[str] = []

    async def reviewer(item: Clarification) -> ClarificationAssessment:
        calls.append(item.answers[-1].text)
        if item.answers[-1].text == "It depends":
            return ClarificationAssessment(sufficient=False, reason="Duration still unspecified")
        return ClarificationAssessment(
            sufficient=True,
            reason="Explicit duration supplied",
            resolved_rule="Lock for 15 minutes",
        )

    service = ClarificationService(repo, project_id="P1", workflow_id="W1", reviewer=reviewer)
    service.create_batch([question("C-1"), question("C-2")])
    service.record_stakeholder_answer("C-1", answer("It depends"))
    review = await service.validate_clarifications("W1")
    assert review.reopened_ids == ("C-1",)
    assert review.open_ids == ("C-2",)
    assert service.get("C-1").status == ClarificationStatus.REOPENED
    assert service.get("C-1").resolution is None
    service.record_stakeholder_answer("C-1", answer("Lock for 15 minutes"))
    review = await service.validate_clarifications("W1")
    assert review.resolved_ids == ("C-1",)
    assert service.get("C-1").resolution == "Lock for 15 minutes"
    assert len(service.get("C-1").answers) == 2
    assert calls == ["It depends", "Lock for 15 minutes"]
    history = [event for event in repo.list_decisions("P1") if event.record_id == "C-1"]
    assert len(history) == 5
    assert Clarification.model_validate_json(history[0].payload).status == ClarificationStatus.OPEN
    assert (
        Clarification.model_validate_json(history[-1].payload).status
        == ClarificationStatus.RESOLVED
    )


@pytest.mark.asyncio
async def test_unanswered_cannot_resolve_even_if_reviewer_would_accept(tmp_path: Path) -> None:
    repo = setup_repo(tmp_path)
    called = False

    async def reviewer(_item: Clarification) -> ClarificationAssessment:
        nonlocal called
        called = True
        return ClarificationAssessment(sufficient=True, reason="yes", resolved_rule="15 minutes")

    service = ClarificationService(repo, project_id="P1", workflow_id="W1", reviewer=reviewer)
    service.create_batch([question("C-1")])
    review = await service.validate_clarifications("W1")
    assert review.open_ids == ("C-1",)
    assert not called
    with pytest.raises(ValueError, match="source"):
        service.record_stakeholder_answer(
            "C-1", StakeholderAnswer(author="PO", text="15 minutes", recorded_by="QA")
        )


@pytest.mark.asyncio
async def test_review_cannot_add_rule_absent_from_stakeholder_answer(tmp_path: Path) -> None:
    repo = setup_repo(tmp_path)

    async def reviewer(_item: Clarification) -> ClarificationAssessment:
        return ClarificationAssessment(
            sufficient=True, reason="Model assumed a duration", resolved_rule="Lock for 15 minutes"
        )

    service = ClarificationService(repo, project_id="P1", workflow_id="W1", reviewer=reviewer)
    service.create_batch([question("C-1")])
    service.record_stakeholder_answer("C-1", answer("Lock for 10 minutes"))
    with pytest.raises(ValueError, match="answer text"):
        await service.validate_clarifications("W1")
    assert service.get("C-1").status == ClarificationStatus.ANSWERED


@pytest.mark.asyncio
async def test_uncertain_answer_cannot_resolve_even_when_rule_words_appear(tmp_path: Path) -> None:
    repo = setup_repo(tmp_path)

    async def reviewer(_item: Clarification) -> ClarificationAssessment:
        return ClarificationAssessment(
            sufficient=True, reason="Model wrongly marked sufficient", resolved_rule="15 minutes"
        )

    service = ClarificationService(repo, project_id="P1", workflow_id="W1", reviewer=reviewer)
    service.create_batch([question("C-1")])
    service.record_stakeholder_answer("C-1", answer("I don't know if 15 minutes is right."))
    review = await service.validate_clarifications("W1")
    assert review.reopened_ids == ("C-1",)
    assert service.get("C-1").status == ClarificationStatus.REOPENED


def test_workflow_isolation_and_replayed_answer_are_safe(tmp_path: Path) -> None:
    repo = setup_repo(tmp_path)
    first = ClarificationService(repo, project_id="P1", workflow_id="W1")
    second = ClarificationService(repo, project_id="P1", workflow_id="W2")
    first.create_batch([question("C-1")])
    with pytest.raises(KeyError):
        second.get("C-1")
    with pytest.raises(ValueError, match="workflow"):
        first.validate_workflow_id("W2")
    first.record_stakeholder_answer("C-1", answer("15 minutes"))
    first.record_stakeholder_answer("C-1", answer("15 minutes"))
    assert len([event for event in repo.list_decisions("P1") if event.record_id == "C-1"]) == 2
