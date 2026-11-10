import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field

from quality_platform.agents.planner import TestPlanDraft
from quality_platform.domain.models import Record, Requirement
from quality_platform.domain.schemas import TestCase, TestCaseSet
from quality_platform.rendering.markdown import (
    bullet_list,
    inline_text,
    render_test_plan,
    source_label,
    table_cell,
)

RenderingStyle = Literal["detailed", "gherkin", "both"]


class ChangeManifest(Record):
    before_hash: str = Field(min_length=1)
    after_hash: str = Field(min_length=1)
    added_ids: tuple[str, ...] = ()
    removed_ids: tuple[str, ...] = ()
    changed_ids: tuple[str, ...] = ()
    unchanged_ids: tuple[str, ...] = ()


def _canonical_json(cases: list[TestCase]) -> str:
    validated = TestCaseSet(cases=tuple(cases))
    ordered = validated.model_copy(
        update={"cases": tuple(sorted(validated.cases, key=lambda case: case.id))}
    )
    return (
        json.dumps(
            ordered.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _render_detailed(cases: list[TestCase]) -> str:
    sections = ["# Test Cases", ""]
    for case in cases:
        sections.extend(
            [
                f"## {inline_text(case.id)} - {inline_text(case.title)}",
                "",
                f"**Intent:** {inline_text(case.intent)}",
                f"**Requirements:** {', '.join(inline_text(item) for item in case.requirement_ids)}",
                f"**Scenario:** {inline_text(case.scenario)}",
                f"**Type:** {inline_text(case.case_type)}",
                f"**Priority:** {inline_text(case.priority)}",
                "",
                "### Preconditions",
                "",
                bullet_list(case.preconditions),
                "",
                "### Test data",
                "",
                *(
                    ["| Name | Value |", "| --- | --- |"]
                    + [
                        f"| {table_cell(name)} | {table_cell(value)} |"
                        for name, value in sorted(case.test_data.items())
                    ]
                    if case.test_data
                    else ["None"]
                ),
                "",
                "### Steps",
                "",
                "| # | Action | Expected result | Evidence |",
                "| --- | --- | --- | --- |",
            ]
        )
        for index, step in enumerate(case.steps, 1):
            evidence = ", ".join(source_label(item) for item in step.expected_result_sources)
            sections.append(
                f"| {index} | {table_cell(step.action)} | "
                f"{table_cell(step.expected_result)} | {table_cell(evidence)} |"
            )
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def _render_gherkin(cases: list[TestCase]) -> str:
    sections = ["# Test Cases - Gherkin", ""]
    for case in cases:
        sections.extend(
            [
                f"## {inline_text(case.id)} - {inline_text(case.title)}",
                "",
                f"> Intent: {inline_text(case.intent)}",
                f"> Requirements: {', '.join(inline_text(item) for item in case.requirement_ids)}",
                "",
                "```gherkin",
                f"Feature: {inline_text(case.title)}",
                f"  Scenario: [{inline_text(case.id)}] {inline_text(case.scenario)}",
            ]
        )
        if case.preconditions:
            for index, precondition in enumerate(case.preconditions):
                keyword = "Given" if index == 0 else "And"
                sections.append(f"    {keyword} {inline_text(precondition)}")
        else:
            sections.append("    Given no additional preconditions")
        for name, value in sorted(case.test_data.items()):
            sections.append(
                f"    And {inline_text(name)} is {json.dumps(value, ensure_ascii=False)}"
            )
        for step in case.steps:
            sections.append(f"    When {inline_text(step.action)}")
            sections.append(f"    Then {inline_text(step.expected_result)}")
            sources = ", ".join(source_label(item) for item in step.expected_result_sources)
            if sources:
                sections.append(f"    # Expected-result evidence: {sources}")
        sections.extend(["```", ""])
    return "\n".join(sections).rstrip() + "\n"


def render_test_cases(cases: list[TestCase], style: RenderingStyle) -> dict[str, str]:
    if style not in {"detailed", "gherkin", "both"}:
        raise ValueError(f"unsupported test case rendering style: {style}")
    ordered = sorted(TestCaseSet(cases=tuple(cases)).cases, key=lambda case: case.id)
    rendered = {"json": _canonical_json(ordered)}
    if style in {"detailed", "both"}:
        rendered["detailed"] = _render_detailed(ordered)
    if style in {"gherkin", "both"}:
        rendered["gherkin"] = _render_gherkin(ordered)
    return rendered


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_test_case_artifacts(
    directory: Path, cases: list[TestCase], style: RenderingStyle
) -> dict[str, Path]:
    rendered = render_test_cases(cases, style)
    names = {
        "json": "test-cases.json",
        "detailed": "test-cases.detailed.md",
        "gherkin": "test-cases.gherkin.md",
    }
    written: dict[str, Path] = {}
    for kind, content in rendered.items():
        target = directory / names[kind]
        _atomic_write(target, content)
        written[kind] = target
    return written


def write_test_plan_artifact(
    directory: Path, plan: TestPlanDraft, requirements: Mapping[str, Requirement]
) -> Path:
    target = directory / "test-plan.md"
    _atomic_write(target, render_test_plan(plan, requirements))
    return target


def build_change_manifest(before: list[TestCase], after: list[TestCase]) -> ChangeManifest:
    before_set = TestCaseSet(cases=tuple(before))
    after_set = TestCaseSet(cases=tuple(after))
    before_json = _canonical_json(list(before_set.cases))
    after_json = _canonical_json(list(after_set.cases))
    before_by_id = {case.id: case for case in before_set.cases}
    after_by_id = {case.id: case for case in after_set.cases}
    common = set(before_by_id).intersection(after_by_id)
    return ChangeManifest(
        before_hash=hashlib.sha256(before_json.encode("utf-8")).hexdigest(),
        after_hash=hashlib.sha256(after_json.encode("utf-8")).hexdigest(),
        added_ids=tuple(sorted(set(after_by_id).difference(before_by_id))),
        removed_ids=tuple(sorted(set(before_by_id).difference(after_by_id))),
        changed_ids=tuple(
            sorted(case_id for case_id in common if before_by_id[case_id] != after_by_id[case_id])
        ),
        unchanged_ids=tuple(
            sorted(case_id for case_id in common if before_by_id[case_id] == after_by_id[case_id])
        ),
    )
