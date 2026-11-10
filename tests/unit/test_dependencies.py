from quality_platform.domain.dependencies import DependencyResolver
from quality_platform.domain.models import (
    BusinessScope,
    Requirement,
    RequirementReadiness,
    ScopeStatus,
    SourceReference,
)


def requirement(requirement_id: str, *dependencies: str) -> Requirement:
    return Requirement(
        id=requirement_id,
        version=1,
        statement=requirement_id,
        source_references=(SourceReference(document_id="BRD", start_line=1, end_line=1),),
        dependency_ids=dependencies,
        readiness=RequirementReadiness.VALIDATED,
    )


def scope(
    scope_id: str, *members: str, status: ScopeStatus = ScopeStatus.APPROVED
) -> BusinessScope:
    return BusinessScope(id=scope_id, journey=scope_id, requirement_ids=members, status=status)


def test_shared_membership_does_not_create_dependency() -> None:
    resolver = DependencyResolver(
        scopes={"A": scope("A", "SHARED", "A1"), "B": scope("B", "SHARED", "B1")},
        requirements={key: requirement(key) for key in ("SHARED", "A1", "B1")},
    )
    report = resolver.compute_dependency_closure("A", "BASE-1")
    assert report.required_requirement_ids == ("A1", "SHARED")
    assert report.external_scope_ids == ()
    assert report.eligible


def test_transitive_cross_scope_dependency_requires_external_scope_and_approved_state() -> None:
    resolver = DependencyResolver(
        scopes={"A": scope("A", "A1"), "B": scope("B", "B1", status=ScopeStatus.PROPOSED)},
        requirements={"A1": requirement("A1", "B1"), "B1": requirement("B1")},
    )
    report = resolver.compute_dependency_closure("A", "BASE-1")
    assert report.required_requirement_ids == ("A1", "B1")
    assert report.external_scope_ids == ("B",)
    assert not report.eligible
    assert any("B" in reason for reason in report.reasons)


def test_unknown_dependency_and_cycle_are_reported_without_recursion() -> None:
    resolver = DependencyResolver(
        scopes={"A": scope("A", "A1")},
        requirements={"A1": requirement("A1", "B1", "UNKNOWN"), "B1": requirement("B1", "A1")},
    )
    report = resolver.compute_dependency_closure("A", "BASE-1")
    assert report.missing_requirement_ids == ("UNKNOWN",)
    assert report.cycles
    assert not report.eligible


def test_unknown_scope_dependency_is_uncertain_and_blocks_final_use() -> None:
    root = scope("A", "A1").model_copy(update={"dependency_scope_ids": ("MISSING",)})
    resolver = DependencyResolver(scopes={"A": root}, requirements={"A1": requirement("A1")})
    report = resolver.compute_dependency_closure("A", "BASE-1")
    assert report.missing_scope_ids == ("MISSING",)
    assert not report.eligible


def test_shared_external_requirement_uses_approved_owner_without_false_blocker() -> None:
    resolver = DependencyResolver(
        scopes={
            "A": scope("A", "A1"),
            "B": scope("B", "B1"),
            "C": scope("C", "B1", status=ScopeStatus.PROPOSED),
        },
        requirements={"A1": requirement("A1", "B1"), "B1": requirement("B1")},
    )
    report = resolver.compute_dependency_closure("A", "BASE-1")
    assert report.external_scope_ids == ("B",)
    assert report.eligible


def test_rejected_original_does_not_block_dependency_after_replacement() -> None:
    resolver = DependencyResolver(
        scopes={
            "A": scope("A", "A1"),
            "OLD": scope("OLD", "B1", status=ScopeStatus.REJECTED),
            "NEW": scope("NEW", "B1"),
        },
        requirements={"A1": requirement("A1", "B1"), "B1": requirement("B1")},
    )
    report = resolver.compute_dependency_closure("A", "BASE-1")
    assert report.external_scope_ids == ("NEW",)
    assert report.eligible


def test_explicit_cross_workflow_scope_dependency_is_blocked() -> None:
    root = scope("A", "A1").model_copy(
        update={"workflow_id": "W1", "dependency_scope_ids": ("FOREIGN",)}
    )
    foreign = scope("FOREIGN", "B1").model_copy(update={"workflow_id": "W2"})
    resolver = DependencyResolver(
        scopes={"A": root, "FOREIGN": foreign},
        requirements={"A1": requirement("A1"), "B1": requirement("B1")},
    )
    report = resolver.compute_dependency_closure("A", "BASE-1")
    assert report.foreign_scope_ids == ("FOREIGN",)
    assert not report.eligible
