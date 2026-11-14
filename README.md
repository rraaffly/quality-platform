# Quality Platform

Quality Platform runs QA workflows and evaluation in a local Web app. Use `quality-platform` to configure and operate the local dashboard on Windows, macOS, or Linux. The owner reviews requirements, scopes, plans, test cases, benchmark evidence, and release decisions in the browser.

The bundled benchmark is synthetic-first. Its `v0.1` approved cases lack semantic definitions, quality thresholds are illustrative, and no live-model quality or release readiness is claimed. A release requires approved real cases, calibrated thresholds, independent human review, workflow evidence, and an explicit Web promotion decision.

## Install a release wheel

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), download the release wheel,
then run the following on Windows, macOS, or Linux:

```sh
uv tool install --python 3.12 ./quality_platform-0.1.0-py3-none-any.whl
uv tool update-shell
```

Open a new terminal, then run `quality-platform setup` and `quality-platform dashboard`.
uv manages the isolated Python environment and installs Python 3.12 if needed. The wheel bundles
the Python app, built Web UI, and public synthetic benchmark resources. No source checkout,
Bun, Node, or Git is needed on the user's machine. Installing still needs access to dependency
packages; the application wheel alone is not an offline dependency bundle.

For an upgrade, stop the dashboard and back up the private data first, then install the new
wheel with `uv tool install --force --python 3.12 /path/to/new-release.whl`. Configuration and
data remain outside the installation. Do not assume an older app can read a newer database.

Workflow/policy evidence capture requires the matching source checkout with test dependencies.
Run the benchmark and evidence suite together there; the wheel deliberately rejects capture
rather than claiming that absent tests passed. Wheel benchmark fingerprints cover installed
code and resources. Without a source checkout, the Git commit is recorded as `unavailable`
unless the operator supplies `QA_BENCHMARK_COMMIT_SHA`; that value is an assertion, not Git verification.
Private real-world datasets and reports are never bundled. Prepare those in a source checkout,
not by writing into the wheel's read-only resources.

## Set up and operate the dashboard

```sh
quality-platform setup
quality-platform dashboard
quality-platform status
quality-platform stop
```

`setup` runs without model or account questions. It initializes the owner display name `Owner`
and the initial password `1234567890`; sign in with the password only. Change your name and
password in **Settings**. Replacement passwords require at least 12 characters and changing a
password signs out every session. Remote mode is refused until the initial password is replaced.

All application data is stored in `~/.quality-platform` on Linux/macOS or
`%USERPROFILE%\.quality-platform` on Windows, outside the installation. The default workspace is
the `workspace` subdirectory. Models are initially unconfigured: you can create projects, but
configure all four agents before creating workflows or running benchmarks. Use **Settings** or:

```sh
quality-platform config model
quality-platform config workspace /absolute/path/to/workspace
```

The workspace command also accepts paths relative to your terminal's current directory; Web
Settings requires an absolute path (or `~/...`). Workspace changes affect **new projects only**;
existing projects, workflows, sources, and artifacts remain at their original locations. The
database and application state stay in `~/.quality-platform`. No data migration or project move
is performed. CLI and Web changes share the versioned settings in the control database;
`config.yaml` is the initial bootstrap record, not the active settings editor.

`dashboard` runs a worker and a loopback server in the background and
opens the browser after the server is ready. Commands can be run from any directory. Set the
selected provider's API key in the environment **before** starting the dashboard; keys are never
saved in the configuration. Re-run `setup` safely; it does not replace an initialized owner or
data. There are no `--skip-models`, `--data-dir`, owner, or password options on `setup`.

Starting the dashboard does not call a model. Workflow runs and paid Tier 2/Tier 3 benchmarks require explicit confirmation. Workflow calls have no monetary cap; paid benchmarks use a declared USD cap and token rates. See [local Web operations](docs/web.md) and [benchmark and release evidence](docs/benchmarking.md).

## Build a release from source

```sh
uv sync --locked --dev
cd web
bun install --frozen-lockfile
bun run build
cd ..
uv build --wheel
uv run python scripts/smoke_release.py dist/quality_platform-0.1.0-py3-none-any.whl
```

`uv build --wheel` produces a universal application wheel, the supported release format. The build fails
if `web/dist/index.html` is absent; frontend compilation is a maintainer step, not an end-user
installation step. The smoke test installs the wheel with `uv tool install` in a temporary
tool directory, runs outside the checkout, checks benchmark resources and served frontend assets,
and exercises unattended setup, model/workspace configuration, start/status/stop/restart without
model calls. It uses an isolated temporary home directory. Run it on each supported OS.
The wheel contains application code and resources, not development dependencies or test suites.
Dependency versions resolve from wheel metadata; `uv.lock` only locks the source checkout workflow.

To develop or capture workflow/policy evidence from a source checkout, run
`uv sync --locked --dev`, build the frontend as above, then use `uv run quality-platform setup`
and `uv run quality-platform dashboard`. Source ZIP installers and platform-specific launcher
wrappers are not provided; the same Python CLI operates the app on every supported OS.

## Verify offline

```sh
uv run pytest -q
uv run ruff check .
uv run mypy
cd web
bunx tsc --noEmit
bun run lint
bun run build
```

Offline tests use fake models; they do not establish live-model quality.
