# Contributing to MX Swarm

MX Swarm is an experimental research prototype exploring PRD-to-shipping multi-agent orchestration, deterministic quality gates, and control-plane routing/governance.

## Supported Environment

- **OS:** macOS or Linux (POSIX environment; uses `fcntl` file locks and process-group signals)
- **Python:** Python `3.11+` (`3.11`–`3.14` tested)
- **Git:** `2.39+`
- **Optional (Dashboard V2 frontend rebuilds only):** Node.js `20+` and `npm`

## Single Tested Setup Path

Run from a clean checkout outside cloud-synced folders (such as iCloud Drive or Dropbox) so virtual environment I/O is not stalled:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

## Working Offline Example (No API Keys or External Network Required)

You can execute a complete end-to-end single-slice worker proof (`jsonpeek`) offline using the canned completion fixture and the local mock GitHub sink:

```bash
python scripts/run_mock_sink_live_proof.py \
  --slice jsonpeek \
  --canned-completion-file proof_fixtures/jsonpeek_canned_completion.json
```

You can also run the deterministic control-plane simulation offline:

```bash
python scripts/run_scale_control_plane.py --logical-lanes 50 --logical-tasks 50 --seed 7
```

## Authoritative Verification Commands

All pull requests must pass these checks before merge:

```bash
# 1. Default regression test suite (excludes unimplemented qualification benchmark scaffolds)
pytest

# 2. Ruff linter
ruff check .

# 3. Static type checking
mypy shared planner worker judge cli --ignore-missing-imports

# 4. Bandit security scan
bandit -r shared planner worker judge cli -ll -ii

# 5. Release hygiene & boundary check
python scripts/check_release_hygiene.py
```

### Qualification Benchmark Scaffolds vs. Regression Tests

The modules under `tools/` (`jsonpeek.py`, `envpeek.py`, `tomlpeek.py`, `namecore.py`, `namecli.py`, `slugcore.py`, `slugcli.py`) are intentional `NotImplementedError` benchmark scaffolds that MX workers implement during qualification runs.

- Default `pytest` runs ignore their acceptance tests (`tests/test_jsonpeek.py`, etc.) via `tests/conftest.py` so the core regression suite tests MX Swarm itself.
- When a worker or proof script explicitly targets a slice test (for example, `pytest tests/test_jsonpeek.py`) or when `--run-benchmark-scaffolds` is passed, pytest collects and executes those scaffold tests normally.

## Architecture Entry Points

| Area | Entry Point | Documentation |
| --- | --- | --- |
| CLI & Preflight | `cli/main.py` | `README.md`, `AGENTS.md` |
| Planner (PRD -> `SPEC.json` -> `TaskContract`) | `planner/service.py` | `docs/reliability/stage_gate_criteria.md` |
| Worker (Ralph Wiggum Loop, Containment, Reconciliation) | `worker/service.py` | `docs/reliability/delivery_chain_contract.md` |
| Judge & Quality Assessment | `judge/service.py` | `docs/reliability/stage_gate_criteria.md` |
| Control Plane, Routing & Governor | `shared/utils/control_plane.py`, `shared/utils/routing.py` | `docs/architecture/scaling_to_1000.md` |
| VCS Adapters (`github`, `aether`) | `shared/adapters/` | `docs/architecture/aether_sink_contract.md` |
| Dashboard (`/` and `/v2/`) | `dashboard/api.py`, `dashboard_v2/` | `AGENTS.md` |

### Execution Mode Distinctions

When documenting or reporting results, always distinguish between:
1. **Live-model execution:** Real LLM provider calls (`anthropic`, `openai`, `google`, `ollama`) with live or local VCS verification.
2. **Canned worker replay (`--canned-completion-file` / `worker_load_canned`):** Real worker subprocesses and test runners replaying a deterministic completion fixture without live LLM calls.
3. **Mock sink (`mock_github`):** Local git branch + `mock_prs.json` sink rather than remote GitHub or Aether APIs.
4. **Control-plane simulation (`control_plane_hybrid`):** In-memory routing, dependency, and governor simulation without worker subprocesses or live sinks.

## Pull Request Guidelines

- Keep commits and PRs small and focused (`type: brief description`).
- Do not commit secrets, `.env` files, or raw `.runtime/` / `reports/` directories outside `RELEASE_ALLOWLIST.json`.
- Preserve third-party license notices in `LICENSE` and `THIRD_PARTY_NOTICES.md`.
