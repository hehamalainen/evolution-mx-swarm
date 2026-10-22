# MX Sampled-Real Hybrid Report

- generated_at: `2026-10-22T09:03:10.799079+00:00`
- phase: `sampled_real_hybrid`
- logical_lanes: `1000`
- seed: `23`
- sampled_real_workers: `50`
- sampled_real_share: `0.05`
- active_concurrency: `10`
- sink_mode: `mock_github`
- overall_passed: `False`
- overall_reasons: `['control_plane_loss_budget_fail', 'sampled_real_worker_batch_fail', 'sampled_real_dlq_detected:50']`

## Control Plane
- run_id: `scale-control-plane-20261022t090310z`
- structural_pass: `True`
- loss_budget_pass: `False`
- verified_landings: `1061`
- total_terminal_tasks: `2000`
- dead_letter_ratio: `0.4405`
- dead_letter_ratio_limit: `0.14`
- dlq_volume: `881`
- dead_letter_reason_distribution: `{'RETRY_EXHAUSTED': 881}`

## Sampled Real
- run_id: `scale-worker-load-20261022t090235z`
- passed: `False`
- verified_landings: `0`
- total_terminal_tasks: `50`
- dlq_volume: `50`
- dead_letter_ratio: `1.0`
- sink_settlement_latency_p95_s: `8.116913`
- batch_wall_clock_seconds: `34.292118`
- throughput_tasks_per_minute: `87.48366`
- sink_truth_divergence_count: `0`
