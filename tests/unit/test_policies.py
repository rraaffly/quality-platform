from quality_platform.domain.policies import PolicyContext, validate_transition


def test_validated_only_requirement_cannot_support_final_artifact_approval() -> None:
    context = PolicyContext(
        scope_approved=True,
        human_reviewed=True,
        requirement_versions={"REQ-1": 2},
        approved_requirement_versions={},
    )
    decision = validate_transition("artifact", "READY_FOR_REVIEW", "APPROVED", context)
    assert not decision.allowed
    assert any("REQ-1" in reason for reason in decision.reasons)


def test_unresolved_contradiction_blocks_scope_approval() -> None:
    context = PolicyContext(human_reviewed=True, open_clarification_ids=frozenset({"CL-1"}))
    decision = validate_transition("scope", "PROPOSED", "APPROVED", context)
    assert not decision.allowed
    assert any("CL-1" in reason for reason in decision.reasons)


def test_final_approval_requires_every_guard() -> None:
    context = PolicyContext(
        scope_approved=True,
        human_reviewed=True,
        requirement_versions={"REQ-1": 2},
        approved_requirement_versions={"REQ-1": 2},
        dependencies_resolved=True,
        source_baseline_consistent=True,
    )
    assert validate_transition("artifact", "READY_FOR_REVIEW", "APPROVED", context).allowed
    assert not validate_transition(
        "artifact",
        "READY_FOR_REVIEW",
        "APPROVED",
        context.model_copy(update={"dependencies_resolved": False}),
    ).allowed


def test_invalid_or_skipped_transition_is_denied() -> None:
    context = PolicyContext()
    assert not validate_transition("artifact", "DRAFT", "APPROVED", context).allowed
    assert not validate_transition("unknown", "A", "B", context).allowed


def test_final_approval_denied_when_baseline_has_no_requirements() -> None:
    context = PolicyContext(scope_approved=True, human_reviewed=True)
    decision = validate_transition("artifact", "READY_FOR_REVIEW", "APPROVED", context)
    assert not decision.allowed
    assert any("requirement" in reason for reason in decision.reasons)


def test_final_approval_denied_when_approval_is_for_old_requirement_version() -> None:
    context = PolicyContext(
        scope_approved=True,
        human_reviewed=True,
        requirement_versions={"REQ-1": 2},
        approved_requirement_versions={"REQ-1": 1},
        dependencies_resolved=True,
        source_baseline_consistent=True,
    )
    decision = validate_transition("artifact", "READY_FOR_REVIEW", "APPROVED", context)
    assert not decision.allowed
    assert any("REQ-1" in reason for reason in decision.reasons)
