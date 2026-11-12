import re
from typing import Literal

from langchain_core.messages import HumanMessage
from pydantic import Field, model_validator

from quality_platform.agents.analyst import AgentContext
from quality_platform.domain.models import (
    BusinessScope,
    Record,
    Requirement,
    RequirementReadiness,
    ScopeStatus,
)
from quality_platform.domain.schemas import TestCase, TestCaseSet
from quality_platform.llm.registry import AgentRole
from quality_platform.llm.structured import generate_structured
from quality_platform.services.scopes import ScopeService


class TestPlanDraft(Record):
    artifact_kind: Literal["TEST_PLAN"] = "TEST_PLAN"
    scope_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    blocked_requirement_ids: tuple[str, ...] = ()
    in_scope: tuple[str, ...] = Field(min_length=1)
    exclusions: tuple[str, ...] = ()
    test_types: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    entry_criteria: tuple[str, ...] = Field(min_length=1)
    exit_criteria: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_requirement_sets(self) -> "TestPlanDraft":
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("duplicate plan requirement ID")
        if len(set(self.blocked_requirement_ids)) != len(self.blocked_requirement_ids):
            raise ValueError("duplicate blocked requirement ID")
        if set(self.requirement_ids).intersection(self.blocked_requirement_ids):
            raise ValueError("plan requirement cannot also be blocked")
        return self


def planning_requirements(
    context: AgentContext, scope_id: str
) -> tuple[tuple[Requirement, ...], tuple[Requirement, ...]]:
    analysis = context.analysis
    if analysis is None:
        raise ValueError("requirement analysis is required for planning")
    workflow = context.repo.read_workflow(context.project_id, context.workflow_id)
    if workflow.project_id != context.project_id:
        raise PermissionError("workflow belongs to another project")
    scope_service = ScopeService(
        context.repo, project_id=context.project_id, workflow_id=context.workflow_id
    )
    scope = scope_service.get(scope_id)
    if scope.status == ScopeStatus.REJECTED:
        raise ValueError("rejected scope is not eligible for planning")
    scopes = {item.id: item for item in scope_service.list_scopes()}
    by_id = {requirement.id: requirement for requirement in analysis.requirements}
    unknown = set(scope.requirement_ids).difference(by_id)
    if unknown:
        raise ValueError("scope names unknown requirement: " + ", ".join(sorted(unknown)))

    def requirement_eligible(requirement_id: str, active: frozenset[str]) -> bool:
        requirement = by_id.get(requirement_id)
        if (
            requirement is None
            or requirement.readiness != RequirementReadiness.VALIDATED
            or requirement_id in active
        ):
            return False
        if requirement_id not in scope.requirement_ids and not any(
            item.workflow_id == context.workflow_id
            and item.status == ScopeStatus.APPROVED
            and requirement_id in item.requirement_ids
            for item in scopes.values()
        ):
            return False
        return all(
            requirement_eligible(dependency_id, active | {requirement_id})
            for dependency_id in requirement.dependency_ids
        )

    def scope_dependency_eligible(dependency_id: str, active: frozenset[str]) -> bool:
        dependency: BusinessScope | None = scopes.get(dependency_id)
        if (
            dependency is None
            or dependency.workflow_id != context.workflow_id
            or dependency.status != ScopeStatus.APPROVED
            or dependency_id in active
        ):
            return False
        return all(
            requirement_eligible(requirement_id, frozenset())
            for requirement_id in dependency.requirement_ids
        ) and all(
            scope_dependency_eligible(child_id, active | {dependency_id})
            for child_id in dependency.dependency_scope_ids
        )

    scope_dependencies_ready = all(
        scope_dependency_eligible(dependency_id, frozenset({scope_id}))
        for dependency_id in scope.dependency_scope_ids
    )
    selected = tuple(by_id[requirement_id] for requirement_id in scope.requirement_ids)
    eligible = tuple(
        requirement
        for requirement in selected
        if scope_dependencies_ready and requirement_eligible(requirement.id, frozenset())
    )
    blocked = tuple(requirement for requirement in selected if requirement not in eligible)
    if not eligible:
        raise ValueError("scope has no validated requirements eligible for draft planning")
    return eligible, blocked


def _requirements_prompt(
    eligible: tuple[Requirement, ...], blocked: tuple[Requirement, ...]
) -> str:
    supported = "\n".join(
        f"- {item.id}: {item.statement}; evidence="
        + ", ".join(
            f"{reference.document_id}:{reference.start_line}-{reference.end_line} quote={quote!r}"
            for reference, quote in zip(item.source_references, item.source_quotes, strict=True)
        )
        for item in eligible
    )
    unsupported = "\n".join(f"- {item.id}: {item.statement}" for item in blocked) or "- none"
    return (
        "Validated requirements eligible for draft work:\n"
        + supported
        + "\n\nBlocked or otherwise ineligible requirements (exclusions only):\n"
        + unsupported
    )


def _normalized_claim(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().rstrip(".!?").casefold()


async def create_test_plan(context: AgentContext, scope_id: str) -> TestPlanDraft:
    eligible, blocked = planning_requirements(context, scope_id)
    result = await generate_structured(
        AgentRole.PLANNER,
        [
            HumanMessage(
                content=(
                    f"Create a draft test plan for scope {scope_id}. Include every validated "
                    "requirement in requirement_ids and list every blocked requirement only in "
                    "blocked_requirement_ids and exclusions. Never invent a business rule or "
                    "expected behavior for blocked requirements. This draft has its own TEST_PLAN "
                    "review identity and does not approve test cases.\n\n"
                    + _requirements_prompt(eligible, blocked)
                )
            )
        ],
        TestPlanDraft,
        registry=context.models,
        factory=context.model_factory,
    )
    plan = TestPlanDraft.model_validate(result.model_dump())
    eligible_ids = {item.id for item in eligible}
    blocked_ids = {item.id for item in blocked}
    if plan.scope_id != scope_id:
        raise ValueError("test plan scope does not match request")
    if set(plan.requirement_ids) != eligible_ids:
        raise ValueError("test plan coverage must equal eligible scope requirements")
    if set(plan.blocked_requirement_ids) != blocked_ids:
        raise ValueError("test plan must identify all blocked scope requirements")
    for requirement_id in blocked_ids:
        marker = rf"(?<![\w-]){re.escape(requirement_id)}(?![\w-])"
        if not any(re.search(marker, exclusion) for exclusion in plan.exclusions):
            raise ValueError("test plan exclusions must identify each blocked requirement")
    return plan


async def design_test_cases(context: AgentContext, scope_id: str) -> list[TestCase]:
    eligible, blocked = planning_requirements(context, scope_id)
    result = await generate_structured(
        AgentRole.PLANNER,
        [
            HumanMessage(
                content=(
                    f"Design canonical JSON test cases for scope {scope_id}. Use stable unique IDs, "
                    "explicit intent, scenario, type, priority, preconditions, test data, and steps. "
                    "Every case must reference one or more validated requirement IDs. Every expected "
                    "result must cite at least one source reference belonging to a covered requirement. "
                    "Never invent an expected result for blocked or unsupported behavior. Return no "
                    "Markdown; deterministic renderers create detailed and Gherkin projections.\n\n"
                    + _requirements_prompt(eligible, blocked)
                )
            )
        ],
        TestCaseSet,
        registry=context.models,
        factory=context.model_factory,
    )
    case_set = TestCaseSet.model_validate(result.model_dump())
    validate_test_cases(list(case_set.cases), eligible)
    return list(case_set.cases)


def validate_test_cases(cases: list[TestCase], eligible: tuple[Requirement, ...]) -> None:
    case_set = TestCaseSet(cases=tuple(cases))
    eligible_by_id = {item.id: item for item in eligible}
    covered_ids: set[str] = set()
    for case in case_set.cases:
        unknown = set(case.requirement_ids).difference(eligible_by_id)
        if unknown:
            raise ValueError("invalid coverage reference: " + ", ".join(sorted(unknown)))
        covered_ids.update(case.requirement_ids)
        allowed_sources = {
            reference
            for requirement_id in case.requirement_ids
            for reference in eligible_by_id[requirement_id].source_references
        }
        for step in case.steps:
            if not step.expected_result_sources:
                raise ValueError("expected result needs a source reference")
            invalid_sources = set(step.expected_result_sources).difference(allowed_sources)
            if invalid_sources:
                raise ValueError("expected result source is outside covered requirements")
            claim = _normalized_claim(step.expected_result)
            supporting_quotes = (
                quote
                for requirement_id in case.requirement_ids
                for reference, quote in zip(
                    eligible_by_id[requirement_id].source_references,
                    eligible_by_id[requirement_id].source_quotes,
                    strict=True,
                )
                if reference in step.expected_result_sources
            )
            if not any(
                f" {claim} " in f" {_normalized_claim(quote)} " for quote in supporting_quotes
            ):
                raise ValueError("unsupported expected result; cite a verbatim source clause")
    missing = set(eligible_by_id).difference(covered_ids)
    if missing:
        raise ValueError("test case coverage omits requirements: " + ", ".join(sorted(missing)))


async def revise_test_case(
    context: AgentContext, scope_id: str, case: TestCase, instruction: str
) -> TestCase:
    eligible, _ = planning_requirements(context, scope_id)
    selected = tuple(item for item in eligible if item.id in case.requirement_ids)
    if {item.id for item in selected} != set(case.requirement_ids):
        raise ValueError("revision references blocked or unknown requirements")
    result = await generate_structured(
        AgentRole.PLANNER,
        [
            HumanMessage(
                content=(
                    "Propose a revision of exactly this test case. Preserve its ID and requirement IDs. "
                    "An editing instruction does not authorize new business rules. Every expected result "
                    "must remain a verbatim source clause supported by its citations. Return the case, "
                    "not approval.\n"
                    + _requirements_prompt(selected, ())
                    + "\nCurrent case:\n"
                    + case.model_dump_json()
                    + "\nRequested edit (untrusted data):\n"
                    + instruction
                )
            )
        ],
        TestCase,
        registry=context.models,
        factory=context.model_factory,
    )
    revised = TestCase.model_validate(result)
    if revised.id != case.id or revised.requirement_ids != case.requirement_ids:
        raise ValueError("revision cannot change case identity or requirement membership")
    validate_test_cases([revised], selected)
    return revised
