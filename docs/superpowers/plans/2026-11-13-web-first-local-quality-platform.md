# Web-First Local Quality Platform Implementation Plan

**Execution status (2026-11-13):** Tasks 1–8 are implemented on `codex/web-first-local`. The Reports page hosts benchmark intake and cost reservation estimates; the per-report review page hosts pinned inputs, blind review, release decisions and promotion. `qa-web` exposes only `init`, `worker`, and `serve`. The checkboxes below are the original planning steps, not a checked execution ledger.

| Task | Implementation | Verification limit |
| --- | --- | --- |
| 1–2 Windows portability | Complete | One symlink test skips without Windows symlink privilege. |
| 3 Local launcher | Complete | Native smoke test covers start, health and stop; a separate clean-checkout manual trial has not been recorded. |
| 4–5 Web intake and paid benchmark | Complete | Fake-model tests cover queueing, idempotency, cost reservation and report storage; no paid provider run was authorized. |
| 6–7 Evidence and release | Complete | Offline tests cover blind packet/context, calibration, release gates, stale comparator and retry; a live evidence-based promotion has not been performed. |
| 8 CLI retirement | Complete | `qa-orchestrator` and the QA command adapter are removed; operational `qa-web` commands remain. |

The completion gate remains **partially verified** until a fresh Windows checkout is manually configured and exercised through the browser. Paid-provider quality and release readiness require separate approved evidence and are outside offline verification.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local Web app the sole QA product interface, provide a reliable Windows startup path, move evaluation and release decisions into Web, and retire the QA workflow CLI without backward compatibility.

**Architecture:** Keep `domain`, `services`, `workflows`, `evaluation`, and the single-writer worker as shared application code. FastAPI serves the built React app and same-origin API. The browser submits reviewed, idempotent commands; the worker owns database and artifact mutations. Evaluation records are immutable files under the configured local state, indexed by the control database; reports contain only safe summaries. `qa-web` remains an operational bootstrap/serve/worker entry point, not a QA workflow interface.

**Tech Stack:** Python 3.12+, uv, FastAPI, SQLite, LangGraph, Pydantic, pytest; React, TanStack Router, Rsbuild, Bun; native Windows PowerShell for local startup.

**Spec:** `docs/superpowers/specs/2026-11-10-qa-orchestrator-mvp-python-v0.2-design.md` and `docs/superpowers/specs/2026-11-12-quality-platform-web-addendum.md`, superseded where they require CLI workflow or read-only Web reports by the user's 2026-11-13 Web-first decision. This plan does not preserve the `qa-orchestrator` interface.

## Global Constraints

- One local owner, one machine, one worker writer, one same-origin browser app. Default HTTP binding stays `127.0.0.1`; remote access is outside this plan.
- Keep immutable source/artifact versions, exact approval evidence, independent blind human ratings, explicit release promotion, project isolation, and fail-closed recovery.
- No provider call on startup, page view, report import, or offline evidence capture. Paid runs require an explicit per-run cost confirmation and bounded benchmark ledger. Workflow calls still have no monetary cap until separately designed.
- Never accept arbitrary filesystem paths, provider keys, actor identities, or raw secret-bearing errors from browser requests. Keep private review packets and real-world documents outside Git.
- Preserve pinned workflow model configuration; changing settings only affects new workflows. No model fallback.
- Do not alter or remove existing uncommitted `web/` changes as part of this plan without reviewing their diff first.
- Use `rtk` for shell commands in this repository. Keep every commit scoped to one task.

## Review Focus

1. Windows without symlink privilege: ordinary startup and tests work; a dedicated privileged or junction test still checks path escape rejection (Task 1).
2. Windows paths with spaces or reserved characters: writer lock works or fails clearly without creating an unsafe alternate lock (Task 1).
3. A browser retry with the same idempotency key and changed benchmark payload is rejected; a worker crash never starts a paid run automatically (Task 4).
4. A human reviewer cannot see AI scores before submitting blind ratings, including every repeat output (Task 6).
5. A release approval bound to stale report, thresholds, calibration, workflow suite, or baseline cannot promote (Task 7).

## File map

| Responsibility | Files |
| --- | --- |
| Windows portability and regression tests | `src/quality_platform/storage/ownership.py`, `src/quality_platform/evaluation/{runner,reporting,workflow_evidence}.py`, affected tests in `tests/{unit,integration,e2e}` |
| Local process launcher and operations | `scripts/start-local.ps1` (new), `scripts/stop-local.ps1` (new), `docs/web.md`, `README.md` |
| Web evaluation input, jobs, immutable records | `src/quality_platform/web/evaluation.py` (new), `src/quality_platform/web/{app,control,worker,models}.py`, `src/quality_platform/evaluation/*.py` only when extracting existing CLI orchestration |
| Evaluation UI | `web/src/routes/_app/reports.tsx`, `web/src/routes/_app/evaluation/` (new routes), `web/src/lib/api.ts` |
| Legacy CLI retirement | `pyproject.toml`, `src/quality_platform/__init__.py`, `src/quality_platform/cli/`, CLI-only tests, `docs/cli.md`, `docs/benchmarking.md`, `config.yaml`, `README.md` |

## Phase 1 — Native Windows baseline and local operation

### Task 1: Fix Windows filesystem portability

**Files:** Modify `src/quality_platform/storage/ownership.py`, `src/quality_platform/evaluation/reporting.py`, `tests/integration/test_application_contract.py`, `tests/integration/test_benchmark_reports.py`, `tests/e2e/test_web_workflows.py`, `tests/unit/test_benchmark_integrity.py`; add focused Windows tests where needed.

**Interfaces:** Preserve `writer_lock(database: Path)` and report packet read/write APIs. No database or report schema change.

- [ ] Reproduce the Windows failures for symlink privilege, lock path `domain ?# space.sqlite`, and POSIX mode assertions. Record which failures are platform limitations versus product defects.
- [ ] Write failing tests: a lock path with spaces and `?#` has one stable lock identity; packet privacy uses Windows ACL checks on Windows and `0600` on POSIX; symlink-dependent cases skip only when creation privilege is absent, while a supported Windows junction/privileged path-escape case still asserts denial.
- [ ] Run those tests on Windows and confirm the intended red results.
- [ ] Implement minimal platform-specific path/permission handling without weakening `confined()` or writer exclusivity.
- [ ] Run focused tests and the Web security/E2E suites on Windows; commit only this task.

### Task 2: Make benchmark fingerprints and nested workflow evidence portable

**Files:** Modify `src/quality_platform/evaluation/{runner,workflow_evidence}.py` only if production logic is wrong; otherwise correct fixture construction in `tests/unit/test_benchmark_integrity.py`, `tests/unit/test_evaluation_dispatch.py`, `tests/unit/test_workflow_suite_evidence.py`, and `tests/integration/test_evaluation_release_cli.py`.

**Interfaces:** Keep SHA-256 over the exact stored snapshot bytes and `capture_workflow_suite(report, junit_path)` semantics. Do not bypass hash validation to make tests pass.

- [ ] Diagnose the Windows `benchmark snapshot hash mismatch: brd` and nested workflow-suite `REVIEW_REQUIRED` failures using byte-level fixture and JUnit comparisons.
- [ ] Add failing platform-neutral tests that write identical fixture bytes on Windows/POSIX and assert that a changed byte still fails the pinned hash check.
- [ ] Fix the narrow newline/path or nested-test invocation cause; keep approval and evidence validation strict.
- [ ] Run the focused benchmark/evidence tests, then `uv run pytest -q` on Windows. Record any remaining failures separately; commit.

### Task 3: Add one-command local startup after initial configuration

**Files:** Create `scripts/start-local.ps1`, `scripts/stop-local.ps1`; modify `docs/web.md`, `README.md`; add a small process-management smoke test under `tests/integration/` if the launcher is testable without model calls.

**Interfaces:** `start-local.ps1 -State <absolute path> [-Port 8000]`; `stop-local.ps1 -State <absolute path>`. Existing `qa-web init` remains a one-time private password prompt. The launcher starts exactly one worker and one loopback server, records process IDs under state, and reports the local URL.

- [ ] Add a failing smoke test for missing `web/dist`, uninitialized state, an already-running instance, and clean stop. No startup path may call a provider.
- [ ] Implement startup using hidden background processes and checked state paths; do not put secrets on the command line or in process logs. Detect a dead child and show its log location.
- [ ] Document Windows setup: copy `config.yaml` to a private directory outside the repo, fill model IDs, `uv sync --dev`, `bun install --frozen-lockfile`, `bun run build`, run `qa-web init` once, then the launcher. Include consistent stopped-service backup/restore steps.
- [ ] Run the smoke test and manually verify start, browser login, worker heartbeat, stop, and restart with a fake/offline workflow; commit.

## Phase 2 — Evaluation and release entirely through Web

### Task 4: Add immutable evaluation records and safe Web job intake

**Files:** Create `src/quality_platform/web/evaluation.py`, `tests/integration/test_web_evaluation.py`; modify `src/quality_platform/web/{app,control,models,worker}.py`.

**Interfaces:** Add `EvaluationStore(state: Path)` with `save(kind: str, record: EvalRecord) -> str` and `load(identity: str, schema: type[T]) -> T`. Add owner-authenticated `POST /api/evaluation/jobs` and `GET /api/evaluation/jobs/{id}`. Browser payloads contain typed values and uploaded files, never host paths; control DB stores immutable record IDs, project ID, command digest, status, and safe summary. Reuse `Control` idempotency and the worker writer lock.

- [ ] Test unauthorized/CSRF requests, wrong project, path-like IDs, oversized/invalid uploads, duplicate key with changed payload, restart with queued/interrupted job, and no model call before explicit confirmation.
- [ ] Run the new tests red, implement the store/schema/routes/worker dispatch, then run them green plus existing Web security tests.
- [ ] Commit with the first usable Web evaluation job intake; no CLI behavior is extended.

### Task 5: Run Tier 2 and Tier 3 benchmarks from Web

**Files:** Modify `src/quality_platform/web/evaluation.py`, `src/quality_platform/web/worker.py`, `web/src/routes/_app/reports.tsx`; create `web/src/routes/_app/evaluation/index.tsx`; add `tests/e2e/test_web_evaluation.py`.

**Interfaces:** Web request selects tier, approved dataset version, repeat count, static candidate/evaluator configuration, declared token rates, and a maximum USD cost. The worker invokes the existing `evaluation.runner` functions with the existing cost ledger and stores full private packets locally; UI exposes progress and redacted report summary.

- [ ] Test rejected unapproved gold, missing prices/credential, insufficient cap, over-budget next call, static model identity, worker restart, and report redaction. Use fake models only.
- [ ] Run red tests; implement explicit paid-run confirmation and cost estimate in UI and typed worker command; run green tests.
- [ ] Verify the UI cannot initiate a paid call by navigation or report reading; commit.

### Task 6: Move evidence capture and blind human review to Web

**Files:** Modify `src/quality_platform/web/evaluation.py`; create `web/src/routes/_app/evaluation/$run.tsx`; add `tests/e2e/test_web_evaluation_review.py`.

**Interfaces:** Provide explicit actions for offline workflow-suite capture/attach, equivalent-task productivity evidence, blind review capture, and evaluator calibration. Reuse `validate_review_packet`, `capture_workflow_suite`, `attach_workflow_evidence`, `validate_independent_reviews`, and `create_calibration`; extract orchestration from `cli/evaluation.py` into `evaluation/` where needed. Every action creates a new immutable report/evidence version.

- [ ] Test that review UI serves the private candidate packet and pinned sources without AI scores, rejects incomplete/multirun ratings, and reveals calibration only after accepted blind submission.
- [ ] Test stale source fingerprint, failed/skipped workflow suite, and incomplete productivity evidence remain visible blockers.
- [ ] Run tests red, implement actions/UI, run green and Web authorization tests; commit.

### Task 7: Add explicit Web release review and promotion

**Files:** Modify `src/quality_platform/web/evaluation.py`; create `web/src/routes/_app/evaluation/$run/release.tsx`; add `tests/e2e/test_web_release.py`.

**Interfaces:** Reuse `decide_regression`, `review_release`, and `load_baseline`. An owner enters a human release decision referencing exact displayed evidence hashes. Promotion is a separate confirmed action under the existing baseline file lock, with current report/comparator revalidation. Only `PASS` or valid `APPROVED_EXCEPTION` may advance `latest-approved.json`; critical hard gates cannot be excepted.

- [ ] Test bootstrap, rejected decision, stale comparator, missing review/calibration/suite digest, critical failure, double submission, and promotion crash/retry with no duplicate history.
- [ ] Run red tests; implement review UI, evidence-hash display, and worker promotion action; run green tests.
- [ ] Confirm that reading reports never changes release state; commit.

## Phase 3 — Retire CLI and finish the Web-only contract

### Task 8: Remove QA workflow CLI and its compatibility surface

**Files:** Modify `pyproject.toml`, `src/quality_platform/__init__.py`, `README.md`, `docs/{cli,benchmarking,web}.md`, `config.yaml`; remove `src/quality_platform/cli/` and CLI-only tests after replacing any domain-invariant coverage in Web/application tests.

**Interfaces:** Remove the `qa-orchestrator` script. Keep `qa-web init`, `qa-web worker`, and `qa-web serve` as operational commands. `evaluation` remains callable by Web services and tests, not by a separate product CLI. Existing CLI command names, output formats, and selection files receive no compatibility shim or migration promise.

- [ ] Inventory every CLI-only test: move meaningful policy/evaluation assertions to application/Web tests, delete command-format assertions, and prove no production module imports `quality_platform.cli`.
- [ ] Remove the entry point and obsolete adapter/docs; update README and Web guide so all QA work, benchmark evidence, and release actions point to Web.
- [ ] Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy`, `bunx tsc --noEmit`, `bun run lint`, and `bun run build` on Windows. Check `qa-web --help` and a full fake-model Web journey.
- [ ] Commit the retirement separately. Do not assert paid-model quality or release readiness from offline tests.

## Completion gate

- [ ] A fresh Windows checkout installs locked dependencies, builds the UI, initializes private state outside the repo, and starts/stops the local server plus one worker using the documented path.
- [ ] All supported QA workflow, benchmark, evidence, human-review, and release actions are available from the authenticated Web app; no user workflow depends on `qa-orchestrator`.
- [ ] Full offline Python and frontend checks pass on Windows; real provider runs remain opt-in and are reported separately from offline verification.
- [ ] Existing user data is preserved by the Web runtime; if an old CLI-only selection file exists, ignoring it is acceptable because backward compatibility was explicitly declined.
