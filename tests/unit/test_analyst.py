import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage
from pydantic import ValidationError

from quality_platform.agents.analyst import (
    AgentContext,
    RequirementAnalysisDraft,
    analyze_documents,
)
from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.domain.models import (
    Clarification,
    ClarificationIssueType,
    ProjectRecord,
    Requirement,
    RequirementReadiness,
    SourceDocument,
    SourceReference,
    WorkflowRecord,
)
from quality_platform.evaluation.models import (
    BenchmarkCase,
    CandidateSnapshot,
    Dataset,
    EvaluationOutput,
    GoldStatus,
    ObservedItem,
    ReleaseDecision,
)
from quality_platform.evaluation.registry import load_dataset
from quality_platform.evaluation.runner import run_offline_suite
from quality_platform.llm.registry import ModelRegistry
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


class FakeStructured:
    def __init__(self, model: "FakeModel") -> None:
        self.model = model

    async def ainvoke(self, messages: list[BaseMessage]) -> RequirementAnalysisDraft:
        self.model.messages = messages
        return self.model.result


class FakeModel:
    def __init__(self, result: RequirementAnalysisDraft) -> None:
        self.result = result
        self.messages: list[BaseMessage] = []

    def with_structured_output(self, _schema: object) -> FakeStructured:
        return FakeStructured(self)


def source(document_id: str, line: int = 3) -> SourceReference:
    return SourceReference(document_id=document_id, start_line=line, end_line=line)


def context(tmp_path: Path, draft: RequirementAnalysisDraft) -> tuple[AgentContext, FakeModel]:
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Test", root=tmp_path))
    repo.create_workflow(
        WorkflowRecord(
            id="W1",
            project_id="P1",
            goal="Login",
            thread_id="T1",
            source_snapshot_ids=("BRD", "SRS", "PRD"),
        )
    )
    fixture_root = Path(__file__).parents[2] / "benchmarks" / "datasets" / "synthetic" / "analyst"
    for source_id, filename in (
        ("BRD", "synthetic-login-brd.md"),
        ("SRS", "synthetic-login-srs.md"),
        ("PRD", "synthetic-independent-prd.md"),
    ):
        snapshot = tmp_path / filename
        snapshot.write_bytes((fixture_root / filename).read_bytes())
        repo.save_source_document(
            SourceDocument(
                id=source_id,
                project_id="P1",
                input_type=source_id,
                content_hash=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                source_path=snapshot,
                snapshot_path=snapshot,
                line_count=3,
            )
        )
    config = AppConfig(
        database_path=tmp_path / "qa.sqlite",
        workspace_root=tmp_path,
        agent_models=AgentModels(
            orchestrator=ModelSpec(provider="openai", model_id="configured-orchestrator"),
            analyst=ModelSpec(provider="openai", model_id="configured-analyst"),
            planner=ModelSpec(provider="openai", model_id="configured-planner"),
            evaluator=ModelSpec(provider="openai", model_id="configured-evaluator"),
        ),
    )
    fake = FakeModel(draft)
    return AgentContext(
        project_id="P1",
        workflow_id="W1",
        repo=repo,
        models=ModelRegistry(config),
        model_factory=lambda _spec: fake,
    ), fake


def sample_draft() -> RequirementAnalysisDraft:
    return RequirementAnalysisDraft(
        requirements=(
            Requirement(
                id="REQ-LOGIN",
                version=1,
                statement="The account locks after failed attempts; the threshold differs between sources.",
                source_references=(source("BRD"), source("SRS")),
                readiness=RequirementReadiness.BLOCKED,
            ),
            Requirement(
                id="REQ-MFA",
                version=1,
                statement="Users can enable TOTP.",
                source_references=(source("PRD"),),
                source_quotes=("Users can enable TOTP.",),
                readiness=RequirementReadiness.VALIDATED,
            ),
        ),
        clarifications=(
            Clarification(
                id="C-COUNT",
                issue_type=ClarificationIssueType.CROSS_DOCUMENT_CONFLICT,
                question="Should lockout happen after 3 or 5 failed attempts?",
                source_references=(source("BRD"), source("SRS")),
                affected_requirement_ids=("REQ-LOGIN",),
            ),
            Clarification(
                id="C-DURATION",
                issue_type=ClarificationIssueType.MISSING_RULE,
                question="How long does the account remain locked?",
                source_references=(source("BRD"),),
                affected_requirement_ids=("REQ-LOGIN",),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_conflict_and_missing_rule_remain_blocked_while_independent_requirement_is_eligible(
    tmp_path: Path,
) -> None:
    agent_context, fake = context(tmp_path, sample_draft())
    result = await analyze_documents(agent_context, ["BRD", "SRS", "PRD"])
    assert {item.issue_type for item in result.clarifications} == {
        ClarificationIssueType.CROSS_DOCUMENT_CONFLICT,
        ClarificationIssueType.MISSING_RULE,
    }
    assert result.clarifications[0].source_references == (source("BRD"), source("SRS"))
    assert result.draft_eligible_requirement_ids == ("REQ-MFA",)
    assert result.requirements[0].readiness == RequirementReadiness.BLOCKED
    assert "3 failed attempts" in fake.messages[0].content
    assert "5 failed attempts" in fake.messages[0].content
    assert "no source hierarchy" in str(fake.messages[0].content).lower()


def test_tier1_analyst_fixtures_match_approved_gold_references() -> None:
    benchmark_root = Path(__file__).parents[2] / "benchmarks"
    dataset = load_dataset("v0.1", benchmark_root)
    fixture_root = benchmark_root / "datasets" / "synthetic" / "analyst"
    analyst_cases = [case for case in dataset.cases if case.task_type == "ANALYST"]
    assert {case.case_id for case in analyst_cases} >= {"conflict", "missing_rule"}
    for case in analyst_cases:
        if case.case_id not in {"conflict", "missing_rule"}:
            continue
        assert case.gold.status == GoldStatus.APPROVED
        for expected in case.gold.expected_items:
            for reference in expected.source_references:
                path = fixture_root / f"{reference.document_id}.md"
                lines = path.read_text(encoding="utf-8").splitlines()
                assert reference.end_line <= len(lines)
                assert "attempt" in lines[reference.start_line - 1]


def test_tier1_analyst_evaluation_scores_approved_human_gold() -> None:
    benchmark_root = Path(__file__).parents[2] / "benchmarks"
    dataset = load_dataset("v0.1", benchmark_root)
    analyst_only = Dataset(
        version=dataset.version,
        rubric_id=dataset.rubric_id,
        rubric_version=dataset.rubric_version,
        cases=tuple(case for case in dataset.cases if case.case_id in {"conflict", "missing_rule"}),
    )

    observed = {
        "conflict": (
            "login-attempt-count-conflict",
            "CONFLICT",
            (source("synthetic-login-brd"), source("synthetic-login-srs")),
        ),
        "missing_rule": ("lockout-duration-missing", "GAP", (source("synthetic-login-brd"),)),
    }

    def fixture_observation(case: BenchmarkCase) -> EvaluationOutput:
        item_id, kind, references = observed[case.case_id]
        return EvaluationOutput(
            observed_items=(ObservedItem(id=item_id, kind=kind, source_references=references),)
        )

    report = run_offline_suite(
        analyst_only,
        fixture_observation,
        candidate=CandidateSnapshot(
            candidate_id="analyst-task8-offline",
            commit_sha="working-tree",
            config_hash="synthetic-fixture-v0.1",
            model_snapshot_id="fake-analyst",
            prompt_snapshot_id="task8",
        ),
    )
    assert report.eligible_count == 2
    assert report.excluded_count == 0
    assert report.overall_precision == 1
    assert report.overall_recall == 1
    assert report.decision == ReleaseDecision.PASS


@pytest.mark.asyncio
async def test_unknown_or_out_of_range_evidence_is_rejected(tmp_path: Path) -> None:
    draft = sample_draft()
    invalid = draft.model_copy(
        update={
            "clarifications": (
                draft.clarifications[0].model_copy(
                    update={"source_references": (source("BRD", 99), source("SRS"))}
                ),
            )
        }
    )
    agent_context, _ = context(tmp_path, invalid)
    with pytest.raises(ValueError, match="source reference"):
        await analyze_documents(agent_context, ["BRD", "SRS", "PRD"])
    with pytest.raises(ValueError, match="source reference"):
        await analyze_documents(agent_context, ["BRD"])


@pytest.mark.asyncio
async def test_analyst_rejects_source_outside_workflow_baseline(tmp_path: Path) -> None:
    agent_context, _ = context(tmp_path, sample_draft())
    snapshot = tmp_path / "outside.md"
    snapshot.write_text("# Other\n\nOutside baseline.\n", encoding="utf-8")
    agent_context.repo.save_source_document(
        SourceDocument(
            id="OUTSIDE",
            project_id="P1",
            input_type="PRD",
            content_hash=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            source_path=snapshot,
            snapshot_path=snapshot,
            line_count=3,
        )
    )
    with pytest.raises(ValueError, match="workflow baseline"):
        await analyze_documents(agent_context, ["OUTSIDE"])


@pytest.mark.asyncio
async def test_granted_cross_project_source_uses_owner_snapshot_root(tmp_path: Path) -> None:
    agent_context, _ = context(tmp_path, sample_draft())
    other_root = tmp_path / "other"
    other_root.mkdir()
    agent_context.repo.create_project(ProjectRecord(id="P2", name="Other", root=other_root))
    snapshot = other_root / "source.md"
    snapshot.write_text("# External\n\nExternal requirement.\n", encoding="utf-8")
    agent_context.repo.save_source_document(
        SourceDocument(
            id="FOREIGN",
            project_id="P2",
            input_type="PRD",
            content_hash=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            source_path=snapshot,
            snapshot_path=snapshot,
            line_count=3,
        )
    )
    agent_context.repo.create_workflow(
        WorkflowRecord(
            id="W2",
            project_id="P1",
            goal="External",
            thread_id="T2",
            source_snapshot_ids=("FOREIGN",),
        )
    )
    draft = RequirementAnalysisDraft(
        requirements=(
            Requirement(
                id="REQ-EXT",
                version=1,
                statement="External requirement.",
                source_references=(source("FOREIGN"),),
                source_quotes=("External requirement.",),
                readiness=RequirementReadiness.VALIDATED,
            ),
        ),
        clarifications=(),
    )
    fake = FakeModel(draft)
    other_context = replace(agent_context, workflow_id="W2", model_factory=lambda _spec: fake)
    with pytest.raises(PermissionError):
        await analyze_documents(other_context, ["FOREIGN"])
    agent_context.repo.grant_cross_project_read("P2", "P1")
    result = await analyze_documents(other_context, ["FOREIGN"])
    assert result.draft_eligible_requirement_ids == ("REQ-EXT",)


def test_issue_must_block_affected_requirement_and_have_real_affected_id() -> None:
    draft = sample_draft()
    with pytest.raises(ValidationError):
        RequirementAnalysisDraft(
            requirements=(
                draft.requirements[0].model_copy(
                    update={"readiness": RequirementReadiness.VALIDATED}
                ),
            ),
            clarifications=(draft.clarifications[0],),
        )
    with pytest.raises(ValidationError):
        RequirementAnalysisDraft(
            requirements=draft.requirements,
            clarifications=(
                draft.clarifications[0].model_copy(
                    update={"affected_requirement_ids": ("MISSING",)}
                ),
            ),
        )


def test_cross_document_conflict_requires_two_distinct_documents() -> None:
    draft = sample_draft()
    with pytest.raises(ValidationError):
        RequirementAnalysisDraft(
            requirements=draft.requirements,
            clarifications=(
                draft.clarifications[0].model_copy(update={"source_references": (source("BRD"),)}),
            ),
        )


def test_blocked_requirement_cannot_lack_a_question() -> None:
    draft = sample_draft()
    with pytest.raises(ValidationError, match="blocked requirement"):
        RequirementAnalysisDraft(requirements=draft.requirements, clarifications=())


def test_validated_requirement_must_be_extracted_from_its_evidence_quote() -> None:
    draft = sample_draft()
    fabricated = draft.requirements[1].model_copy(update={"statement": "Payments settle instantly"})
    with pytest.raises(ValidationError, match="validated requirement"):
        RequirementAnalysisDraft(
            requirements=(draft.requirements[0], fabricated),
            clarifications=draft.clarifications,
        )


@pytest.mark.asyncio
async def test_fabricated_source_quote_is_rejected_even_when_line_exists(tmp_path: Path) -> None:
    draft = sample_draft()
    fabricated = draft.requirements[1].model_copy(
        update={"statement": "TOTP can be bypassed.", "source_quotes": ("TOTP can be bypassed.",)}
    )
    proposal = RequirementAnalysisDraft(
        requirements=(draft.requirements[0], fabricated),
        clarifications=draft.clarifications,
    )
    agent_context, _ = context(tmp_path, proposal)
    with pytest.raises(ValueError, match="source quote"):
        await analyze_documents(agent_context, ["BRD", "SRS", "PRD"])


@pytest.mark.asyncio
async def test_analyst_cannot_choose_one_numeric_rule_and_omit_conflict(tmp_path: Path) -> None:
    one_sided = RequirementAnalysisDraft(
        requirements=(
            Requirement(
                id="REQ-COUNT",
                version=1,
                statement="The account locks after 3 failed attempts.",
                source_references=(source("BRD"),),
                source_quotes=("The account locks after 3 failed attempts.",),
                readiness=RequirementReadiness.VALIDATED,
            ),
        ),
        clarifications=(),
    )
    agent_context, _ = context(tmp_path, one_sided)
    with pytest.raises(ValueError, match="numeric conflict"):
        await analyze_documents(agent_context, ["BRD", "SRS"])


@pytest.mark.asyncio
async def test_validated_claim_cannot_quote_only_part_of_a_source_sentence(tmp_path: Path) -> None:
    draft = sample_draft()
    partial = draft.requirements[1].model_copy(
        update={"statement": "Users can enable TOTP", "source_quotes": ("Users can enable TOTP",)}
    )
    proposal = RequirementAnalysisDraft(
        requirements=(draft.requirements[0], partial), clarifications=draft.clarifications
    )
    agent_context, _ = context(tmp_path, proposal)
    with pytest.raises(ValueError, match="source quote"):
        await analyze_documents(agent_context, ["BRD", "SRS", "PRD"])


@pytest.mark.asyncio
async def test_version_labels_across_documents_do_not_halt_analysis(tmp_path: Path) -> None:
    agent_context, _ = context(tmp_path, sample_draft())
    for source_id, version in (("V1", 1), ("V2", 2)):
        snapshot = tmp_path / f"{source_id}.md"
        snapshot.write_text(f"# Version\n\nVersion {version} includes login.\n", encoding="utf-8")
        agent_context.repo.save_source_document(
            SourceDocument(
                id=source_id,
                project_id="P1",
                input_type="PRD",
                content_hash=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                source_path=snapshot,
                snapshot_path=snapshot,
                line_count=3,
            )
        )
    agent_context.repo.create_workflow(
        WorkflowRecord(
            id="W2",
            project_id="P1",
            goal="Versions",
            thread_id="T2",
            source_snapshot_ids=("V1", "V2"),
        )
    )
    fake = FakeModel(RequirementAnalysisDraft(requirements=(), clarifications=()))
    version_context = replace(agent_context, workflow_id="W2", model_factory=lambda _spec: fake)
    result = await analyze_documents(version_context, ["V1", "V2"])
    assert result.clarifications == ()
