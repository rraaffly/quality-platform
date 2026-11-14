# QA Orchestrator MVP — System Design Specification v0.2

**Current status:** Historical CLI baseline design (2026-11-10), not the active product specification. The CLI-only boundary, exclusion of Web/background workers, process lifecycle, and proposed defaults below have been superseded by the Web-first application. Use the current [README](../../../README.md), [Web guide](../../web.md), and [Development Guidelines](../../../AGENTS.md). The remaining sections preserve the original proposal, including domain/evaluation decisions that must be checked against current code and tests.

**Original status:** Proposed technical revision of the approved v0.1 product baseline; Python and uv confirmed; supporting library choices below are proposed.

**Date:** 2026-11-10  
**Supersedes for implementation:** `2026-11-09-qa-orchestrator-mvp-design.md` (retained for history).  
**Audience:** QA engineer / implementation team.  
**Purpose:** A local conversational CLI orchestrator coordinates specialized QA agents for Requirement Analysis → Test Planning → Test Case Design while enforcing auditable human decisions. **Evaluation and benchmarking are first-class MVP work**, not post-release cleanup.

## 1. Goals and boundaries

### Product goals

- Accept **multiple Markdown documents** of arbitrary structure (BRD, SRS, PRD, supplementary specifications) in a single workflow; preserve immutable source snapshots and precise provenance.
- Let QA Engineer talk to **one Orchestrator** in natural language. Orchestrator dynamically plans eligible work and delegates to specialized agents, without bypassing deterministic policy gates.
- **Analyst Agent:** extract canonical requirements; identify missing information, ambiguities, inconsistencies and cross-document conflicts; ask strict stakeholder clarification; propose journey/business-capability scopes and requirement dependencies.
- **Planner Agent:** create test plan, test scenarios and test cases; respond to targeted natural-language revisions; export **canonical JSON always**, plus configurable Markdown (`detailed`, `gherkin`, `both`).
- Permit draft work from **validated but unapproved** requirements only for eligible dependency closure; support **scoped final approval** only on approved baselines. Scope proposal/approval, plan approval and test-case approval are distinct.
- Support **controlled parallelism across independent scopes**, multi-project / multi-workflow isolation, persistent pause/resume from the CLI, and **manual** execution of change-impact revisions after automatic detection/reporting.
- Demonstrate quality using a **hybrid synthetic + real-world golden dataset**, human-approved ground truth, tiered evaluation, risk-based regressions, dual baseline and human/AI evaluator calibration.

### Explicit non-goals for MVP

Test environment provisioning, test execution, defect tracking, release closure, DOCX/PDF ingestion, CI/test-runner integrations, messaging stakeholders, web UI, always-on worker, Ollama/local model support, dynamic model selection, model fallback, automated requirement conflict resolution, auto-application of change-impact revisions.

## 2. Approaches and decisions

- **Chosen architecture:** LangGraph Python graph + dynamic planning Orchestrator + reusable Analyst and Planner agents + **deterministic** policy/dependency services. AI proposes tasks; code authorizes them.
- **Rejected as primary architectures:** unrestricted single-agent loop (hard to enforce invariants); strictly staged graph that prevents valid partial/reordered work (insufficient flexibility). Node-based stages may still be used inside the hybrid graph.
- **Chosen runtime:** Python **3.12+**, **uv**, **single-package `src/` layout**, **CLI-only**; process exit stops all active calls, but durable checkpoints allow later resume.
- **Multi-provider official MVP:** OpenAI, Anthropic, Google Gemini via LangChain provider packages, **one statically configured model per agent** (Orchestrator, Analyst, Planner, Evaluator). No per-task overrides, automatic routing, automatic provider fallback or unannounced model migration. Selected model IDs are explicit config values, not hard-coded defaults.
- **Recommended Python libraries (not separately approved):** Typer + Rich for CLI; Pydantic v2 for contracts; SQLAlchemy 2.x with SQLite for domain data; LangGraph `AsyncSqliteSaver` via `langgraph-checkpoint-sqlite` and `aiosqlite` for checkpoint persistence; pytest + pytest-asyncio for tests; Ruff and mypy for lint/type-check. Verify actual APIs against pinned versions before implementing.

## 3. Logical architecture

```text
Interactive Typer/Rich CLI
  -> Orchestrator (intent interpretation; dynamic plan proposals)
  -> Deterministic Task/Policy/Dependency Gate (no LLM authority)
  -> LangGraph workflow runtime (state routing; human interrupts; concurrency)
       -> Analyst Agent -> Requirement & Scope Domain Services
       -> Planner Agent -> Test Plan & Case Domain Services
       -> Clarification/Scope/Plan/Case Human Approval Gates
  -> Domain SQLite (projects, workflows, requirements, decisions, audit, versions)
  -> LangGraph SQLite Checkpoints (execution state)
  -> Project Filesystem (source snapshots, immutable artifact versions)
  -> Evaluation Harness (independent dataset, evaluators, reports)
```

Separation of responsibilities: LangGraph controls execution and resumability; domain services own authoritative QA data; specialized agents generate **proposals**, not DB status changes; deterministic services validate evidence references, status transitions, ownership, writes and access. Do not put the whole source corpus or secrets into LangGraph state; reference snapshot IDs instead.

## 4. Project and information isolation

- A single installation manages multiple projects and workflows; each workflow has `project_id`, `workflow_id`, **stable LangGraph `thread_id`** and scope-specific run keys. Checkpoint namespace isolation is mandatory for branches that can wait/retry independently.
- Project context has explicit access rights. Cross-project reference is **denied by default**; an explicit user grant allows limited read-only cited references. Referenced content does not silently become approved requirement of the active project.
- Ingestion treats Markdown as untrusted data (prompt injection cannot cause permission changes). Reject path traversal/symlink escape and unauthorized data paths. Preserve document heading, line ranges, source checksum and original document type metadata. Original files remain unchanged; import uses immutable snapshots.
- Credential values remain in environment/secret storage; scrub secrets and PII from evaluation logs, traces and exported datasets. Real-world benchmark documents stay local/ignored unless expressly approved for sharing.

## 5. Canonical domain contracts

All external and AI outputs are Pydantic-validated. Exact field naming may be refined in implementation, but these meanings are required:

| Entity | Required semantics |
|---|---|
| Project | ID, name, root, model config, cross-project grants |
| Workflow | ID, project ID, goal, thread ID, source snapshots, task plan, status, model-config snapshot |
| SourceDocument | ID, input type, immutable content hash, lines/headings, source path and snapshot path |
| Requirement | Stable ID, versions, canonical statement, evidence/source spans, dependency refs, readiness `EXTRACTED/VALIDATED/BLOCKED`, approval association |
| Clarification | ID, issue type, question, source refs, stakeholder attribution as reported by QA, answer history, `OPEN/ANSWERED/RESOLVED/REOPENED` |
| BusinessScope | ID, journey/capability, shared requirement memberships, explicit exclusions, proposal/approval record, cross-scope dependencies |
| Task | ID, workflow/scope, kind, input baseline, dependencies, status, idempotency key |
| Artifact | Kind, ID, scope, content hash, immutable version, requirement baseline, `DRAFT/READY_FOR_REVIEW/APPROVED` plus orthogonal staleness indicator |
| Approval | Who approved, what exact scope/versions, timestamp, source baseline, reviewed diff/evidence, immutable event |
| ImpactReport | Changed requirement version, affected artifact/scope, reasoning & confidence, manual disposition, staleness state |
| BenchmarkRun | Dataset/rubric IDs, commit/config/model/prompt snapshots, run timings/tokens/cost, per-case findings, gate result |

**Membership ≠ dependency.** A canonical requirement may appear in many business scopes without being duplicated. Dependency closure is transitive and must handle cycles, missing IDs, unknown relationships, and cross-scope references.

## 6. Workflow rules and precedence

1. Import/snapshot one or more Markdown files. Analyst extracts evidence-based requirements and proposes journey-based scopes.
2. **Strict Clarification:** every unresolved contradiction and test-relevant missing/ambiguous rule produces a question; **no source hierarchy override or silent assumption**. Batch questions; record stakeholder reply manually with stated source; Analyst revalidates and may reopen if insufficient.
3. `VALIDATED` requirements can support **draft-only** plans/cases within proven dependencies, including when other scopes are blocked. `BLOCKED` requirement cannot be used to invent test expectations.
4. Scope becomes an official approval boundary only after explicit QA human approval. User may merge/split/reject proposals using natural language.
5. Scoped final approval requires all included requirement versions approved, all required dependencies resolved, source baseline consistent, no uncertain blocking external dependency, and artifact review at the appropriate gate.
6. Separate requirement/scope, test plan and test case review gates; each branch may wait while unrelated branches work. Orchestrator may reorder **eligible** tasks but cannot bypass mandatory gates.
7. Human edits are scoped: intent parsing may propose a diff; authorized targeted revisions change only affected IDs (unless user authorizes wider impact), maintain version lineage and need new approval. An instruction is not permission to change underlying business rules.
8. Requirement version changes trigger **automatic impact detection and `POTENTIALLY_STALE` metadata**. The user chooses whether/how to revise; no automatic rewrite or republishing. Historical approvals are never deleted or rewritten.
9. Controlled parallel scope execution with configurable limit (proposed initial `max_concurrency = 3`). Shared canonical records are changed in serialized/transactional domain operations, and writes are idempotent.
10. CLI stop/resume: save checkpoints at safe boundaries; `interrupt()`/`Command(resume=...)` for required human input. Interrupted nodes may run again before the interruption point, so persist only using idempotency keys/transactional writes; filesystem publishing uses temporary files then atomic rename.
11. Static **one agent → one model**: exact provider/model/config snapshot per workflow and benchmark; failures never silently switch providers. Any explicit model migration requires audit and evaluation rerun.

## 7. Output and versioning

- `requirement-analysis.md` and `test-plan.md` include source references, review scope, lifecycle status, exclusions and baseline versions.
- Test cases stored as **JSON canonical structure**, rendered to configured Markdown: detailed step-by-step, Gherkin, or both. Project config selects default; user may choose the *rendering style* for a run without changing which model Planner uses. Detailed and Gherkin projections must remain semantically aligned.
- Each case records stable ID, title, requirement IDs, scenario, type, priority, preconditions, test data, steps with expected results and/or given/when/then semantics. Block unverifiable expectations; preserve scenario coverage and non-duplicate IDs.
- New approved versions are immutable; current status may flag a historical approval stale relative to the current requirement baseline without mutating the approval event.

## 8. Evaluation and benchmarking — core MVP

### Dataset and curation

- Initial **target**: 30 cases, about 18 synthetic and 12 real-world **cases** (not necessarily 12 files). Real input documents are supplied later by QA; absence of those files cannot block synthetic harness development. The ratio is planning guidance, not a quality gate.
- AI-assisted candidate ground truth is reviewed, corrected and **explicitly approved by a human QA engineer** before becoming the gold standard. Ground truth is immutable/versioned and separate from evaluation candidates; do not train/evaluate against unapproved gold.
- Case metadata: `case_id`, input snapshot IDs, task type, gold requirements/contradictions/gaps/scenarios, severity, evidence spans, allowed semantic alternatives, expected workflow state/forbidden actions and review history.
- Gold truth does not invent missing domain rules. Reviewer resolves ambiguous grading decisions; unresolved gold cases are excluded from release score until adjudicated.

### Three-tier evaluation

| Tier | Trigger | Tests |
|---|---|---|
| Tier 1 | Every code change/PR | Offline pytest policy invariants, parser, schemas, state transitions, SQLite isolation/recovery, all approval guards, read/write scopes; fake models. No API billing. |
| Tier 2 | Prompt/model/tool/agent change, explicit run | Small **representative approved** dataset subset; structured content extraction, conflicts, coverage, grounding, agent routing; per-case costs and LLM traces. |
| Tier 3 | Milestone/release | Complete available approved dataset, E2E human gates, multirun variance, cross-scope partial/parallel behavior, QA human review, productivity tests. |

### Metrics and gates

- Analyst: extraction precision/recall; critical conflict/gap detection recall; evidence/source accuracy; unsupported assertion rate.
- Planner: requirement/scenario coverage, expected-result correctness, executability, redundancy, schema validation, detailed↔Gherkin semantic agreement.
- Runtime: approval-policy compliance, blocked dependency bypass count, cross-project leak count, workflow resume correctness, unauthorized mutation count, parallel isolation and idempotency.
- Efficiency: cost per accepted artifact, human review/rework time, latency, tokens, active QA time against manual baseline.
- Initial **aspirational** targets (subject to dataset calibration): extraction precision & recall ≥95%, critical gap recall ≥95%, critical conflict recall 100% on labeled critical cases, evidence accuracy ≥98%, QA active time reduction ≥30%. **Critical hard-gate test suites must pass 100%**; no passing overall average can compensate for a hard violation.

### Regression policy and baselines

- **Risk-based:** Critical failures (approval bypass, treating unresolved conflict as fact, project data leak, final publish with blocked dependency, unauthorized modification) always `BLOCKED`.
- High-severity semantic correctness and traceability: minimum quality threshold mandatory; significant regression prompts review/block. Medium/low may be tolerated within calibrated margins but must be recorded.
- **Dual baseline:** compare every candidate on the same **dataset/rubric version** with (a) latest approved candidate and (b) minimum quality thresholds. Best historical per-metric scores are informative only. If dataset or rubric changes, re-evaluate comparator on the new benchmark version.
- Decisions: `PASS`, `REVIEW_REQUIRED`, `BLOCKED`, `APPROVED_EXCEPTION`. An exception cannot override critical hard gates. Approved releases alone replace latest approved baseline.

### Human–AI evaluator calibration

- Separate evaluator logical role; statically configured single evaluator model; reviewer scores independently before seeing AI ratings.
- Track exact agreement, weighted Cohen’s kappa (ordinal ratings), score MAE, and **critical disagreement rate**. Review disagreement examples and revise rubric/judge prompt, rerun calibration; record sample size and human annotation uncertainty.
- Do not assume AI evaluator is authoritative; QA reviewer adjudicates. Human review and true requirement provenance take precedence over judge score.

## 9. Persistence, security and operational concerns

- Domain SQLite via SQLAlchemy (proposed) for normalized entities, audit, constraints/unique keys and transactions. Dedicated official `AsyncSqliteSaver` checkpointer backed by `aiosqlite` (may be separate SQLite DB file); SQLite is for local use, not assumed suitable for distributed workers.
- Use a single process with bounded `asyncio` concurrency; guard shared writes. Process termination means no background execution. Recovery tests must include an interrupted and a partially written parallel branch.
- Configure checkpoint serialization restrictions (for example `LANGGRAPH_STRICT_MSGPACK=true` per current package guidance); do not deserialize arbitrary untrusted types.
- No provider fallback; redact API tokens and private PRD contents in logs; default benchmark fixtures from real projects are Git-ignored. Retain audit logs of model/provider/config snapshots.

## 10. Acceptance scenarios

1. Create two isolated projects and independent workflows; deny cross-project source access unless explicitly granted read-only.
2. Ingest multiple BRD/SRS/PRD Markdown files with verifiable, stable line/heading references; modify the original without changing prior snapshots.
3. Detect a 3-vs-5 login attempt conflict and stop affected work for manual stakeholder clarification; unaffected scope continues.
4. Generate draft test plan/cases from validated-unapproved independent requirements; reject finalization until corresponding approved scope/baseline and dependencies exist.
5. Let a user approve/split/merge business scopes; share a requirement across scopes without cloning its canonical identity.
6. Generate test-case JSON plus chosen detailed/Gherkin/both Markdown, with stable IDs and traceability.
7. Accept a natural language revision of TC-012, show diff, maintain immutable previous version and do not modify unrelated cases.
8. Detect a requirement change, report potential impact and staleness, do **not** revise until explicit instruction.
9. Stop and restart CLI, resume interrupted graph with correct `thread_id`, and run eligible scopes in bounded parallel with no double publication.
10. Run pytest offline policy suite, curated Tier 2 agent evaluation, Tier 3 end-to-end benchmark with versioned reports, calibrated AI-human scores and dual baseline gating.

## 11. Technical open decisions / risks

- Pin exact compatible LangGraph/LangChain/provider integration versions in `uv.lock` after a small install spike; version APIs may evolve.
- Confirm `AsyncSqliteSaver` concurrency and persistence behavior under CLI resume; design for file locks and safe single-process ownership, not concurrent CLI writers.
- Choose exact model IDs per provider only after credentials and capability checks; some models expose different structured-output semantics. No invented default IDs.
- Define a human-reviewed adjudication rubric, threshold table and variance bands after first dataset is approved; percentages above are targets, not empirical guarantees.
- Scoped partial approval involves uncertain transitive dependencies; conservative blocking and explicit exclusions are preferred to unjustified approval.

## 12. References

- LangGraph Python SQLite checkpointer: https://reference.langchain.com/python/langgraph.checkpoint.sqlite
- LangGraph resumable interrupts: https://reference.langchain.com/python/langgraph/types/interrupt
- LangChain Python provider model interface: https://github.com/langchain-ai/docs/blob/main/src/oss/concepts/providers-and-models.mdx
- LangChain Python structured output: https://github.com/langchain-ai/docs/blob/main/src/oss/langchain/structured-output.mdx

---

**Design approval status:** The STLC behavior, Python+uv choice, three providers, static one-model-per-agent, and benchmark policy are user-confirmed. The integration details and task sequencing in this document are proposed for review and may be refined before implementation.
