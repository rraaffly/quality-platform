# Cross-Platform Operational CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user run `quality-platform setup` once, then `quality-platform dashboard` from any directory to start the local Web app in the background and open a browser on Windows, macOS and Linux.

**Architecture:** Add a user-facing Typer entry point that delegates setup to the existing `Control` and domain database and delegates lifecycle to a Python process manager. The manager records PID plus creation time and uses one file lock, one worker, one loopback server, private logs and health polling. Keep the existing `qa-web` operational commands as low-level entry points. Release ZIPs contain a built frontend and the source/test/benchmark tree required for workflow evidence.

**Tech Stack:** Python 3.12, Typer, platformdirs, psutil, filelock, pytest, PowerShell/bash install scripts, uv and Bun for release builds only.

**Spec:** `docs/superpowers/specs/2026-11-13-operational-cli-launcher-design.md`.

## Global Constraints

- All four user commands run on Windows, macOS and Linux. The normal path does not call PowerShell or a POSIX shell.
- `setup` and `dashboard` do not call a provider. Provider keys remain in the user's process environment, never in config, logs or command arguments.
- `dashboard` binds `127.0.0.1`, starts one worker and one server, opens the browser only after health is confirmed, and works from any current directory.
- `stop` verifies PID plus creation time before termination. A mismatch must not kill the process.
- Store state, database, workspace and logs outside the installation directory. Never erase an existing initialized state.

## Review Focus

1. A stale marker with a reused PID cannot terminate an unrelated process.
2. Two simultaneous dashboard starts cannot create duplicate workers or servers.
3. A server startup failure cleans up only children started by that attempt and points to logs.
4. An existing installation's owner/password/config are not silently reset by rerunning setup.
5. A release extracted under a path with spaces launches from another working directory without Bun.

## Task 1: Add user-facing setup and state resolution

**Files:** Create `src/quality_platform/web/launcher.py`; modify `pyproject.toml`; add `tests/integration/test_operational_cli.py`.

**Interfaces:** `data_root(override: Path | None) -> Path`; `setup(data_dir: Path | None, config: Path | None, owner: str | None) -> None`. Add `quality-platform = "quality_platform.web.launcher:main"`. Interactive setup writes a local config, initializes `Control`, and reports missing provider environment variables.

- [x] Test platformdirs root, setup from another CWD, explicit config, interactive defaults and safe rerun red.
- [x] Implement setup with `Control.initialize`, `writer_lock`, `open_database`, private paths and no model calls; run focused tests green.
- [x] Commit.

## Task 2: Implement cross-platform background lifecycle

**Files:** Create `src/quality_platform/web/processes.py`; modify `src/quality_platform/web/launcher.py` and `src/quality_platform/web/cli.py`; add lifecycle integration tests.

**Interfaces:** `start(state: Path, port: int) -> str`; `status(state: Path) -> dict`; `stop(state: Path) -> None`. Child process calls `python -m quality_platform.web.cli worker|serve`; use `psutil.Process.create_time()` in the marker, `FileLock`, private log files, POSIX session detachment and Windows creation flags.

- [x] Test duplicate start, existing dashboard, health and browser call, stale/mismatched process, dead child, startup failure and clean stop red.
- [x] Implement process manager and Typer `dashboard/status/stop`; run focused tests green on native Windows and portable unit tests elsewhere.
- [x] Commit.

## Task 3: Ship an installable release ZIP

**Files:** Add `scripts/package-release.py`, `scripts/install.ps1`, `scripts/install.sh`; update `README.md`, `docs/web.md`; add archive manifest tests and cross-platform CI workflow.

**Interfaces:** Release archive contains `src`, `tests`, `benchmarks`, `web/dist`, `config.yaml`, `pyproject.toml`, `uv.lock` and installer scripts. Installers create a locked environment and a user-level `quality-platform` shim; the Python launcher finds bundled resources independent of CWD.

- [x] Test the archive contains every runtime/evidence file and excludes private data and caches red.
- [x] Implement packaging and install scripts; verify a fresh extracted archive can initialize, start, stop and restart on Windows.
- [x] Add Windows, macOS and Linux CI smoke matrix; run available local checks and record unverified OSs honestly.
- [x] Commit.

## Completion gate

- [x] `quality-platform --help` lists `setup`, `dashboard`, `status`, `stop` in a clean Windows release install; the CI matrix checks the same contract on macOS and Linux.
- [x] Full offline Python and frontend checks pass; no provider call is made by setup/dashboard.
- [ ] Clean release ZIP smoke is verified on Windows; macOS and Linux CI results remain to be observed after push.
