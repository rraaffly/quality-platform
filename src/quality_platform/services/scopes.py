import hashlib
from collections.abc import Iterable
from enum import StrEnum

from langchain_core.messages import HumanMessage
from pydantic import Field, model_validator

from quality_platform.agents.analyst import AgentContext
from quality_platform.domain.models import (
    BusinessScope,
    Clarification,
    ClarificationStatus,
    Record,
    ScopeStatus,
)
from quality_platform.llm.registry import AgentRole
from quality_platform.llm.structured import generate_structured
from quality_platform.storage.repositories import QaRepositories


class ScopeProposal(Record):
    id: str = Field(min_length=1)
    journey: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    excluded_requirement_ids: tuple[str, ...] = ()
    dependency_scope_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_membership(self) -> "ScopeProposal":
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("duplicate scope requirement ID")
        if set(self.requirement_ids).intersection(self.excluded_requirement_ids):
            raise ValueError("scope requirement cannot be included and excluded")
        if self.id in self.dependency_scope_ids:
            raise ValueError("scope cannot depend on itself")
        return self


class ScopeProposalSet(Record):
    proposals: tuple[ScopeProposal, ...]

    @model_validator(mode="after")
    def unique_ids(self) -> "ScopeProposalSet":
        if len({item.id for item in self.proposals}) != len(self.proposals):
            raise ValueError("duplicate scope proposal ID")
        return self


class ScopeAction(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MERGE = "MERGE"
    SPLIT = "SPLIT"


class ScopeDecision(Record):
    action: ScopeAction
    actor: str = Field(min_length=1)
    human_reviewed: bool = False
    reason: str | None = None
    merge_with_ids: tuple[str, ...] = ()
    replacements: tuple[ScopeProposal, ...] = ()


class ScopeChange(Record):
    workflow_id: str
    actor: str
    scopes: tuple[BusinessScope, ...]


async def propose_scopes(context: AgentContext) -> list[ScopeProposal]:
    analysis = context.analysis
    if analysis is None:
        raise ValueError("requirement analysis is required for scope proposal")
    workflow = context.repo.read_workflow(context.project_id, context.workflow_id)
    if workflow.project_id != context.project_id:
        raise PermissionError("workflow belongs to another project")
    requirements = "\n".join(
        f"{item.id}: {item.statement} ({item.readiness})" for item in analysis.requirements
    )
    result = await generate_structured(
        AgentRole.ANALYST,
        [
            HumanMessage(
                content=(
                    "Propose business journey or capability scopes from these canonical requirements. "
                    "A requirement may belong to multiple scopes without being duplicated. "
                    "Membership does not imply dependency; list only explicit dependencies. "
                    "All scopes are proposals until a QA human reviews them.\n" + requirements
                )
            )
        ],
        ScopeProposalSet,
        registry=context.models,
        factory=context.model_factory,
    )
    proposals = ScopeProposalSet.model_validate(result.model_dump()).proposals
    known_ids = {item.id for item in analysis.requirements}
    covered: set[str] = set()
    for proposal in proposals:
        unknown = set(proposal.requirement_ids).difference(known_ids)
        if unknown:
            raise ValueError(
                "scope proposal names unknown requirement: " + ", ".join(sorted(unknown))
            )
        covered.update(proposal.requirement_ids)
    if known_ids.difference(covered):
        raise ValueError(
            "scope proposals omit requirements: " + ", ".join(sorted(known_ids - covered))
        )
    return list(proposals)


class ScopeService:
    def __init__(self, repo: QaRepositories, *, project_id: str, workflow_id: str) -> None:
        self.repo = repo
        self.project_id = project_id
        self.workflow_id = workflow_id
        workflow = repo.read_workflow(project_id, workflow_id)
        if workflow.project_id != project_id:
            raise PermissionError("workflow belongs to another project")

    def _items(self) -> dict[str, BusinessScope]:
        scopes: dict[str, BusinessScope] = {}
        for event in self.repo.list_decisions(self.project_id):
            if event.event_type != "scope_change":
                continue
            change = ScopeChange.model_validate_json(event.payload)
            if change.workflow_id != self.workflow_id:
                continue
            for item in change.scopes:
                scopes[item.id] = item
        return scopes

    def get(self, scope_id: str) -> BusinessScope:
        try:
            return self._items()[scope_id]
        except KeyError:
            raise KeyError("scope is outside this workflow") from None

    def list_scopes(self) -> list[BusinessScope]:
        return list(self._items().values())

    def _append(self, actor: str, scopes: Iterable[BusinessScope]) -> None:
        change = ScopeChange(workflow_id=self.workflow_id, actor=actor, scopes=tuple(scopes))
        payload = change.model_dump_json()
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.repo.append_scope_change(
            self.project_id,
            self.workflow_id,
            payload,
            idempotency_key=f"scope:{self.project_id}:{self.workflow_id}:{digest}",
        )

    def create_batch(
        self, proposals: list[ScopeProposal], *, known_requirement_ids: set[str]
    ) -> list[BusinessScope]:
        batch = ScopeProposalSet(proposals=tuple(proposals)).proposals
        existing = self._items()
        prepared: list[BusinessScope] = []
        new: list[BusinessScope] = []
        for proposal in batch:
            unknown = set(proposal.requirement_ids).difference(known_requirement_ids)
            if unknown:
                raise ValueError("unknown requirement: " + ", ".join(sorted(unknown)))
            item = BusinessScope(
                id=proposal.id,
                workflow_id=self.workflow_id,
                journey=proposal.journey,
                requirement_ids=proposal.requirement_ids,
                excluded_requirement_ids=proposal.excluded_requirement_ids,
                dependency_scope_ids=proposal.dependency_scope_ids,
            )
            previous = existing.get(item.id)
            if previous is not None and previous != item:
                raise ValueError("scope ID already exists")
            prepared.append(item)
            if previous is None:
                new.append(item)
        if new:
            self._append("ANALYST", new)
        return prepared

    def require_approved(self, scope_id: str) -> BusinessScope:
        item = self.get(scope_id)
        if item.status != ScopeStatus.APPROVED:
            raise ValueError("approved scope required")
        return item

    def _unresolved_questions(self, requirement_ids: tuple[str, ...]) -> tuple[str, ...]:
        latest: dict[str, Clarification] = {}
        for event in self.repo.list_decisions(self.project_id):
            if event.event_type != "clarification":
                continue
            item = Clarification.model_validate_json(event.payload)
            if item.workflow_id == self.workflow_id:
                latest[item.id] = item
        return tuple(
            item.id
            for item in latest.values()
            if item.status != ClarificationStatus.RESOLVED
            and (
                not item.affected_requirement_ids
                or set(item.affected_requirement_ids).intersection(requirement_ids)
            )
        )

    def review_scope(self, scope_id: str, decision: ScopeDecision) -> BusinessScope:
        if not decision.human_reviewed:
            raise ValueError("human scope review required")
        current = self.get(scope_id)
        if (
            current.status == ScopeStatus.APPROVED
            and decision.action == ScopeAction.APPROVE
            and current.reviewed_by == decision.actor
        ):
            return current
        if current.status != ScopeStatus.PROPOSED:
            raise ValueError("only proposed scopes can be reviewed")
        if decision.action == ScopeAction.APPROVE:
            if decision.merge_with_ids or decision.replacements:
                raise ValueError("approve decision cannot include replacements")
            unresolved = self._unresolved_questions(current.requirement_ids)
            if unresolved:
                raise ValueError("unresolved clarifications: " + ", ".join(unresolved))
            changed = current.model_copy(
                update={"status": ScopeStatus.APPROVED, "reviewed_by": decision.actor}
            )
            self._append(decision.actor, [changed])
            return changed
        if decision.action == ScopeAction.REJECT:
            if decision.merge_with_ids or decision.replacements:
                raise ValueError("reject decision cannot include replacements")
            changed = current.model_copy(
                update={"status": ScopeStatus.REJECTED, "reviewed_by": decision.actor}
            )
            self._append(decision.actor, [changed])
            return changed
        existing = self._items()
        if decision.action == ScopeAction.MERGE:
            if not decision.merge_with_ids or len(decision.replacements) != 1:
                raise ValueError("merge needs other scopes and one replacement")
            originals = [current]
            for other_id in decision.merge_with_ids:
                other = existing.get(other_id)
                if other is None or other.status != ScopeStatus.PROPOSED or other.id == scope_id:
                    raise ValueError("merge target must be another proposed scope")
                originals.append(other)
            if len({item.id for item in originals}) != len(originals):
                raise ValueError("duplicate merge target")
            expected_members = set().union(*(set(item.requirement_ids) for item in originals))
        else:
            if decision.merge_with_ids or len(decision.replacements) < 2:
                raise ValueError("split needs two or more replacements")
            originals = [current]
            expected_members = set(current.requirement_ids)
        replacement_ids = {item.id for item in decision.replacements}
        if len(replacement_ids) != len(decision.replacements) or replacement_ids.intersection(
            existing
        ):
            raise ValueError("replacement scope IDs must be new and unique")
        actual_members = set().union(*(set(item.requirement_ids) for item in decision.replacements))
        if actual_members != expected_members:
            raise ValueError("scope membership must be preserved by merge or split")
        if decision.action == ScopeAction.MERGE:
            expected_exclusions = set().union(
                *(set(item.excluded_requirement_ids) for item in originals)
            )
            if set(decision.replacements[0].excluded_requirement_ids) != expected_exclusions:
                raise ValueError("scope exclusions must be preserved by merge")
        elif any(
            set(item.excluded_requirement_ids) != set(current.excluded_requirement_ids)
            for item in decision.replacements
        ):
            raise ValueError("scope exclusions must be preserved by split")
        expected_dependencies = set().union(*(set(item.dependency_scope_ids) for item in originals))
        expected_dependencies.difference_update(item.id for item in originals)
        actual_dependencies = set().union(
            *(set(item.dependency_scope_ids) for item in decision.replacements)
        )
        if actual_dependencies != expected_dependencies:
            raise ValueError("scope dependencies must be preserved by merge or split")
        changed_originals = [
            item.model_copy(update={"status": ScopeStatus.REJECTED, "reviewed_by": decision.actor})
            for item in originals
        ]
        created = [
            BusinessScope(
                id=item.id,
                workflow_id=self.workflow_id,
                journey=item.journey,
                requirement_ids=item.requirement_ids,
                excluded_requirement_ids=item.excluded_requirement_ids,
                dependency_scope_ids=item.dependency_scope_ids,
            )
            for item in decision.replacements
        ]
        old_ids = {item.id for item in originals}
        new_ids = tuple(item.id for item in created)
        rewired: list[BusinessScope] = []
        for item in existing.values():
            if item.id in old_ids or item.status == ScopeStatus.REJECTED:
                continue
            if not old_ids.intersection(item.dependency_scope_ids):
                continue
            dependency_ids: list[str] = []
            for dependency_id in item.dependency_scope_ids:
                if dependency_id in old_ids:
                    dependency_ids.extend(new_ids)
                else:
                    dependency_ids.append(dependency_id)
            rewired.append(
                item.model_copy(
                    update={
                        "dependency_scope_ids": tuple(dict.fromkeys(dependency_ids)),
                        "status": ScopeStatus.PROPOSED,
                        "reviewed_by": None,
                        "approval_id": None,
                    }
                )
            )
        self._append(decision.actor, [*changed_originals, *created, *rewired])
        return changed_originals[0]
