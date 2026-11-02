# MX Swarm

**PRD-to-Shipping Agent Delivery Harness (Experimental Research Prototype)**

An autonomous multi-agent research prototype that takes a PRD (Product Requirements Document), generates structured specifications and tasks (`SPEC.json`, `TaskContract`), and coordinates worker execution, verification checks, and VCS landing (`github`, `aether`, or offline `mock_github`).

> **Research Prototype Notice:** MX Swarm is released as an experimental research prototype. Live-model execution, canned worker-load replay (`worker_load_canned`), mock-sink verification (`mock_github`), and control-plane simulation (`control_plane_hybrid`) are distinct modes; see [`reports/MX_CLOUD_AGENT_50_WORKER_DEMO_BRIEF_2026-10-22.md`](reports/MX_CLOUD_AGENT_50_WORKER_DEMO_BRIEF_2026-10-22.md) and [`reports/MX_SCALE_GATE_STATUS_2026-04-27.md`](reports/MX_SCALE_GATE_STATUS_2026-04-27.md) for measured attempt populations and current scale-gate status.

## Features

- **Drop-in PRD execution**: Decompose `PRD.md` into structured `SPEC.json` and `TaskContract` items
- **Multi-mode verification**: Live LLM worker execution, deterministic canned-completion worker proofs, and hybrid control-plane simulations
- **Guardrails-first**: Enforced CI lint, type, unit test, Bandit security, and release-hygiene gates
- **Workspace containment**: Canonical path containment, symlink rejection, sanitized subprocess environments, and process-group timeout reaping
- **Evidence packs**: Structured verification and settlement telemetry per task attempt

## Quick Start

### Prerequisites

- Python 3.11+
- Git
- Optional: Anthropic, OpenAI, or Gemini API key, or local Ollama server (not required for offline proofs or tests)

### Installation (Single Tested Setup Path)

```bash
# Create and activate a virtual environment (outside cloud-synced directories)
python3 -m venv .venv
source .venv/bin/activate

# Install MX Swarm and development/verification dependencies
python -m pip install --upgrade pip
pip install -e ".[dev]"

# Verify the installation with the default regression suite
pytest
```

### Working Offline Example (No API Keys Required)

```bash
# Run an end-to-end single-slice worker proof using a deterministic canned fixture and mock GitHub sink
python scripts/run_mock_sink_live_proof.py \
  --slice jsonpeek \
  --canned-completion-file proof_fixtures/jsonpeek_canned_completion.json

# Run a 50-lane deterministic control-plane simulation
python scripts/run_scale_control_plane.py --logical-lanes 50 --logical-tasks 50 --seed 7
```

For the bounded MX Swarm × Project Aether semantic-merge canary ([`docs/aether_semantic_canary_reproduction.md`](docs/aether_semantic_canary_reproduction.md)), AST-level checks run on all platforms while the full behavioral sandbox check requires macOS (`/usr/bin/sandbox-exec`).

### Configuration

**Option A: Cloud API (Anthropic / OpenAI / Gemini)**

Set environment variables:

```bash
export ANTHROPIC_API_KEY="your-api-key"
export GITHUB_TOKEN="your-github-token"
```

Or create a `.env` file (never committed):

```env
ANTHROPIC_API_KEY=your-api-key
GITHUB_TOKEN=your-github-token
```

**Option B: Local Models (Ollama)**

```bash
# Install Ollama and pull a local model
ollama pull mistral:7b
ollama serve
```

Configure `mx.config.json` for local models:
```json
{
  "llm": {
    "provider": "ollama",
    "model": "mistral:7b",
    "base_url": "http://localhost:11434"
  }
}
```

### Running the Swarm

```bash
# Initialize project configuration
python -m cli.main init --owner YOUR_GITHUB_USER --repo YOUR_REPO

# Start the swarm
python -m cli.main run --workers 5

# Or run individual components
python -m cli.main plan      # Generate tasks from PRD
python -m cli.main status    # Check progress
python -m cli.main judge     # Run evaluation
```

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Planner   │────▶│    Queue    │◀────│   Workers   │
│             │     │  (Pub/Sub)  │     │   (1-100)   │
│ PRD → SPEC  │     └─────────────┘     │             │
│ → Tasks     │            ▲            │ Task → PR   │
└─────────────┘            │            └─────────────┘
                           │
                    ┌─────────────┐
                    │    Judge    │
                    │             │
                    │ Evaluate +  │
                    │ Replan/Stop │
                    └─────────────┘
```

### Components

| Component | Role |
|-----------|------|
| **Planner** | Parses PRD, generates SPEC.json, creates TaskContracts |
| **Worker** | Pulls tasks, implements changes, opens PRs with evidence |
| **Judge** | Evaluates progress, triggers replan/reset/stop |
| **CLI** | User interface for controlling the swarm |

## CLI Commands

```bash
mx run [--workers N]     # Start full swarm
mx run --dashboard-url URL  # Sync status with dashboard at URL
mx status               # Show progress and metrics
mx plan [--force]       # Run planner only
mx judge                # Run judge evaluation
mx scale --max N        # Adjust worker limit
mx pause                # Pause all workers
mx resume               # Resume workers
mx budget --daily N     # Set daily USD cap
mx ignite               # Agent Boss: analyze PRD and setup team
mx dashboard [--port N] # Start web dashboard (default: 8080)
```

### Dashboard + CLI Sync

Run dashboard and swarm in separate terminals for real-time monitoring:

```bash
# Terminal 1: Start dashboard
python -m cli.main dashboard

# Terminal 2: Start swarm (auto-syncs with dashboard)
python -m cli.main run --workers 5
```

The CLI swarm sends heartbeats every 5 seconds so the dashboard shows accurate "Running" status.

Org chart interactions:
- Drag nodes to reposition streams during demos.
- Click "Freeze" to pause the layout animation (click again to unfreeze).

## Repo Artifacts

| File | Purpose |
|------|---------|
| `PRD.md` | Human-readable requirements (input) |
| `AGENTS.md` | Guidelines for agent behavior |
| `SPEC.json` | Machine-readable requirements graph |
| `mx.config.json` | Runtime configuration |
| `JUDGE_REPORT.md` | Append-only judge decisions |

## TaskContract Schema

Workers execute TaskContracts with this structure:

```json
{
  "id": "abc123",
  "type": "feature|bugfix|test|refactor|ops|doc",
  "goal": "One sentence objective",
  "context": {
    "prd_refs": ["REQ-001"],
    "files_hint": ["src/app.py"],
    "do_not_touch": ["config/secrets.py"]
  },
  "acceptance": {
    "checks": ["pytest tests/", "ruff check ."],
    "criteria": ["Feature X works as specified"]
  },
  "constraints": {
    "timebox_minutes": 60,
    "risk": "low|medium|high",
    "merge_policy": "manual|auto_on_pass|require_review",
    "retry_max": 3
  }
}
```

## Deployment

### Local Development

```bash
# Using Docker Compose
docker-compose --profile run up --scale worker=5
```

### GCP (Cloud Run + Pub/Sub)

```bash
# Set GCP project
export GCP_PROJECT_ID=your-project

# Deploy
./deploy/gcp/deploy.sh

# Run planner
gcloud run jobs execute mx-planner --region=us-central1
```

### GitHub Actions

Trigger via workflow dispatch:
1. Go to Actions → "MX Swarm"
2. Click "Run workflow"
3. Select action (run/plan/status/judge)

## Guardrails

### Hard Guardrails (Enforced)
- Workers cannot push to `main`
- PRs must pass lint/test/security checks
- Evidence pack required in every PR
- Timebox enforced with automatic retry

### Soft Guardrails (Guidance)
- Tasks should be 30-90 minutes
- Prefer modular changes
- Add tests for new behavior

## Configuration

`mx.config.json`:

```json
{
  "version": "1.0.0",
  "queue": {
    "provider": "memory",  // or "gcp", "azure"
    "gcp": {
      "project_id": "your-project",
      "topic_id": "mx-tasks"
    }
  },
  "llm": {
    "provider": "ollama",  // or "anthropic", "openai"
    "model": "mistral:7b",
    "base_url": "http://localhost:11434"
  },
  "worker": {
    "max_workers": 10,
    "timebox_minutes": 60,
    "retry_max": 3
  },
  "judge": {
    "interval_minutes": 60,
    "failure_rate_threshold": 0.3
  },
  "github": {
    "repo_owner": "your-user",
    "repo_name": "your-repo",
    "auto_merge_low_risk": false
  },
  "budget": {
    "daily_usd_cap": 100.0
  }
}
```

## Observability

Metrics tracked:
- Tasks created/completed/failed
- Queue depth
- PR throughput
- CI pass/fail rate
- LLM token usage

View via:
- `mx status` CLI command
- `JUDGE_REPORT.md` for evaluations
- Cloud provider dashboards

## Security

See [`SECURITY.md`](SECURITY.md) for private vulnerability reporting and a detailed description of the trusted-code execution boundary:
- Canonical workspace path containment and symlink rejection in `worker/service.py`
- Sanitized subprocess environments (host API keys/tokens stripped) and process-group termination on timeout
- Loopback-by-default dashboard binding (`127.0.0.1`) and token-authenticated control routes (`MX_DASHBOARD_TOKEN`)

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the supported environment, single tested setup path, offline verification workflow, architecture entry points, and benchmark scaffold conventions.

## License & Third-Party Notices

- Licensed under the MIT License — see [`LICENSE`](LICENSE).
- Third-party dependency notices and attributions — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
