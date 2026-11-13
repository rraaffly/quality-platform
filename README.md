# Quality Platform

Quality Platform runs QA workflows and evaluation in a local Web app. Use `quality-platform` to configure and operate the local dashboard on Windows, macOS, or Linux. The owner reviews requirements, scopes, plans, test cases, benchmark evidence, and release decisions in the browser.

The bundled benchmark is synthetic-first. Its `v0.1` approved cases lack semantic definitions, quality thresholds are illustrative, and no live-model quality or release readiness is claimed. A release requires approved real cases, calibrated thresholds, independent human review, workflow evidence, and an explicit Web promotion decision.

## Install a release ZIP

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and extract the release ZIP into a permanent directory. Python 3.12 is installed by uv if needed. The release already contains the built Web UI; Bun is not needed.
2. From the extracted `quality-platform` directory, run the installer for your OS:

   ```powershell
   # Windows PowerShell
   .\scripts\install.ps1
   ```

   ```sh
   # macOS or Linux
   sh scripts/install.sh
   ```

3. Open a new terminal. The installers add the launcher directory to your user `PATH` when needed. Then run:

   ```sh
   quality-platform setup
   quality-platform dashboard
   quality-platform status
   quality-platform stop
   ```

`setup` asks for the model provider, model IDs, owner, and password once. It stores the database, workspace, configuration, and logs in your user data directory, outside the installation. `dashboard` runs a worker and a loopback server in the background and opens the browser after the server is ready. Commands can be run from any directory. Set the selected provider's API key in the environment **before** starting the dashboard; keys are never saved in the configuration. Re-run `setup` safely; it does not replace an initialized owner or data.

Starting the dashboard does not call a model. Workflow runs and paid Tier 2/Tier 3 benchmarks require explicit confirmation. Workflow calls have no monetary cap; paid benchmarks use a declared USD cap and token rates. See [local Web operations](docs/web.md) and [benchmark and release evidence](docs/benchmarking.md).

## Build a release from source

```sh
uv sync --locked --dev
cd web
bun install --frozen-lockfile
bun run build
cd ..
uv run python scripts/package_release.py dist/quality-platform.zip
```

To run from a source checkout, use `uv run quality-platform setup` and `uv run quality-platform dashboard` after building the frontend. The release ZIP carries the source tree and offline evidence required by the app. Move the extracted installation only while the dashboard is stopped; rerun the installer after moving it to update the launcher path.

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
