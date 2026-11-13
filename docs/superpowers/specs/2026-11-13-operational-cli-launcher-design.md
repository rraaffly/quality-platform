# Operational CLI Launcher Design

## Intent

After installing Quality Platform on Windows, macOS or Linux, a user runs `quality-platform setup` once and `quality-platform dashboard` whenever they want to open the local Web UI. The terminal may close after `dashboard`; the local worker and HTTP server continue running. QA workflows, evaluation, review and release remain Web actions. This operational launcher does not restore `qa-orchestrator` or its compatibility surface.

## Approaches

1. **Chosen: cross-platform background dashboard launcher.** Reuse the existing single-worker and loopback-server model in Python, add a user-facing `quality-platform` command, and open the browser after both processes are healthy. `status` and `stop` manage the recorded installation. This matches the requested Hermes-style interaction across all three operating systems.
2. **Foreground dashboard.** Keep both processes attached to the terminal and stop them with Ctrl+C. It is simpler to debug but closes the dashboard when the terminal closes; the user chose background operation.
3. **Desktop executable with embedded browser.** Could hide the local server entirely, but requires a desktop runtime and a larger packaging/security change. It is outside this feature.

## User flow

- `quality-platform setup` runs from any working directory. It resolves the per-user data root with `platformdirs`: AppData on Windows, Application Support on macOS, and XDG data home on Linux. It writes a local configuration from the bundled template, prompts for owner name and password, and initializes the existing control state. It asks for the four explicit model IDs and provider choices. Setup never writes provider API keys into YAML, logs or command arguments; it checks the required environment variables and tells the user which are missing. A second setup run reports the existing installation and does not reset its data.
- `quality-platform dashboard` uses the saved state without `--state`. If the services are stopped, it starts exactly one worker and one server bound to `127.0.0.1`, waits for worker health, then opens the default browser. If they already run, it opens the existing URL. It never starts a model call. `--port` and `--no-open` are optional.
- A Python process manager launches children using `subprocess.Popen`, redirects output to private logs, and applies platform-specific detachment flags only inside that manager. `psutil` records and verifies PID plus creation time on Windows, macOS and Linux. The launcher does not invoke PowerShell or shell scripts to perform its normal operations.
- `quality-platform status` reports the URL, worker/server health and log directory. `quality-platform stop` stops only processes whose PID and creation time match the recorded values. A stale or reused PID is reported rather than killed.
- State, domain database, workspace and logs stay outside the installation directory. Password stays interactive; provider keys stay in the launching user's environment. A failed startup reports the log path and cleans up only children started by that attempt.

## Distribution boundary

The first release format is one internal source ZIP usable on Windows, macOS and Linux. It contains prebuilt `web/dist`, Python source, `uv.lock`, benchmark manifests and fixed workflow evidence tests. Small platform-specific bootstrap scripts may install a locked Python environment and expose `quality-platform` on PATH; all application commands and process management live in Python. Users need `uv` but not Bun. The launcher resolves bundled assets from the installation root and private state from the platform's user data root, regardless of the current directory. A wheel or executable can follow after the frontend, benchmark and test resources are made package-aware. Runtime artifacts are built and checked on each supported OS rather than copying a virtual environment between systems.

## Verification

- Run the same command contract in native Windows, macOS and Linux CI plus a clean-install smoke test on each OS. Setup from an arbitrary working directory creates private state and works without model credentials or provider calls; rerun preserves the owner and existing data.
- Dashboard starts a healthy loopback server and one worker, opens the expected URL, and does not duplicate processes on replay. `--no-open` supports automated tests.
- Status and stop handle a stopped service, a dead child and a reused PID safely; startup failure leaves no orphan child.
- A clean ZIP extraction on every supported OS installs without Bun, launches the Web UI with `quality-platform setup` / `quality-platform dashboard`, and can stop and restart without changing domain data. The existing PowerShell launcher may remain as a convenience wrapper, but it is not the product entry point.
