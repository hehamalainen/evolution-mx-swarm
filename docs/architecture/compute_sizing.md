# MX Compute Sizing

## Purpose
This note defines how to reason about compute, token, electricity, and silicon cost for MX.

The key principle is:
- size MX by active inference and verification load
- not by the raw count of logical agents

`1000` logical agents does not mean `1000` simultaneous expensive model calls.

Most MX scale is control-plane work:
- routing
- dependency blocking
- settlement logic
- capability updates
- governor decisions
- verification orchestration

## Core sizing variables
Use these variables as the base sizing model:

- `logical_lanes`
- `active_model_concurrency`
- `avg_prompt_tokens`
- `avg_completion_tokens`
- `tasks_per_hour`
- `verification_cpu_seconds`
- `sink_settlements_per_hour`
- `power_watts_per_model_lane`
- `usd_per_1M_tokens`
- `hardware_amortization_usd_per_hour`

Derived metrics:
- `tokens_per_hour = active_model_concurrency * tasks_per_hour_per_lane * (avg_prompt_tokens + avg_completion_tokens)`
- `verification_cpu_hours = tasks_per_hour * verification_cpu_seconds / 3600`
- `inference_power_kwh = power_watts_per_model_lane * active_model_concurrency / 1000 * hours`
- `effective_cost_per_success = (token_cost + power_cost + hardware_amortization + sink_cost) / verified_landings`

## Deployment ladder
### 1. Local developer lane
Use when you need:
- proof ladders
- control-plane development
- narrow local model-backed slices
- low active concurrency

Typical shape:
- MacBook / Mac mini / Apple Silicon class machine
- local sink or very narrow real sink path
- one to a few active model lanes

What is realistic locally:
- deterministic adapter proofs
- canned worker proofs
- control-plane scale simulations
- narrow local live proofs

What is not realistic locally:
- sustained high-concurrency real-model generation
- large mixed-model production swarms

### 2. Team or private inference node
Use when you need:
- moderate real-model concurrency
- private or sovereign inference
- persistent worker throughput beyond a single developer box

Typical shape:
- one stronger private inference box or a small GPU node set
- moderate concurrency
- shared team deployment

This is often the first practical sovereign deployment tier.

### 3. Institutional or sovereign cluster
Use when you need:
- high sustained active model concurrency
- mixed model tiers
- strong isolation or national/private hosting guarantees
- predictable latency under load

Typical shape:
- dedicated GPU cluster or sovereign private cloud
- token and energy budget modeled explicitly
- verification compute sized separately from inference compute

## What drives real compute need
You need real compute sooner when:
- active model concurrency rises
- prompt/completion sizes grow
- tasks become broader and less canned
- latency targets tighten
- retries rise
- more lanes use real models instead of synthetic or canned outputs

You can stay local longer when:
- most lanes are deterministic or canned
- real-model lanes are sparse
- verification dominates over generation
- latency tolerance is high

## What to measure in MX
Every serious sizing discussion should include:
- verified landings
- false-success rate
- cost per success
- latency by lane
- active model concurrency
- tokens per successful landing
- verification CPU seconds per landing
- sink settlement latency
- power draw per active inference lane

## Practical implication for MX
Current evidence supports this statement:
- MX correctness, proofs, and control-plane scaling can run locally
- real sustained autonomous generation at scale is where specialized compute begins to matter

So the end state is not:
- every user needs frontier-scale GPUs

The end state is:
- small teams can run narrow governed swarms locally or on small private hardware
- larger organizations size inference infrastructure according to active model concurrency and latency targets

## Sovereign AI SDLC framing
For sovereign deployments, the sizing question should be modeled as four coupled budgets:

- inference budget
- verification budget
- electricity budget
- silicon amortization budget

Token pricing is only one part of the answer.

A serious sovereign AI SDLC model needs both:
- cloud-equivalent token cost
- local/private equivalent power plus hardware cost

## Decision rule
Move from local hardware to dedicated inference infrastructure when any of these becomes binding:
- active model concurrency exceeds local latency tolerance
- effective cost per success is better on dedicated hardware than on local developer machines
- verification and inference contend for the same box and damage throughput
- sovereignty requirements forbid the needed cloud models

## Recommended next artifact
After the next MX scale batch, produce a sizing table with rows for:
- local developer lane
- private team node
- sovereign cluster

And columns for:
- active model concurrency
- tasks per hour
- verified landings per hour
- average latency
- average power
- cost per success
