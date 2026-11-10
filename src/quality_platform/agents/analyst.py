import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import model_validator

from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    ClarificationStatus,
    Record,
    Requirement,
    RequirementReadiness,
    SourceReference,
)
from quality_platform.llm.model_factory import create_chat_model
from quality_platform.llm.registry import AgentRole, ModelRegistry
from quality_platform.llm.structured import generate_structured
from quality_platform.storage.repositories import QaRepositories


class RequirementAnalysisDraft(Record):
    requirements: tuple[Requirement, ...]
    clarifications: tuple[Clarification, ...]

    @model_validator(mode="after")
    def strict_clarifications(self) -> "RequirementAnalysisDraft":
        requirements = {requirement.id: requirement for requirement in self.requirements}
        if len(requirements) != len(self.requirements):
            raise ValueError("duplicate requirement ID")
        for requirement in self.requirements:
            if requirement.readiness == RequirementReadiness.VALIDATED:
                if len(requirement.source_quotes) != len(requirement.source_references):
                    raise ValueError("validated requirement needs a quote for each source")
                statement = _normalized(requirement.statement)
                if not any(statement == _normalized(quote) for quote in requirement.source_quotes):
                    raise ValueError("validated requirement must be verbatim from source quote")
        if len({item.id for item in self.clarifications}) != len(self.clarifications):
            raise ValueError("duplicate clarification ID")
        questioned_ids: set[str] = set()
        for item in self.clarifications:
            if item.status != ClarificationStatus.OPEN or item.answers or item.resolution:
                raise ValueError("new clarification must be open and unanswered")
            if not item.affected_requirement_ids:
                raise ValueError("clarification must identify affected requirements")
            if (
                item.issue_type == ClarificationIssueType.CROSS_DOCUMENT_CONFLICT
                and len({reference.document_id for reference in item.source_references}) < 2
            ):
                raise ValueError("cross-document conflict needs two documents")
            for requirement_id in item.affected_requirement_ids:
                questioned_ids.add(requirement_id)
                affected = requirements.get(requirement_id)
                if affected is None:
                    raise ValueError("clarification names unknown requirement")
                if affected.readiness != RequirementReadiness.BLOCKED:
                    raise ValueError("unresolved clarification must block affected requirement")
                if not set(item.source_references).intersection(affected.source_references):
                    raise ValueError("clarification evidence does not support affected requirement")
        if any(
            requirement.readiness == RequirementReadiness.BLOCKED
            and requirement.id not in questioned_ids
            for requirement in self.requirements
        ):
            raise ValueError("blocked requirement needs a clarification question")
        return self

    @property
    def draft_eligible_requirement_ids(self) -> tuple[str, ...]:
        return tuple(
            item.id
            for item in self.requirements
            if item.readiness == RequirementReadiness.VALIDATED
        )


@dataclass(frozen=True)
class AgentContext:
    project_id: str
    workflow_id: str
    repo: QaRepositories
    models: ModelRegistry
    model_factory: Callable[[Any], Any] = create_chat_model
    analysis: RequirementAnalysisDraft | None = None


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _check_reference(reference: SourceReference, lines_by_id: dict[str, list[str]]) -> None:
    lines = lines_by_id.get(reference.document_id)
    if lines is None or reference.end_line > len(lines):
        raise ValueError("source reference is outside selected documents")
    if not any(line.strip() for line in lines[reference.start_line - 1 : reference.end_line]):
        raise ValueError("source reference points to empty content")


def _check_quote(quote: str, reference: SourceReference, lines_by_id: dict[str, list[str]]) -> None:
    lines = lines_by_id[reference.document_id]
    content = "\n".join(lines[reference.start_line - 1 : reference.end_line])
    sentences = {_normalized(sentence) for sentence in re.split(r"(?<=[.!?])\s+", content)}
    if not quote.strip() or _normalized(quote) not in sentences:
        raise ValueError("source quote does not occur in cited lines")


def _check_numeric_conflicts(
    draft: RequirementAnalysisDraft, lines_by_id: dict[str, list[str]]
) -> None:
    patterns: dict[str, list[tuple[SourceReference, tuple[str, ...]]]] = {}
    for document_id, lines in lines_by_id.items():
        for line_number, line in enumerate(lines, 1):
            for sentence in re.split(r"(?<=[.!?])\s+", line):
                if not re.search(
                    r"\b(after|before|within|at least|at most|maximum|minimum|must|shall|locks?|expires?|limit)\b",
                    sentence.casefold(),
                ):
                    continue
                numbers = tuple(re.findall(r"\b\d+(?:\.\d+)?\b", sentence))
                if not numbers:
                    continue
                template = _normalized(re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", sentence))
                if len(template.split()) < 4:
                    continue
                patterns.setdefault(template, []).append(
                    (
                        SourceReference(
                            document_id=document_id,
                            start_line=line_number,
                            end_line=line_number,
                        ),
                        numbers,
                    )
                )
    for entries in patterns.values():
        if (
            len({reference.document_id for reference, _ in entries}) < 2
            or len({numbers for _, numbers in entries}) < 2
        ):
            continue
        required = {
            (reference.document_id, reference.start_line, reference.end_line)
            for reference, _ in entries
        }
        reported = any(
            item.issue_type == ClarificationIssueType.CROSS_DOCUMENT_CONFLICT
            and required.issubset(
                {
                    (reference.document_id, reference.start_line, reference.end_line)
                    for reference in item.source_references
                }
            )
            for item in draft.clarifications
        )
        if not reported:
            raise ValueError("unreported numeric conflict across documents")
        if any(
            requirement.readiness == RequirementReadiness.VALIDATED
            and any(
                (reference.document_id, reference.start_line, reference.end_line) in required
                for reference in requirement.source_references
            )
            for requirement in draft.requirements
        ):
            raise ValueError("numeric conflict cannot produce a validated requirement")


async def analyze_documents(
    context: AgentContext, source_ids: list[str]
) -> RequirementAnalysisDraft:
    if not source_ids or len(source_ids) != len(set(source_ids)):
        raise ValueError("source IDs must be nonempty and unique")
    workflow = context.repo.read_workflow(context.project_id, context.workflow_id)
    if workflow.project_id != context.project_id:
        raise PermissionError("workflow belongs to another project")
    lines_by_id: dict[str, list[str]] = {}
    inputs: list[str] = []
    for source_id in source_ids:
        if source_id not in workflow.source_snapshot_ids:
            raise ValueError("source is outside workflow baseline")
        document = context.repo.read_source_document(context.project_id, source_id)
        owner = context.repo.read_project_record(context.project_id, document.project_id)
        project_root = owner.root.resolve()
        snapshot = Path(document.snapshot_path).resolve(strict=True)
        if not snapshot.is_relative_to(project_root):
            raise ValueError("source snapshot is outside project")
        raw = snapshot.read_bytes()
        if hashlib.sha256(raw).hexdigest() != document.content_hash:
            raise ValueError("source snapshot hash mismatch")
        text = raw.decode("utf-8")
        lines = text.splitlines()
        if len(lines) != document.line_count:
            raise ValueError("source line count mismatch")
        lines_by_id[source_id] = lines
        numbered = "\n".join(f"{index}: {line}" for index, line in enumerate(lines, 1))
        inputs.append(f"Document {source_id} ({document.input_type}):\n{numbered}")
    instruction = (
        "Extract requirements with exact document, line references, and source quotes. "
        "Mark a requirement VALIDATED only when its statement occurs verbatim in a cited quote. "
        "Otherwise leave it EXTRACTED or BLOCKED. Identify every test-relevant "
        "missing rule, ambiguity, contradiction, and cross-document conflict. There is no source hierarchy. "
        "Never choose a side of a conflict or invent an expected result. Ask specific questions for "
        "unresolved rules. Mark affected requirements BLOCKED; independent clear requirements may be "
        "VALIDATED for draft work only. Return all questions in one batch.\n\n"
        + "\n\n".join(inputs)
    )
    result = await generate_structured(
        AgentRole.ANALYST,
        [HumanMessage(content=instruction)],
        RequirementAnalysisDraft,
        registry=context.models,
        factory=context.model_factory,
    )
    draft = RequirementAnalysisDraft.model_validate(result.model_dump())
    for requirement in draft.requirements:
        for reference in requirement.source_references:
            _check_reference(reference, lines_by_id)
        for reference, quote in zip(
            requirement.source_references, requirement.source_quotes, strict=False
        ):
            _check_quote(quote, reference, lines_by_id)
    for item in draft.clarifications:
        for reference in item.source_references:
            _check_reference(reference, lines_by_id)
    _check_numeric_conflicts(draft, lines_by_id)
    return draft
