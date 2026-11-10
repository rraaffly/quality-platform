from collections.abc import Mapping

from pydantic import Field

from quality_platform.domain.models import BusinessScope, Record, Requirement, ScopeStatus


class DependencyReport(Record):
    scope_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    required_requirement_ids: tuple[str, ...]
    external_scope_ids: tuple[str, ...]
    missing_requirement_ids: tuple[str, ...]
    missing_scope_ids: tuple[str, ...]
    foreign_scope_ids: tuple[str, ...]
    uncertain_external_ids: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...]
    eligible: bool
    reasons: tuple[str, ...]


class DependencyResolver:
    def __init__(
        self,
        *,
        scopes: Mapping[str, BusinessScope],
        requirements: Mapping[str, Requirement],
    ) -> None:
        self.scopes = scopes
        self.requirements = requirements

    def compute_dependency_closure(self, scope_id: str, baseline_id: str) -> DependencyReport:
        root = self.scopes.get(scope_id)
        if root is None:
            raise KeyError("scope does not exist")
        required: set[str] = set()
        external_scopes: set[str] = set()
        missing_requirements: set[str] = set()
        missing_scopes: set[str] = set()
        foreign_scopes: set[str] = set()
        uncertain_external: set[str] = set()
        cycles: set[tuple[str, ...]] = set()
        seen_scopes: set[str] = set()
        seen_requirements: set[str] = set()
        active_scopes: list[str] = []
        active_requirements: list[str] = []

        def visit_requirement(
            requirement_id: str, *, is_dependency: bool, from_scope_id: str | None = None
        ) -> None:
            if requirement_id in active_requirements:
                start = active_requirements.index(requirement_id)
                cycles.add(tuple(active_requirements[start:] + [requirement_id]))
                return
            requirement = self.requirements.get(requirement_id)
            if requirement is None:
                missing_requirements.add(requirement_id)
                return
            if (
                is_dependency
                and requirement_id not in root.requirement_ids
                and from_scope_id in (None, scope_id)
            ):
                owners = [
                    item
                    for item in self.scopes.values()
                    if requirement_id in item.requirement_ids
                    and item.id != scope_id
                    and item.status != ScopeStatus.REJECTED
                ]
                local = sorted(
                    (item for item in owners if item.workflow_id == root.workflow_id),
                    key=lambda item: item.id,
                )
                approved = [item for item in local if item.status == ScopeStatus.APPROVED]
                if approved:
                    external_scopes.add(approved[0].id)
                elif len(local) == 1:
                    external_scopes.add(local[0].id)
                elif local:
                    uncertain_external.add(requirement_id)
                elif owners:
                    foreign_scopes.update(item.id for item in owners)
                else:
                    uncertain_external.add(requirement_id)
            required.add(requirement_id)
            if requirement_id in seen_requirements:
                return
            active_requirements.append(requirement_id)
            for dependency_id in requirement.dependency_ids:
                visit_requirement(dependency_id, is_dependency=True)
            active_requirements.pop()
            seen_requirements.add(requirement_id)

        def visit_scope(current_id: str) -> None:
            if current_id in active_scopes:
                start = active_scopes.index(current_id)
                cycles.add(tuple(active_scopes[start:] + [current_id]))
                return
            current = self.scopes.get(current_id)
            if current is None:
                missing_scopes.add(current_id)
                return
            if current.workflow_id != root.workflow_id:
                foreign_scopes.add(current_id)
                return
            if current_id in seen_scopes:
                return
            active_scopes.append(current_id)
            if current_id != scope_id:
                external_scopes.add(current_id)
            for requirement_id in current.requirement_ids:
                visit_requirement(
                    requirement_id,
                    is_dependency=current_id != scope_id,
                    from_scope_id=current_id,
                )
            for dependency_id in current.dependency_scope_ids:
                visit_scope(dependency_id)
            active_scopes.pop()
            seen_scopes.add(current_id)

        visit_scope(scope_id)
        reasons: list[str] = []
        if root.status != ScopeStatus.APPROVED:
            reasons.append(f"scope {scope_id} is not approved")
        for external_id in sorted(external_scopes):
            external = self.scopes.get(external_id)
            if external is None or external.status != ScopeStatus.APPROVED:
                reasons.append(f"external scope {external_id} is not approved")
        if missing_requirements:
            reasons.append("missing requirements: " + ", ".join(sorted(missing_requirements)))
        if missing_scopes:
            reasons.append("missing scopes: " + ", ".join(sorted(missing_scopes)))
        if foreign_scopes:
            reasons.append("foreign workflow scopes: " + ", ".join(sorted(foreign_scopes)))
        if uncertain_external:
            reasons.append(
                "uncertain external requirements: " + ", ".join(sorted(uncertain_external))
            )
        if cycles:
            reasons.append("dependency cycle detected")
        return DependencyReport(
            scope_id=scope_id,
            baseline_id=baseline_id,
            required_requirement_ids=tuple(sorted(required)),
            external_scope_ids=tuple(sorted(external_scopes)),
            missing_requirement_ids=tuple(sorted(missing_requirements)),
            missing_scope_ids=tuple(sorted(missing_scopes)),
            foreign_scope_ids=tuple(sorted(foreign_scopes)),
            uncertain_external_ids=tuple(sorted(uncertain_external)),
            cycles=tuple(sorted(cycles)),
            eligible=not reasons,
            reasons=tuple(reasons),
        )
