# MX Scale & Cloud-Agent Qualification Brief (Corrected Scope & Attempt Population)

- Date: `2026-10-22`
- Status: `PARTIAL / EXPERIMENTAL PROTOTYPE`
- Scope separation: **Live-model smoke**, **canned worker-load replay**, **mock-sink behavior**, and **control-plane simulation** are distinct execution modes and must not be conflated as a single 50-worker live cloud-agent run.

---

## 1. Execution Mode Separation & Attempt Population (2026-10-22)

| Execution Mode | Phase / Harness | Model Mode | Sink Mode | Attempt Population (2026-10-22) | Pass / Fail | Key Failure Reasons |
| --- | --- | --- | --- | --- | --- | --- |
| **1-Worker Live Cloud-Agent Smoke** | `scripts/run_semantic_canary_qualification.py` (`jsonpeek` slice, 1 worker) | Live `google` / `gemini-2.5-flash` | `mock_github` (local git + mock PR API) | `4` attempts (`172606Z`, `172716Z`, `172920Z`, `174159Z`) | `1 / 4` passed (`174159Z`) | `3` earlier attempts failed (`1` LLM timeout, `2` task ended in `settled_nack`) |
| **50-Worker Canned Worker-Load** | `scripts/run_scale_worker_load.py` (`phase=worker_load_canned`, `logical_workers=50`, `active_concurrency=10`) | Canned completion replay (`proof_fixtures/jsonpeek_canned_completion.json`, **no live LLM calls**) | `mock_github` (`sink_mode="mock_github"`, **not live GitHub or Aether**) | `4` attempts (`172606Z`, `172722Z`, `172959Z`, `174315Z`) | `1 / 4` passed (`174315Z`: `50/50` verified landings in `29.38s`) | `3` earlier same-day attempts (`172606Z`, `172722Z`, `172959Z`) failed `0/50` (`post_landing_verify` failed with `unexpected branch diff: []`) |
| **50-Lane Control-Plane Simulation** | `scripts/run_scale_control_plane.py` (`phase=control_plane_hybrid`, `logical_lanes=50`, `logical_tasks=50`) | Simulated latency/cost envelopes (**no live LLM calls**) | `simulated` (**no live VCS or worker processes**) | `4` attempts (`172606Z`, `172716Z`, `172920Z`, `174159Z`) | `4 / 4` passed (`43` `settled_ack`, `7` `dead_letter`) | N/A (simulation only; `false_success_rate` and `sink_truth_divergence_count` are `unmeasured`) |
| **1,000-Lane Sampled-Real Hybrid** | `scripts/run_sampled_real_scale.py` (`phase=sampled_real_hybrid`, `logical_lanes=1000`, `real_workers=8`) | `8` canned worker proofs + `992` simulated lanes | `mock_github` + `simulated` | `4` attempts (`172606Z`, `172742Z`, `173255Z`, `174345Z`) | `0 / 4` passed (**FAILED**) | All `4` attempts failed `control_plane_loss_budget_fail` (`805` `settled_ack`, `195` `dead_letter`, loss ratio `19.5%` > `15%` budget) |

---

## 2. Detailed Breakdown by Mode

### 2.1 Live Cloud-Agent Smoke (1 Worker, Live `gemini-2.5-flash`)
- **What ran:** A single worker executing the `jsonpeek` qualification slice with live `gemini-2.5-flash` inference and local `pytest`/`ruff` verification against `mock_github`.
- **Attempt population:** 4 runs on `2026-10-22`:
  - `20261022T172606Z`: **FAILED** (`ERR_CHECK_FAIL` / `settled_nack`)
  - `20261022T172716Z`: **FAILED** (`ERR_TIMEOUT` / LLM request timed out)
  - `20261022T172920Z`: **FAILED** (`ERR_CHECK_FAIL` / `settled_nack`)
  - `20261022T174159Z`: **PASSED** (`settled_ack`, `post_landing_verify=pass`, duration `69.92s`)
- **Limitation:** Demonstrates single-worker live model execution on 1 slice (`1/4` attempt success rate), **not** 50 concurrent live LLM workers.

### 2.2 50-Worker Isolated Load (`worker_load_canned` on `mock_github`)
- **What ran:** 50 isolated worker subprocesses (`active_concurrency=10`) replaying the deterministic fixture `proof_fixtures/jsonpeek_canned_completion.json` against `mock_github`.
- **Attempt population:** 4 runs on `2026-10-22`:
  - `20261022T172606Z`: **FAILED** (`0/50` verified landings; `unexpected branch diff: []`)
  - `20261022T172722Z`: **FAILED** (`0/50` verified landings; `unexpected branch diff: []`)
  - `20261022T172959Z`: **FAILED** (`0/50` verified landings; `unexpected branch diff: []`)
  - `20261022T174315Z`: **PASSED** (`50/50` verified landings, `batch_wall_clock_seconds=29.38126`, `throughput_tasks_per_minute=102.105899`, `sink_settlement_latency_p95_s=4.530547`)
- **Unmeasured metrics in `worker_load_canned`:**
  - `blocked_dependency_ratio`: `unmeasured`
  - `governor_interventions`: `unmeasured`
  - `cost_per_success_usd`: `unmeasured`
  - `queue_depth_p95`: `unmeasured`
  - `retry_cascade_count`: `unmeasured`
  - `duplicate_terminal_settlements`: `unmeasured`
  - `zombie_blocked_tasks`: `unmeasured`
  - `unstable_governor_oscillation_count`: `unmeasured`

### 2.3 1,000-Lane Sampled-Real Combined Run (`sampled_real_hybrid`)
- **What ran:** Combined 1,000-lane control-plane simulation calibrated by 8 canned `mock_github` worker proofs.
- **Attempt population:** 4 runs on `2026-10-22` (`172606Z`, `172742Z`, `173255Z`, `174345Z`).
- **Result:** **FAILED (`0/4` passed)**. Even after the 8 canned worker proofs succeeded (`8/8`) in `174345Z`, the 1,000-lane combined run failed with `verdict_reasons: ["control_plane_loss_budget_fail"]` (`805` `settled_ack`, `195` `dead_letter`, `0` zombies).
