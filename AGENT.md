# MX Swarm Agent Continuation Guide

**Last Updated**: 2026-01-26
**Session**: Claude Code + Antigravity collaboration + User (Heikki)

---

## Project Overview

MX Swarm is an autonomous agent orchestration system that takes PRDs (Product Requirements Documents) and executes them using AI worker swarms. The system operates with minimal human intervention after "ignition" - the initial planning and team composition phase.

It has now evolved to support a **Local Mode**, allowing it to run entirely offline with local LLMs (Ollama) and local filesystem persistence (no GitHub token required).

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Agent Boss                              │
│  (Strategic orchestration, team composition, risk mgmt)      │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────┴───────────────────────────────────┐
│                       Planner                                │
│  (PRD decomposition → TaskContracts, dependency graph)       │
└─────────────────────────┬───────────────────────────────────┘
                          │
                    ┌─────┴─────┐
                    │   Queue   │
                    │ (Redis)   │
                    └─────┬─────┘
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
┌───────┴───────┐ ┌───────┴───────┐ ┌───────┴───────┐
│   Worker 1    │ │   Worker 2    │ │   Worker N    │
│ (Code + PRs)  │ │ (Code + PRs)  │ │ (Code + PRs)  │
└───────┬───────┘ └───────┬───────┘ └───────┬───────┘
        │                 │                 │
        └─────────────────┼─────────────────┘
                          │
┌─────────────────────────┴───────────────────────────────────┐
│                        Judge                                 │
│  (CI monitoring, PR review, Governor threshold enforcement)  │
└─────────────────────────────────────────────────────────────┘
```

---

## Session Work Summary (2026-01-26)

### What Was Done

#### 1. Dashboard-CLI Sync (Heartbeat System)
Fixed the dashboard showing "Stopped" when swarm was actually running.

**Problem**: Dashboard and CLI swarm run as separate processes with no communication. When running `mx run`, the dashboard had no way to know workers were active.

**Solution**:
- Added `/api/swarm/heartbeat` endpoint to dashboard API
- CLI swarm now sends heartbeats every 5 seconds with worker status
- Dashboard checks both `active_workers` and `online_workers` for status detection

**New CLI argument**:
```bash
mx run --dashboard-url http://localhost:8080  # Default
```

#### 2. Judge Reset Loop Fix
Fixed the Judge triggering infinite resets because:
- Planner wasn't actually invoked after reset
- Stall detection state (`no_progress_since`, `consecutive_failures`) wasn't cleared

#### 3. GitHub Token Integration
- Added `.env` file support with absolute path resolution
- Token now loads correctly when CLI runs as subprocess

#### 4. InMemoryQueue Singleton
Fixed workers not seeing tasks because Planner and Workers created separate queue instances. Now uses singleton pattern per queue name.

---

## Session Work Summary (2026-01-25)

### What Was Done

#### 1. Local Mode & "Offline" Sovereignty
We successfully implemented a fully "sovereign" mode where the swarm creates code without external dependencies.
- **Mock GitHub API**: `worker/service.py` detects missing `GITHUB_TOKEN` and swaps in a `MockGitHubAPI`.
- **Local Persistence**: Instead of pushing to `origin`, workers branch and push to the local filesystem.
- **Result**: You can checkout agent-created branches (`feature/task-X`) directly.

#### 2. Real-time Dashboard Streaming
- **Log Streaming**: `dashboard/api.py` now captures `stdout/stderr` from the swarm subprocess (`mx run`) and streams it line-by-line to the web UI.
- **Visual Feedback**: The dashboard accurately reflects "Ignition," "Planning," and "Execution" phases.

#### 3. End-to-End Verification ("The Counter API")
- **Input**: Provided a simple "Counter API" PRD.
- **Execution**: The swarm:
    1.  Ignited and analyzed requirements.
    2.  Split work into **Implementation** (Worker-0) and **Testing** (Worker-1).
    3.  Configured 2 parallel tasks.
    4.  Successfully generated `app/api.py` and `tests/test_api.py`.
- **Outcome**: Proven capability to generate functional, multi-file codebases from scratch using a local 7B model.

---

## How to Continue

### 1. Requirements
- **Python**: 3.11+
- **Ollama**: Running `mistral:7b` (or similar)
- **aiohttp** (optional): For async heartbeats (`pip install aiohttp`)

### 2. Running with Dashboard Sync

**Terminal 1 - Start Dashboard:**
```bash
python -m cli.main dashboard
# Or: python -m uvicorn dashboard.api:app --port 8080
# Dashboard available at http://localhost:8080
```

**Terminal 2 - Start Swarm:**
```bash
python -m cli.main run --workers 3
# Automatically sends heartbeats to dashboard every 5s
```

The dashboard will show "Running" status and update worker counts in real-time.

### 3. Ignite from Dashboard
1. **Edit PRD**: content of `PRD.md` determines what they build.
2. **Dashboard**: Go to `http://localhost:8080`.
3. **Launch**: Drag & drop PRD file or click "Ignite Swarm".
4. **Watch**: See the logs in "Event Stream", workers in "Worker Scaling" tile.

### 4. Retrieve Code
The swarm pushes code to local branches (Local Mode) or creates PRs (GitHub Mode).
```bash
git branch
# > feature/task-12345
git checkout feature/task-12345
```

With `GITHUB_TOKEN` set, PRs are created on GitHub automatically.

---

## Roadmap / Next Steps

1.  **Security Harness**: Implement the "Judge" as a security gatekeeper (SAST/DAST) before allowing any merge.
2.  **Production Hardening**: Move from `MockGitHub` to real GitHub App integration for multi-user collaboration.
3.  **Model Upgrades**: Test with larger local models (Llama-3 70B) for complex architecture tasks.

---

## Key Files
- `README.md` - Public documentation.
- `AGENTS.md` - System instructions.
- `walkthrough.md` - User guide for Local Mode.
- `dashboard/api.py` - The brain of the local UI.
- `worker/service.py` - The muscle (git operations).

---
**Status**: Alpha (Functional Local Prototype)
**Sign-off**: Antigravity & User (Heikki)
