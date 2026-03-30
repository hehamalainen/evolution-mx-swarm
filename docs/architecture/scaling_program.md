# MX Swarm Scaling Program

Date: `2026-03-25`

This document defines the next execution phase after the local proof ladder.
It is not a general vision note.
It is the explicit scaling program for MX Swarm.

## 1. Starting Point

The baseline assumptions for this program are:

- `MX -> Aether` adapter proof is green
- canned worker proof is green
- local model-backed `jsonpeek` proof is green
- local narrow repeatability is proven

That means the next question is no longer:

- can MX land a real artifact at all

The next question is:

- how does MX behave as logical lane count, concurrency, dependency depth, and model mix increase

## 2. Core Principle

`1000` agents does not mean `1000` simultaneous model calls.

For MX, a `1000`-agent test means:

- `1000` logical lanes or logical worker slots
- real routing
- real queueing
- real dependency blocking
- real capability updates
- passive governor behavior
- a bounded amount of actual inference

The goal is to test MX system physics, not laptop throughput.

## 3. Metrics That Matter

Every scale run must report:

- verified landings
- false-success rate
- blocked dependency ratio
- convergence time
- governor interventions
- cost per success
- latency by lane
- queue depth distribution
- hold reason distribution
- retry cascade count
- DLQ volume
- sink settlement latency

Runs that do not emit these are not scale experiments.

## 4. Program Phases

### Phase A: Baseline Revalidation

Purpose:

- confirm the frozen branch is trustworthy

Required gates:

- `mx aether-adapter-proof`
- `mx aether-worker-proof`
- `mx aether-live-proof`
- dashboard smoke

Exit condition:

- all gates green on the committed baseline

### Phase B: 1000-Lane Hybrid Control-Plane Run

Purpose:

- stress MX routing, queueing, dependency flow, capability learning, and governor logic

Shape:

- `1000` logical lanes
- `70-85%` deterministic canned lanes
- `10-25%` noise-injected simulated lanes
- `2-5%` real sampled lanes

Required characteristics:

- real routing
- real shard selection
- real dependency blocking
- real hold reasons
- real capability updates
- passive governor recommendations enabled

Noise classes:

- transient infra failure
- deterministic check failure
- dependency wait
- escalation-triggering repeated failure
- reconciliation delay

Exit condition:

- no false success
- no queue corruption
- no zombie blocked tasks
- no unstable governor oscillation
- clean convergence

### Phase C: Canned Worker Load Run

Purpose:

- test the real worker substrate under load without model noise

Target envelope:

- `100-250` logical workers
- lower active concurrency caps at first
- real worker plumbing
- real verification
- real sink path

Required outcomes:

- stable branch/workspace handling
- no lease corruption
- no duplicate settlement
- bounded queue growth
- clean shutdown and convergence

### Phase D: Mixed-Fidelity Scale Run

Purpose:

- approximate real operating conditions without wasting frontier budget

Shape:

- most lanes synthetic/canned
- minority cheap real-model lanes
- tiny frontier slice

This is the first run that should be treated as a realistic scale experiment.

### Phase E: Targeted Cloud Model Validation

Purpose:

- answer a specific question only

Allowed questions:

- speed improvement
- reliability margin
- broader capability slice

Disallowed use:

- generic debugging
- proving baseline correctness again

Gemini belongs here, not earlier.

## 5. Acceptance Gates

### Control-Plane Gate

Fail immediately if:

- `false_success > 0`
- queue corruption is observed
- blocked dependents become zombies
- hold reasons collapse into an incoherent bucket
- governor oscillates repeatedly without convergence

### Worker Load Gate

Fail immediately if:

- duplicate claim or settlement appears
- workspaces leak or collide
- post-landing verification diverges from sink truth
- shutdown leaves stranded active work

### Mixed-Fidelity Gate

Fail immediately if:

- cheap lanes contaminate frontier policy
- synthetic lanes hide real failures
- sink visibility is no longer trustworthy

## 6. Required Artifacts

Each run must emit:

- run manifest
- topology and lane mix
- queue / hold / retry summary
- capability registry delta
- governor recommendation summary
- sink settlement summary
- pass/fail verdict

Suggested report roots:

- `reports/scale_control_plane/`
- `reports/scale_worker_load/`
- `reports/scale_mixed_fidelity/`

## 7. Immediate Implementation Order

1. finish baseline gate rerun on the committed branch
2. implement the `1000`-lane hybrid control-plane harness
3. define the emitted report schema
4. run first hybrid control-plane experiment
5. implement the canned worker load harness
6. run first worker load experiment
7. only then design mixed-fidelity lane composition

## 8. Blunt Position

The next phase is not more proof-of-existence work.

It is:

- scaling law measurement
- failure-mode containment
- economics and latency characterization

That is the correct frontier for MX now.
