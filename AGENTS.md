# Development Guidelines

## Product boundary and current references

- Quality Platform is a local, single-owner Web application. `quality-platform` operates setup/configuration and background services; `qa-web` exposes low-level init/worker/serve commands. QA workflows, benchmark review, and release promotion belong in the Web app, not a restored QA workflow CLI.
- Use [README.md](README.md), [docs/web.md](docs/web.md), [docs/benchmarking.md](docs/benchmarking.md), and [web/README.md](web/README.md) for current behavior. Dated files in `docs/superpowers/` are historical records, not an implementation backlog; their commands, checklists, and verification counts do not describe the current checkout.

## Code ownership

- Backend module paths below are relative to `src/quality_platform/`. Frontend and benchmark paths are repository-relative.
- `domain/` and `services/` own deterministic policies, dependencies, scoped approvals, and revisions. Keep authorization and human-review gates in code rather than delegating them to model prompts.
- `application.py`, `workflows/`, and `agents/` own orchestration, checkpoints, execution, and provider interaction. Preserve pinned workflow configuration, request identities, and explicit resume semantics; model selection is static per role without automatic fallback.
- `storage/` owns domain persistence, immutable artifacts, and writer ownership. Route domain/artifact mutations through the durable worker in `web/worker.py`; HTTP routes may update the separate control store for sessions, settings, and queued jobs. Reuse the existing repository and writer-lock APIs.
- `web/launcher.py` and `web/processes.py` own user-facing CLI operations and service lifecycle; `web/app.py`, `web/models.py`, `web/control.py`, and `web/queries.py` own the same-origin API, command contracts, authentication, and job state.
- `evaluation/` owns benchmark execution, scoring, evidence, and release decisions. `benchmarks/` contains versioned input data, approved reference findings, rubrics, and tier configuration, not user output.
- `rendering/` projects canonical test-case JSON into detailed Markdown and Gherkin. Keep JSON as the source of truth; changing an export style must not change test-case semantics or approval eligibility.
- `web/src/routes/` owns React pages; `web/src/features/workspace/ui.tsx` and its CSS module own shared page/form primitives; `web/src/components/arc/` owns Arc UI components and tokens. Edit route source files instead of generated `web/src/routeTree.gen.ts`.
- Input Markdown snapshots under `tests/fixtures/` and `benchmarks/datasets/` can be executable or hash-pinned test input. Treat snapshots as fixture data, not prose to modernize during documentation cleanup; preserve human approval records when updating review notes.

## Development commands

- Use Python 3.12+ with `uv` and the committed `uv.lock`. Run `uv sync --locked --dev` at the repository root; use Bun and `web/bun.lock` for frontend dependencies with `bun install --frozen-lockfile` from `web/`.
- From the repository root, run backend checks with `uv run pytest -q`, `uv run ruff check .`, and `uv run mypy`. Tests are grouped under `tests/unit/`, `tests/integration/`, and `tests/e2e/`; the Python e2e tests are not a browser-rendering suite.
- From `web/`, run `bunx tsc --noEmit`, `bun run lint`, and `bun run build`. Check edited-file formatting with `bunx oxfmt --check <paths>` and apply `--write` only to intended files; `bun run format` rewrites and lint-fixes the entire frontend.
- Build the frontend before starting FastAPI. For a normal source installation, use `uv run quality-platform setup` and `uv run quality-platform dashboard`; these commands use the real home-directory installation, not disposable test state.
- Use temporary database/workspace/state paths and fake-model fixtures for tests and demos. For an isolated initialized state, the launcher supports `dashboard/status/stop --state <path>`; `setup` has no custom data-directory option. See the Web guide for low-level worker/server operation.
- Run UI/API verification on the FastAPI origin. Rsbuild dev/preview does not start the backend or proxy its API. Inspect rendered desktop/mobile and changed non-default states for appearance changes, including keyboard focus and overflow.

## UI contract

- Keep React, TanStack Router, and Arc UI; reuse existing Arc components and shared page primitives before introducing another component system.
- Preserve the sidebar/main-content shell, Instrument Sans for sans text, Spline Sans Mono for mono text, and zero-radius components. The login page has no app/company branding.
- Keep theme/token values in `web/src/components/arc/foundation.css`; `web/src/assets/css/globals.css` maps Tailwind utilities to those tokens. Preserve square controls in portals and native checkboxes, along with accessible labels, keyboard focus, and reduced-motion behavior.
- Use `web/src/lib/api.ts` for authenticated requests and existing action submission patterns for CSRF/idempotency. Keep UI form fields and response types aligned with the Python command/API contracts.

## State, security, and review invariants

- Normal setup uses `Path.home() / ".quality-platform"` on every OS. Models start unconfigured; project creation is allowed before model setup, while workflow creation and benchmarks require all four roles. Active model/workspace settings live in the control database; the generated home-directory `config.yaml` is a bootstrap record, not an active settings editor.
- Workspace changes apply to new projects only. Existing projects retain their persisted roots; do not reinterpret old paths using the latest workspace or add automatic project moves.
- The initial `Owner` password is a special setup case, not a relaxed replacement-password policy. Preserve current-password checks, replacement-password validation, full session revocation after rotation, stable audit identity, and refusal of remote access while the initial password is active.
- Keep provider keys in the server/worker environment. Preserve same-origin requests, HttpOnly cookies, session-bound CSRF, path confinement, project isolation, and reviewed-state/idempotency validation rather than bypassing them in the browser.
- Page reads, startup, cost estimates, and offline evidence capture do not authorize paid calls. Keep explicit paid-run/resume/retry confirmation and benchmark reservations. Workflow calls have no monetary cap; do not describe concurrency as a cost limit.
- Sources and artifact versions are immutable. Scope review, requirement-baseline approval, artifact approval, revision apply, release review, and promotion remain separate explicit decisions tied to exact versions/hashes. A successful job is not an approved artifact or an approved release.
- Update pinned snapshot hashes when intentionally changing benchmark inputs, keep candidate outputs separate from human-approved reference findings, and preserve review-required/illustrative readiness labels until real evidence justifies changing them. Offline fake-model tests are not live-model quality evidence.

## Release verification

- The supported application release is one wheel containing backend code, built `web/dist`, and public benchmark resources. Resolve read-only assets through `quality_platform.resources.runtime_root()` so execution is independent of the current directory and works outside a source checkout.
- From the repository root, after building the frontend, run `uv build --wheel` and `uv run python scripts/smoke_release.py dist/quality_platform-0.1.0-py3-none-any.whl` (use the version in `pyproject.toml`). `scripts/hatch_build.py` is a Hatch hook, not a standalone frontend builder.
- The installation smoke test uses an isolated home/tool directory and no provider keys; it exercises real setup, configuration, assets, and service lifecycle. Validate supported operating systems separately rather than treating one local run as cross-platform proof.
- Keep private real-world data, reports, secrets, and runtime state out of the wheel. Workflow/policy evidence capture needs a matching source checkout and test dependencies; an installed wheel must not claim absent tests passed. See the benchmarking guide for the evidence/promotion contract.
