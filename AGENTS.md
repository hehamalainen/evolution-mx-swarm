# AGENTS.md

Guidelines for AI coding agents working on MX Swarm.

## Setup commands

- Install deps: `pip install -e ".[dev]"`
- Activate venv: `source venv/bin/activate`
- Start dashboard: `python3 dashboard/api.py`
- Run CLI: `python -m cli.main run`

## Testing

- Run all tests: `pytest`
- Run specific test: `pytest tests/test_file.py::test_name`
- Run with coverage: `pytest --cov`
- Lint: `ruff check .`
- Type check: `mypy shared planner worker judge cli`
- Security scan: `bandit -r shared planner worker judge cli`

Tests must pass before committing. Tests live in `tests/` directory.

## Code style

- Python 3.11+, type hints required
- snake_case for functions/variables
- Line length: 100 chars (ruff handles formatting)
- Use existing patterns in codebase
- Add comments only for complex logic

## PR instructions

- Keep PRs focused and small
- Title format: `type: brief description` (e.g., `fix: resolve queue timeout`)
- Run `ruff check .` and `pytest` before committing
- Never commit secrets or `.env` files

## Project structure

```
cli/          # CLI entry point (mx command)
shared/       # Shared utilities, models, config
planner/      # Task decomposition from PRD
worker/       # Task execution agents
judge/        # Quality assessment and retry logic
boss/         # Orchestration and coordination
dashboard/    # Web UI (FastAPI + static HTML)
tests/        # Pytest test suite
```

## Key files

- `mx.config.json` - Runtime configuration
- `SPEC.json` - Current sprint specification
- `TEAMPLAN.json` - Task breakdown for workers
- `PRD.md` - Product requirements document

## Constraints

- Do not modify `.github/` without explicit task
- Do not modify core infrastructure (shared/utils/config.py) without explicit task
- Prefer incremental changes over large rewrites
- VCS targets: only `aether` or `github` (not both)
- VCS preflight validates target readiness before launch

## Dashboard wiring

- No dead controls: UI exposes only actions wired to real backend behavior
- Telemetry source: status, queue depth, active tasks come from CLI heartbeats
- Org Chart: force-layout graph with drag-to-move, double-click to pin

## Running the swarm

1. Configure `mx.config.json` (set VCS provider, LLM settings)
2. Start dashboard: `python3 dashboard/api.py`
3. Launch swarm: `python -m cli.main run`
4. Monitor at `http://localhost:8080`

## Dashboard V2 (Gas Town)

- Source lives in `dashboard_v2/` (imported from `temp_analysis/gas_town_ui_poc/client`)
- Build steps: `cd dashboard_v2`, `npm install`, `npm run build`
- Served at `http://127.0.0.1:8080/v2/` by `dashboard/api.py`
- API base: uses `/api/status` (same FastAPI server)
- Key file: `src/lib/api_adapter.ts` maps SwarmStatus to graph nodes

## Dashboard Budget Policy

- Mission: dashboard is for observability and demo narrative, not core execution logic.
- Time cap: dashboard work should stay under 10% of implementation effort per sprint unless explicitly overridden.
- Reliability contract: both `http://127.0.0.1:8080/` and `http://127.0.0.1:8080/v2/` must stay runnable.
- Pre-merge smoke checks for dashboard changes:
  - `python -m cli.main dashboard --host 127.0.0.1 --port 8080`
  - `cd dashboard_v2 && npm run build`
  - Verify `/api/status`, `/`, and `/v2/` respond.
- Scope guardrail: reject dashboard-only rewrites that do not improve operator signal quality or reliability.

### npm install troubleshooting

**Problem**: npm install times out (30+ minutes) in agent environments.

**Root cause**: No `package-lock.json` exists. Without it, npm must resolve the entire dependency tree from scratch (56 packages with thousands of transitive deps).

**Fix**: Run locally once to generate lockfile:
```bash
cd dashboard_v2
npm install
git add package-lock.json
git commit -m "Add package-lock.json for faster installs"
```

After lockfile exists, future installs complete in seconds instead of hanging.

## Environment variables

Required in `.env`:
- `ANTHROPIC_API_KEY` - For Claude-based workers
- `GITHUB_TOKEN` - For GitHub VCS provider
- `AETHER_TOKEN` - For Aether VCS provider (if using)
