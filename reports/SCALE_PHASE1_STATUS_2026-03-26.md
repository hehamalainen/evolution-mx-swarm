# MX Scale Phase 1 Status

## Scope Completed Today
- Track A item 1: staged hybrid control-plane experiments at `100`, `500`, `1000` logical lanes with multiple seeds.
- Track A item 2: canned worker-load characterization on real Aether sink at `100` logical workers.

## Track A Item 1: Hybrid Control-Plane Results
Primary comparison artifact:
- `reports/SCALE_CONTROL_PLANE_COMPARISON_2026-03-25.md`

Summary:
- `100` lanes, `3/3` seeds passed
- `500` lanes, `3/3` seeds passed
- `1000` lanes, `3/3` seeds passed
- `false_success_rate = 0.0` across all runs
- `zombie_blocked_tasks = 0` across all runs
- `duplicate_terminal_settlements = 0` across all runs
- `unstable_governor_oscillation_count = 0` across all runs

Aggregates:
- `100` lanes: avg verified `88.7/150`, avg convergence `55.0s`, avg DLQ `57.0`
- `500` lanes: avg verified `467.7/750`, avg convergence `68.3s`, avg DLQ `262.0`
- `1000` lanes: avg verified `874.3/1500`, avg convergence `66.7s`, avg DLQ `585.0`

Interpretation:
- The control plane remains stable through `1000` logical lanes.
- Queue pressure and DLQ volume scale materially with lane count, but not via false-success or queue-corruption failure modes.
- Passive governor remained bounded in all staged runs.

## Track A Item 2: Worker-Load Characterization
Validated smoke artifact:
- `reports/scale_worker_load/20260325T221828Z/scale_report.md`

Full `100`-worker characterization artifact:
- `reports/scale_worker_load/20260326T063023Z/scale_report.md`

Final result:
- `logical_workers = 100`
- `active_concurrency = 4`
- `verified_landings = 100`
- `total_terminal_tasks = 100`
- `false_success_rate = 0.0`
- `dlq_volume = 0`
- `duplicate_terminal_settlements = 0`
- `zombie_blocked_tasks = 0`
- `sink_truth_divergence_count = 0`
- `convergence_time_seconds = 54.302968`
- `sink_settlement_latency_p95_s = 44.48818175`

Interpretation:
- The canned worker path through real Aether sink is now stable at `100` logical workers on this machine when constrained to `active_concurrency = 4`.
- This harness measures worker/bootstrap/sink plumbing, not shared-queue contention.
- The current result is sufficient for Phase 1 worker-load admission.

## Engineering Fixes Required To Reach Stable Worker-Load
Files changed:
- `scripts/run_aether_live_proof.py`
- `scripts/run_scale_worker_load.py`
- `simulation/scale_harness.py`
- `worker/service.py`
- `tests/test_aether_live_proof.py`
- `tests/test_worker_stability.py`

Key fixes:
1. Replaced full-history per-proof clone with lightweight git snapshot repo bootstrap.
2. Disabled GPG signing in temporary proof repos to avoid hanging commits.
3. Excluded `venv` from proof workspace copy.
4. Switched proof workspace copy from broad blacklist to proof-critical allowlist.
5. Added `--source-root` support so worker-load runs can fan out from a prepared proof source tree.
6. Normalized `python -m ruff ...` to the direct `ruff` binary in worker command execution.

## Current Boundary
Proven now:
- `1000` logical-lane hybrid control-plane stability
- `100` logical-worker canned load through Aether at concurrency `4`

Not proven yet:
- worker-load at higher active concurrency on this machine
- shared-queue contention worker harness
- mixed-fidelity scale runs
- cloud model scaling economics

## Recommended Next Step
1. Freeze these Track A Phase 1 artifacts.
2. Extend worker-load characterization to `150` and `250` logical workers on the same fast path.
3. Keep `active_concurrency = 4` as the known-good local baseline until higher concurrency is explicitly re-qualified.
4. After that, start the actual MX vs Moltbook split.
