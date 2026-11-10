from copy import deepcopy

from quality_platform.domain.models import ArtifactStaleness
from quality_platform.domain.schemas import TestCase, TestStep
from quality_platform.services.impact import (
    ArtifactDependency,
    RequirementChange,
    SemanticDependency,
    detect_impact,
)


def test_requirement_change_marks_traced_and_uncertain_impact_without_editing_cases() -> None:
    cases = [
        TestCase(
            id="TC-012",
            title="Five failed attempts lock the account",
            intent="Verify the configured lockout threshold",
            requirement_ids=("R-LOCKOUT",),
            scenario="Five failed attempts",
            steps=(TestStep(action="Fail login five times", expected_result="Account locks"),),
        ),
        TestCase(
            id="TC-020",
            title="Reset password",
            intent="Verify password reset",
            requirement_ids=("R-RESET",),
            scenario="Reset password",
            steps=(TestStep(action="Reset password", expected_result="Password changes"),),
        ),
    ]
    unchanged = deepcopy(cases)
    change = RequirementChange(
        project_id="P1",
        requirement_id="R-LOCKOUT",
        from_version=1,
        to_version=2,
        before_statement="The account locks after 5 failed attempts.",
        after_statement="The account locks after 3 failed attempts.",
        traced_artifacts=(
            ArtifactDependency(
                project_id="P1",
                artifact_id="CASES-LOGIN",
                scope_id="LOGIN",
                requirement_ids=("R-LOCKOUT",),
            ),
        ),
        semantic_dependencies=(
            SemanticDependency(
                project_id="P1",
                artifact_id="PLAN-SECURITY",
                scope_id="SECURITY",
                reason="Security plan discusses lockout thresholds without a canonical trace.",
                confidence=0.62,
            ),
        ),
    )

    report = detect_impact(change)

    assert report.changed_requirement_version == 2
    assert report.staleness == ArtifactStaleness.POTENTIALLY_STALE
    assert report.affected_artifact_ids == ("CASES-LOGIN", "PLAN-SECURITY")
    assert report.affected_scope_ids == ("LOGIN", "SECURITY")
    direct = next(item for item in report.findings if item.artifact_id == "CASES-LOGIN")
    uncertain = next(item for item in report.findings if item.artifact_id == "PLAN-SECURITY")
    assert direct.uncertain is False
    assert direct.confidence == 1
    assert uncertain.uncertain is True
    assert uncertain.confidence == 0.62
    assert "5" in report.reasoning and "3" in report.reasoning
    assert cases == unchanged


def test_cross_project_impact_candidate_is_rejected() -> None:
    change = RequirementChange(
        project_id="P1",
        requirement_id="R-1",
        from_version=1,
        to_version=2,
        before_statement="Old",
        after_statement="New",
        traced_artifacts=(
            ArtifactDependency(
                project_id="P2",
                artifact_id="SECRET-P2",
                scope_id="PRIVATE",
                requirement_ids=("R-1",),
            ),
        ),
    )

    try:
        detect_impact(change)
    except ValueError as error:
        assert "cross-project" in str(error)
    else:
        raise AssertionError("cross-project impact data must be rejected")
