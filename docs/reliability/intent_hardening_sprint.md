# MX Swarm Intent Hardening Sprint

This is not a feature roadmap.
It is a reliability sprint to make MX Swarm run toward intent with minimum waste and maximum proof.

Success means:
- the swarm receives intent once
- decomposes it correctly
- executes narrow work without drift
- settles honestly
- lands safely into the canonical sink
- produces evidence for every claim

If any link fails, the sprint is incomplete.

## Sprint Objective

Turn MX Swarm from:
- a proven narrow artifact generator

into:
- a disciplined intent compiler plus execution control plane

without pretending that scale and correctness are the same thing.

## Hardening Priorities

Priority order:

1. intent fidelity
2. dependency correctness
3. worker execution correctness
4. honest settlement
5. post-landing proof
6. escalation and replan correctness
7. cloud scaling and cost control

This order is strict.
No cloud push or dashboard work is allowed to outrank intent fidelity or settlement correctness.

## Sprint Baseline

Current proof base:
- Gate 1 repeatability is green
- Gate 2 local parallel is green
- model qualification exists
- frontier routing exists
- budget controls exist
- CLI proof command exists

Current hard blockers before serious scale:
- no enforced dependency graph execution
- no real rig coordinator layer
- shard key is still too coarse for final `1000+` shape
- real Aether-backed end-to-end proof is not yet established

## Sprint Structure

The sprint is split into six hardening batches.
Each batch has a stop condition and an exit gate.

## Batch 0: Freeze the Known-Good Corpus

Goal:
- stabilize the narrow proof baseline so later failures are real regressions, not moving targets

Tasks:
- freeze the qualified slice corpus
- freeze exact expected CLI commands for qualification, proof, and Gate 2
- keep a known-good manifest for local/open-weight admission
- keep one reproducible frontier smoke slice

Exit gate:
- `mx qualify` is green on the admitted local model
- `mx proof` produces either a successful proof or an honest failure report
- `mx gate2` still passes on the frozen corpus

## Batch 1: Intent Compiler Hardening

Goal:
- make planner output preserve human intent with less ambiguity

Tasks:
- compile PRD into requirement graph, not flat task list
- attach dependency edges explicitly
- attach file-scope intent, not just broad goals
- attach rationale for every task split
- attach allowed mutable files and read-only context set
- attach decomposition quality checks before task publication

Required rules:
- workers do not read raw PRD by default
- every task must point to a spec/intention anchor
- every non-self-contained task must declare prerequisites

Exit gate:
- planner emits dependency-aware task contracts
- no dependent task is published as runnable without declared prerequisites
- mixed-failure replans can point back to a bad decomposition decision

## Batch 2: Dependency-Aware Execution

Goal:
- stop parallel work from racing ahead of unsatisfied prerequisites

Tasks:
- add blocked/unblocked execution state for dependent tasks
- enforce prerequisite settlement before claimability
- add freshness check so stale prerequisite outputs cannot satisfy a dependent task forever
- add adjacency/collision checks for slices touching the same directory or module cluster

Required rules:
- queue claimability must be dependency-aware
- settlement of child tasks must not imply requirement completion if dependencies remain open

Exit gate:
- run `2-3` multi-task requirements with correct ordering
- zero premature settlements
- zero hidden dependency violations

## Batch 3: Worker Lane Hardening

Goal:
- make worker failure cheap, explicit, and useful

Tasks:
- keep fast-fail gates strict
- keep retry context injection mandatory
- add deterministic context pre-hydration for narrow tasks
- add import-graph and adjacent-file read-only context where needed
- tighten changed-file accounting and path-safety
- keep single-flight and lease protections enabled

Required rules:
- worker retries must carry previous failure reason and detail
- no retry is allowed to be blind
- no ACK without real artifact + checks + submission evidence

Exit gate:
- failure taxonomy stays informative
- retry loops show decreasing ambiguity, not repeated blind attempts
- no lifecycle bug reasons in normal slice execution

## Batch 4: Governance, Escalation, and Replan Hardening

Goal:
- route failures to the correct control action instead of throwing everything at a better model

Tasks:
- strengthen reason-aware routing
- make replan a first-class output, not just a side effect
- add failure-signature clustering for repeated `ERR_CHECK_FAIL`
- add shard health views for backlog, nack pattern, and settlement latency
- define when human review is mandatory versus optional

Required rules:
- capability failures escalate
- decomposition failures replan
- runtime failures go to operator/infrastructure review
- mixed failures do not trigger blind frontier escalation

Exit gate:
- local hard tasks route predictably to `frontier`, `replan`, or `human_review`
- no infinite retry loops
- frontier remains a minority lane, not a dumping ground

## Batch 5: Delivery Chain and Aether Hardening

Goal:
- make the final product claim real:
  - intent
  - artifact
  - honest settlement
  - safe landing
  - post-landing validity

Tasks:
- keep Aether adapter narrow and explicit
- require canonical acceptance in Aether mode
- require post-landing verification in Aether mode
- reconcile timeouts without creating false success
- emit proof reports for successful and failed end-to-end runs

Required rules:
- accepted without landed integrity is failure
- canonical timeout is not success
- post-landing mismatch is a hard fail

Exit gate:
- one real Aether-backed proof run completes, or
- one real Aether-backed proof run fails honestly at one exact link with explicit evidence

## Batch 6: Cloud Hardening

Goal:
- prove MX Swarm can scale without losing control or burning the budget

Tasks:
- move narrow proof corpus to GCP Gate A/B harness
- keep Gemini frontier-only
- enforce frontier concurrency and budget freeze
- keep local/open-weight as bulk lane
- measure shard backlog, settlement latency, duplicate claims, and frontier burn

Required rules:
- cloud scale is not allowed to widen task freedom automatically
- `100`, `300`, `600`, and `1000` remain stage gates, not ambitions

Exit gate:
- Gate A passes
- Gate B passes
- frontier lane remains bounded and cheap
- local lane remains the bulk executor

## Daily Execution Rhythm

Every hardening day should start with the same CLI checks:

1. qualification
- `mx qualify`

2. single proof
- `mx proof`

3. local parallel check
- `mx gate2`

4. frontier lane check
- `mx frontier-smoke`

If those are red, do not widen scope.

## What We Explicitly Do Not Do In This Sprint

- no broad PRD autonomy claims
- no `1000-agent` marketing language
- no dashboard-first detours
- no new agent features that do not improve the delivery chain
- no defaulting bulk execution to Gemini

## Sprint Success Criteria

This sprint is successful only if all are true:
- planner preserves intent better than today
- dependent work is ordered explicitly
- worker retries are informative
- frontier routing is reason-aware
- proof reports are reliable
- Aether landing semantics are explicit
- the swarm becomes more predictable per unit of cost

If scale increases but predictability does not, the sprint failed.
