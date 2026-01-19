# MX Swarm Agent Continuation Guide

**Last Updated**: 2026-01-19
**Session**: Claude Code + Antigravity collaboration

---

## Project Overview

MX Swarm is an autonomous agent orchestration system that takes PRDs (Product Requirements Documents) and executes them using AI worker swarms. The system operates with minimal human intervention after "ignition" - the initial planning and team composition phase.

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

## Session Work Summary (2026-01-20)

### What Was Done

#### 1. Dashboard Telemetry Enhancements
Added granular telemetry components to the dashboard:
- **Worker Scaling Card**: Real-time display of active workers vs. cap with progress bar
- **Governor Adjustments Feed**: Live feed of governor actions (throttle, adjust_pr_cap, etc.)
- **Escalations Panel**: Active escalations with severity badges and acknowledge buttons

New API endpoints added:
```python
GET /api/telemetry/snapshot     # Current TelemetrySnapshot from Governor
GET /api/governor/adjustments   # Recent governor adjustments with mode/caps
GET /api/escalations            # Active escalations (already existed)
```

#### 2. Simulation + Dashboard Integration
Enhanced the simulation harness to work with the live dashboard:
- Simulation sends events to `/api/simulation/event` endpoint
- Dashboard updates in real-time showing:
  - PRs opened/merged in Event Stream
  - Workers count updating
  - CI health pass/fail ratio
  - Governor state changes (NORMAL → THROTTLED)
  - Burn rate accumulating
  - Governor adjustments feed populating

Fixed bugs:
- Added `GOV_CHECK` to `EventType` enum
- Fixed `KeyError: 'last_seen'` when registering simulated workers
- Added `Reporter` import that was missing after refactor

#### 3. Swarm Operating Model Consolidation
Unified Governor and EscalationEngine across all components:
- `judge/service.py` now uses `boss.Governor` for state management
- `dashboard/api.py` syncs with centralized Governor
- `simulation/harness.py` provides telemetry to Governor
- Fixed circular import between `boss` and `judge` modules

#### 4. Simulation Improvements
- Changed tick speed from 0.1s to 2s for observable demos
- Enhanced events to include workers, CI health, costs, PR counts
- Added PR_MERGE and GOV_CHECK event notifications

---

## Session Work Summary (2026-01-19)

### What Was Done

#### 1. Environment Setup
- Created Python virtual environment with Python 3.14.2
- Installed all dependencies from `requirements.txt`
- Created `.env.example` template for API keys

#### 2. Import Fixes (Critical)
Fixed `ImportError: attempted relative import beyond top-level package` by converting all relative imports to absolute imports in these files:
- `planner/service.py` - changed `from ..shared` to `from shared`
- `worker/service.py` - changed `from ..shared` to `from shared`
- `judge/service.py` - changed `from ..shared` to `from shared`
- `boss/agent_boss.py` - changed `from ..shared` to `from shared`
- `boss/org_builder.py` - changed `from ..shared` to `from shared`
- `boss/governor.py` - changed `from ..shared` to `from shared`
- `shared/adapters/queue.py` - changed `from ..shared` to `from shared`

#### 3. New Files Created

**`boss/escalation.py`** - Escalation Engine
```python
# Event-driven human alerts with triggers:
# - CI health < 85% for 30 min
# - Retry rate > 25% for 30 min
# - No merges for 60+ min (stall)
# - Budget burn > 80% of cap
# - Security gate failures

class EscalationEngine:
    def check_triggers(self, metrics: Dict) -> List[Escalation]
    def acknowledge(self, escalation_id: str) -> bool
    def resolve(self, escalation_id: str) -> bool
```

**`boss/reporter.py`** - Ops Report Generator (by Antigravity)
```python
# Generates hourly and daily ops reports
# Stores as Markdown in reports/hourly/ and reports/daily/

class Reporter:
    def generate_hourly_report(self, metrics: Dict) -> OpsReport
    def generate_daily_report(self, metrics: Dict) -> OpsReport
```

**`simulation/harness.py`** - 20-Agent Simulation (by Antigravity)
```python
# Test harness with mocked GitHub, CI, and LLM
# Validates governor stability, PR flood control

class SimulationRunner:
    def run_simulation(self, duration_minutes: int)
```

**`dashboard/__init__.py`** - Module init (was missing)

**`simulation/__init__.py`** - Module init

**`.env.example`** - Environment template

#### 4. Dashboard Enhancements

**PRD Drop Zone** - Added to `dashboard/index.html`
- Drag-and-drop PRD file upload
- Keyword-based analysis (works WITHOUT API keys)
- Shows: complexity, risk level, recommended workers, categories, team composition

**New API Endpoints** - Added to `dashboard/api.py`
```python
# PRD Analysis
POST /api/prd/analyze           # Analyze PRD content

# Escalations
GET  /api/escalations           # List active escalations
POST /api/escalations/check     # Check triggers with metrics
POST /api/escalations/{id}/acknowledge
POST /api/escalations/{id}/resolve

# Reports
GET  /api/reports/hourly        # List hourly reports
GET  /api/reports/daily         # List daily reports
POST /api/reports/generate/hourly
POST /api/reports/generate/daily
```

#### 5. Updated Exports

**`boss/__init__.py`** - Added exports:
- `Reporter`, `OpsReport`
- `EscalationEngine`, `Escalation`, `EscalationType`, `EscalationSeverity`

---

## Three Operating Loops (from GPT_INSTRUCTIONS.md)

### Loop A - Worker Loop (Continuous)
- Workers pull `TaskContracts` from queue
- Implement code changes, open PRs
- Retry with bounded policy (max retries + backoff + DLQ)
- Never exit - idle/sleep when no tasks ("Ralph Wiggum" loop)

### Loop B - Judge/Governor Loop (Every 5-15 min)
- Monitors CI health, PR backlog, retry rates, budget burn
- Actions: throttle workers, freeze PRs, shift allocation, replan, escalate

### Loop C - Executive Summary Loop (Hourly + Daily)
- Generates ops reports as timestamped Markdown
- Shows in dashboard
- Format: what shipped, blockers, quality, budget, decisions needed

---

## File Structure

```
evolution-mx-swarm/
├── boss/
│   ├── __init__.py          # Exports all boss modules
│   ├── agent_boss.py        # Strategic orchestration
│   ├── escalation.py        # NEW: Event-driven alerts
│   ├── governor.py          # Threshold enforcement
│   ├── org_builder.py       # Team composition
│   └── reporter.py          # NEW: Ops report generator
├── cli/
│   └── main.py              # CLI entry point
├── dashboard/
│   ├── __init__.py          # NEW: Module init
│   ├── api.py               # FastAPI backend
│   └── index.html           # Web UI with PRD drop zone
├── judge/
│   └── service.py           # CI/PR monitoring
├── planner/
│   └── service.py           # PRD decomposition
├── shared/
│   ├── adapters/
│   │   └── queue.py         # Redis queue adapter
│   ├── schemas/
│   │   └── task_contract.py # TaskContract schema
│   └── utils/
│       └── config.py        # Configuration
├── simulation/
│   ├── __init__.py          # NEW: Module init
│   └── harness.py           # NEW: 20-agent simulation
├── worker/
│   └── service.py           # Worker implementation
├── templates/
│   ├── AGENTS.md            # Agent guidelines
│   └── ...
├── reports/                 # NEW: Generated reports
│   ├── hourly/
│   └── daily/
├── .env.example             # NEW: Environment template
├── GPT_INSTRUCTIONS.md      # NEW: Operating model spec
├── AGENT.md                 # NEW: This file
└── requirements.txt
```

---

## How to Continue

### 1. Start Fresh Session
```bash
cd evolution-mx-swarm
source .venv/bin/activate
```

### 2. Run Dashboard (No API Keys Needed)
```bash
uvicorn dashboard.api:app --host 0.0.0.0 --port 8080
# Open http://localhost:8080
# Drop a PRD file to analyze it
```

### 3. Run Tests
```bash
pytest tests/ -v
# All 41 tests should pass
```

### 4. Run Full Swarm (Needs API Keys)
```bash
# Copy .env.example to .env and add your keys:
# - ANTHROPIC_API_KEY or OPENAI_API_KEY
# - GITHUB_TOKEN

python cli/main.py run --workers 5
```

### 5. Run Simulation (No API Keys)
```bash
python simulation/harness.py
# Runs 20 mock agents for 10 minutes
```

---

## API Keys Required

For full operation, add to `.env`:
```bash
# LLM Provider (one of these)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# GitHub (for PR creation)
GITHUB_TOKEN=ghp_...

# Optional
REDIS_URL=redis://localhost:6379
```

---

## Known Issues / TODO

1. **Redis optional**: Queue falls back to in-memory if Redis not available
2. **Simulation**: Currently mocks LLM responses - could add real LLM testing
3. **Dashboard**: Escalation panel UI exists but needs wiring to backend events
4. **Governor**: Hysteresis logic implemented but needs real-world tuning

---

## Key Files to Read First

1. `GPT_INSTRUCTIONS.md` - Operating model specification
2. `PRD.md` - Product requirements document
3. `templates/AGENTS.md` - Worker guidelines
4. `boss/governor.py` - Threshold enforcement logic
5. `boss/escalation.py` - Alert triggers
6. `dashboard/api.py` - All API endpoints

---

## Commands Reference

```bash
# CLI
python cli/main.py --help
python cli/main.py status
python cli/main.py run --workers 5

# Dashboard
uvicorn dashboard.api:app --port 8080

# Tests
pytest tests/ -v

# Simulation
python simulation/harness.py
```

---

## Contact / Context

This project was built collaboratively by:
- Initial structure: Claude Code Mobile
- Enhancements: Antigravity + Claude Code (Opus 4.5)

The goal is autonomous software delivery with minimal human oversight after "ignition".
