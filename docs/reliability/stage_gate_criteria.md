# MX Swarm Stage Gate Criteria

This document turns the milestone roadmap into explicit promotion gates.
A stage is not complete because time elapsed. A stage is complete only when the gate passes.

## Gate 0: Artifact Integrity

Purpose:
- prove a single swarm-generated artifact is real

Pass only if all are true:
- generated branch exists
- submission record exists
- terminal settlement event exists
- independent verification passes in a separate worktree
- no manual edits were made to the generated implementation file
- strict-target branch diff contains only the intended mutable files

Current proven artifacts:
- `golden_path_001`: `task/4b6070bf-implement-textstats-cli-using-`
- `golden_path_002`: `task/4ab625a3-implement-jsonpeek-cli-using-o`

## Gate 1: Repeatability

Purpose:
- prove the harness can reproduce narrow wins reliably

Load:
- `1` worker
- local mode
- constrained PRD path

Run requirement:
- `5` consecutive golden-path runs

Pass only if all are true across all 5 runs:
- false-success rate = `0`
- every run emits a submission event and a terminal settlement event
- every run produces a branch + submission id
- every run passes independent verification
- no branch contains read-only scaffold contamination
- no zombie workers after run completion

Blockers:
- any missing settlement event
- any manual code rescue of generated implementation
- any staged context file leakage

## Gate 2: Local Multi-Agent

Purpose:
- prove queueing, settlement, and worker lifecycle correctness under parallel narrow tasks

Load:
- `5-10` workers
- `3-5` independent golden-path slices
- slices must remain self-contained until explicit dependency execution is implemented
- at least one pair of slices must target files in the same directory
- local/open-weight model pool

Duration:
- `3` consecutive runs with all slices scheduled

Pass only if all are true:
- false-success rate = `0`
- duplicate settlement events = `0`
- duplicate claim events = `0`
- orphaned workers after stop = `0`
- stop convergence <= `30s` after final settlement
- queue depth drains to `0`
- per-slice branch + submission + settlement provenance exists
- every slice passes independent verification
- no DLQ event is caused by lifecycle bugs (`ERR_QUEUE_LEASE`, zombie pause loops, duplicate settlement)
- dashboard event stream reflects actual terminal states, not synthetic progress

## Gate 3: Local Escalation

Purpose:
- prove local/open-weight and frontier escalation interact correctly

Load:
- `5-10` workers
- mixed easy and intentionally hard tasks

Pass only if all are true:
- tasks failing `3` consecutive times with the same reason route out of the local lane
- `ERR_NO_DIFF` and `ERR_SYNTAX` escalate by routing policy, not blind retry count
- repeated `ERR_CHECK_FAIL` routes to replan when the failure signature indicates bad task decomposition
- frontier success still produces the same branch/submission/settlement proof set
- no infinite retry loops
- Gemini is admitted only through the `frontier` lane; no first-attempt Gemini claims
- frontier attempts are blocked when budget, hourly escalation, or frontier concurrency caps are exceeded
- budget-blocked frontier tasks fail closed with traceable reason codes and budget events

## Model Qualification Precondition

No model enters the bulk local worker pool without passing a narrow-task qualification corpus.

Minimum bar:
- file-block format compliance is measured on a fixed corpus
- deterministic acceptance rate is measured separately from format compliance
- qualification is repeated `3` times before promotion into the worker pool

The exact thresholds are versioned in the model qualification corpus, not guessed ad hoc during a run.

Implementation path:
- corpus manifest: `qualification/corpora/gate2_local_open_weight.json`
- qualification runner: `scripts/run_model_qualification.py`

## Gate 4: GCP Staging A/B

Purpose:
- prove production queueing/autoscaling on real infra before burst qualification

Infra:
- GCP Pub/Sub
- GKE
- KEDA/HPA

Gate A:
- `100` workers for `60` minutes

Pass only if all are true:
- false-success = `0`
- settlement correctness >= `99.99%`
- DLQ rate < `2%`
- p95 claim -> start <= `2s`
- orphaned workers at stop = `0`
- Gemini frontier concurrency remains <= `2`
- frontier spend remains <= configured `frontier_daily_usd_cap`
- local/open-weight lane still handles the majority of claims
- cost per successful settlement remains <= `cost_per_success_ceiling_usd`

Gate B:
- `300` workers for `120` minutes

Pass only if all are true:
- all Gate A criteria pass
- lease corruption events = `0`
- p99 settlement latency <= `30s`
- duplicate settlement events = `0`
- Gemini frontier concurrency remains <= `5`
- frontier spend ratio stays bounded and does not become the default execution lane
- hourly frontier escalations remain <= configured cap

Preparation path:
- staging harness: `scripts/run_gcp_gate_ab.py`

## Gate 5: Burst Qualification C/D

Purpose:
- prove burst behavior and recovery up to the MX release target

Gate C:
- `600` workers for `120` minutes

Pass only if all are true:
- all Gate B criteria pass
- backlog slope stable over `30m` windows
- budget burn stays within `+/-10%` of projected linear envelope
- redelivery storm threshold not exceeded

Gate D:
- `1000` workers for `180` minutes

Pass only if all are true:
- all Gate C criteria pass
- full SLO compliance
- cost per task stays within the defined ceiling
- stop convergence <= `60s`
- reconciliation backlog p95 clearance <= `15m`

## Gate 6: 96h Qualification

Phase A:
- `72h` sustained at `100-200` workers

Pass only if all are true:
- no slow memory leak
- no trace cardinality runaway
- no queue drift / lease expiry storms
- all core SLOs hold

Phase B:
- `24h` with at least two burst cycles to `600+`

Pass only if all are true:
- no residual state corruption after drain
- no reconciliation backlog runaway
- dashboards stay event-truthful during burst and recovery

## Promotion Rule

Failing any stage gate blocks progression to the next stage.
There is no calendar override and no manual waiver for reliability gates.

## Rollback Rule

If a stage fails after code rollout:
- freeze promotion
- preserve runtime artifacts and traces
- revert only the minimal feature/config required
- rerun the same stage until it passes cleanly
