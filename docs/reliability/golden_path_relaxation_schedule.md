# MX Swarm Golden-Path Relaxation Schedule

This document defines how MX Swarm widens task freedom without losing the proof discipline learned from `golden_path_001` and `golden_path_002`.

## Principle

Do not relax multiple constraints at once.
Each relaxation step changes one variable, measures the new failure profile, and either stabilizes or rolls back.

## Baseline: Locked Golden Path

Current proven shape:
- one mutable implementation file
- fixed read-only tests
- strict file targets
- constrained local planner mode
- deterministic syntax gate
- deterministic `ruff --fix` + `ruff format`
- local-mode submission + terminal settlement proof
- independent verification in separate worktree

Expected failure profile:
- mostly prompt/implementation quality failures
- minimal lifecycle ambiguity

## Relaxation 1: Two Mutable Files

Change:
- allow one implementation file + one helper/module file

Still fixed:
- read-only tests
- strict file targets
- constrained planner mode

Goal:
- prove workers can coordinate tiny multi-file edits without contaminating unrelated files

Expected new risks:
- missing imports
- partial file emission across multiple targets
- staging contamination if changed-file tracking regresses

Promotion requirement:
- `3` consecutive passing runs with no extra files in branch diff

## Relaxation 2: Worker Writes Tests and Implementation

Change:
- allow worker to modify implementation file and matching test file

Still fixed:
- strict file targets
- constrained planner mode
- independent verification

Goal:
- prove the worker can expand or patch the executable specification safely

Expected new risks:
- test laundering
- tautological tests
- passing tests with weak assertions

Required guardrails before promotion:
- mutation-resistant assertions or secondary deterministic checks
- dashboard surfacing of test-file modification events

Promotion requirement:
- `3` consecutive passing runs with no false-success and no vacuous tests

## Relaxation 3: Ambiguous File Target Within a Small Directory

Change:
- planner provides a directory scope rather than exact file path

Still fixed:
- task remains narrow
- tests are read-only
- deterministic file-allowlist derived from directory scope

Goal:
- prove limited path inference without raw free-form repo exploration

Expected new risks:
- wrong file selection
- no-diff loops
- duplicate functionality in new files

Promotion requirement:
- `5` passing runs with correct target selection and zero duplicate implementations

## Multi-File Readiness Gate

Change:
- allow exactly two mutable files where one imports or directly depends on the other

Still fixed:
- same directory scope
- read-only tests
- independent verification

Goal:
- prove cross-file consistency before allowing broader multi-file work

Expected new risks:
- broken imports
- partial emission of only one file
- mismatched interfaces across the two files

Required guardrails before promotion:
- deterministic import/integration check across both files
- both files must appear in the branch diff and acceptance evidence

Promotion requirement:
- `3` consecutive passing runs with both files verified together and zero extra files in diff

## Relaxation 4: Multi-Task Requirement Slice

Change:
- one requirement produces `2-3` coordinated tasks in parallel

Still fixed:
- each worker task remains narrow
- settlement proof remains per task

Goal:
- prove task dependency ordering and parallel execution inside a single requirement

Expected new risks:
- cross-task dependency breakage
- race conditions on shared files
- false completion of partially satisfied requirements

Required guardrails before promotion:
- task dependency graph emitted by planner
- blocked/unblocked state transitions visible in traces
- governor replan path enabled for broken decompositions
- queue lease exclusivity visible in the event log

Promotion requirement:
- `3` passing multi-task runs with correct dependency ordering and zero false-success

## Relaxation 5: Frontier Escalation

Change:
- allow automatic reroute from local/open-weight lane to frontier lane

Policy:
- escalate after `3` consecutive failures with the same reason code for the same task contract
- mixed reason-code failures route to replan review, not automatic frontier escalation

Goal:
- separate model-capability failures from task-contract failures

Expected new risks:
- cost spikes
- queue bottlenecks in frontier lane
- masking planning errors with better models

Promotion requirement:
- frontier queue stays bounded
- cost per rescued task stays under threshold
- rescued tasks still satisfy full settlement proof

## Relaxation 6: Cross-Shard Dependencies

Change:
- allow tasks in one shard to depend on artifacts from another shard

Required before promotion:
- dependency metadata emitted at intake/compiler stage
- cross-shard ordering constraints tracked explicitly
- settlement does not mark dependents complete before prerequisites settle

Goal:
- prove federated execution without global ambiguity

Expected new risks:
- deadlocks
- premature settlement
- hidden shard coupling

Promotion requirement:
- `3` passing dependency-bound runs with zero premature settlement

## Rollback Rule

If a relaxation step fails repeatedly:
- revert to the previous proven constraint level
- preserve traces and artifacts
- fix the harness or planner contract
- rerun the same relaxation step until it passes

## Context Pre-Hydration

Before leaving single-file golden paths, planner output must include deterministic context enrichment.

Minimum pre-hydration set:
- adjacent target file contents as read-only context
- relevant test file contents, even when read-only
- local import graph for the target directory

Reason:
- without pre-hydration, workers waste tokens rediscovering obvious context
- that increases retry rate and hides whether the failure came from the model or from missing task context
