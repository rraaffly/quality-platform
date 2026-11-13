# Operational CLI Launcher Design

## Intent

After installing Quality Platform, a Windows user runs `quality-platform setup` once and `quality-platform dashboard` whenever they want to open the local Web UI. The terminal may close after `dashboard`; the local worker and HTTP server continue running. QA workflows, evaluation, review and release remain Web actions. This operational launcher does not restore `qa-orchestrator` or its compatibility surface.

## Approaches

1. **Chosen: background dashboard launcher.** Reuse the existing single-worker and loopback-server process model, add a user-facing `quality-platform` command, and open the browser after both processes are healthy. `status` and `stop` manage the recorded installation. This matches the requested Hermes-style interaction and the existing Windows launcher.
2. **Foreground dashboard.** Keep both processes attached to the terminal and stop them with Ctrl+C. It is simpler to debug but closes the dashboard when the terminal closes; the user chose background operation.
3. **Desktop executable with embedded browser.** Could hide the local server entirely, but requires a desktop runtime and a larger packaging/security change. It is outside this feature.

## User flow

- `quality-platform setup` runs from any working directory. It selects a default private data root under `%LOCALAPPDATA%/QualityPlatform`, writes a local configuration from the bundled template, prompts for owner name and password, and initializes the existing control state. It asks for the four explicit model IDs and provider choices. Setup never writes provider API keys into YAML, logs or command arguments; it checks the required environment variables and tells the user which are missing. A second setup run reports the existing installation and does not reset its data.
- `quality-platform dashboard` uses the saved state without `--state`. If the services are stopped, it starts exactly one worker and one server bound to `127.0.0.1`, waits for worker health, then opens the default browser. If they already run, it opens the existing URL. It never starts a model call. `--port` and `--no-open` are optional.
- `quality-platform status` reports the URL, worker/server health and log directory. `quality-platform stop` stops only processes whose PID and start time match the recorded values. A stale or reused PID is reported rather than killed.
- State, domain database, workspace and logs stay outside the installation directory. Password stays interactive; provider keys stay in the launching user's environment. A failed startup reports the log path and cleans up only children started by that attempt.

## Distribution boundary

The first release format is an internal ZIP with a prebuilt `web/dist`, Python source, `uv.lock`, benchmark manifests, fixed workflow evidence tests and an installer script. The installer prepares a locked Python environment and exposes `quality-platform` on the user's PATH; users need `uv` but not Bun. The launcher resolves bundled assets from the installation root and private state from the user data root, regardless of the current directory. A wheel or single-file executable can follow after the frontend, benchmark and test resources are made package-aware.

## Verification

- Setup from an arbitrary working directory creates private state and works without model credentials or provider calls; rerun preserves the owner and existing data.
- Dashboard starts a healthy loopback server and one worker, opens the expected URL, and does not duplicate processes on replay. `--no-open` supports automated tests.
- Status and stop handle a stopped service, a dead child and a reused PID safely; startup failure leaves no orphan child.
- A clean ZIP extraction on Windows installs without Bun, launches the Web UI with `quality-platform setup` / `quality-platform dashboard`, and can stop and restart without changing domain data.
