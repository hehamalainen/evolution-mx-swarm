# MX Swarm Load Gate Contract v3

Date: 2026-03-05

Progression policy: failing any gate blocks progression to the next stage.

## Gate A
- Profile: 100 workers, 60 minutes.
- Pass criteria:
  1. false-success = 0
  2. settlement correctness >= 99.99%
  3. DLQ rate < 2%
  4. p95 claim->start <= 2s
  5. orphaned workers at STOP = 0

## Gate B
- Profile: 300 workers, 120 minutes.
- Pass criteria:
  1. all Gate A criteria
  2. lease corruption events = 0
  3. p99 settlement latency <= 30s
  4. duplicate settlement events = 0

## Gate C
- Profile: 600 workers, 120 minutes.
- Pass criteria:
  1. all Gate B criteria
  2. backlog slope stable (no runaway growth in 30m windows)
  3. budget burn within +/-10% of projected linear envelope
  4. redelivery storm threshold not exceeded

## Gate D
- Profile: 1000 workers, 180 minutes.
- Pass criteria:
  1. all Gate C criteria
  2. full SLO compliance
  3. cost-per-task <= defined ceiling
  4. STOP convergence <= 60s
  5. Aether reconciliation backlog p95 clearance <= 15m

## Qualification Window (96h)
### Phase A (72h sustained)
- Load: 100-200 workers sustained.
- Objective: detect slow leaks (memory, traces, quotas, queue drift).
- Requirement: all core SLOs hold.

### Phase B (24h burst-cycling)
- Load: at least two cycles to 600+ (burst -> drain -> burst -> drain).
- Objective: validate burst transition integrity and recovery.
- Requirement: no residual state corruption after each cycle.

## Required Gate Artifact
Each gate run must persist:
- timestamp
- gate id
- metrics snapshot
- pass/fail verdict
- failure reasons (if any)
