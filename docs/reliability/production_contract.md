# MX Swarm Production Contract v3

Date: 2026-03-05
Scope: Release-1 reliability freeze

## Locked Baseline
- Cloud baseline: GCP.
- Canonical completion source: Aether ledger.
- Scale target: burst to 1000 workers.
- Runtime: Kubernetes (GKE + KEDA/HPA).
- Drift rollout: shadow -> deterministic -> semantic.

## Invariants
1. False-success invariant is absolute:
   - A task can be settled as success only if all are true:
     - artifact exists,
     - verification step passed,
     - canonical Aether acceptance signal exists.
2. Queue ACK is a state transition proof, not a proof of effort.
3. No terminal success settlement may be emitted from `QUEUED|CLAIMED|EXECUTING|ARTIFACT_READY`.
4. Worker retries never mutate canonical completion state directly.

## Budget Freeze Interaction Rule
At 100% budget cap:
- Stop new task claims.
- Do not stop in-flight settlement completion.
- Any task in `SUBMITTED_PENDING_LEDGER` or later is settlement-exempt and must finish bounded retry.
- Settlement retries are operational overhead and excluded from claim-budget limiter.

## Gate Progression Rule
- Progression to higher load stage is blocked unless all machine-evaluable criteria in the current gate pass.
- A failed gate requires remediation + re-run of the same gate.

## Outage Degradation Rule
During Aether outage:
- Completed work must not be re-executed solely because ledger acceptance is unavailable.
- Completed work enters `SUBMITTED_PENDING_LEDGER` with bounded retry.
- Reconciliation loop is responsible for eventual canonical convergence.

## Event Contract
All lifecycle transitions must emit append-only events with:
- `event_id, trace_id, task_id, attempt_id, state_from, state_to, reason, provider, timestamp, actor_id`

## Rollback Safety
Any enforcement feature must expose a config-level rollback switch; rollback must preserve event logging and traceability.
