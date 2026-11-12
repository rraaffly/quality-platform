from collections.abc import Mapping
from typing import TYPE_CHECKING

from quality_platform.agents.planner import TestPlanDraft
from quality_platform.domain.models import Requirement, SourceReference

if TYPE_CHECKING:
    from quality_platform.agents.analyst import RequirementAnalysisDraft


def bullet_list(values: tuple[str, ...]) -> str:
    return "\n".join(f"- {inline_text(value)}" for value in values) if values else "- None"


def inline_text(value: str) -> str:
    return " ".join(value.split())


def source_label(reference: SourceReference) -> str:
    document_id = inline_text(reference.document_id)
    if reference.start_line == reference.end_line:
        return f"{document_id}:L{reference.start_line}"
    return f"{document_id}:L{reference.start_line}-L{reference.end_line}"


def table_cell(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("|", "\\|").replace("\n", "<br>")


def render_requirement_analysis(analysis: "RequirementAnalysisDraft", workflow_id: str) -> str:
    sections = [
        "# Requirement Analysis",
        "",
        "Status: DRAFT",
        f"Workflow: {inline_text(workflow_id)}",
        "",
        "## Requirement baseline",
        "",
    ]
    for item in analysis.requirements:
        sections.extend(
            [
                f"### {inline_text(item.id)} v{item.version}",
                "",
                f"Readiness: {item.readiness}",
                inline_text(item.statement),
                "Sources: " + ", ".join(source_label(ref) for ref in item.source_references),
                "Dependencies: " + (", ".join(item.dependency_ids) or "None"),
                "",
            ]
        )
    sections.extend(["## Clarifications and exclusions", ""])
    sections.extend(
        f"- {inline_text(item.id)}: {inline_text(item.question)}; excluded from final work: "
        + ", ".join(item.affected_requirement_ids)
        for item in analysis.clarifications
    )
    if not analysis.clarifications:
        sections.append("- None")
    sections.extend(
        [
            "",
            "Review scope: only the requirement versions and source references listed above.",
            "Human scope/baseline approval is required before final artifact approval.",
        ]
    )
    return "\n".join(sections).rstrip() + "\n"


def render_test_plan(plan: TestPlanDraft, requirements: Mapping[str, Requirement]) -> str:
    referenced_ids = (*plan.requirement_ids, *plan.blocked_requirement_ids)
    missing = set(referenced_ids).difference(requirements)
    if missing:
        raise ValueError(
            "test plan requirement baseline is incomplete: " + ", ".join(sorted(missing))
        )

    def requirement_line(requirement_id: str) -> str:
        requirement = requirements[requirement_id]
        sources = ", ".join(source_label(item) for item in requirement.source_references)
        return f"{inline_text(requirement.id)} v{requirement.version} — {sources}"

    sections = [
        "# Test Plan",
        "",
        "Status: DRAFT",
        f"Scope: {inline_text(plan.scope_id)}",
        "",
        "## Objective",
        "",
        inline_text(plan.objective),
        "",
        "## Requirement baseline",
        "",
        bullet_list(tuple(requirement_line(item) for item in plan.requirement_ids)),
        "",
        "## Blocked requirements",
        "",
        bullet_list(tuple(requirement_line(item) for item in plan.blocked_requirement_ids)),
    ]
    for heading, values in (
        ("In scope", plan.in_scope),
        ("Exclusions", plan.exclusions),
        ("Test types", plan.test_types),
        ("Risks", plan.risks),
        ("Entry criteria", plan.entry_criteria),
        ("Exit criteria", plan.exit_criteria),
    ):
        sections.extend(["", f"## {heading}", "", bullet_list(values)])
    return "\n".join(sections).rstrip() + "\n"
