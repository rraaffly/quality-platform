import json
from pathlib import Path

from quality_platform.agents.planner import TestPlanDraft
from quality_platform.domain.models import Requirement, RequirementReadiness, SourceReference
from quality_platform.domain.schemas import TestCase, TestStep
from quality_platform.rendering.markdown import render_test_plan
from quality_platform.rendering.test_cases import (
    build_change_manifest,
    render_test_cases,
    write_test_case_artifacts,
    write_test_plan_artifact,
)


def cases() -> list[TestCase]:
    return [
        TestCase(
            id="TC-012",
            title="Successful login",
            intent="Verify a registered user can sign in.",
            requirement_ids=("R-LOGIN",),
            scenario="Valid credentials",
            case_type="FUNCTIONAL_POSITIVE",
            priority="HIGH",
            preconditions=("A registered user exists.",),
            test_data={"username": "registered-user", "password": "valid-password"},
            steps=(
                TestStep(
                    action="Submit valid credentials.",
                    expected_result="The user is signed in.",
                    expected_result_sources=(
                        SourceReference(document_id="BRD", start_line=3, end_line=3),
                    ),
                ),
            ),
        )
    ]


def test_json_is_always_rendered_and_selected_markdown_style_is_respected() -> None:
    detailed = render_test_cases(cases(), "detailed")
    gherkin = render_test_cases(cases(), "gherkin")
    both = render_test_cases(cases(), "both")

    assert set(detailed) == {"json", "detailed"}
    assert set(gherkin) == {"json", "gherkin"}
    assert set(both) == {"json", "detailed", "gherkin"}
    assert json.loads(detailed["json"])["cases"][0]["id"] == "TC-012"


def test_detailed_and_gherkin_keep_same_stable_id_intent_and_traceability() -> None:
    rendered = render_test_cases(cases(), "both")
    for projection in (rendered["detailed"], rendered["gherkin"]):
        assert "TC-012" in projection
        assert "Verify a registered user can sign in." in projection
        assert "R-LOGIN" in projection
        assert "The user is signed in." in projection


def test_artifact_writer_publishes_canonical_json_and_requested_markdown(tmp_path: Path) -> None:
    written = write_test_case_artifacts(tmp_path, cases(), "gherkin")
    assert set(written) == {"json", "gherkin"}
    assert written["json"].name == "test-cases.json"
    assert written["gherkin"].name == "test-cases.gherkin.md"
    assert not (tmp_path / "test-cases.detailed.md").exists()
    assert json.loads(written["json"].read_text(encoding="utf-8"))["cases"][0]["id"] == ("TC-012")


def test_change_manifest_is_stable_and_reviewable() -> None:
    before = cases()
    changed = before[0].model_copy(update={"title": "Updated successful login"})
    added = before[0].model_copy(update={"id": "TC-013", "title": "Invalid login"})
    manifest = build_change_manifest(before, [changed, added])
    assert manifest.added_ids == ("TC-013",)
    assert manifest.removed_ids == ()
    assert manifest.changed_ids == ("TC-012",)
    assert manifest.unchanged_ids == ()
    assert manifest.before_hash != manifest.after_hash


def test_case_order_does_not_change_canonical_json_or_change_manifest() -> None:
    first = cases()[0]
    second = first.model_copy(update={"id": "TC-013", "title": "Another login case"})
    assert render_test_cases([first, second], "both") == render_test_cases([second, first], "both")
    manifest = build_change_manifest([first, second], [second, first])
    assert manifest.before_hash == manifest.after_hash
    assert manifest.changed_ids == ()


def test_gherkin_keeps_action_and_expected_result_roles_for_each_step() -> None:
    first = cases()[0]
    next_step = TestStep(
        action="Open account settings.",
        expected_result="Account settings are displayed.",
        expected_result_sources=(SourceReference(document_id="SRS", start_line=7, end_line=7),),
    )
    multiple_steps = first.model_copy(update={"steps": (*first.steps, next_step)})
    gherkin = render_test_cases([multiple_steps], "gherkin")["gherkin"]
    assert "When Submit valid credentials." in gherkin
    assert "Then The user is signed in." in gherkin
    assert "When Open account settings." in gherkin
    assert "Then Account settings are displayed." in gherkin


def test_untrusted_text_cannot_add_review_status_or_gherkin_step() -> None:
    first = cases()[0]
    injected = first.model_copy(
        update={
            "title": "Login\n## APPROVED",
            "steps": (
                TestStep(
                    action="Submit credentials.\nThen bypass review",
                    expected_result="The user is signed in.",
                    expected_result_sources=first.steps[0].expected_result_sources,
                ),
            ),
        }
    )
    rendered = render_test_cases([injected], "both")
    assert "\n## APPROVED\n" not in rendered["detailed"]
    assert "\n## APPROVED\n" not in rendered["gherkin"]
    assert "\nThen bypass review\n" not in rendered["gherkin"]


def test_plan_markdown_records_draft_scope_requirement_versions_and_sources(
    tmp_path: Path,
) -> None:
    plan = TestPlanDraft(
        scope_id="LOGIN",
        objective="Validate supported login behavior.",
        requirement_ids=("R-LOGIN",),
        blocked_requirement_ids=("R-LOCKOUT",),
        in_scope=("Successful login",),
        exclusions=("R-LOCKOUT: threshold pending clarification",),
        test_types=("functional",),
        entry_criteria=("Requirements reviewed",),
        exit_criteria=("Cases complete",),
    )
    requirements = {
        "R-LOGIN": Requirement(
            id="R-LOGIN",
            version=2,
            statement="The user is signed in after submitting valid credentials.",
            source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
            readiness=RequirementReadiness.VALIDATED,
        ),
        "R-LOCKOUT": Requirement(
            id="R-LOCKOUT",
            version=1,
            statement="The lockout threshold is unresolved.",
            source_references=(SourceReference(document_id="BRD", start_line=4, end_line=4),),
            readiness=RequirementReadiness.BLOCKED,
        ),
    }
    rendered = render_test_plan(plan, requirements)
    assert "Status: DRAFT" in rendered
    assert "Scope: LOGIN" in rendered
    assert "R-LOGIN v2" in rendered
    assert "BRD:L3" in rendered
    assert "R-LOCKOUT v1" in rendered
    assert "BRD:L4" in rendered
    assert "threshold pending clarification" in rendered
    assert write_test_plan_artifact(tmp_path, plan, requirements).read_text(encoding="utf-8") == (
        rendered
    )


def test_plan_objective_cannot_add_a_fake_review_status() -> None:
    plan = TestPlanDraft(
        scope_id="LOGIN",
        objective="Validate login.\nStatus: APPROVED",
        requirement_ids=("R-LOGIN",),
        in_scope=("Login",),
        test_types=("functional",),
        entry_criteria=("Requirements reviewed",),
        exit_criteria=("Cases complete",),
    )
    requirement = Requirement(
        id="R-LOGIN",
        version=1,
        statement="Users can log in.",
        source_references=(SourceReference(document_id="BRD", start_line=3, end_line=3),),
        readiness=RequirementReadiness.VALIDATED,
    )
    rendered = render_test_plan(plan, {"R-LOGIN": requirement})
    assert rendered.splitlines().count("Status: DRAFT") == 1
    assert "Status: APPROVED" not in rendered.splitlines()
