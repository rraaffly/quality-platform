import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from quality_platform.agents.analyst import RequirementAnalysisDraft
from quality_platform.agents.orchestrator import ProposedTask, TaskAction, TaskProposal
from quality_platform.agents.planner import TestPlanDraft
from quality_platform.cli.app import app
from quality_platform.config.models import AppConfig
from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    ClarificationStatus,
    ProjectRecord,
    Requirement,
    RequirementReadiness,
    SourceReference,
    WorkflowStatus,
)
from quality_platform.domain.schemas import TestCase, TestCaseSet, TestStep
from quality_platform.services.clarifications import ClarificationAssessment, ClarificationService
from quality_platform.services.revisions import RevisionDraft
from quality_platform.services.scopes import (
    ScopeAction,
    ScopeDecision,
    ScopeEdit,
    ScopeProposal,
    ScopeProposalSet,
    ScopeService,
)
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories

runner = CliRunner()


def invoke(*arguments: str) -> str:
    result = runner.invoke(app, list(arguments))
    assert result.exit_code == 0, result.output
    return result.output


def test_switching_projects_preserves_context_and_lists_workflow_status(tmp_path: Path) -> None:
    database = tmp_path / "qa.sqlite"
    for project_id in ("P1", "P2"):
        invoke(
            "project",
            "create",
            "--database",
            str(database),
            "--project-id",
            project_id,
            "--name",
            f"Project {project_id}",
            "--root",
            str(tmp_path / project_id),
        )

    invoke("project", "select", "--database", str(database), "--project-id", "P1")
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--goal",
        "First workflow",
    )
    invoke("project", "select", "--database", str(database), "--project-id", "P2")
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W2",
        "--goal",
        "Second workflow",
    )
    invoke("project", "select", "--database", str(database), "--project-id", "P1")

    output = invoke("workflow", "list", "--database", str(database))
    assert "W1" in output
    assert "ACTIVE" in output
    assert "W2" not in output

    repo = QaRepositories(open_database(database))
    assert repo.read_workflow("P1", "W1").status == WorkflowStatus.ACTIVE
    assert repo.read_workflow("P2", "W2").status == WorkflowStatus.ACTIVE


def test_cli_uses_clarification_and_scope_services_for_selected_project(tmp_path: Path) -> None:
    database = tmp_path / "qa.sqlite"
    repo = QaRepositories(open_database(database))
    repo.create_project(ProjectRecord(id="P1", name="Project", root=tmp_path))
    invoke("project", "select", "--database", str(database), "--project-id", "P1")
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--goal",
        "Review login",
    )
    clarification = Clarification(
        id="C1",
        issue_type=ClarificationIssueType.MISSING_RULE,
        question="How long is the lockout?",
        source_references=(SourceReference(document_id="BRD", start_line=1, end_line=1),),
        affected_requirement_ids=("R1",),
    )
    ClarificationService(repo, project_id="P1", workflow_id="W1").create_batch([clarification])

    listed = invoke(
        "clarification",
        "list",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
    )
    assert "C1" in listed and "OPEN" in listed
    invoke(
        "clarification",
        "answer",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--clarification-id",
        "C1",
        "--author",
        "Product Owner",
        "--text",
        "15 minutes",
        "--recorded-by",
        "QA",
        "--source",
        "review meeting",
    )
    assert (
        ClarificationService(repo, project_id="P1", workflow_id="W1").get("C1").status
        == ClarificationStatus.ANSWERED
    )

    scopes = ScopeService(repo, project_id="P1", workflow_id="W1")
    scopes.create_batch(
        [ScopeProposal(id="S1", journey="Independent MFA", requirement_ids=("R2",))],
        known_requirement_ids={"R2"},
    )
    output = invoke(
        "scope",
        "review",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--scope-id",
        "S1",
        "--action",
        "approve",
        "--actor",
        "QA",
    )
    assert "APPROVED" in output


def test_cli_exposes_complete_mvp_command_surface() -> None:
    root = invoke("--help")
    for group in ("project", "workflow", "clarification", "scope", "artifact", "benchmark"):
        assert group in root
    expected = {
        "project": ("create", "list", "select"),
        "workflow": ("start", "list", "resume"),
        "clarification": ("list", "answer"),
        "scope": ("review",),
        "artifact": ("show", "review"),
        "benchmark": ("run", "report"),
    }
    for group, commands in expected.items():
        output = invoke(group, "--help")
        for command in commands:
            assert command in output


def test_raw_markdown_to_reviewed_cases_revision_and_automatic_impact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "qa.sqlite"
    checkpoint = tmp_path / "checkpoints.sqlite"
    project_root = tmp_path / "workspace" / "P1"
    project_root.mkdir(parents=True)
    sources = []
    for name, statement in (
        ("brd", "The account locks after 3 failed attempts."),
        ("srs", "The account locks after 5 failed attempts."),
        ("prd", "Users can enable TOTP."),
    ):
        path = project_root / f"{name}.md"
        path.write_text(f"# {name.upper()}\n\n{statement}\n")
        sources.append(path)
    config = AppConfig.model_validate(
        {
            "database_path": str(database),
            "workspace_root": str(tmp_path / "workspace"),
            "agent_models": {
                role: {"provider": "openai", "model_id": f"offline-{role}"}
                for role in ("orchestrator", "analyst", "planner", "evaluator")
            },
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    invoke(
        "project",
        "create",
        "--database",
        str(database),
        "--project-id",
        "P1",
        "--name",
        "Login",
        "--root",
        str(project_root),
    )
    invoke("project", "select", "--database", str(database), "--project-id", "P1")
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--goal",
        "Analyze login and draft eligible plans and cases",
        "--config",
        str(config_path),
        *(arg for path in sources for arg in ("--source", str(path))),
    )
    engine = open_database(database)
    repo = QaRepositories(engine)
    workflow = repo.read_workflow("P1", "W1")
    assert len(workflow.source_snapshot_ids) == 3
    assert workflow.model_config_snapshot_id is not None
    changed_config = config.model_dump(mode="json")
    changed_config["agent_models"]["planner"]["model_id"] = "different-planner"
    config_path.write_text(yaml.safe_dump(changed_config))
    migrated = runner.invoke(
        app,
        [
            "workflow",
            "import",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--config",
            str(config_path),
            "--source",
            str(sources[0]),
        ],
    )
    assert migrated.exit_code != 0
    assert "immutable" in str(migrated.exception)
    assert repo.read_workflow("P1", "W1") == workflow
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    sources[0].write_text("# BRD\n\nThe account locks after 99 failed attempts.\n")
    refs = [
        SourceReference(document_id=source_id, start_line=3, end_line=3)
        for source_id in workflow.source_snapshot_ids
    ]
    analysis = RequirementAnalysisDraft(
        requirements=(
            Requirement(
                id="R-LOCK",
                version=1,
                statement="Lockout threshold is unresolved.",
                source_references=tuple(refs[:2]),
                readiness=RequirementReadiness.BLOCKED,
            ),
            Requirement(
                id="R-MFA",
                version=1,
                statement="Users can enable TOTP.",
                source_references=(refs[2],),
                source_quotes=("Users can enable TOTP.",),
                readiness=RequirementReadiness.VALIDATED,
            ),
        ),
        clarifications=(
            Clarification(
                id="C-LOCK",
                issue_type=ClarificationIssueType.CROSS_DOCUMENT_CONFLICT,
                question="Should lockout happen after 3 or 5 failed attempts?",
                source_references=tuple(refs[:2]),
                affected_requirement_ids=("R-LOCK",),
            ),
        ),
    )
    plan = TestPlanDraft(
        scope_id="MFA",
        objective="Verify enrollment",
        requirement_ids=("R-MFA",),
        in_scope=("TOTP",),
        test_types=("FUNCTIONAL",),
        entry_criteria=("Source reviewed",),
        exit_criteria=("Enrollment covered",),
    )
    cases = TestCaseSet(
        cases=tuple(
            TestCase(
                id=case_id,
                title=f"Enroll TOTP {case_id}",
                intent="Verify enrollment",
                requirement_ids=("R-MFA",),
                scenario="User enables TOTP",
                steps=(
                    TestStep(
                        action="Enable TOTP",
                        expected_result="Users can enable TOTP.",
                        expected_result_sources=(refs[2],),
                    ),
                ),
            )
            for case_id in ("TC-011", "TC-012")
        )
    )
    proposal = TaskProposal(
        project_id="P1",
        workflow_id="W1",
        tasks=tuple(
            ProposedTask(
                id=task_id,
                action=action,
                project_id="P1",
                workflow_id="W1",
                target_id=target,
                dependency_ids=dependencies,
            )
            for task_id, action, target, dependencies in (
                ("analyze", TaskAction.ANALYZE_CONFLICTS, None, ()),
                ("plans", TaskAction.DRAFT_TEST_PLAN, "ALL", ("analyze",)),
                ("cases", TaskAction.DRAFT_TEST_CASES, "ALL", ("analyze",)),
            )
        ),
    )
    results: dict[type[Any], Any] = {
        TaskProposal: proposal,
        RequirementAnalysisDraft: analysis,
        TestPlanDraft: plan,
        TestCaseSet: cases,
        ScopeProposalSet: ScopeProposalSet(
            proposals=(
                ScopeProposal(id="LOGIN", journey="Login", requirement_ids=("R-LOCK",)),
                ScopeProposal(id="MFA", journey="MFA enrollment", requirement_ids=("R-MFA",)),
            )
        ),
        TestCase: cases.cases[1].model_copy(update={"title": "Enroll with a valid authenticator"}),
    }
    fail_cases_once = True
    resolved_rule: str | None = None

    class FakeModel:
        def with_structured_output(self, schema: type[Any]) -> Any:
            class Structured:
                async def ainvoke(self, messages: Any) -> Any:
                    nonlocal fail_cases_once
                    if schema is TestCaseSet and fail_cases_once:
                        fail_cases_once = False
                        raise RuntimeError("transient provider failure")
                    if schema is RequirementAnalysisDraft and resolved_rule is not None:
                        decision_id = next(
                            source_id
                            for source_id in repo.read_workflow("P1", "W1").source_snapshot_ids
                            if repo.read_source_document("P1", source_id).input_type
                            == "STAKEHOLDER_DECISION"
                        )
                        return RequirementAnalysisDraft(
                            requirements=(
                                analysis.requirements[0].model_copy(
                                    update={
                                        "statement": resolved_rule,
                                        "source_references": (
                                            SourceReference(
                                                document_id=decision_id, start_line=3, end_line=3
                                            ),
                                        ),
                                        "source_quotes": (resolved_rule,),
                                        "readiness": RequirementReadiness.VALIDATED,
                                    }
                                ),
                                analysis.requirements[1],
                            ),
                            clarifications=(),
                        )
                    return results[schema]

            return Structured()

    monkeypatch.setattr(
        "quality_platform.workflows.execution.create_chat_model", lambda spec: FakeModel()
    )
    arguments = (
        "workflow",
        "run",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--checkpoint-database",
        str(checkpoint),
        "--request-id",
        "initial",
        "--instruction",
        "Analyze all documents and draft plans and cases for independent scopes",
        "--style",
        "gherkin",
    )
    failed = runner.invoke(app, list(arguments))
    assert failed.exit_code != 0
    published_before_resume = [
        event for event in repo.list_decisions("P1") if event.event_type == "artifact"
    ]
    assert len(published_before_resume) == 1
    executed = json.loads(
        invoke(
            "workflow",
            "resume",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--checkpoint-database",
            str(checkpoint),
            "--request-id",
            "initial",
        )
    )
    assert sum(item["status"] == "BLOCKED" for item in executed) == 2
    assert sum(item["status"] == "COMPLETED" for item in executed) == 3
    case_id = next(
        item["artifact_id"]
        for item in executed
        if item["task_id"] == "cases" and item["target_id"] == "MFA"
    )
    plan_id = next(
        item["artifact_id"]
        for item in executed
        if item["task_id"] == "plans" and item["target_id"] == "MFA"
    )
    assert plan_id == published_before_resume[0].record_id
    assert (
        len([event for event in repo.list_decisions("P1") if event.event_type == "artifact"]) == 2
    )
    assert repo.read_workflow("P1", "W1").task_ids == (
        "initial:analyze",
        "initial:plans",
        "initial:cases",
    )
    assert list(project_root.glob("artifacts/ANALYSIS-*/v1/requirement-analysis.md"))
    original = (project_root / "artifacts" / case_id / "v1" / "test-cases.json").read_bytes()
    assert (project_root / "artifacts" / case_id / "v1" / "test-cases.gherkin.md").exists()
    assert not (project_root / "artifacts" / case_id / "v1" / "test-cases.detailed.md").exists()
    assert json.loads(invoke(*arguments)) == executed
    assert repo.read_artifact_version("P1", case_id).version == 1

    denied = runner.invoke(
        app,
        [
            "scope",
            "review",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--scope-id",
            "LOGIN",
            "--action",
            "approve",
            "--actor",
            "QA",
        ],
    )
    assert denied.exit_code != 0
    invoke(
        "scope",
        "review",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--scope-id",
        "MFA",
        "--action",
        "approve",
        "--actor",
        "QA",
    )
    approved_baseline = json.loads(
        invoke(
            "scope",
            "baseline",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--scope-id",
            "MFA",
            "--actor",
            "QA",
        )
    )["baseline"]["id"]
    submitted = json.loads(
        invoke(
            "artifact",
            "submit",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--artifact-id",
            case_id,
            "--baseline-id",
            approved_baseline,
            "--checkpoint-database",
            str(checkpoint),
        )
    )
    assert submitted["status"] == "WAITING_FOR_REVIEW"
    assert repo.read_artifact_version("P1", case_id).status == "READY_FOR_REVIEW"
    for _ in range(2):
        invoke(
            "workflow",
            "resume",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--artifact-id",
            case_id,
            "--checkpoint-database",
            str(checkpoint),
            "--actor",
            "QA",
            "--approve",
        )
    assert repo.read_artifact_version("P1", case_id).status == "APPROVED"
    assert repo.read_artifact_version("P1", plan_id).status == "DRAFT"
    approvals = [
        item.payload for item in repo.list_decisions("P1") if item.event_type == "approval"
    ]
    assert len(approvals) == 1
    draft_path = tmp_path / "revision.json"
    invoke(
        "artifact",
        "revision",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--artifact-id",
        case_id,
        "--instruction",
        "Change only TC-012 title to Enroll with a valid authenticator",
        "--output",
        str(draft_path),
    )
    draft = RevisionDraft.model_validate_json(draft_path.read_text())
    assert repo.read_artifact_version("P1", case_id).version == 2
    invoke(
        "workflow",
        "start",
        "--database",
        str(database),
        "--workflow-id",
        "W2",
        "--goal",
        "Independent workflow",
        "--config",
        str(config_path),
    )
    for command, options in (
        (
            "revision",
            (
                "--artifact-id",
                case_id,
                "--instruction",
                "Rename TC-012",
                "--output",
                str(tmp_path / "unauthorized.json"),
            ),
        ),
        (
            "commit-revision",
            ("--draft", str(draft_path), "--reviewed-diff-hash", draft.diff_hash, "--actor", "QA"),
        ),
    ):
        denied_revision = runner.invoke(
            app,
            [
                "artifact",
                command,
                "--database",
                str(database),
                "--workflow-id",
                "W2",
                *options,
            ],
        )
        assert denied_revision.exit_code != 0
        assert isinstance(denied_revision.exception, PermissionError)
        assert "another workflow" in str(denied_revision.exception)
        assert repo.read_artifact_version("P1", case_id).version == 2
    assert not (tmp_path / "unauthorized.json").exists()
    invoke(
        "artifact",
        "commit-revision",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--draft",
        str(draft_path),
        "--reviewed-diff-hash",
        draft.diff_hash,
        "--actor",
        "QA",
    )
    revised_path = project_root / "artifacts" / case_id / "v3"
    revised = TestCaseSet.model_validate_json((revised_path / "test-cases.json").read_bytes())
    assert revised.cases[0] == cases.cases[0]
    assert revised.cases[1].title == "Enroll with a valid authenticator"
    assert (revised_path / "test-cases.detailed.md").exists()
    assert (revised_path / "test-cases.gherkin.md").exists()
    assert (
        project_root / "artifacts" / case_id / "v1" / "test-cases.json"
    ).read_bytes() == original
    # A stakeholder may choose a new rule, not just one side of the original conflict.
    resolved_rule = "The account locks after 7 failed attempts."
    results[ClarificationAssessment] = ClarificationAssessment(
        sufficient=True,
        reason="Explicit product-owner decision",
        resolved_rule=resolved_rule,
    )
    results[TaskProposal] = proposal.model_copy(update={"tasks": (proposal.tasks[0],)})
    invoke(
        "clarification",
        "answer",
        "--database",
        str(database),
        "--workflow-id",
        "W1",
        "--clarification-id",
        "C-LOCK",
        "--author",
        "Product Owner",
        "--text",
        resolved_rule,
        "--recorded-by",
        "QA",
        "--source",
        "decision meeting",
    )
    resolve_arguments = list(arguments)
    resolve_arguments[resolve_arguments.index("--request-id") + 1] = "resolve-lock"
    resolved = json.loads(invoke(*resolve_arguments))
    assert [item["status"] for item in resolved] == ["COMPLETED"]
    rule = repo.read_requirement_version("P1", "R-LOCK", 2)
    assert rule.statement == resolved_rule
    assert rule.readiness == RequirementReadiness.VALIDATED
    decision_source = repo.read_source_document("P1", rule.source_references[0].document_id)
    assert decision_source.input_type == "STAKEHOLDER_DECISION"
    assert "Stakeholder: Product Owner" in decision_source.snapshot_path.read_text()
    assert (
        repo.read_source_document("P1", refs[0].document_id)
        .snapshot_path.read_text()
        .endswith("The account locks after 3 failed attempts.\n")
    )
    assert repo.read_artifact_version("P1", case_id).staleness == "CURRENT"
    # Natural-language scope edits are proposals; sharing a requirement never clones it.
    for action, target, companions, replacements in (
        (ScopeAction.SPLIT, "LOGIN", (), ("LOGIN-DESKTOP", "LOGIN-MOBILE")),
        (ScopeAction.MERGE, "LOGIN-DESKTOP", ("LOGIN-MOBILE",), ("LOGIN-COMBINED",)),
    ):
        results[ScopeEdit] = ScopeEdit(
            scope_id=target,
            decision=ScopeDecision(
                action=action,
                actor="ORCHESTRATOR",
                human_reviewed=True,
                merge_with_ids=companions,
                replacements=tuple(
                    ScopeProposal(id=key, journey=key, requirement_ids=("R-LOCK",))
                    for key in replacements
                ),
            ),
        )
        proposal_path = tmp_path / f"{action}.json"
        scopes = ScopeService(repo, project_id="P1", workflow_id="W1")
        before_scopes = scopes.list_scopes()
        invoke(
            "scope",
            "propose-edit",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--instruction",
            f"{action.value.lower()} {target}",
            "--output",
            str(proposal_path),
        )
        assert scopes.list_scopes() == before_scopes
        assert not ScopeEdit.model_validate_json(proposal_path.read_text()).decision.human_reviewed
        invoke(
            "scope",
            "apply-edit",
            "--database",
            str(database),
            "--workflow-id",
            "W1",
            "--proposal",
            str(proposal_path),
            "--actor",
            "QA",
        )
        assert scopes.get(target).status == "REJECTED"
        assert all(scopes.get(key).requirement_ids == ("R-LOCK",) for key in replacements)
    assert {item.id for item in repo.list_requirements("P1")} == {"R-LOCK", "R-MFA"}
    requirement = repo.read_requirement_version("P1", "R-MFA", 1)
    repo.save_requirement_version(
        "P1",
        requirement.model_copy(update={"version": 2, "readiness": RequirementReadiness.BLOCKED}),
        idempotency_key="human-update-mfa-v2",
    )
    assert repo.read_artifact_version("P1", case_id).staleness == "POTENTIALLY_STALE"
    assert repo.read_artifact_version("P1", plan_id).staleness == "POTENTIALLY_STALE"
    assert [
        item.payload for item in repo.list_decisions("P1") if item.event_type == "approval"
    ] == approvals
    assert '"affected_artifact_ids"' in invoke("workflow", "impact", "--database", str(database))
    engine.dispose()
