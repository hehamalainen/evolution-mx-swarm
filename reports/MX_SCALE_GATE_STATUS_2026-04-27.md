# MX Scale Gate Status Summary

- Date: `2026-10-22` (updated from `2026-04-27` baseline)
- Overall Scale Gate Verdict: **NOT QUALIFIED FOR 1,000-LANE PRODUCTION / RESEARCH PROTOTYPE ONLY**

## Summary by Gate & Execution Mode

1. **Control-Plane Simulation (`control_plane_hybrid`)**
   - Mode: Pure control-plane simulation (`sink_mode="simulated"`, no live LLM or VCS calls).
   - Unmeasured metrics explicitly labeled `unmeasured` (`None` in JSON): `false_success_rate`, `sink_truth_divergence_count`.
   - `duplicate_terminal_settlements` is computed from observed task terminal transitions during the simulation run.

2. **Isolated Worker Load (`worker_load_canned`)**
   - Mode: Per-worker subprocesses replaying `proof_fixtures/jsonpeek_canned_completion.json` against `mock_github` (`active_concurrency=10`).
   - Does **not** exercise live LLM inference or the shared control-plane router/governor.
   - Unmeasured metrics explicitly labeled `unmeasured` (`None` in JSON): `blocked_dependency_ratio`, `governor_interventions`, `cost_per_success_usd`, `queue_depth_p95`, `retry_cascade_count`, `duplicate_terminal_settlements`, `zombie_blocked_tasks`, `unstable_governor_oscillation_count`.
   - October 22 attempt population (`logical_workers=50`): `1/4` passed (`174315Z`), `3/4` failed (`172606Z`, `172722Z`, `172959Z`).

3. **Sampled-Real 1,000-Lane Combined Run (`sampled_real_hybrid`)**
   - Mode: 8 canned `mock_github` worker proofs + 992 simulated lanes.
   - October 22 attempt population: `0/4` passed (`172606Z`, `172742Z`, `173255Z`, `174345Z` all failed `control_plane_loss_budget_fail` with `805/1000` `settled_ack` and `195/1000` `dead_letter`).

4. **Single-Worker Live Model Smoke (`gemini-2.5-flash`)**
   - Mode: 1 worker calling live `gemini-2.5-flash` on `jsonpeek` against `mock_github`.
   - October 22 attempt population: `1/4` passed (`174159Z`), `3/4` failed (`172606Z`, `172716Z`, `172920Z`).
