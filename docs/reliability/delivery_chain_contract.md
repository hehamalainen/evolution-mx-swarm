# MX Swarm Delivery Chain Contract

MX Swarm is only successful when all five links in the chain hold:

1. `intent_compiled`
- the task contract preserves the user intent for the slice being executed

2. `artifact_verified_locally`
- the worker produced a real artifact
- deterministic checks passed
- independent verification passed

3. `submission_recorded`
- the sink submission exists
- submission id, trace id, and affected paths were recorded

4. `canonical_accepted`
- the canonical sink returned an accepted state
- pending, timeout, or rejected states are not success

5. `post_landing_verified`
- the canonical sink returned enough evidence to verify what landed
- for Aether mode this is mandatory
- canonical acceptance without landed-integrity evidence is failure

## Success Invariant

MX Swarm must not emit terminal success unless:
- local verification passed
- submission was recorded
- canonical acceptance exists when canonical enforcement is enabled
- post-landing verification passed when required by the sink contract

## Failure Invariant

If any link fails:
- queue settlement must be `SETTLED_NACK` or `DEAD_LETTER`
- a machine-readable reason code must be attached
- the proof report must identify the exact failed link

## Current Runtime Mapping

- local/mock GitHub lane:
  - uses local verification plus mock sink settlement for constrained proof runs
- Aether canonical lane:
  - requires canonical acceptance
  - requires post-landing verification
  - otherwise fails closed with `ERR_POST_LANDING_VERIFY`
