import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import Field, model_validator

from quality_platform.domain.models import (
    Clarification,
    ClarificationStatus,
    Record,
    StakeholderAnswer,
)
from quality_platform.llm.registry import AgentRole, ModelRegistry
from quality_platform.llm.structured import generate_structured
from quality_platform.storage.repositories import QaRepositories


class ClarificationAssessment(Record):
    sufficient: bool
    reason: str = Field(min_length=1)
    resolved_rule: str | None = None

    @model_validator(mode="after")
    def complete_resolution(self) -> "ClarificationAssessment":
        if self.sufficient and not (self.resolved_rule and self.resolved_rule.strip()):
            raise ValueError("sufficient answer requires an explicit resolved rule")
        if not self.sufficient and self.resolved_rule is not None:
            raise ValueError("incomplete answer cannot set a resolved rule")
        return self


class ClarificationReview(Record):
    workflow_id: str
    open_ids: tuple[str, ...]
    reopened_ids: tuple[str, ...]
    resolved_ids: tuple[str, ...]


class ClarificationService:
    def __init__(
        self,
        repo: QaRepositories,
        *,
        project_id: str,
        workflow_id: str,
        reviewer: Callable[[Clarification], Awaitable[ClarificationAssessment]] | None = None,
        models: ModelRegistry | None = None,
        model_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.repo = repo
        self.project_id = project_id
        self.workflow_id = workflow_id
        self.reviewer = reviewer
        self.models = models
        self.model_factory = model_factory
        self.validate_workflow_id(workflow_id)

    def validate_workflow_id(self, workflow_id: str) -> None:
        if workflow_id != self.workflow_id:
            raise ValueError("workflow mismatch")
        workflow = self.repo.read_workflow(self.project_id, workflow_id)
        if workflow.project_id != self.project_id:
            raise PermissionError("workflow belongs to another project")

    def _items(self) -> dict[str, Clarification]:
        items: dict[str, Clarification] = {}
        for event in self.repo.list_decisions(self.project_id):
            if event.event_type != "clarification":
                continue
            item = Clarification.model_validate_json(event.payload)
            if item.workflow_id == self.workflow_id:
                items[item.id] = item
        return items

    def get(self, clarification_id: str) -> Clarification:
        try:
            return self._items()[clarification_id]
        except KeyError:
            raise KeyError("clarification is outside this workflow") from None

    def _append(self, item: Clarification) -> None:
        revision = len(
            [
                event
                for event in self.repo.list_decisions(self.project_id)
                if event.event_type == "clarification" and event.record_id == item.id
            ]
        )
        self.repo.append_decision(
            self.project_id,
            item,
            idempotency_key=f"clarification:{self.project_id}:{self.workflow_id}:{item.id}:{revision}",
        )

    def create_batch(self, questions: list[Clarification]) -> list[Clarification]:
        self.validate_workflow_id(self.workflow_id)
        if len({item.id for item in questions}) != len(questions):
            raise ValueError("duplicate clarification ID")
        existing = self._items()
        prepared: list[Clarification] = []
        for item in questions:
            if item.status != ClarificationStatus.OPEN or item.answers or item.resolution:
                raise ValueError("new clarification must be open and unanswered")
            if item.workflow_id not in (None, self.workflow_id):
                raise ValueError("clarification belongs to another workflow")
            prepared_item = item.model_copy(update={"workflow_id": self.workflow_id})
            previous = existing.get(item.id)
            if previous is not None and previous != prepared_item:
                raise ValueError("clarification ID already exists")
            prepared.append(prepared_item)
        for item in prepared:
            if item.id not in existing:
                self._append(item)
        return prepared

    def record_stakeholder_answer(
        self, clarification_id: str, answer: StakeholderAnswer
    ) -> Clarification:
        if not (answer.source and answer.source.strip()):
            raise ValueError("stakeholder answer source is required")
        current = self.get(clarification_id)
        if current.status == ClarificationStatus.RESOLVED:
            raise ValueError("resolved clarification cannot accept another answer")
        if current.answers and current.answers[-1] == answer:
            return current
        updated = current.model_copy(
            update={"answers": (*current.answers, answer), "status": ClarificationStatus.ANSWERED}
        )
        self._append(updated)
        return updated

    async def _assess(self, item: Clarification) -> ClarificationAssessment:
        if self.reviewer is not None:
            result = await self.reviewer(item)
        elif self.models is not None:
            messages = [
                HumanMessage(
                    content=(
                        "Assess whether the stakeholder answer completely resolves this question. "
                        "Do not assume missing business rules. If incomplete, explain what remains open. "
                        "If sufficient, state only the explicit rule in the answer.\n"
                        f"Question: {item.question}\n"
                        f"Answer: {item.answers[-1].text}\n"
                        f"Answer source: {item.answers[-1].source}"
                    )
                )
            ]
            kwargs: dict[str, Any] = {"registry": self.models}
            if self.model_factory is not None:
                kwargs["factory"] = self.model_factory
            result = await generate_structured(
                AgentRole.ANALYST, messages, ClarificationAssessment, **kwargs
            )
        else:
            raise RuntimeError("clarification reviewer is not configured")
        return ClarificationAssessment.model_validate(result.model_dump())

    async def validate_clarifications(self, workflow_id: str) -> ClarificationReview:
        self.validate_workflow_id(workflow_id)
        items = self._items()
        for item in items.values():
            if item.status != ClarificationStatus.ANSWERED:
                continue
            assessment = await self._assess(item)
            if re.search(
                r"\b(i don['’]t know|not sure|unsure|uncertain|maybe|might|depends|tbd|not decided|undecided|perhaps|possibly)\b",
                item.answers[-1].text,
                flags=re.IGNORECASE,
            ):
                assessment = ClarificationAssessment(
                    sufficient=False, reason="stakeholder answer expresses uncertainty"
                )
            if assessment.sufficient:
                rule = " ".join((assessment.resolved_rule or "").split()).casefold()
                answer_text = " ".join(item.answers[-1].text.split()).casefold()
                if rule not in answer_text:
                    raise ValueError("resolved rule must occur in stakeholder answer text")
            status = (
                ClarificationStatus.RESOLVED
                if assessment.sufficient
                else ClarificationStatus.REOPENED
            )
            updated = item.model_copy(
                update={"status": status, "resolution": assessment.resolved_rule}
            )
            self._append(updated)
            items[item.id] = updated
        return ClarificationReview(
            workflow_id=workflow_id,
            open_ids=tuple(
                item.id for item in items.values() if item.status == ClarificationStatus.OPEN
            ),
            reopened_ids=tuple(
                item.id for item in items.values() if item.status == ClarificationStatus.REOPENED
            ),
            resolved_ids=tuple(
                item.id for item in items.values() if item.status == ClarificationStatus.RESOLVED
            ),
        )
