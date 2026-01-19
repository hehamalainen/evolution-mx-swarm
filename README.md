# MX Swarm

**PRD-to-Shipping Agent Delivery Harness**

An autonomous system that takes a PRD (Product Requirements Document), generates structured specifications and tasks, and runs a scalable worker swarm (up to 100) that opens PRs, passes CI gates, and iterates until the Definition of Done is satisfied.

## Features

- **Drop-in PRD execution**: Commit `PRD.md` and trigger the swarm
- **Scale to 100 workers**: Parallel task execution with automatic coordination
- **Guardrails-first**: No merge without passing deterministic CI gates
- **Cloud-agnostic**: GCP-first with Azure portability
- **Evidence packs**: Every PR includes verification evidence

## Quick Start

### Prerequisites

- Python 3.11+
- Git
- GitHub account with repo access
- Anthropic API key (or OpenAI/Ollama)

### Installation

```bash
# Clone and enter directory
cd your-project-repo

# Install MX Swarm
pip install -r mx-swarm/requirements.txt

# Initialize project
python -m mx-swarm.cli.main init --owner YOUR_GITHUB_USER --repo YOUR_REPO
```

### Configuration

Set environment variables:

```bash
export ANTHROPIC_API_KEY="your-api-key"
export GITHUB_TOKEN="your-github-token"
```

Or create a `.env` file:

```env
ANTHROPIC_API_KEY=your-api-key
GITHUB_TOKEN=your-github-token
```

### Running the Swarm

```bash
# Edit PRD.md with your requirements
vim PRD.md

# Start the swarm
python -m mx-swarm.cli.main run --workers 5

# Or run individual components
python -m mx-swarm.cli.main plan      # Generate tasks from PRD
python -m mx-swarm.cli.main status    # Check progress
python -m mx-swarm.cli.main judge     # Run evaluation
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
mx status               # Show progress and metrics
mx plan [--force]       # Run planner only
mx judge                # Run judge evaluation
mx scale --max N        # Adjust worker limit
mx pause                # Pause all workers
mx resume               # Resume workers
mx budget --daily N     # Set daily USD cap
```

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
    "provider": "anthropic",
    "model": "claude-sonnet-4-20250514"
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

- Secrets stored in Secret Manager / Key Vault
- No secrets in logs
- Least privilege IAM
- Scoped GitHub tokens

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes with tests
4. Submit a PR

## License

MIT License - see LICENSE file
