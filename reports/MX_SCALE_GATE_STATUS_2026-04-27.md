# MX Scale Gate Status Summary

- **Baseline Date:** `2026-04-27` (with `2026-10-22` requalification update)
- **Overall Verdict:** **RESEARCH PROTOTYPE ONLY — NOT QUALIFIED FOR UNBOUNDED / LIVE 1,000-LANE PRODUCTION**

---

## 1. April 27, 2026 Controlled Canned/Simulated Ladder (Retained Artifacts)

All worker-load runs below used `phase="worker_load_canned"` (replaying `proof_fixtures/jsonpeek_canned_completion.json`, **not live LLM inference**), and all control-plane lanes used `phase="control_plane_hybrid"` (`sink_mode="simulated"`):

| Gate Run | Artifact | Mode | Result | Key Metrics |
| --- | --- | --- | --- | --- |
| **25-Worker Canned Aether Ladder** | [`reports/scale_worker_load/20260427T044307Z/scale_report.json`](scale_worker_load/20260427T044307Z/scale_report.json) | `worker_load_canned` (`sink_mode="aether"`, `active_concurrency=4`) | `passed=true` | `verified_landings=25/25`, `dlq_volume=0`, `sink_settlement_latency_p95_s=5.3408173` |
| **50-Worker Canned Aether Ladder** | [`reports/scale_worker_load/20260427T044823Z/scale_report.json`](scale_worker_load/20260427T044823Z/scale_report.json) | `worker_load_canned` (`sink_mode="aether"`, `active_concurrency=4`) | `passed=true` | `verified_landings=50/50`, `dlq_volume=0`, `sink_settlement_latency_p95_s=7.0285884` |
| **100-Worker Canned Aether Ladder** | [`reports/scale_worker_load/20260427T050008Z/scale_report.json`](scale_worker_load/20260427T050008Z/scale_report.json) | `worker_load_canned` (`sink_mode="aether"`, `active_concurrency=4`) | `passed=true` | `verified_landings=100/100`, `dlq_volume=0`, `sink_settlement_latency_p95_s=7.24934135` |
| **1,000-Lane / 50-Worker Sampled-Real Hybrid** | [`reports/scale_sampled_real_hybrid/20260427T050424Z/scale_report.json`](scale_sampled_real_hybrid/20260427T050424Z/scale_report.json) | `sampled_real_hybrid` (`50` canned Aether workers + `1,000` simulated lanes) | `overall_passed=true` | `sampled_real_workers=50`, `dlq_volume=0`, `sink_settlement_latency_p95_s=5.8242839` |
| **2,000-Lane / 100-Worker Sampled-Real Hybrid** | [`reports/scale_sampled_real_hybrid/20260427T051559Z/scale_report.json`](scale_sampled_real_hybrid/20260427T051559Z/scale_report.json) | `sampled_real_hybrid` (`100` canned Aether workers + `2,000` simulated lanes) | **`overall_passed=false`** | Failed `sampled_real_settlement_latency_exceeded:10.410133>10.000000` (`sink_settlement_latency_p95_s=10.410133`) |

---

## 2. October 22, 2026 Requalification Attempt Population (Retained Artifacts)

See [`reports/MX_CLOUD_AGENT_50_WORKER_DEMO_BRIEF_2026-10-22.md`](MX_CLOUD_AGENT_50_WORKER_DEMO_BRIEF_2026-10-22.md) for full details:
- **1-Worker Live `gemini-2.5-flash` Smoke (`scripts/run_frontier_smoke.py`):** `1 / 4` passed (`20261022T092200Z` passed; `085926Z`, `090226Z`, `092017Z` failed).
- **50-Worker Canned `mock_github` Load (`scripts/run_scale_worker_load.py`):** `1 / 4` passed (`20261022T092213Z` passed `50/50` in `35.241819s`, `85.126139` tasks/min, p95 `7.927865s`; `085958Z`, `090235Z`, `092032Z` failed `0/50`).
- **1,000-Lane / 50-Worker Sampled-Real Hybrid (`scripts/run_scale_sampled_real_hybrid.py`):** `0 / 4` passed (`085958Z`, `090235Z`, `092032Z`, `092213Z` all failed `control_plane_loss_budget_fail` with `dead_letter_ratio=0.4405 > 0.14`).
- **Unmeasured Metric Labeling:**
  - In `control_plane_hybrid` (`simulation/scale_harness.py`), `false_success_rate` and `sink_truth_divergence_count` are `unmeasured` (`None`).
  - In `worker_load_canned` (`simulation/scale_harness.py`), `blocked_dependency_ratio`, `governor_interventions`, `cost_per_success_usd`, `queue_depth_p95`, `retry_cascade_count`, `duplicate_terminal_settlements`, `zombie_blocked_tasks`, and `unstable_governor_oscillation_count` are `unmeasured` (`None`).
