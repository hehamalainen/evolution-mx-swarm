# Aether Sink Contract

Aether remains a separate system. MX integrates through a narrow adapter contract only.

## Submission Contract

MX submits:
- `task_id`
- `agent_id`
- `change_type`
- `change_content`
- `affected_paths`
- `risk_tier`
- `confidence_score`
- `model_id`
- `rationale`
- `idempotency_key`

MX expects:
- `submission_id`
- initial `status`
- optional URL/reference

## Canonical Acceptance Contract

MX may trust Aether for canonical settlement only when `get_status(submission_id)` returns:
- an accepted status such as `accepted`, `committed`, `completed`, or `promoted`

Rejected, unknown, or timeout states are not success.

## Post-Landing Verification Contract

Canonical acceptance is not enough. For terminal success in Aether mode, MX must also verify:
- Aether returned landed path evidence
- landed paths include all expected affected paths
- if diff echo is available, it matches the submitted change payload

If Aether returns accepted status without landed evidence:
- MX must fail closed
- reason code: `ERR_POST_LANDING_VERIFY`

## Reconciliation Contract

When canonical acceptance times out:
- task enters `SUBMITTED_PENDING_LEDGER`
- reconciliation polls Aether status
- reconciliation may settle `SETTLED_ACK` only after post-landing verification passes

## Separation Rule

MX must not depend on Aether internals.

Allowed integration surface:
- adapter methods
- config
- idempotent submission ids
- status polling
- landed verification response fields

Disallowed integration surface:
- shared runtime state
- direct CRDT internals
- implicit coupling to Aether storage layout
