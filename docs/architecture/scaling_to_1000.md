# MX Swarm Scaling to 1000+

This document explains how MX Swarm scales from proven narrow artifacts to burst execution at `1000` workers.
It is not a product pitch. It is an execution contract.

## Starting Point

Current proven artifacts:

- `golden_path_001`: `task/4b6070bf-implement-textstats-cli-using-`
- `golden_path_002`: `task/4ab625a3-implement-jsonpeek-cli-using-o`

What these prove:

- the swarm can generate a real branch
- the branch can settle via the local VCS lane
- fixed acceptance checks can pass
- the artifact can be verified independently in a separate worktree

What they do not prove:

- broad PRD autonomy
- multi-file synthesis at scale
- real cloud queue behavior
- cross-shard dependency handling

## Core Principle

`1000` agents is not `1000` free-roaming coders on one queue.

It is:

- one narrow intake/compiler layer
- many sharded execution lanes
- pooled model serving
- append-only settlement evidence
- explicit escalation and replan paths

The control plane stays small.
The execution plane scales.

## What Scales and What Must Stay Small

Things that scale horizontally:

- queue shards
- worker pools
- model-serving pools
- trace volume
- event volume
- settlement throughput

Things that must remain intentionally small:

- planners
- judges
- governors
- topology decisions
- replan authority

If planners and judges scale like workers, the system turns into distributed confusion instead of distributed execution.

## Four-Layer Runtime Architecture

### 1. Intake / Spec Compiler

Responsibility:

- read the PRD once
- extract requirements
- compile narrow task contracts
- assign routing metadata before execution begins

Every emitted task contract must include:

- `task_id`
- `trace_id`
- `attempt_id`
- `rig_id`
- `stream_id`
- `task_class`
- `model_tier`
- `shard_key`
- strict file targets when applicable
- acceptance checks
- dependency edges when a task cannot settle independently

Required rule:

- leaf workers never see the raw PRD unless the task explicitly requires it

### 2. Sharded Execution Plane

Responsibility:

- claim work from matching shards
- execute in isolated ephemeral workspaces
- produce artifacts or fail cheaply

Shard key format:

- `rig_id + task_class + model_tier`

Examples:

- `frontend.feature.local7b`
- `backend.test.local7b`
- `infra.ops.frontier`

Worker rule:

- a worker subscribes only to the shards it is allowed to execute

This prevents the same generic worker from improvising across incompatible domains.

### 3. Review / Settlement Plane

Responsibility:

- verify whether work is real
- submit the artifact
- record canonical settlement evidence

Success requires all of:

- artifact exists
- deterministic checks passed
- submission succeeded
- canonical acceptance exists for the active ledger mode

Local mode:

- GitHub mock lane is acceptable for demos

Production mode:

- Aether remains the canonical ledger

Required evidence for every settled success:

- branch name
- submission id
- terminal settlement event
- affected paths
- acceptance proof
- trace linkage

### 4. Governance / Escalation Plane

Responsibility:

- observe shard health
- detect repeated failure patterns
- route to retry, frontier escalation, or replan

Governance must not be a single retry counter.
It must inspect:

- failure reason
- repetition pattern
- shard backlog
- cost pressure
- reconciliation backlog

## Model Strategy

Critical rule:

- do not run one model instance per worker

Workers are stateless executors.
Inference is a pooled service.

Default pools:

- `local/open-weight` pool for cheap narrow tasks
- `frontier` pool for escalations and ambiguous tasks

Economic rule:

- Gemini is a frontier-only resource by default
- Gemini is never the default worker lane for Gate A/B
- frontier claims are admitted only when frontier eligibility, daily budget, hourly escalation cap, and frontier concurrency cap all pass
- if the Gemini cap is reached, new frontier claims freeze while local/open-weight work continues

Default routing:

- local/open-weight handles:
  - fixed-target edits
  - scaffold completion
  - narrow CLI/library slices
  - test-only or implementation-only tasks
- frontier handles:
  - repeated local failures
  - multi-file refactors
  - ambiguous or dependency-heavy tasks
  - recovery from DLQ

## Escalation Policy

Escalation must be reason-aware.

Escalate a task when it fails `3` consecutive times with the same reason code:

- `ERR_SYNTAX`
- `ERR_NO_DIFF`
- `ERR_RATE_LIMIT`
- `ERR_CHECK_FAIL`

But the action depends on the reason:

- `ERR_SYNTAX` or `ERR_NO_DIFF`: escalate model tier first
- `ERR_CHECK_FAIL`: evaluate task contract quality before model escalation
- `ERR_QUEUE_LEASE`: fix runtime, do not escalate the coding task
- `ERR_RECON_DIVERGENCE`: route to reconciliation or operator attention

If failure reasons vary between attempts, the task likely needs replanning instead of a better model.

Runtime requirement:

- retry routing decisions must inspect failure history, not only the latest failure
- retry context must be injected back into the next attempt so the worker can see what failed previously
- queue or task payloads must preserve enough evidence to distinguish formatting failure from task-contract failure

## Replan Path

Frontier escalation is not the only escape hatch.
MX Swarm also needs a replan loop.

Use replan when:

- the same task fails with mixed reason codes
- acceptance checks appear unsatisfiable
- a dependency is missing from the task contract
- a narrow task target is too small to satisfy the requirement

Replan authority belongs to the governance plane, not to workers.

Replan output can:

- split a task into smaller tasks
- widen file targets
- add missing dependency edges
- move a task to a different shard
- change model tier

Temporary rule for Gate `2`:

- until dependency execution is enforced in the task contract, Gate `2` runs only self-contained slices
- if a task's acceptance depends on another task's output, it is out of scope for Gate `2`
- this keeps local parallel proof focused on queueing and settlement correctness instead of hidden planner dependency bugs

## Model Qualification

Weak models are not admitted into the bulk worker lane by default.

Every candidate model must pass a narrow-task qualification corpus before it is allowed into the local/open-weight pool.

The corpus must measure both:

- output-format compliance
- deterministic acceptance success

This prevents cheap models from creating retry storms that look like worker scale problems but are actually model suitability problems.

## Golden-Path Relaxation Schedule

The swarm must not stay trapped forever in one-file golden paths.
But relaxation must be staged, not accidental.

Stage progression:

1. single mutable implementation file, fixed read-only tests
2. two mutable implementation files, fixed read-only tests
3. implementation + generated tests, still under strict file targets
4. small multi-file tasks with dependency edges
5. bounded ambiguous tasks with frontier fallback

Rule:

- relax one constraint at a time
- hold the same settlement invariants at every stage

Reference:

- `docs/reliability/golden_path_relaxation_schedule.md`

## Cross-Shard Dependency Contract

At `1000+`, the hard problem is not raw concurrency.
It is dependency ordering.

Required rule:

- the intake/compiler must emit dependency edges before execution

Each task may declare:

- `depends_on_task_ids`
- `produces_interface_ref`
- `consumes_interface_ref`

Settlement rule:

- a dependent task cannot settle as final success if an upstream dependency is unresolved or later invalidated

Blocked execution rule:

- a task with unresolved dependencies is queued as blocked, not executable
- it becomes runnable only after all upstream tasks reach `SETTLED_ACK`
- if any upstream task reaches `SETTLED_NACK` or `DEAD_LETTER`, the dependent task cannot auto-settle and must replan or fail

Freshness rule:

- a task cannot settle successfully if one of its dependencies settled after the task workspace was cloned
- in that case the task must re-execute against fresh state

Cross-shard coordination options:

- compile ordering constraints at intake time
- reconcile dependency state during settlement
- re-open downstream tasks if upstream canonical state changes

Without this, shard federation will lie about progress.

## Queueing and Concurrency Physics

Production baseline:

- GCP Pub/Sub
- GKE
- KEDA/HPA

Required message metadata:

- `task_id`
- `trace_id`
- `attempt_id`
- `rig_id`
- `stream_id`
- `model_tier`
- `shard_key`
- `lease_expires_at`
- `receive_count`

Required behaviors:

- heartbeat visibility extension stays enabled
- ACK is proof of settlement, not effort
- every shard has a DLQ
- redelivery storms are measured per shard

## Observability Contract

Operators must be able to answer three questions instantly:

- did the swarm actually ship code
- which shard is unhealthy
- what failure mode is dominant

Dashboard truth must come from append-only events, not synthetic counters.

Required metrics before any real `1000` claim:

- queue depth by shard
- inflight workers by shard
- DLQ rate by shard
- settlement latency by shard
- reconciliation backlog
- provider/model saturation
- cost burn by model tier
- submission outcomes by provider
- trace continuity across planner -> worker -> VCS or Aether

Gastown V2 is the primary operator surface for this.

## Stage Gates

Scaling is blocked until the current stage passes.
There is no calendar override.

Current gate model:

- Gate `0`: artifact integrity
- Gate `1`: repeatability
- Gate `2`: local multi-agent
- Gate `3`: local escalation
- Gate `4`: GCP staging A/B
- Gate `5`: burst qualification C/D
- Gate `6`: `96h` qualification

Reference:

- `docs/reliability/stage_gate_criteria.md`

## Budget and Settlement Interaction

Budget kill switches must not destroy already-produced work.

Required rule:

- budget freeze blocks new claims only
- tasks already in settlement states continue through bounded settlement retries

This prevents the budget policy from becoming a silent data-loss mechanism.

## Reconciliation

Production readiness requires active reconciliation.

Reason:

- local state and canonical ledger state can diverge after submission

Required behavior:

- settled items are checked against canonical status
- divergence emits explicit `ERR_RECON_DIVERGENCE`
- reconciliation backlog is visible in dashboards

## Roadmap

### Now

- integrate `golden_path_001`
- prove `golden_path_002`
- surface recent settlements in Gastown V2

### Repeatability

- `5` consecutive constrained golden-path runs
- false-success = `0`

### Local Multi-Agent

- `5-10` workers
- several narrow slices in parallel
- zero zombie workers
- zero false ACKs
- zero duplicate claims
- stop convergence <= `30s`

### GCP Staging

- move queueing and autoscaling to GCP
- run Gate A and Gate B

### Burst Qualification

- run Gate C and Gate D
- complete the `96h` qualification window

## Bottom Line

MX Swarm reaches `1000+` safely only if it keeps the current lesson intact:

- prove tiny wins with hard evidence
- keep planners/judges small
- shard the execution plane
- pool model inference
- make settlement event-sourced
- use escalation and replan intentionally

The moat is not that one weak model can do everything.
The moat is that the harness can turn constrained execution into reliable, scalable delivery.
