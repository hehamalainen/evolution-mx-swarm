# MX Swarm SLO/SLI Spec v3

Date: 2026-03-05

## Global SLOs
1. False-success rate: 0 (absolute).
2. Settlement correctness: >= 99.99%.
3. Duplicate settlement events per task attempt: 0.
4. STOP convergence (burst profile): <= 60s.
5. Aether reconciliation backlog p95 clearance: <= 15m.

## Scope Clarification
- False-success = 0 applies to "declared success without verifiable artifact+verification+Aether canonical acceptance".
- Settlement correctness >= 99.99% covers all settlement paths, including NACK/DLQ routing and outage/degraded flows.

## Core SLIs
1. `sli_false_success_rate`
2. `sli_settlement_correctness`
3. `sli_duplicate_settlement_count`
4. `sli_claim_to_start_latency_ms_p95`
5. `sli_settlement_latency_ms_p99`
6. `sli_dlq_rate`
7. `sli_lease_corruption_events`
8. `sli_redelivery_storm_events`
9. `sli_budget_burn_variance`
10. `sli_aether_reconciliation_clearance_ms_p95`

## Cost and Budget Controls
- `budget.enforcement=on` enforces caps.
- 85% cap: throttle new claims.
- 100% cap: freeze new claims.
- Settlement-exempt states (`SUBMITTED_PENDING_LEDGER+`) bypass claim freeze.
- `sli_cost_per_task` must remain <= ceiling defined per environment.

## Provider Backpressure SLO
- Provider rate-limit storms must not violate settlement correctness.
- Token-bucket backpressure must bound queue surge and avoid runaway retries.

## Alert Thresholds
- Any false-success event: page immediately.
- Settlement correctness < 99.99% (5m rolling): page.
- Duplicate settlement > 0 over 5m: page.
- Aether recon backlog p95 > 15m for 10m: page.
- DLQ rate >= 2% over 30m: warn/page based on environment.
