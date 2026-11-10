from pydantic import BaseModel, ConfigDict, Field


class PolicyContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    human_reviewed: bool = False
    scope_approved: bool = False
    requirement_versions: dict[str, int] = Field(default_factory=dict)
    approved_requirement_versions: dict[str, int] = Field(default_factory=dict)
    open_clarification_ids: frozenset[str] = frozenset()
    dependencies_resolved: bool = False
    source_baseline_consistent: bool = False


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reasons: list[str]


_TRANSITIONS: dict[str, set[tuple[str, str]]] = {
    "requirement": {
        ("EXTRACTED", "VALIDATED"),
        ("EXTRACTED", "BLOCKED"),
        ("VALIDATED", "BLOCKED"),
        ("BLOCKED", "VALIDATED"),
    },
    "clarification": {
        ("OPEN", "ANSWERED"),
        ("ANSWERED", "RESOLVED"),
        ("ANSWERED", "REOPENED"),
        ("RESOLVED", "REOPENED"),
        ("REOPENED", "ANSWERED"),
    },
    "scope": {("PROPOSED", "APPROVED"), ("PROPOSED", "REJECTED")},
    "artifact": {("DRAFT", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "APPROVED")},
    "task": {
        ("PENDING", "RUNNING"),
        ("RUNNING", "COMPLETED"),
        ("RUNNING", "FAILED"),
        ("FAILED", "PENDING"),
    },
    "workflow": {
        ("NEW", "ACTIVE"),
        ("ACTIVE", "PAUSED"),
        ("PAUSED", "ACTIVE"),
        ("ACTIVE", "COMPLETED"),
    },
}


def validate_transition(entity: str, before: str, after: str, context: PolicyContext) -> Decision:
    if (before, after) not in _TRANSITIONS.get(entity, set()):
        return Decision(
            allowed=False, reasons=[f"invalid {entity} transition: {before} -> {after}"]
        )

    reasons: list[str] = []
    if entity == "scope" and after == "APPROVED":
        if not context.human_reviewed:
            reasons.append("human scope review required")
        if context.open_clarification_ids:
            reasons.append(
                "unresolved clarifications: " + ", ".join(sorted(context.open_clarification_ids))
            )

    if entity == "artifact" and after == "APPROVED":
        if not context.human_reviewed:
            reasons.append("human artifact review required")
        if not context.scope_approved:
            reasons.append("scope approval required")
        if not context.requirement_versions:
            reasons.append("requirement baseline is empty")
        unapproved = {
            requirement_id
            for requirement_id, version in context.requirement_versions.items()
            if context.approved_requirement_versions.get(requirement_id) != version
        }
        if unapproved:
            reasons.append("unapproved requirements: " + ", ".join(sorted(unapproved)))
        if context.open_clarification_ids:
            reasons.append(
                "unresolved clarifications: " + ", ".join(sorted(context.open_clarification_ids))
            )
        if not context.dependencies_resolved:
            reasons.append("requirement dependencies unresolved")
        if not context.source_baseline_consistent:
            reasons.append("source baseline inconsistent")

    return Decision(allowed=not reasons, reasons=reasons)
