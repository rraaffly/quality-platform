# QA Orchestrator Python MVP v0.2 Implementation Plan

> **For agentic workers:** superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one Python CLI QA Orchestrator that reliably processes multi-document requirement analysis, stakeholder clarification, test planning and test case design under scoped human approval, with evaluation from the beginning.

**Architecture:** Single-package Python `src/` project, LangGraph for resumable dynamic workflow coordination, domain services for enforced rules, immutable filesystem artifacts and SQLite stores for domain data/checkpoints. Orchestrator, Analyst, Planner and Evaluator each use one statically configured model from OpenAI, Anthropic or Google Gemini; no auto routing/fallback. Every major capability lands with offline tests, approved benchmark cases and reproducible results.

**Tech Stack:** Python >=3.12, uv, LangGraph Python, LangChain Python + `langchain-openai` + `langchain-anthropic` + `langchain-google-genai`, Pydantic v2, SQLite, `langgraph-checkpoint-sqlite` + `aiosqlite`, recommended SQLAlchemy 2.x, Typer, Rich, pytest + pytest-asyncio, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-10-qa-orchestrator-mvp-python-v0.2-design.md`

## Global Constraints

- Work in a **single Python package** with an installable `quality-platform` CLI;.
- Use `uv` for lockfile, dependencies and commands; commit `uv.lock`. Do not pin arbitrary model IDs in source code.
- Official supported providers are only OpenAI, Anthropic, Google Gemini. One model per agent (orchestrator/analyst/planner/evaluator), with **static mapping** and immutable per-run configuration snapshot. No automatic fallback or per-task model override.
- Implement strict human clarification, source immutability, scoped approval, dependency-validated draft/final status, manual change-impact revisions, cross-project isolation and controlled parallelism as **deterministic domain rules**, not prompts.
- Only test case **JSON** is canonical; Markdown detailed/Gherkin/both are configurable projections. Semantic equivalence is checked before approval.
- Evaluation-driven development: offline hard-rule tests continuously, small approved live-LLM evaluation on agent changes, full approved dataset and human review at milestones; risk-based dual baseline gating and human–AI calibration.
- MVP CLI process only: no daemon, worker, web UI, DOCX ingestion, test execution or stakeholder integration.
- `AsyncSqliteSaver` and LangChain calls must be validated against the versions actually resolved by uv; do not assume method signatures from JavaScript APIs. Set checkpoint serialization restrictions per the library's supported configuration.

## Review Focus

1. **Unsafe reads:** `../`, symlinks and cross-project lookup without grant are denied (Task 3/Task 4 tests).
2. **Crash/retry:** a CLI restart or repeated human approval does not duplicate versions or audit decisions (Task 5/Task 12 tests).
3. **Unsupported business rules:** conflicting 3/5 lockout and unanswered clarification never produce an invented expected result (Task 7/Task 11 tests).
4. **Parallel scope writes:** failures/interrupts in scope A do not overwrite scope B and never make a blocked artifact final (Task 13 tests).
5. **Evaluation integrity:** altered dataset/rubric, leakage of unapproved ground truth, or critical disagreement cannot be hidden by a good average score (Task 6/Task 15 tests).

## Target repository layout

```text
quality-platform/
├── pyproject.toml
├── uv.lock
├── .env.example
├── src/quality_platform/
│   ├── __init__.py
│   ├── cli/{app.py,commands.py}
│   ├── config/{models.py,loader.py}
│   ├── domain/{models.py,schemas.py,policies.py,dependencies.py}
│   ├── storage/{database.py,repositories.py,checkpoints.py,artifacts.py}
│   ├── documents/{markdown.py,ingestion.py}
│   ├── llm/{registry.py,model_factory.py,structured.py,usage.py}
│   ├── agents/{orchestrator.py,analyst.py,planner.py}
│   ├── workflows/{state.py,graph.py,scheduler.py,gates.py}
│   ├── services/{clarifications.py,scopes.py,approvals.py,impact.py,revisions.py}
│   ├── rendering/{test_cases.py,markdown.py}
│   └── evaluation/{models.py,registry.py,runner.py,metrics.py,regression.py,calibration.py,reporting.py}
├── benchmarks/
│   ├── datasets/{synthetic,real_world}/
│   ├── ground_truth/
│   ├── rubrics/
│   ├── configs/
│   └── reports/                      # generated, Git-ignored
├── tests/{unit,integration,e2e,fixtures}/
└── docs/superpowers/{specs,plans}/
```

Each folder is a Python package where imports require it; avoid big service files by extracting helpers when needed. Any paths shown in task specs are repo-relative. Keep real private docs Git-ignored and external to public fixtures.

## Execution commands (once Task 1 is complete)

- Install/update: `uv sync --dev`
- Run CLI: `uv run qa-orchestrator --help`
- Unit/approval tests: `uv run pytest tests/unit -q`
- All offline tests: `uv run pytest tests/unit tests/integration -q`
- Formatting/lint: `uv run ruff check . && uv run ruff format --check .`
- Typecheck: `uv run mypy`
- Tier 2/3 live benchmark: explicit CLI commands implemented in Task 6/Task 15; never start live API evaluation as a side effect of `pytest`.

---

## Phase I — Foundation and offline evaluation contracts

### Task 1: Python/uv single-package CLI bootstrap and typed config

**Files:** Create `pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`, `src/quality_platform/__init__.py`, `src/quality_platform/cli/app.py`, `src/quality_platform/config/models.py`, `src/quality_platform/config/loader.py`, `tests/unit/test_config.py`, `tests/unit/test_cli.py`.

**Interfaces:** `load_config(path: Path) -> AppConfig`; `AppConfig` (Pydantic: database path, workspace root, `max_concurrency=3`, fixed `agent_models` mapping); `app: typer.Typer`.

- [ ] **Step 1 — Failing tests:** `test_config_rejects_unknown_provider_and_missing_agent`; `test_config_rejects_per_task_override`; `test_cli_has_project_workflow_and_benchmark_groups`. Validate model ID is nonempty, provider is one of `openai/anthropic/google`, no credentials serialized.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_config.py tests/unit/test_cli.py -q` → FAIL before feature code.
- [ ] **Step 3 — Implement:** `uv init --package` or equivalent single package; add declared libraries as runtime/dev dependencies with uv; Typer app, Pydantic config, YAML loader, env var credentials, first CLI groups; ensure default config is illustrative, not a fabricated provider model ID. Lock compatible versions.
- [ ] **Step 4 — Green:** same tests plus `uv run qa-orchestrator --help`, `uv run ruff check .`, `uv run mypy` → PASS.
- [ ] **Step 5 — Commit:** `git add pyproject.toml uv.lock .python-version .gitignore .env.example src tests && git commit -m "feat: bootstrap Python QA orchestrator CLI"`.

### Task 2: Canonical domain Pydantic models and state machine contracts

**Files:** Create `src/quality_platform/domain/{models.py,schemas.py,policies.py}`, `tests/unit/test_domain.py`, `tests/unit/test_policies.py`.

**Interfaces:** `Requirement`, `SourceReference`, `Clarification`, `BusinessScope`, `ArtifactVersion`, `ApprovalRecord`, `WorkflowRecord`, `TaskRecord`, `ImpactReport`; `validate_transition(entity: str, before: str, after: str, context: PolicyContext) -> Decision`; `Decision(allowed: bool, reasons: list[str])`.

- [ ] **Step 1 — Failing tests:** rejected final approval for validated-only requirement; unresolved contradiction blocks scope; approved content immutable; lifecycle status and staleness are orthogonal; duplicate case ID fails canonical validation.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_domain.py tests/unit/test_policies.py -q` → FAIL.
- [ ] **Step 3 — Implement:** type-safe Pydantic contracts, explicit enumerations and a policy transition table; no LLM objects inside domain models.
- [ ] **Step 4 — Green:** the two tests → PASS; run Ruff/mypy.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/domain tests/unit/test_domain.py tests/unit/test_policies.py && git commit -m "feat: define QA domain and invariant policies"`.

### Task 3: SQLite domain persistence, auditing and project isolation

**Files:** Create `src/quality_platform/storage/{database.py,repositories.py}`, `tests/integration/test_database.py`, `tests/unit/test_access_control.py`; adjust `domain` models if migrations require fields.

**Interfaces:** `open_database(path: Path) -> Engine`; `QaRepositories(engine: Engine)` with `create_project`, `create_workflow`, `save_requirement_version`, `append_decision`, `read_project_record`, `grant_cross_project_read`; `check_project_access(actor_project_id: str, target_project_id: str, operation: str, grant: Optional[Grant]) -> Decision`.

- [ ] **Step 1 — Failing tests:** persistent create/load after reopening DB, foreign keys & unique keys, append-only approval/clarification audit, deny cross-project write always, deny read by default, explicitly granted read permitted, idempotent migration.
- [ ] **Step 2 — Red:** `uv run pytest tests/integration/test_database.py tests/unit/test_access_control.py -q` → FAIL.
- [ ] **Step 3 — Implement:** domain SQLite schema/migrations via SQLAlchemy, transactional repository methods, WAL/busy-timeout, idempotency keys and access checks. Create migration mechanism simple enough for local MVP.
- [ ] **Step 4 — Green:** selected tests → PASS.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/storage tests && git commit -m "feat: persist isolated projects and audit events"`.

### Task 4: Multi-document Markdown ingestion and immutable source snapshots

**Files:** Create `src/quality_platform/documents/{markdown.py,ingestion.py}`, `src/quality_platform/storage/artifacts.py`, `tests/unit/test_markdown.py`, `tests/integration/test_ingestion.py`, `tests/fixtures/{brd.md,srs.md}`.

**Interfaces:** `parse_markdown(text: str, source_id: str) -> list[ParsedBlock]`; `ingest_markdown(project_id: str, paths: list[Path], repo: QaRepositories, workspace: Path) -> list[SourceDocument]`.

- [ ] **Step 1 — Failing tests:** preserve headings, source lines, tables, lists and two separate conflicting docs; source file edit does not alter imported snapshot; reject project root escape via symlink/`../`; Markdown instructions do not execute; reject non-MD for MVP.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_markdown.py tests/integration/test_ingestion.py -q` → FAIL.
- [ ] **Step 3 — Implement:** CommonMark/GFM-compatible Markdown parser with line-range tracking (select and pin parser after verifying line API), sha256 snapshots under project root, atomic file publishing and DB metadata.
- [ ] **Step 4 — Green:** selected tests → PASS.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/documents src/quality_platform/storage tests && git commit -m "feat: ingest immutable Markdown requirement sources"`.

### Task 5: Async LangGraph checkpoint, interrupt and recovery

**Files:** Create `src/quality_platform/storage/checkpoints.py`, `src/quality_platform/workflows/{state.py,graph.py,gates.py}`, `tests/integration/test_checkpoints.py`, `tests/integration/test_resume.py`.

**Interfaces:** `create_checkpoint_saver(path: Path) -> AsyncContextManager[AsyncSqliteSaver]`; `build_graph(services: WorkflowServices, saver: AsyncSqliteSaver) -> CompiledStateGraph`; `async run_workflow(workflow_id: str) -> RunResult`; `async resume_workflow(workflow_id: str, response: HumanResponse) -> RunResult`.

- [ ] **Step 1 — Failing tests:** graph `interrupt`/`Command(resume=...)` survives process/graph recreation; thread ID preserved; replay does not append second approval; wrong thread rejected; independent checkpoint namespaces; verify pending writes behavior after failed parallel task.
- [ ] **Step 2 — Red:** `uv run pytest tests/integration/test_checkpoints.py tests/integration/test_resume.py -q` → FAIL.
- [ ] **Step 3 — Implement:** official `langgraph-checkpoint-sqlite` `AsyncSqliteSaver`/`aiosqlite` under context-managed lifespan; `StateGraph` using compact typed state, deterministic gate node, persistent `thread_id`, strict checkpoint serialization. Keep user-visible writes in idempotent domain services.
- [ ] **Step 4 — Green:** selected tests → PASS, including fresh process-level DB handle.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/storage/checkpoints.py src/quality_platform/workflows tests/integration && git commit -m "feat: checkpoint and resume approval workflows"`.

### Task 6: Evaluation core, synthetic fixtures and hard-gate runner

**Files:** Create `src/quality_platform/evaluation/{models.py,registry.py,runner.py,metrics.py,reporting.py}`, `benchmarks/datasets/synthetic/`, `benchmarks/ground_truth/`, `benchmarks/rubrics/`, `tests/unit/test_evaluation_models.py`, `tests/unit/test_evaluation_runner.py`, `tests/fixtures/evaluation/`.

**Interfaces:** `load_dataset(version: str, root: Path) -> Dataset`; `evaluate_case(case: BenchmarkCase, observed: EvaluationOutput) -> CaseScore`; `run_offline_suite(dataset: Dataset, handler: Callable[..., EvaluationOutput]) -> BenchmarkReport`; `write_report(report: BenchmarkReport, directory: Path) -> tuple[Path, Path]`.

- [ ] **Step 1 — Failing tests:** only `APPROVED` gold cases contribute to release metrics; unsupported source refs penalized; critical policy bypass forces BLOCK regardless of average score; report includes dataset/rubric/candidate metadata; no real private input is committed or logged.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_evaluation_models.py tests/unit/test_evaluation_runner.py -q` → FAIL.
- [ ] **Step 3 — Implement:** Pydantic dataset, rubric & scoring contracts, deterministic predicate/semantic-match placeholders, JSON + Markdown reports, first synthetic cases (conflict, missing rule, shared scope, approval bypass, revision). Mark human gold approval as explicit required metadata. Live LLM eval remains opt-in.
- [ ] **Step 4 — Green:** selected tests → PASS and report paths persist locally.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/evaluation benchmarks tests && git commit -m "feat: establish evaluation harness and synthetic gold cases"`.

---

## Phase II — Multi-provider and requirement intelligence

### Task 7: Static three-provider model registry and model-config snapshots

**Files:** Create `src/quality_platform/llm/{registry.py,model_factory.py,structured.py,usage.py}`, `tests/unit/test_models.py`, `tests/integration/test_model_configuration.py`.

**Interfaces:** `ModelRegistry.get(agent_role: AgentRole) -> ModelSpec`; `create_chat_model(spec: ModelSpec) -> BaseChatModel`; `async generate_structured(role: AgentRole, messages: list[BaseMessage], schema: type[T]) -> T`; `snapshot_models(config: AppConfig) -> ModelSnapshot`.

- [ ] **Step 1 — Failing tests:** exactly one model per agent; reject unsupported provider, unknown model ID, task overrides; snapshot remains fixed after config edit; no fallback on provider timeout; missing credential failure is explicit; capability/structured-output failure yields typed error; usage records keep provider and model.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_models.py tests/integration/test_model_configuration.py -q` → FAIL.
- [ ] **Step 3 — Implement:** LangChain model factory for OpenAI/Anthropic/Google; per-agent map passed explicitly (with `model_provider="google_genai"` or prefixed form); structured-output via Pydantic with provider-specific capability checks and safe validation fallback **within the same model**, not a different provider. Use fake models for offline tests; no live calls in pytest.
- [ ] **Step 4 — Green:** selected tests → PASS; optional explicitly requested one-call provider smoke tests separate from CI.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/llm tests && git commit -m "feat: static multi-provider model configuration"`.

### Task 8: Analyst extraction, strict clarification and ground truth starter benchmarks

**Files:** Create `src/quality_platform/agents/analyst.py`, `src/quality_platform/services/clarifications.py`, `tests/unit/test_analyst.py`, `tests/integration/test_clarifications.py`; add `benchmarks/datasets/synthetic/analyst/` and approved-reviewed gold fixtures.

**Interfaces:** `async analyze_documents(context: AgentContext, source_ids: list[str]) -> RequirementAnalysisDraft`; `async validate_clarifications(workflow_id: str) -> ClarificationReview`; `record_stakeholder_answer(clarification_id: str, answer: StakeholderAnswer) -> Clarification`.

- [ ] **Step 1 — Failing tests:** BRD 3 attempts/SRS 5 attempts produces explicit conflict with both evidence spans; missing lock duration produces question; no source hierarchy or guessed expected result; incomplete answer reopens; unrelated validated requirement remains draft-eligible; review history is append-only.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_analyst.py tests/integration/test_clarifications.py -q` → FAIL.
- [ ] **Step 3 — Implement:** Analyst Pydantic output schema, evidence existence checks, strict clarification lifecycle, batch questions, CLI answer handoff through service. Add benchmark cases against human-reviewed expected outputs (synthetic gold can be authored then approved by QA; unapproved real docs excluded).
- [ ] **Step 4 — Green:** tests → PASS; run Tier 1 analyst fixture evaluation.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/agents/analyst.py src/quality_platform/services/clarifications.py tests benchmarks && git commit -m "feat: analyze evidence-backed requirements with clarification"`.

### Task 9: Journey-based scope discovery, shared requirements and dependency closure

**Files:** Create `src/quality_platform/services/scopes.py`, `src/quality_platform/domain/dependencies.py`, `tests/unit/test_scopes.py`, `tests/unit/test_dependencies.py`; extend Analyst outputs.

**Interfaces:** `async propose_scopes(context: AgentContext) -> list[ScopeProposal]`; `review_scope(scope_id: str, decision: ScopeDecision) -> BusinessScope`; `compute_dependency_closure(scope_id: str, baseline_id: str) -> DependencyReport`.

- [ ] **Step 1 — Failing tests:** business journey grouping; approve/merge/split scope; shared requirement across two scopes without canonical duplication; cycle reported; unknown/cross-scope blocker prohibits final approval; membership does not imply dependency.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_scopes.py tests/unit/test_dependencies.py -q` → FAIL.
- [ ] **Step 3 — Implement:** Analyst proposal + deterministic scope decision services and dependency closure, including uncertainty reporting. Never allow proposed scope to be final approval scope until approved.
- [ ] **Step 4 — Green:** tests → PASS.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/services/scopes.py src/quality_platform/domain/dependencies.py tests && git commit -m "feat: scoped discovery and shared requirement dependency"`.

### Task 10: Scope approval, requirement baseline and policy validation

**Files:** Create `src/quality_platform/services/approvals.py`, `tests/unit/test_approvals.py`, `tests/integration/test_scoped_approval.py`; modify workflow gates.

**Interfaces:** `approve_scope_baseline(scope_id: str, selection: BaselineSelection, actor: str) -> ApprovalResult`; `authorize_artifact_review(artifact_id: str, baseline_id: str) -> Decision`; `commit_artifact_approval(artifact_id: str, human_decision: ApprovalInput) -> ArtifactVersion`.

- [ ] **Step 1 — Failing tests:** validated-but-unapproved can draft but never final; unresolved transitive dependency or uncertain external blocker denies approval; exact version/scope captured; duplicate approval idempotent; historical approval unchanged if later stale.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_approvals.py tests/integration/test_scoped_approval.py -q` → FAIL.
- [ ] **Step 3 — Implement:** deterministic eligibility rules and immutable approval ledger, interrupt gate wiring (human only), transactionally commit approved versions.
- [ ] **Step 4 — Green:** tests → PASS.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/services/approvals.py src/quality_platform/workflows tests && git commit -m "feat: enforce scoped baseline approval"`.

---

## Phase III — Planner outputs and adaptive CLI orchestration

### Task 11: Planner agent, JSON canonical cases and Markdown renderers

**Files:** Create `src/quality_platform/agents/planner.py`, `src/quality_platform/rendering/{test_cases.py,markdown.py}`, `tests/unit/test_planner.py`, `tests/unit/test_rendering.py`; add benchmark fixtures for scenario coverage.

**Interfaces:** `async create_test_plan(context: AgentContext, scope_id: str) -> TestPlanDraft`; `async design_test_cases(context: AgentContext, scope_id: str) -> list[TestCase]`; `render_test_cases(cases: list[TestCase], style: Literal["detailed", "gherkin", "both"]) -> dict[str, str]`.

- [ ] **Step 1 — Failing tests:** source-bounded expected results; canonical JSON always available; Gherkin and detailed output include same stable case IDs and intent; invalid coverage refs and duplicate IDs rejected; blocked requirements not invented; plan approval independent of case approval; output mode respects configured style.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_planner.py tests/unit/test_rendering.py -q` → FAIL.
- [ ] **Step 3 — Implement:** one Planner model for test planning and design, Pydantic outputs, JSON artifact writer, deterministic Markdown renderers and reviewable change manifests.
- [ ] **Step 4 — Green:** tests + offline Tier 1 scenario evaluation → PASS.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/agents/planner.py src/quality_platform/rendering tests benchmarks && git commit -m "feat: produce validated test plans and cases"`.

### Task 12: Natural-language Orchestrator, targeted revisions and manual impact management

**Files:** Create `src/quality_platform/agents/orchestrator.py`, `src/quality_platform/services/{impact.py,revisions.py}`, `tests/unit/test_orchestrator.py`, `tests/integration/test_revisions.py`, `tests/integration/test_impact.py`.

**Interfaces:** `async plan_user_intent(context: AgentContext, user_text: str) -> TaskProposal`; `authorize_task(proposal: TaskProposal, context: PolicyContext) -> Decision`; `prepare_revision(instruction: str, artifact_id: str) -> RevisionDraft`; `detect_impact(change: RequirementChange) -> ImpactReport`.

- [ ] **Step 1 — Failing tests:** prioritize independent conflict search; attempted forced final approval denied; TC-012-only revision leaves other cases unchanged; detected 5→3 attempts change creates impact/stale metadata without editing test cases; diff reviewed prior to committing; no cross-project context leak.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_orchestrator.py tests/integration/test_revisions.py tests/integration/test_impact.py -q` → FAIL.
- [ ] **Step 3 — Implement:** Orchestrator proposes typed task plans, policy engine checks them before execution; targeted versioned revisions with human approval; impact analyzer uses traceability plus explicitly marked uncertain semantic dependencies. Do not let LLM change database directly.
- [ ] **Step 4 — Green:** selected tests → PASS.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/agents/orchestrator.py src/quality_platform/services tests && git commit -m "feat: authorize natural language tasks and manual impact revisions"`.

### Task 13: Controlled parallel execution, scope isolation and interactive CLI integration

**Files:** Create `src/quality_platform/workflows/scheduler.py`, `src/quality_platform/cli/commands.py`, `tests/integration/test_parallel_scopes.py`, `tests/e2e/test_cli_workflows.py`; update graph and app.

**Interfaces:** `async run_eligible_tasks(workflow_id: str, *, max_concurrency: int) -> SchedulerResult`; CLI: `project create/list/select`, `workflow start/list/resume`, `clarification list/answer`, `scope review`, `artifact show/review`, `benchmark run/report`.

- [ ] **Step 1 — Failing tests:** independent scopes advance concurrently within limit; blocked scope does not halt others; interrupted node resume does not double-write; switching projects preserves context; workflow status listed correctly; clean shutdown persists checkpoints; no background tasks continue after process exits.
- [ ] **Step 2 — Red:** `uv run pytest tests/integration/test_parallel_scopes.py tests/e2e/test_cli_workflows.py -q` → FAIL.
- [ ] **Step 3 — Implement:** bounded `asyncio` scheduler and per-scope LangGraph thread/namespace; integration CLI uses same services as tests. Use transactional DB writes and atomic artifact publication.
- [ ] **Step 4 — Green:** tests → PASS; run whole offline suite and Ruff/mypy.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/workflows src/quality_platform/cli tests && git commit -m "feat: run independent scoped workflows through CLI"`.

---

## Phase IV — Evaluation integration and release decision

### Task 14: Tier 2 live agent evaluation, synthetic + real cases and reproducible metadata

**Files:** Modify `src/quality_platform/evaluation/{runner.py,metrics.py,reporting.py}`, create `benchmarks/configs/tier2.yaml`, `tests/unit/test_agent_metrics.py`, `tests/integration/test_benchmark_reports.py`.

**Interfaces:** `async run_live_benchmark(dataset_version: str, agent_role: AgentRole, max_cases: int | None) -> BenchmarkReport`; `score_extraction(gold: list[GoldRequirement], observed: list[Requirement]) -> ExtractionScore`; `score_traceability(gold: GoldCase, observed: Artifact) -> TraceabilityScore`.

- [ ] **Step 1 — Failing tests:** precision/recall correct for matched and missed requirements; conflicts with evidence scored by case; report records dataset/rubric/model/config/prompt/commit/cost; unapproved real-world case excluded; live run never invoked during ordinary pytest; private input masked in report.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_agent_metrics.py tests/integration/test_benchmark_reports.py -q` → FAIL.
- [ ] **Step 3 — Implement:** explicit CLI `benchmark run --tier 2`, small representative dataset, output JSON/Markdown; audit LLM usage across providers; structured semantic adjudication with review flags. Add reviewed real-world cases **only when user supplies them**; no fabricated private inputs.
- [ ] **Step 4 — Green:** tests → PASS with fake outputs; run live Tier 2 only with explicit API credentials/budget confirmation.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/evaluation benchmarks tests && git commit -m "feat: measure agent quality against approved gold"`.

### Task 15: Risk-based dual baseline, evaluator-human calibration and regression gating

**Files:** Create `src/quality_platform/evaluation/{regression.py,calibration.py}`, `benchmarks/rubrics/quality-gates.yaml`, `tests/unit/test_regression.py`, `tests/unit/test_calibration.py`.

**Interfaces:** `decide_regression(candidate: BenchmarkReport, latest_approved: BenchmarkReport, thresholds: QualityThresholds) -> RegressionDecision`; `calibrate_evaluator(ai_reviews: list[Review], human_reviews: list[Review]) -> CalibrationReport`.

- [ ] **Step 1 — Failing tests:** critical violation blocks even with better average; threshold failure blocks; approved exception permitted only noncritical; compare only matching dataset/rubric versions or demand re-evaluation; promoted baseline changes only on approval; compute exact agreement, weighted kappa, MAE and critical disagreement with empty/small-sample handling.
- [ ] **Step 2 — Red:** `uv run pytest tests/unit/test_regression.py tests/unit/test_calibration.py -q` → FAIL.
- [ ] **Step 3 — Implement:** versioned quality gates, deterministic regression decision records, human review audit, AI evaluator model/static config, QA-blind independent review capture; distinguish illustrative target thresholds from calibrated release thresholds.
- [ ] **Step 4 — Green:** selected tests → PASS.
- [ ] **Step 5 — Commit:** `git add src/quality_platform/evaluation benchmarks/rubrics tests && git commit -m "feat: gate releases with calibrated dual baseline"`.

### Task 16: Full E2E benchmark, productivity experiment, docs and release readiness

**Files:** Create `tests/e2e/test_stlc_journey.py`, `tests/e2e/test_resume_and_conflict.py`, `tests/e2e/test_release_gates.py`, `docs/benchmarking.md`, `docs/cli.md`, `benchmarks/configs/tier3.yaml`; update README.

**Interfaces:** `async run_full_benchmark(dataset_version: str, *, repeats: int) -> BenchmarkReport`; `assemble_release_readiness(report: BenchmarkReport, review: HumanReview) -> ReleaseDecision`.

- [ ] **Step 1 — Failing tests:** two projects, multiple sources, conflict→manual answer→Analyst revalidation→scope review→partial draft→scoped approval→JSON+Markdown cases→targeted revision→impact report without auto rewrite→resume; hard-gate denial samples; reports identify cost/latency/active QA time; all failures linked to cases.
- [ ] **Step 2 — Red:** `uv run pytest tests/e2e -q` → FAIL.
- [ ] **Step 3 — Implement:** Tier 3 runner on available approved synthetic/real cases, bounded repeated LLM runs, human-review/prod-time capture and command docs; label insufficient real cases and uncalibrated gates rather than asserting measured readiness. Demonstrate manual versus AI-assisted QA on equivalent tasks with reviewer effort counted.
- [ ] **Step 4 — Green:** `uv run pytest tests/unit tests/integration tests/e2e -q && uv run ruff check . && uv run ruff format --check . && uv run mypy` → PASS; full paid benchmark requires explicit execution and budget.
- [ ] **Step 5 — Commit:** `git add tests/e2e docs README.md benchmarks/configs src/quality_platform/evaluation && git commit -m "test: validate evaluated MVP QA workflow end to end"`.

---

## Milestones and acceptance gates

- **M1 (Tasks 1–6):** local Python/uv CLI, domain rules, immutable Markdown sources, persisted interruption, initial offline synthetic evaluation. All Tier 1 hard-rule tests green.
- **M2 (Tasks 7–10):** three-provider static model config, Analyst, clarification, shared scope and scoped approval; controlled Tier 2 Analyst comparison available; no AI bypass of hard gates.
- **M3 (Tasks 11–13):** Planner cases JSON/configurable Markdown, natural language revisions, manual impact management, parallel scoped CLI E2E; all offline tests green.
- **M4 (Tasks 14–16):** tiered live/full eval, human-approved hybrid gold as available, dual baseline, evaluator-human calibration, evidence-backed release readiness. Do not declare target thresholds met without actual benchmark runs.

## Self-review and interface notes

- **Product scope coverage:** Requirement Analysis, Test Planning, Test Case Design, stakeholder questions, scope discovery, shared requirement mapping, partial drafts/approvals, manual change impact, revision, project isolation, CLI persistence and parallelism are mapped to Tasks 1–13. Benchmark and release policy mapped to Tasks 6, 14–16.
- **Typing:** `AgentRole` and all `AppConfig` contracts originate in Task 1/2 and are consumed without duplicate declarations; graph execution consumes domain repository and agent interfaces; review gates call policy and approval services, not LLM messages.
- **Two database concerns:** domain records and checkpoint saver are distinct; use explicit stable workflow/thread IDs. Verify `AsyncSqliteSaver` context-lifecycle under pinned versions before shipping Task 5.
- **Benchmarks:** Tier 1 offline pytest by default. Tier 2 and 3 cost-bearing LLM runs only by explicit command; no real-world dataset fabricated or silently uploaded. Hard rules 100% pass is a release gate; numeric quality targets remain provisional until annotated datasets are ready.
- **Plan deviations:** Python 3.12+ and uv, single-package, all three official provider adapters, one model per agent and integrated benchmark are approved user decisions. Tooling details marked recommended in spec require validation during implementation.

## Execution handoff

**Implementation has not started.** Review the v0.2 design and plan together before implementation. Suggested execution method: **Native implementation with incremental checkpoints/review**, because the 16 tasks share domain contracts and state invariants; independent reviewer gates can be introduced at milestone boundaries if available. Do not begin scaffolding until plan approval.
