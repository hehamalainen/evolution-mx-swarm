# MX Swarm Agent Organization Sprint

This sprint starts after the current intent-hardening batch.

It is not about adding more agents.
It is about making larger agent populations legible, governable, and economically sane.

## Objective

Turn the current stream-based staffing model into a hierarchy that can support:

- a small control plane
- many execution lanes
- explicit shard ownership
- deterministic escalation paths
- intent-preserving work allocation

Success means MX Swarm can answer these questions with code and runtime data, not presentation language:

- who is allowed to work on what
- why that work was assigned to that lane
- what happens when one lane stalls
- when work should escalate, replan, or stop
- how the org shape changes as coupling and risk increase

## Current Baseline

Current implementation already provides:

- PRD analysis and staffing heuristics in `boss/org_builder.py`
- stream-level staffing for `feature`, `qa`, `ops`, `security`, and `docs`
- topology choice locked to `STREAMS`
- risk, coupling, and complexity scores

That is a solid v1.
It is not enough for `1000+` workers because streams alone are too coarse.

## Sprint Priorities

Priority order:

1. org structure correctness
2. allocation heuristics
3. shard ownership
4. escalation ownership
5. economic controls by lane
6. dashboard visibility

## Batch A: Formalize the Runtime Org Model

Goal:

- define the runtime hierarchy explicitly in schema and docs

Target structure:

- `global compiler`
- `rig coordinator`
- `stream`
- `shard`
- `worker lane`

Required outputs:

- schema for rig-level config
- schema for shard ownership and lane limits
- mapping from task contract metadata to org units

Hard rule:

- `1000` workers never report to one flat queue identity

## Batch B: Upgrade Staffing Heuristics

Goal:

- evolve the current `complexity_score`, `coupling_score`, and `risk_score` heuristics into lane allocation logic

Move from:

- percentage of total workers per stream

To:

- worker counts per `rig + stream + model_tier + shard`

Expected heuristic outputs:

- high coupling -> fewer feature shards, more QA density
- high risk -> tighter caps, more security and QA lanes
- heavy integration work -> more ops and integration lanes
- admitted narrow slices -> local/open-weight first
- ambiguous or repeated-failure lanes -> frontier only under explicit cap

## Batch C: Introduce Rig Coordinators

Goal:

- add a middle layer between the global compiler and workers

Rig coordinator responsibilities:

- own shard inventory for one domain or repo slice
- enforce per-rig caps
- monitor local backlog and DLQ
- request replan when local failure signatures indicate decomposition problems

This keeps the global governor small and policy-focused.

## Batch D: Make Escalation Ownership Explicit

Goal:

- every escalation path must have an owner

Ownership model:

- local capability failures -> rig coordinator
- repeated semantic/task-contract failures -> replan lane
- economic freeze or frontier saturation -> governor
- reconciliation or ledger divergence -> settlement / Aether lane
- severe runtime integrity failures -> operator

Required output:

- one escalation matrix that maps reason codes to owner, action, and cap policy

## Batch E: Add Dependency-Aware Org Planning

Goal:

- match organization shape to dependency shape

Rules:

- strongly connected work should not be sprayed across too many shards
- dependency-heavy work should stay under tighter coordinators
- independent narrow slices can scale horizontally

This sprint should make the org builder dependency-aware at organization time, not only at task runtime.

## Batch F: Add Economic Lane Controls

Goal:

- make organization cost-aware by default

Required controls:

- frontier concurrency by rig
- daily spend by rig
- cost-per-success ceiling by rig or stream
- local lane default for qualified narrow tasks
- frontier-only Gemini posture remains intact

Hard rule:

- expensive models are organizational exceptions, not staffing defaults

## Batch G: Add Operator-Visible Org Truth

Goal:

- make org decisions inspectable

Need visible runtime answers for:

- active rigs
- stream headcount by rig
- shard ownership
- blocked dependency lanes
- frontier saturation
- replan pressure
- cost burn by lane

This is not dashboard-first work.
It is event-first work that dashboards can consume later.

## Test Plan

### Heuristic tests

- same PRD yields stable staffing outputs
- higher coupling reduces feature parallelism
- higher risk increases QA and security allocation
- integration-heavy PRDs allocate more ops/integration capacity

### Routing tests

- task metadata resolves deterministically to rig, stream, and shard
- frontier-ineligible work never lands in frontier lanes
- dependency-heavy slices are grouped as designed

### Escalation tests

- reason codes map to correct owner and action
- governor does not absorb local rig problems that should stay local
- replan is used for decomposition failures, not as a generic fallback

### Economic tests

- per-rig frontier caps hold
- freeze mode blocks new expensive claims without killing local execution
- lane-level burn can be derived from append-only events

## Acceptance Criteria

This sprint is successful only if all are true:

- org structure is encoded in runtime contracts, not only docs
- staffing logic produces shard-aware allocations
- escalation ownership is explicit and testable
- dependency shape influences org shape
- lane-level cost policy exists before large-scale cloud claims
- the resulting org model is still small enough for a human to understand

If the org model becomes clever but unverifiable, the sprint failed.
