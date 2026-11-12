"""Production Planner adapter with disposable, explicitly scoped benchmark state.

This exercises the production agent, not LangGraph human gates or workflow resume.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain_core.messages import AIMessage

from quality_platform.agents.analyst import (
    AgentContext,
    RequirementAnalysisDraft,
    validate_analysis_draft,
)
from quality_platform.agents.planner import design_test_cases
from quality_platform.domain.models import ProjectRecord, WorkflowRecord
from quality_platform.evaluation.models import BenchmarkCase, EvaluationOutput, ObservedItem
from quality_platform.llm.registry import ModelRegistry
from quality_platform.rendering.test_cases import render_test_cases
from quality_platform.services.scopes import ScopeProposal, ScopeService
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories


async def invoke_planner(
    case: BenchmarkCase,
    lines_by_id: dict[str, list[str]],
    registry: ModelRegistry,
    model: Any,
) -> tuple[EvaluationOutput, AIMessage]:
    if not case.planner_requirements:
        raise ValueError("Planner benchmark requires pinned validated input requirements")
    analysis = RequirementAnalysisDraft(requirements=case.planner_requirements, clarifications=())
    validate_analysis_draft(analysis, lines_by_id)
    messages: list[AIMessage] = []

    class CapturingStructured:
        def __init__(self, schema: Any) -> None:
            self.structured = model.with_structured_output(schema, include_raw=True)

        async def ainvoke(self, prompt: Any) -> Any:
            result = await self.structured.ainvoke(prompt)
            if not isinstance(result, dict) or result.get("parsed") is None:
                raise ValueError("Planner returned invalid structured output")
            raw = result.get("raw")
            if not isinstance(raw, AIMessage):
                raise TypeError("Planner omitted usage metadata")
            messages.append(raw)
            return result["parsed"]

    class CapturingModel:
        def with_structured_output(self, schema: Any) -> CapturingStructured:
            return CapturingStructured(schema)

    with TemporaryDirectory(prefix="qa-planner-benchmark-") as directory:
        root = Path(directory)
        connection = open_database(root / "benchmark.sqlite")
        try:
            repo = QaRepositories(connection)
            repo.create_project(ProjectRecord(id="BENCH", name="Benchmark", root=root))
            repo.create_workflow(
                WorkflowRecord(
                    id="RUN",
                    project_id="BENCH",
                    goal="Planner benchmark",
                    thread_id="BENCH",
                    source_snapshot_ids=case.input_snapshot_ids,
                )
            )
            ScopeService(repo, project_id="BENCH", workflow_id="RUN").create_batch(
                [
                    ScopeProposal(
                        id=case.planner_scope_id,
                        journey="Benchmark scope",
                        requirement_ids=tuple(item.id for item in case.planner_requirements),
                    )
                ],
                known_requirement_ids={item.id for item in case.planner_requirements},
            )
            context = AgentContext(
                project_id="BENCH",
                workflow_id="RUN",
                repo=repo,
                models=registry,
                model_factory=lambda _spec: CapturingModel(),
                analysis=analysis,
            )
            cases = await design_test_cases(context, case.planner_scope_id)
        finally:
            connection.dispose()
    return EvaluationOutput(
        observed_items=tuple(
            ObservedItem(
                id=item.id,
                kind="SCENARIO",
                content=item.model_dump_json(),
                semantic_context=json.dumps(render_test_cases([item], "both"), sort_keys=True),
                source_references=tuple(
                    dict.fromkeys(
                        reference
                        for step in item.steps
                        for reference in step.expected_result_sources
                    )
                ),
            )
            for item in cases
        ),
        requirement_coverage=len(
            {
                requirement_id for item in cases for requirement_id in item.requirement_ids
            }.intersection(requirement.id for requirement in case.planner_requirements)
        )
        / len(case.planner_requirements),
        schema_valid=True,
        policy_observation_complete=False,
    ), messages[0]
