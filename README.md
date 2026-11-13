# Quality Platform

Quality Platform runs QA workflows and evaluation in a local Web app. The owner reviews requirements, scopes, plans, test cases, benchmark evidence, and release decisions in the browser. `qa-web` is used only to initialize and operate the local server and worker.

The bundled benchmark is synthetic-first. Its `v0.1` approved cases lack semantic definitions, quality thresholds are illustrative, and no live-model quality or release readiness is claimed. A release requires approved real cases, calibrated thresholds, independent human review, workflow evidence, and an explicit Web promotion decision.

## Run locally on Windows

1. Copy [config.yaml](config.yaml) into a private directory outside this repository. Set absolute `database_path` and `workspace_root`, then choose provider and model IDs. Keep provider API keys in the process environment.
2. Install and build:

   ```powershell
   uv sync --dev
   cd web
   bun install --frozen-lockfile
   bun run build
   cd ..
   ```

3. Initialize once, then start the local worker and server:

   ```powershell
   uv run qa-web init --config "C:\QualityPlatformData\config.yaml" --state "C:\QualityPlatformData\state" --owner qa
   .\scripts\start-local.ps1 -State "C:\QualityPlatformData\state" -Port 8000
   ```

4. Open `http://127.0.0.1:8000`. Stop both processes before backup:

   ```powershell
   .\scripts\stop-local.ps1 -State "C:\QualityPlatformData\state"
   ```

Starting the app does not call a model. Workflow runs and paid Tier 2/Tier 3 benchmarks require explicit confirmation. Workflow calls have no monetary cap; paid benchmarks use a declared USD cap and token rates. See [local Web operations](docs/web.md) and [benchmark and release evidence](docs/benchmarking.md).

## Verify offline

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy
cd web
bunx tsc --noEmit
bun run lint
bun run build
```

Offline tests use fake models; they do not establish live-model quality.
