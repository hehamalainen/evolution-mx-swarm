# MX Sampled-Real Hybrid Report

- generated_at: `2026-04-27T05:04:24.080783+00:00`
- phase: `sampled_real_hybrid`
- logical_lanes: `1000`
- seed: `29`
- sampled_real_workers: `50`
- sampled_real_share: `0.05`
- active_concurrency: `4`
- sink_mode: `aether`
- overall_passed: `True`
- overall_reasons: `[]`

## Control Plane
- run_id: `scale-control-plane-20260427t050209z`
- structural_pass: `True`
- loss_budget_pass: `True`
- verified_landings: `1681`
- total_terminal_tasks: `2000`
- dead_letter_ratio: `0.1215`
- dead_letter_ratio_limit: `0.13999999999999999`
- dlq_volume: `243`
- dead_letter_reason_distribution: `{'ERR_PREREQUISITE_FAILED': 158, 'ERR_REFUSAL': 13, 'ERR_CHECK_FAIL': 11, 'NO_ELIGIBLE_LANE': 61}`

## Sampled Real
- run_id: `scale-worker-load-20260427t050211z`
- passed: `True`
- verified_landings: `50`
- total_terminal_tasks: `50`
- dlq_volume: `0`
- dead_letter_ratio: `0.0`
- sink_settlement_latency_p95_s: `5.8242839`
- batch_wall_clock_seconds: `132.906395`
- throughput_tasks_per_minute: `22.572277`
- sink_truth_divergence_count: `0`
