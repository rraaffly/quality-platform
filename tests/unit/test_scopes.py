from pathlib import Path

import pytest

from quality_platform.agents.analyst import AgentContext, RequirementAnalysisDraft
from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    ProjectRecord,
    Requirement,
    ScopeStatus,
    SourceReference,
    WorkflowRecord,
)
from quality_platform.llm.registry import ModelRegistry
from quality_platform.services.clarifications import ClarificationService
from quality_platform.services.scopes import (
    ScopeAction,
    ScopeDecision,
    ScopeProposal,
    ScopeProposalSet,
    ScopeService,
    propose_scopes,
)
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


def setup_service(tmp_path: Path) -> ScopeService:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Project", root=tmp_path))
    repo.create_workflow(WorkflowRecord(id="W1", project_id="P1", goal="Review", thread_id="T1"))
    return ScopeService(repo, project_id="P1", workflow_id="W1")


def proposal(scope_id: str, *members: str) -> ScopeProposal:
    return ScopeProposal(id=scope_id, journey=scope_id, requirement_ids=members)


def test_approve_scope_requires_human_decision_and_preserves_shared_membership(
    tmp_path: Path,
) -> None:
    service = setup_service(tmp_path)
    service.create_batch(
        [proposal("LOGIN", "AUTH", "LOGIN-1"), proposal("TRANSFER", "AUTH", "TRANSFER-1")],
        known_requirement_ids={"AUTH", "LOGIN-1", "TRANSFER-1"},
    )
    assert service.get("LOGIN").status == ScopeStatus.PROPOSED
    assert service.get("LOGIN").requirement_ids[0] == service.get("TRANSFER").requirement_ids[0]
    with pytest.raises(ValueError, match="approved scope"):
        service.require_approved("LOGIN")
    approved = service.review_scope(
        "LOGIN", ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    )
    assert approved.status == ScopeStatus.APPROVED
    assert service.require_approved("LOGIN").reviewed_by == "QA"
    assert service.get("TRANSFER").status == ScopeStatus.PROPOSED
    assert (
        len(
            [
                event
                for event in service.repo.list_decisions("P1")
                if event.event_type == "scope_change"
            ]
        )
        == 2
    )


def test_merge_creates_new_proposal_and_rejects_originals(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    service.create_batch(
        [proposal("A", "R1"), proposal("B", "R2")], known_requirement_ids={"R1", "R2"}
    )
    original = service.review_scope(
        "A",
        ScopeDecision(
            action=ScopeAction.MERGE,
            actor="QA",
            human_reviewed=True,
            merge_with_ids=("B",),
            replacements=(proposal("MERGED", "R1", "R2"),),
        ),
    )
    assert original.status == ScopeStatus.REJECTED
    assert service.get("B").status == ScopeStatus.REJECTED
    assert service.get("MERGED").status == ScopeStatus.PROPOSED
    with pytest.raises(ValueError, match="approved scope"):
        service.require_approved("MERGED")


def test_split_preserves_membership_and_requires_new_reviews(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    service.create_batch(
        [proposal("ROOT", "SHARED", "A", "B")], known_requirement_ids={"SHARED", "A", "B"}
    )
    service.review_scope(
        "ROOT",
        ScopeDecision(
            action=ScopeAction.SPLIT,
            actor="QA",
            human_reviewed=True,
            replacements=(proposal("LEFT", "SHARED", "A"), proposal("RIGHT", "SHARED", "B")),
        ),
    )
    assert service.get("LEFT").status == ScopeStatus.PROPOSED
    assert service.get("RIGHT").status == ScopeStatus.PROPOSED
    assert service.get("ROOT").status == ScopeStatus.REJECTED
    assert "SHARED" in service.get("LEFT").requirement_ids
    assert "SHARED" in service.get("RIGHT").requirement_ids


def test_invalid_split_has_no_partial_audit_write(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    service.create_batch([proposal("ROOT", "A", "B")], known_requirement_ids={"A", "B"})
    before = len(service.repo.list_decisions("P1"))
    with pytest.raises(ValueError, match="membership"):
        service.review_scope(
            "ROOT",
            ScopeDecision(
                action=ScopeAction.SPLIT,
                actor="QA",
                human_reviewed=True,
                replacements=(proposal("ONLY", "A"), proposal("EMPTY", "A")),
            ),
        )
    assert len(service.repo.list_decisions("P1")) == before


def test_scope_proposal_rejects_unknown_requirement_id(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    with pytest.raises(ValueError, match="unknown requirement"):
        service.create_batch([proposal("A", "MISSING")], known_requirement_ids={"KNOWN"})
    assert service.repo.list_decisions("P1") == []


def test_unresolved_clarification_blocks_only_affected_scope_review(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    service.create_batch(
        [proposal("LOGIN", "LOGIN-1"), proposal("MFA", "MFA-1")],
        known_requirement_ids={"LOGIN-1", "MFA-1"},
    )
    ClarificationService(service.repo, project_id="P1", workflow_id="W1").create_batch(
        [
            Clarification(
                id="C-1",
                issue_type=ClarificationIssueType.MISSING_RULE,
                question="How long?",
                source_references=(SourceReference(document_id="BRD", start_line=1, end_line=1),),
                affected_requirement_ids=("LOGIN-1",),
            )
        ]
    )
    decision = ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    with pytest.raises(ValueError, match="unresolved"):
        service.review_scope("LOGIN", decision)
    assert service.review_scope("MFA", decision).status == ScopeStatus.APPROVED


@pytest.mark.asyncio
async def test_analyst_proposes_journey_scopes_with_shared_requirement(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    reference = SourceReference(document_id="BRD", start_line=1, end_line=1)
    analysis = RequirementAnalysisDraft(
        requirements=tuple(
            Requirement(id=key, version=1, statement=key, source_references=(reference,))
            for key in ("AUTH", "LOGIN-1", "TRANSFER-1")
        ),
        clarifications=(),
    )
    proposed = ScopeProposalSet(
        proposals=(proposal("LOGIN", "AUTH", "LOGIN-1"), proposal("TRANSFER", "AUTH", "TRANSFER-1"))
    )

    class FakeStructured:
        async def ainvoke(self, _messages: object) -> ScopeProposalSet:
            return proposed

    class FakeModel:
        def with_structured_output(self, _schema: object) -> FakeStructured:
            return FakeStructured()

    config = AppConfig(
        database_path=tmp_path / "qa.sqlite",
        workspace_root=tmp_path,
        agent_models=AgentModels(
            orchestrator=ModelSpec(provider="openai", model_id="configured"),
            analyst=ModelSpec(provider="openai", model_id="configured"),
            planner=ModelSpec(provider="openai", model_id="configured"),
            evaluator=ModelSpec(provider="openai", model_id="configured"),
        ),
    )
    context = AgentContext(
        project_id="P1",
        workflow_id="W1",
        repo=service.repo,
        models=ModelRegistry(config),
        model_factory=lambda _spec: FakeModel(),
        analysis=analysis,
    )
    scopes = await propose_scopes(context)
    assert len(scopes) == 2
    assert "AUTH" in scopes[0].requirement_ids
    assert "AUTH" in scopes[1].requirement_ids
    service.create_batch(scopes, known_requirement_ids={"AUTH", "LOGIN-1", "TRANSFER-1"})
    assert all(item.status == ScopeStatus.PROPOSED for item in service.list_scopes())


def test_merge_rewires_incoming_dependency_and_requires_re_review(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    dependent = proposal("DEPENDENT", "R3").model_copy(update={"dependency_scope_ids": ("A", "B")})
    service.create_batch(
        [proposal("A", "R1"), proposal("B", "R2"), dependent],
        known_requirement_ids={"R1", "R2", "R3"},
    )
    service.review_scope(
        "DEPENDENT", ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    )
    service.review_scope(
        "A",
        ScopeDecision(
            action=ScopeAction.MERGE,
            actor="QA",
            human_reviewed=True,
            merge_with_ids=("B",),
            replacements=(proposal("MERGED", "R1", "R2"),),
        ),
    )
    changed = service.get("DEPENDENT")
    assert changed.dependency_scope_ids == ("MERGED",)
    assert changed.status == ScopeStatus.PROPOSED
    assert changed.reviewed_by is None


def test_split_rewires_incoming_dependency_to_all_children(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    dependent = proposal("DEPENDENT", "R3").model_copy(update={"dependency_scope_ids": ("ROOT",)})
    service.create_batch(
        [proposal("ROOT", "R1", "R2"), dependent],
        known_requirement_ids={"R1", "R2", "R3"},
    )
    service.review_scope(
        "ROOT",
        ScopeDecision(
            action=ScopeAction.SPLIT,
            actor="QA",
            human_reviewed=True,
            replacements=(proposal("LEFT", "R1"), proposal("RIGHT", "R2")),
        ),
    )
    assert service.get("DEPENDENT").dependency_scope_ids == ("LEFT", "RIGHT")
    assert service.get("DEPENDENT").status == ScopeStatus.PROPOSED


def test_split_cannot_drop_explicit_exclusions(tmp_path: Path) -> None:
    service = setup_service(tmp_path)
    root = proposal("ROOT", "R1", "R2").model_copy(
        update={"excluded_requirement_ids": ("EXCLUDED",)}
    )
    service.create_batch([root], known_requirement_ids={"R1", "R2"})
    with pytest.raises(ValueError, match="exclusions"):
        service.review_scope(
            "ROOT",
            ScopeDecision(
                action=ScopeAction.SPLIT,
                actor="QA",
                human_reviewed=True,
                replacements=(proposal("LEFT", "R1"), proposal("RIGHT", "R2")),
            ),
        )
    assert service.get("ROOT").status == ScopeStatus.PROPOSED
