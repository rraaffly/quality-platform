import hashlib
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from quality_platform.agents.analyst import (
    AgentContext,
    RequirementAnalysisDraft,
    analyze_documents,
)
from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.domain.models import (
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersion,
    Clarification,
    ClarificationIssueType,
    ProjectRecord,
    Requirement,
    RequirementReadiness,
    SourceDocument,
    SourceReference,
    StakeholderAnswer,
    WorkflowRecord,
)
from quality_platform.llm.registry import ModelRegistry
from quality_platform.services.approvals import ApprovalService, BaselineSelection
from quality_platform.services.clarifications import (
    ClarificationAssessment,
    ClarificationService,
)
from quality_platform.services.scopes import ScopeAction, ScopeDecision, ScopeProposal, ScopeService
from quality_platform.storage.checkpoints import create_checkpoint_saver
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories
from quality_platform.workflows.gates import HumanResponse, ReviewTarget
from quality_platform.workflows.graph import WorkflowRuntime, WorkflowServices


def source(document_id: str) -> SourceReference:
    return SourceReference(document_id=document_id, start_line=3, end_line=3)


class FakeStructured:
    def __init__(self, result: RequirementAnalysisDraft) -> None:
        self.result = result

    async def ainvoke(self, _messages: list[BaseMessage]) -> RequirementAnalysisDraft:
        return self.result


class FakeModel:
    def __init__(self, result: RequirementAnalysisDraft) -> None:
        self.result = result

    def with_structured_output(self, _schema: object) -> FakeStructured:
        return FakeStructured(self.result)


def model_registry(tmp_path: Path) -> ModelRegistry:
    model = ModelSpec(provider="openai", model_id="offline-fake")
    return ModelRegistry(
        AppConfig(
            database_path=tmp_path / "qa.sqlite",
            workspace_root=tmp_path,
            agent_models=AgentModels(
                orchestrator=model,
                analyst=model,
                planner=model,
                evaluator=model,
            ),
        )
    )


def save_source(
    repo: QaRepositories,
    root: Path,
    project_id: str,
    source_id: str,
    text: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{source_id}.md"
    path.write_text(text, encoding="utf-8")
    repo.save_source_document(
        SourceDocument(
            id=source_id,
            project_id=project_id,
            input_type=source_id,
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            source_path=path,
            snapshot_path=path,
            line_count=len(text.splitlines()),
        )
    )


@pytest.mark.asyncio
async def test_resolved_numeric_conflict_allows_exact_analyst_revalidation(
    tmp_path: Path,
) -> None:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Login", root=tmp_path))
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Resolve login threshold",
            thread_id="T1",
            source_snapshot_ids=("BRD", "SRS"),
        )
    )
    brd_line = "The account locks after 3 failed attempts."
    save_source(repo, tmp_path, "P1", "BRD", f"# Login\n\n{brd_line}\n")
    save_source(
        repo, tmp_path, "P1", "SRS", "# Login\n\nThe account locks after 5 failed attempts.\n"
    )
    blocked = Requirement(
        id="REQ-LOCKOUT",
        version=1,
        statement="The lockout threshold conflicts across sources.",
        source_references=(source("BRD"), source("SRS")),
        readiness=RequirementReadiness.BLOCKED,
    )
    question = Clarification(
        id="C-LOCKOUT",
        issue_type=ClarificationIssueType.CROSS_DOCUMENT_CONFLICT,
        question="Should lockout happen after 3 or 5 failed attempts?",
        source_references=(source("BRD"), source("SRS")),
        affected_requirement_ids=(blocked.id,),
    )
    initial = RequirementAnalysisDraft(requirements=(blocked,), clarifications=(question,))
    initial_context = AgentContext(
        project_id="P1",
        workflow_id="W1",
        repo=repo,
        models=model_registry(tmp_path),
        model_factory=lambda _spec: FakeModel(initial),
    )
    assert (await analyze_documents(initial_context, ["BRD", "SRS"])).requirements[0].readiness == (
        RequirementReadiness.BLOCKED
    )

    async def accept_exact_answer(_item: Clarification) -> ClarificationAssessment:
        return ClarificationAssessment(
            sufficient=True,
            reason="Product owner chose the BRD rule",
            resolved_rule=brd_line,
        )

    clarifications = ClarificationService(
        repo,
        project_id="P1",
        workflow_id="W1",
        reviewer=accept_exact_answer,
    )
    clarifications.create_batch([question])
    clarifications.record_stakeholder_answer(
        question.id,
        StakeholderAnswer(
            author="Product Owner",
            text=brd_line,
            recorded_by="QA",
            source="decision meeting 2026-11-11",
        ),
    )
    assert (await clarifications.validate_clarifications("W1")).resolved_ids == (question.id,)

    validated = Requirement(
        id=blocked.id,
        version=2,
        statement=brd_line,
        source_references=(source("BRD"),),
        source_quotes=(brd_line,),
        readiness=RequirementReadiness.VALIDATED,
    )
    revalidated = RequirementAnalysisDraft(requirements=(validated,), clarifications=())
    revalidation_context = AgentContext(
        project_id="P1",
        workflow_id="W1",
        repo=repo,
        models=model_registry(tmp_path),
        model_factory=lambda _spec: FakeModel(revalidated),
    )

    result = await analyze_documents(revalidation_context, ["BRD", "SRS"])

    assert result.requirements == (validated,)


@pytest.mark.asyncio
async def test_checkpoint_resume_approves_only_the_active_project_artifact(
    tmp_path: Path,
) -> None:
    repo = QaRepositories(open_database(tmp_path / "domain.sqlite"))
    p1_root = tmp_path / "P1"
    p2_root = tmp_path / "P2"
    repo.create_project(ProjectRecord(id="P1", name="One", root=p1_root))
    repo.create_project(ProjectRecord(id="P2", name="Two", root=p2_root))
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Approve MFA cases",
            thread_id="THREAD-1",
            source_snapshot_ids=("P1-BRD",),
        )
    )
    save_source(repo, p1_root, "P1", "P1-BRD", "# MFA\n\nUsers can enable TOTP.\n")
    save_source(repo, p2_root, "P2", "P2-SECRET", "# Secret\n\nPrivate roadmap.\n")
    requirement = Requirement(
        id="REQ-MFA",
        version=1,
        statement="Users can enable TOTP.",
        source_references=(source("P1-BRD"),),
        source_quotes=("Users can enable TOTP.",),
        readiness=RequirementReadiness.VALIDATED,
    )
    repo.save_requirement_version("P1", requirement, idempotency_key="REQ-MFA-v1")
    scopes = ScopeService(repo, project_id="P1", workflow_id="W1")
    scopes.create_batch(
        [ScopeProposal(id="MFA", journey="MFA", requirement_ids=(requirement.id,))],
        known_requirement_ids={requirement.id},
    )
    scopes.review_scope(
        "MFA", ScopeDecision(action=ScopeAction.APPROVE, actor="QA", human_reviewed=True)
    )
    approvals = ApprovalService(repo, scopes, project_id="P1", workflow_id="W1")
    baseline = approvals.approve_scope_baseline(
        "MFA",
        BaselineSelection(
            requirement_versions={requirement.id: 1},
            source_snapshot_ids=("P1-BRD",),
            human_reviewed=True,
        ),
        actor="QA",
    ).baseline
    assert baseline is not None
    artifact = ArtifactVersion(
        id="CASES-MFA",
        kind=ArtifactKind.TEST_CASES,
        scope_id="MFA",
        version=1,
        content_hash="cases-hash",
        requirement_baseline_id=baseline.id,
        status=ArtifactStatus.READY_FOR_REVIEW,
    )
    repo.save_artifact_version("P1", artifact, idempotency_key="cases-mfa-v1")
    services = WorkflowServices(
        repo=repo,
        project_id="P1",
        target=ReviewTarget(
            id=artifact.id,
            version=artifact.version,
            scope_id=artifact.scope_id,
            baseline_id=baseline.id,
        ),
        approval_service=approvals,
    )
    checkpoint = tmp_path / "checkpoints.sqlite"
    async with create_checkpoint_saver(checkpoint) as saver:
        assert (await WorkflowRuntime(services, saver).run_workflow("W1")).status == (
            "WAITING_FOR_REVIEW"
        )
    async with create_checkpoint_saver(checkpoint) as saver:
        resumed = await WorkflowRuntime(services, saver).resume_workflow(
            "W1", HumanResponse(approved=True, actor="QA")
        )

    assert resumed.status == "APPROVED"
    assert repo.read_artifact_version("P1", artifact.id).status == ArtifactStatus.APPROVED
    with pytest.raises(PermissionError):
        repo.read_source_document("P1", "P2-SECRET")
