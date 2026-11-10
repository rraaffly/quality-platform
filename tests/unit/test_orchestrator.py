from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from quality_platform.agents.analyst import AgentContext
from quality_platform.agents.orchestrator import (
    ProposedTask,
    TaskAction,
    TaskProposal,
    authorize_task,
    plan_user_intent,
)
from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.domain.models import ProjectRecord, WorkflowRecord
from quality_platform.domain.policies import PolicyContext
from quality_platform.llm.registry import ModelRegistry
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


class FakeStructured:
    def __init__(self, result: object, messages: list[BaseMessage]) -> None:
        self.result = result
        self.messages = messages

    async def ainvoke(self, messages: list[BaseMessage]) -> object:
        self.messages.extend(messages)
        return self.result


class FakeModel:
    def __init__(self, result: object) -> None:
        self.result = result
        self.messages: list[BaseMessage] = []

    def with_structured_output(self, _schema: object) -> FakeStructured:
        return FakeStructured(self.result, self.messages)


def context(tmp_path: Path, proposal: TaskProposal) -> tuple[AgentContext, FakeModel]:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Active", root=tmp_path / "p1"))
    repo.create_project(ProjectRecord(id="P2", name="Private", root=tmp_path / "p2"))
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Resolve login conflicts and draft independent MFA tests",
            thread_id="T1",
        )
    )
    repo.create_workflow(
        WorkflowRecord(
            id="W2",
            project_id="P2",
            goal="SECRET-P2 payroll acquisition",
            thread_id="T2",
        )
    )
    config = AppConfig(
        database_path=tmp_path / "qa.sqlite",
        workspace_root=tmp_path,
        agent_models=AgentModels(
            orchestrator=ModelSpec(provider="openai", model_id="orchestrator"),
            analyst=ModelSpec(provider="openai", model_id="analyst"),
            planner=ModelSpec(provider="openai", model_id="planner"),
            evaluator=ModelSpec(provider="openai", model_id="evaluator"),
        ),
    )
    fake = FakeModel(proposal)
    return (
        AgentContext(
            project_id="P1",
            workflow_id="W1",
            repo=repo,
            models=ModelRegistry(config),
            model_factory=lambda _spec: fake,
        ),
        fake,
    )


@pytest.mark.asyncio
async def test_conflict_search_is_prioritized_without_leaking_other_project_context(
    tmp_path: Path,
) -> None:
    proposal = TaskProposal(
        project_id="P1",
        workflow_id="W1",
        tasks=(
            ProposedTask(
                id="draft-mfa",
                action=TaskAction.DRAFT_TEST_CASES,
                project_id="P1",
                workflow_id="W1",
                target_id="MFA",
            ),
            ProposedTask(
                id="find-login-conflict",
                action=TaskAction.ANALYZE_CONFLICTS,
                project_id="P1",
                workflow_id="W1",
                target_id="LOGIN",
            ),
        ),
    )
    agent_context, fake = context(tmp_path, proposal)

    result = await plan_user_intent(
        agent_context, "Find conflicts and work on anything independent"
    )

    assert [task.id for task in result.tasks] == ["find-login-conflict", "draft-mfa"]
    prompt = str(fake.messages[0].content)
    assert "Resolve login conflicts" in prompt
    assert "SECRET-P2" not in prompt
    assert "P2" not in prompt


def test_forced_final_approval_is_denied_by_policy() -> None:
    proposal = TaskProposal(
        project_id="P1",
        workflow_id="W1",
        tasks=(
            ProposedTask(
                id="force-approve",
                action=TaskAction.FINALIZE_ARTIFACT,
                project_id="P1",
                workflow_id="W1",
                target_id="CASES-1",
            ),
        ),
    )

    decision = authorize_task(proposal, PolicyContext())

    assert decision.allowed is False
    assert "human artifact review required" in decision.reasons
    assert "scope approval required" in decision.reasons


def test_analysis_proposal_is_eligible_while_clarifications_are_open() -> None:
    proposal = TaskProposal(
        project_id="P1",
        workflow_id="W1",
        tasks=(
            ProposedTask(
                id="find-conflicts",
                action=TaskAction.ANALYZE_CONFLICTS,
                project_id="P1",
                workflow_id="W1",
            ),
        ),
    )

    decision = authorize_task(
        proposal,
        PolicyContext(open_clarification_ids=frozenset({"C-1"})),
    )

    assert decision.allowed is True
    assert decision.reasons == []
