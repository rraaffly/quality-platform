import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage
from pydantic import ValidationError

from quality_platform.agents.analyst import AgentContext, RequirementAnalysisDraft
from quality_platform.agents.planner import TestPlanDraft, create_test_plan, design_test_cases
from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    ProjectRecord,
    Requirement,
    RequirementReadiness,
    SourceDocument,
    SourceReference,
    WorkflowRecord,
)
from quality_platform.domain.schemas import TestCase, TestCaseSet, TestStep
from quality_platform.llm.registry import ModelRegistry
from quality_platform.services.scopes import ScopeAction, ScopeDecision, ScopeProposal, ScopeService
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
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.messages: list[BaseMessage] = []

    def with_structured_output(self, _schema: object) -> FakeStructured:
        return FakeStructured(self.results.pop(0), self.messages)


def reference(line: int) -> SourceReference:
    return SourceReference(document_id="BRD", start_line=line, end_line=line)


def make_test_case(
    case_id: str = "TC-LOGIN-001",
    requirement_ids: tuple[str, ...] = ("R-LOGIN",),
    expected_references: tuple[SourceReference, ...] = (reference(3),),
) -> TestCase:
    return TestCase(
        id=case_id,
        title="Successful login",
        intent="Verify that a registered user can sign in with valid credentials.",
        requirement_ids=requirement_ids,
        scenario="Registered user signs in with valid credentials",
        case_type="FUNCTIONAL_POSITIVE",
        priority="HIGH",
        preconditions=("A registered user exists.",),
        test_data={"username": "registered-user", "password": "valid-password"},
        steps=(
            TestStep(
                action="Submit valid credentials.",
                expected_result="The user is signed in.",
                expected_result_sources=expected_references,
            ),
        ),
    )


def setup_context(
    tmp_path: Path,
    results: list[object],
    *,
    login_rule: str = "The user is signed in after submitting valid credentials.",
) -> tuple[AgentContext, FakeModel, RequirementAnalysisDraft]:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Planner", root=tmp_path))
    source = tmp_path / "brd.md"
    source.write_text(
        f"# Login\n\n{login_rule}\nThe lockout threshold is unresolved.\n",
        encoding="utf-8",
    )
    repo.save_source_document(
        SourceDocument(
            id="BRD",
            project_id="P1",
            input_type="BRD",
            content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            source_path=source,
            snapshot_path=source,
            line_count=4,
        )
    )
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Design login tests",
            thread_id="T1",
            source_snapshot_ids=("BRD",),
        )
    )
    requirements = (
        Requirement(
            id="R-LOGIN",
            version=1,
            statement=login_rule,
            source_references=(reference(3),),
            source_quotes=(login_rule,),
            readiness=RequirementReadiness.VALIDATED,
        ),
        Requirement(
            id="R-LOCKOUT",
            version=1,
            statement="The lockout threshold is unresolved.",
            source_references=(reference(4),),
            readiness=RequirementReadiness.BLOCKED,
        ),
    )
    for requirement in requirements:
        repo.save_requirement_version("P1", requirement, idempotency_key=f"{requirement.id}-v1")
    ScopeService(repo, project_id="P1", workflow_id="W1").create_batch(
        [ScopeProposal(id="LOGIN", journey="Login", requirement_ids=("R-LOGIN", "R-LOCKOUT"))],
        known_requirement_ids={"R-LOGIN", "R-LOCKOUT"},
    )
    analysis = RequirementAnalysisDraft(
        requirements=requirements,
        clarifications=(
            Clarification(
                id="C-LOCKOUT",
                issue_type=ClarificationIssueType.MISSING_RULE,
                question="What is the lockout threshold?",
                source_references=(reference(4),),
                affected_requirement_ids=("R-LOCKOUT",),
            ),
        ),
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
    fake = FakeModel(results)
    context = AgentContext(
        project_id="P1",
        workflow_id="W1",
        repo=repo,
        models=ModelRegistry(config),
        model_factory=lambda _spec: fake,
        analysis=analysis,
    )
    return context, fake, analysis


@pytest.mark.asyncio
async def test_planner_creates_plan_and_cases_only_from_eligible_requirements(
    tmp_path: Path,
) -> None:
    plan = TestPlanDraft(
        scope_id="LOGIN",
        objective="Validate the supported login journey.",
        requirement_ids=("R-LOGIN",),
        blocked_requirement_ids=("R-LOCKOUT",),
        in_scope=("Successful login",),
        exclusions=("R-LOCKOUT: threshold behavior pending clarification",),
        test_types=("functional",),
        risks=("Authentication failure",),
        entry_criteria=("Validated login requirement is available",),
        exit_criteria=("All supported login cases pass",),
    )
    context, fake, _analysis = setup_context(
        tmp_path, [plan, TestCaseSet(cases=(make_test_case(),))]
    )

    observed_plan = await create_test_plan(context, "LOGIN")
    observed_cases = await design_test_cases(context, "LOGIN")

    assert observed_plan.requirement_ids == ("R-LOGIN",)
    assert observed_plan.blocked_requirement_ids == ("R-LOCKOUT",)
    assert observed_cases[0].requirement_ids == ("R-LOGIN",)
    assert "R-LOCKOUT" not in str(observed_cases)
    assert "Never invent" in str(fake.messages[-1].content)


@pytest.mark.asyncio
async def test_invalid_coverage_reference_is_rejected(tmp_path: Path) -> None:
    context, _fake, _analysis = setup_context(
        tmp_path,
        [TestCaseSet(cases=(make_test_case(requirement_ids=("R-UNKNOWN",)),))],
    )
    with pytest.raises(ValueError, match="coverage reference"):
        await design_test_cases(context, "LOGIN")


@pytest.mark.asyncio
async def test_expected_result_must_be_bounded_by_requirement_sources(tmp_path: Path) -> None:
    context, _fake, _analysis = setup_context(
        tmp_path,
        [TestCaseSet(cases=(make_test_case(expected_references=(reference(4),)),))],
    )
    with pytest.raises(ValueError, match="expected result source"):
        await design_test_cases(context, "LOGIN")


@pytest.mark.asyncio
async def test_expected_result_cannot_invent_a_rule_even_with_a_valid_citation(
    tmp_path: Path,
) -> None:
    invented = make_test_case().model_copy(
        update={
            "steps": (
                TestStep(
                    action="Submit invalid credentials.",
                    expected_result="The account locks for 30 minutes.",
                    expected_result_sources=(reference(3),),
                ),
            )
        }
    )
    context, _fake, _analysis = setup_context(tmp_path, [TestCaseSet(cases=(invented,))])
    with pytest.raises(ValueError, match="unsupported expected result"):
        await design_test_cases(context, "LOGIN")


@pytest.mark.parametrize(
    ("source_rule", "invented_result"),
    [
        ("The balance must be >= 10.", "The balance must be <= 10."),
        ("The temperature must be -5.", "The temperature must be 5."),
    ],
)
@pytest.mark.asyncio
async def test_expected_result_preserves_operators_and_numeric_signs(
    tmp_path: Path, source_rule: str, invented_result: str
) -> None:
    altered = make_test_case().model_copy(
        update={
            "steps": (
                TestStep(
                    action="Check the rule.",
                    expected_result=invented_result,
                    expected_result_sources=(reference(3),),
                ),
            )
        }
    )
    context, _fake, _analysis = setup_context(
        tmp_path, [TestCaseSet(cases=(altered,))], login_rule=source_rule
    )
    with pytest.raises(ValueError, match="unsupported expected result"):
        await design_test_cases(context, "LOGIN")


@pytest.mark.asyncio
async def test_rejected_scope_cannot_generate_cases(tmp_path: Path) -> None:
    context, fake, _analysis = setup_context(tmp_path, [TestCaseSet(cases=(make_test_case(),))])
    scopes = ScopeService(context.repo, project_id="P1", workflow_id="W1")
    scopes.review_scope(
        "LOGIN",
        ScopeDecision(action=ScopeAction.REJECT, actor="QA", human_reviewed=True),
    )
    with pytest.raises(ValueError, match="rejected"):
        await design_test_cases(context, "LOGIN")
    assert fake.messages == []


def test_case_requires_explicit_intent_and_scenario() -> None:
    payload = make_test_case().model_dump()
    del payload["intent"]
    del payload["scenario"]
    with pytest.raises(ValidationError):
        TestCase.model_validate(payload)


@pytest.mark.asyncio
async def test_validated_requirement_with_blocked_dependency_is_not_draft_eligible(
    tmp_path: Path,
) -> None:
    context, fake, analysis = setup_context(tmp_path, [TestCaseSet(cases=(make_test_case(),))])
    dependent = analysis.requirements[0].model_copy(update={"dependency_ids": ("R-LOCKOUT",)})
    context = replace(
        context,
        analysis=RequirementAnalysisDraft(
            requirements=(dependent, analysis.requirements[1]),
            clarifications=analysis.clarifications,
        ),
    )
    with pytest.raises(ValueError, match="eligible|dependency"):
        await design_test_cases(context, "LOGIN")
    assert fake.messages == []


@pytest.mark.asyncio
async def test_plan_must_explicitly_exclude_blocked_requirements(tmp_path: Path) -> None:
    plan = TestPlanDraft(
        scope_id="LOGIN",
        objective="Validate login.",
        requirement_ids=("R-LOGIN",),
        blocked_requirement_ids=("R-LOCKOUT",),
        in_scope=("Successful login",),
        exclusions=(),
        test_types=("functional",),
        entry_criteria=("Requirement available",),
        exit_criteria=("Cases pass",),
    )
    context, _fake, _analysis = setup_context(tmp_path, [plan])
    with pytest.raises(ValueError, match="exclusions"):
        await create_test_plan(context, "LOGIN")


def test_duplicate_case_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate test case ID"):
        TestCaseSet(cases=(make_test_case(), make_test_case()))


def test_plan_and_case_models_have_independent_review_identity() -> None:
    plan = TestPlanDraft(
        scope_id="LOGIN",
        objective="Validate login.",
        requirement_ids=("R-LOGIN",),
        in_scope=("Login",),
        test_types=("functional",),
        entry_criteria=("Requirement available",),
        exit_criteria=("Cases pass",),
    )
    case = make_test_case()
    assert plan.artifact_kind == "TEST_PLAN"
    assert case.artifact_kind == "TEST_CASES"
    assert "approval_id" not in TestPlanDraft.model_fields
    assert "approval_id" not in TestCase.model_fields


def test_planner_scenario_fixture_has_traceable_nonduplicate_cases() -> None:
    path = (
        Path(__file__).parents[2]
        / "benchmarks"
        / "datasets"
        / "synthetic"
        / "planner"
        / "scenario-coverage.json"
    )
    fixture = TestCaseSet.model_validate_json(path.read_text(encoding="utf-8"))
    assert {case.id for case in fixture.cases} == {"TC-LOGIN-001", "TC-LOGIN-002"}
    assert {requirement for case in fixture.cases for requirement in case.requirement_ids} == {
        "R-LOGIN"
    }


@pytest.mark.asyncio
async def test_planner_scenario_fixture_runs_through_offline_agent_validation(
    tmp_path: Path,
) -> None:
    fixture_root = Path(__file__).parents[2] / "benchmarks" / "datasets" / "synthetic" / "planner"
    source = tmp_path / "synthetic-planner-login.md"
    source.write_bytes((fixture_root / source.name).read_bytes())
    fixture = TestCaseSet.model_validate_json(
        (fixture_root / "scenario-coverage.json").read_text(encoding="utf-8")
    )
    context, fake, _analysis = setup_context(tmp_path, [fixture])
    context.repo.save_source_document(
        SourceDocument(
            id="synthetic-planner-login",
            project_id="P1",
            input_type="BRD",
            content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            source_path=source,
            snapshot_path=source,
            line_count=4,
        )
    )
    context.repo.create_workflow(
        WorkflowRecord(
            id="W2",
            project_id="P1",
            goal="Offline scenario coverage",
            thread_id="T2",
            source_snapshot_ids=("synthetic-planner-login",),
        )
    )
    ScopeService(context.repo, project_id="P1", workflow_id="W2").create_batch(
        [ScopeProposal(id="SCENARIOS", journey="Login", requirement_ids=("R-LOGIN",))],
        known_requirement_ids={"R-LOGIN"},
    )
    lines = source.read_text(encoding="utf-8").splitlines()
    requirement = Requirement(
        id="R-LOGIN",
        version=1,
        statement=lines[2],
        source_references=(
            SourceReference(document_id="synthetic-planner-login", start_line=3, end_line=3),
            SourceReference(document_id="synthetic-planner-login", start_line=4, end_line=4),
        ),
        source_quotes=(lines[2], lines[3]),
        readiness=RequirementReadiness.VALIDATED,
    )
    context = replace(
        context,
        workflow_id="W2",
        analysis=RequirementAnalysisDraft(requirements=(requirement,), clarifications=()),
    )

    observed = await design_test_cases(context, "SCENARIOS")
    assert {case.id for case in observed} == {"TC-LOGIN-001", "TC-LOGIN-002"}
    assert lines[2] in str(fake.messages[0].content)
    assert lines[3] in str(fake.messages[0].content)
    assert {
        step.expected_result_sources[0].start_line for case in observed for step in case.steps
    } == {
        3,
        4,
    }
