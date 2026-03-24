# MX Swarm Reason Code Taxonomy v3

Date: 2026-03-05

## NACK Reasons
- `ERR_SYNTAX`: generated code failed deterministic syntax gate.
- `ERR_NO_DIFF`: task produced no effective filesystem diff.
- `ERR_REFUSAL`: model refused or aborted task.
- `ERR_CHECK_FAIL`: acceptance or verification command failed.
- `ERR_RATE_LIMIT`: provider/API rate limiting hit.
- `ERR_QUEUE_LEASE`: queue lease expired/corrupted before settlement.
- `ERR_LEDGER_UNAVAILABLE`: Aether unavailable; canonical acceptance not yet possible.
- `ERR_TASK_FAILED`: non-classified execution failure.
- `ERR_RECON_DIVERGENCE`: reconciliation detected canonical/local divergence.

## Settlement Mapping
- `ACK` allowed only from `VERIFIED` after canonical acceptance (or explicit approved degraded mode).
- `NACK(reason)` required for non-terminal recoverable failure.
- `DEAD_LETTER(reason)` required after retry budget exhaustion or unrecoverable policy failure.

## Telemetry Contract
Every terminal or retry settlement event must include:
- `task_id`
- `attempt_id`
- `trace_id`
- `reason`
- `provider`
- `timestamp`

## Forbidden Patterns
- Naked `nack()` without reason.
- `ack()` when no code diff produced.
- `ack()` on deterministic preflight failure.
- Silent transition to dead-letter without reason.
