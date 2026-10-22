# MX Swarm: Cloud Agent & 50-Worker Qualification Brief (Corrected Scope & Attempt Population)

- **Date:** `2026-10-22`
- **Status:** `PARTIAL / EXPERIMENTAL PROTOTYPE`
- **Scope Separation:** **Live-model smoke** (`scripts/run_frontier_smoke.py`), **canned worker-load replay** (`scripts/run_scale_worker_load.py`), **mock-sink behavior** (`mock_github`), **control-plane simulation** (`scripts/run_scale_control_plane.py`), and **sampled-real hybrid runs** (`scripts/run_scale_sampled_real_hybrid.py`) are distinct execution modes and must not be conflated as a single 50-worker live cloud-agent run.

---

## 1. Execution Mode Separation & Full Attempt Population (2026-10-22)

| Execution Mode | Harness Script & Phase | Model Mode | Sink Mode | Attempt Population (`2026-10-22` UTC) | Pass / Fail | Key Metrics & Failure Reasons (from Retained Artifacts) |
| --- | --- | --- | --- | --- | --- | --- |
| **0. GCP Staging Plan** | `cli/main.py gcp-stage` (`Gate A`) | N/A (config generation) | Staging manifest only | `1` (`20261022T082823Z`) | **Staged** | Generates Pub/Sub + KEDA config (`reports/gcp_gate_ab/20261022T082823Z/gcp_gate_ab_plan.md`) |
| **1. 1-Worker Live Cloud-Agent Smoke** | `scripts/run_frontier_smoke.py` (`jsonpeek` slice, `worker_count=1`) | Live `gemini` / `gemini-2.5-flash` | `mock_github` (local git + mock PR API) | `4` attempts (`085926Z`, `090226Z`, `092017Z`, `092200Z`) | **`1 / 4` passed** (`20261022T092200Z`) | `085926Z` failed (`settled_nack`, `ERR_CHECK_FAIL`); `090226Z` and `092017Z` failed post-run verification (`verification_passed=false`); `092200Z` passed (`settled_ack`, `verification_passed=true`, `stop_convergence_seconds=0.035487`) |
| **2. 50-Worker Isolated Canned Load** | `scripts/run_scale_worker_load.py` (`phase=worker_load_canned`, `logical_workers=50`, `active_concurrency=10`) | Canned completion replay (`proof_fixtures/jsonpeek_canned_completion.json`, **no live LLM calls**) | `mock_github` (`sink_mode="mock_github"`, **not live GitHub or Aether**) | `4` attempts (`085958Z`, `090235Z`, `092032Z`, `092213Z`) | **`1 / 4` passed** (`20261022T092213Z`) | `085958Z` (`24.622396s`), `090235Z` (`34.292118s`), and `092032Z` (`30.265162s`) failed `0/50` (`dlq_volume=50`, `unexpected branch diff: []`). `092213Z` passed `50/50` verified landings in `batch_wall_clock_seconds=35.241819` (`throughput_tasks_per_minute=85.126139`, `sink_settlement_latency_p95_s=7.927865`) |
| **3. 50-Lane Control-Plane Simulation** | `scripts/run_scale_control_plane.py` (`phase=control_plane_hybrid`, `logical_lanes=50`, `total_terminal_tasks=100`, `seed=7`) | Simulated latency/cost envelopes (**no live LLM calls**) | `simulated` (**no worker subprocesses or live VCS**) | `1` standalone run (`20261022T083638Z`) | **`1 / 1` structural pass** (`20261022T083638Z`) | `verified_landings=42`, `total_terminal_tasks=100`, `dlq_volume=58`, `governor_interventions=5`, `batch_wall_clock_seconds=0.014207`; `false_success_rate` and `sink_truth_divergence_count` are `unmeasured` in simulation |
| **4. 1,000-Lane Sampled-Real Hybrid** | `scripts/run_scale_sampled_real_hybrid.py` (`phase=sampled_real_hybrid`, `logical_lanes=1000`, `sampled_real_workers=50`, `active_concurrency=10`, `seed=23`) | `50` canned `mock_github` worker proofs + `1,000`-lane (`2,000`-task) control-plane simulation | `mock_github` + `simulated` | `4` attempts (`085958Z`, `090235Z`, `092032Z`, `092213Z`) | **`0 / 4` passed** (**FAILED**) | All `4` attempts failed `control_plane_loss_budget_fail` (`verified_landings=1061/2000`, `dlq_volume=881`, `dead_letter_ratio=0.4405 > 0.14` limit). First `3` attempts (`085958Z`, `090235Z`, `092032Z`) also failed `sampled_real_worker_batch_fail` (`sampled_real_dlq_detected:50`) |

---

## 2. Detailed Evidence Breakdown & Attributable Artifacts

### 2.1 Single-Worker Live Cloud-Agent Smoke (`scripts/run_frontier_smoke.py`)
- **What ran:** 1 worker executing the `jsonpeek` qualification slice with live `gemini-2.5-flash` inference and local `pytest`/`ruff` verification against `mock_github`.
- **Attempt population (`1/4` passed on `2026-10-22`):**
  1. [`reports/frontier_smoke/20261022T085926Z/frontier_smoke.json`](frontier_smoke/20261022T085926Z/frontier_smoke.json): **FAILED** (`status="failed"`, `terminal_state="settled_nack"`, `terminal_reason="ERR_CHECK_FAIL"`, `verification_passed=false`).
  2. [`reports/frontier_smoke/20261022T090226Z/frontier_smoke.json`](frontier_smoke/20261022T090226Z/frontier_smoke.json): **FAILED** (`status="failed"`, `terminal_state="settled_ack"`, `verification_passed=false`).
  3. [`reports/frontier_smoke/20261022T092017Z/frontier_smoke.json`](frontier_smoke/20261022T092017Z/frontier_smoke.json): **FAILED** (`status="failed"`, `terminal_state="settled_ack"`, `verification_passed=false`).
  4. [`reports/frontier_smoke/20261022T092200Z/frontier_smoke.json`](frontier_smoke/20261022T092200Z/frontier_smoke.json): **PASSED** (`status="passed"`, `terminal_state="settled_ack"`, `verification_passed=true`, `stop_convergence_seconds=0.035487`).

### 2.2 50-Worker Isolated Canned Load (`scripts/run_scale_worker_load.py`)
- **What ran:** 50 isolated worker proof subprocesses (`logical_workers=50`, `active_concurrency=10`) replaying `proof_fixtures/jsonpeek_canned_completion.json` against `mock_github` (`phase="worker_load_canned"`). Although the historical manifest metadata included `model_mix: {"gemini-2.5-flash_smoke_qualified": 0.02, "canned_fixture": 0.98}`, all 50 worker subprocesses in `run_isolated_worker_load` executed with `--canned-completion-file proof_fixtures/jsonpeek_canned_completion.json` (0 live LLM calls during the 50-worker batch).
- **Attempt population (`1/4` passed on `2026-10-22`):**
  1. [`reports/scale_worker_load/20261022T085958Z/scale_report.json`](scale_worker_load/20261022T085958Z/scale_report.json): **FAILED** (`verified_landings=0/50`, `dlq_volume=50`, `batch_wall_clock_seconds=24.622396`).
  2. [`reports/scale_worker_load/20261022T090235Z/scale_report.json`](scale_worker_load/20261022T090235Z/scale_report.json): **FAILED** (`verified_landings=0/50`, `dlq_volume=50`, `batch_wall_clock_seconds=34.292118`).
  3. [`reports/scale_worker_load/20261022T092032Z/scale_report.json`](scale_worker_load/20261022T092032Z/scale_report.json): **FAILED** (`verified_landings=0/50`, `dlq_volume=50`, `batch_wall_clock_seconds=30.265162`).
  4. [`reports/scale_worker_load/20261022T092213Z/scale_report.json`](scale_worker_load/20261022T092213Z/scale_report.json): **PASSED** (`verified_landings=50/50`, `dlq_volume=0`, `batch_wall_clock_seconds=35.241819`, `throughput_tasks_per_minute=85.126139`, `convergence_time_seconds=8.041296`, `sink_settlement_latency_p95_s=7.927865`).
- **Unmeasured metrics in `worker_load_canned`:**
  - `blocked_dependency_ratio`, `governor_interventions`, `cost_per_success_usd`, `queue_depth_p95`, `retry_cascade_count`, `duplicate_terminal_settlements`, `zombie_blocked_tasks`, and `unstable_governor_oscillation_count` are **unmeasured** (`None` in current `ScaleMetricsSummary`, previously recorded as constant `0`/`0.0`).

### 2.3 1,000-Lane Sampled-Real Hybrid Run (`scripts/run_scale_sampled_real_hybrid.py`)
- **What ran:** `1,000` logical lanes (`2,000` simulated tasks, `seed=23`) combined with `50` canned `mock_github` worker proofs (`sampled_real_workers=50`, `sampled_real_share=0.05`, `active_concurrency=10`).
- **Attempt population (`0/4` passed on `2026-10-22`):**
  1. [`reports/scale_sampled_real_hybrid/20261022T085958Z/scale_report.json`](scale_sampled_real_hybrid/20261022T085958Z/scale_report.json): **FAILED** (`reasons=["control_plane_loss_budget_fail", "sampled_real_worker_batch_fail", "sampled_real_dlq_detected:50"]`).
  2. [`reports/scale_sampled_real_hybrid/20261022T090235Z/scale_report.json`](scale_sampled_real_hybrid/20261022T090235Z/scale_report.json): **FAILED** (`reasons=["control_plane_loss_budget_fail", "sampled_real_worker_batch_fail", "sampled_real_dlq_detected:50"]`).
  3. [`reports/scale_sampled_real_hybrid/20261022T092032Z/scale_report.json`](scale_sampled_real_hybrid/20261022T092032Z/scale_report.json): **FAILED** (`reasons=["control_plane_loss_budget_fail", "sampled_real_worker_batch_fail", "sampled_real_dlq_detected:50"]`).
  4. [`reports/scale_sampled_real_hybrid/20261022T092213Z/scale_report.json`](scale_sampled_real_hybrid/20261022T092213Z/scale_report.json): **FAILED** (`reasons=["control_plane_loss_budget_fail"]`; `sampled_real.passed=true` with `50/50` verified landings in `35.241819s`, but `control_plane.loss_budget_pass=false` with `verified_landings=1061`, `total_terminal_tasks=2000`, `dlq_volume=881`, `dead_letter_ratio=0.4405 > 0.14`).

### 2.4 50-Lane Control-Plane Simulation (`scripts/run_scale_control_plane.py`)
- **What ran:** Pure control-plane simulation (`phase="control_plane_hybrid"`, `logical_lanes=50`, `total_terminal_tasks=100`, `seed=7`, `sink_mode="simulated"`).
- **Artifact:** [`reports/scale_control_plane/20261022T083638Z/scale_report.json`](scale_control_plane/20261022T083638Z/scale_report.json) (`verified_landings=42`, `total_terminal_tasks=100`, `dlq_volume=58`, `governor_interventions=5`, `batch_wall_clock_seconds=0.014207`).
- **Unmeasured metrics in `control_plane_hybrid`:** `false_success_rate` and `sink_truth_divergence_count` are **unmeasured** in simulation (`None` in `ScaleMetricsSummary`, previously hardcoded to `0.0` and `0`).
