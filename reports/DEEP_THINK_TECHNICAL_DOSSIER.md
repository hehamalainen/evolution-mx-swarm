# Project MX (Evolution MX Swarm)
## Deep Think Technical Dossier for External Peer Review

Prepared for: Google Deep Think peer review
Prepared by: Codex technical audit pass
Date: 2026-02-14
Repository: evolution-mx-swarm

---

## 1) Objective and Use of This Dossier

This document is a technical handoff artifact for external peer review of Project MX direction, focusing on:

- Current architecture and control loops
- Verified operational failure modes
- Evidence from code and logs
- Hypotheses grounded in external research and platform docs
- A concrete 7-day stabilization plan before investor presentation

Primary goal: establish whether Project MX is currently a credible path from ambitious prototype to reliable autonomous engineering system.

---

## 2) Executive Summary (Brutally Honest)

Project MX is ambitious in the right direction, but operationally it is still prototype-grade.

### What is strong

- Clear modular decomposition (`planner` / `worker` / `judge` / adapters).
- Core static quality gates currently pass (`ruff`, `mypy`, `pytest`).
- Good conceptual alignment with modern agentic patterns (tool use, feedback loops, governance layer).

### What is not strong enough yet

- Runtime contracts are inconsistent across modules (especially VCS and queue settlement semantics).
- Termination/control lifecycle is not robust; stop conditions do not always produce full-system convergence.
- Success/failure accounting is too optimistic in some branches (can mark task complete without durable artifact).
- Critical runtime paths are under-tested despite green suite (coverage is low in planner/worker/judge/CLI control paths).

### Hard truth

As of February 14, 2026, this is a serious research prototype, not yet an operationally reliable autonomous delivery platform. It is absolutely worth continuing, but only if next work is reliability-first, not feature-first.

---

## 3) System Snapshot (As Observed)

### 3.1 Control-plane and data-plane loop

1. Planner reads `PRD.md`, derives/loads `SPEC.json`, generates tasks, publishes to queue.
2. Workers pull task contracts, execute LLM-driven edits, run checks, submit through VCS adapter.
3. Judge evaluates progress and decides `CONTINUE`, `RESET`, or `STOP`.
4. CLI orchestrator (`mx run`) starts components and handles dashboard heartbeats.

### 3.2 Key modules

- `planner/service.py`
- `worker/service.py`
- `judge/service.py`
- `cli/main.py`
- `shared/adapters/queue.py`
- `shared/adapters/__init__.py` (VCS factory)
- `shared/utils/config.py`

---

## 4) Verification Baseline (2026-02-14)

Commands executed in repo root:

```bash
./.venv/bin/ruff check .
./.venv/bin/mypy shared planner worker judge cli
./.venv/bin/pytest
./.venv/bin/pytest --cov=. --cov-report=term
```

Results:

- `ruff`: pass (with deprecation warning on top-level Ruff settings format).
- `mypy`: pass (notes that untyped function bodies in `dashboard/api.py` are not checked).
- `pytest`: 58/58 passed.
- Coverage total: 35%.

Notable low coverage in operational modules:

- `cli/main.py`: 19%
- `planner/service.py`: 11%
- `worker/service.py`: 33%
- `judge/service.py`: 39%

Interpretation: quality gates are green, but confidence in real runtime behavior remains low.

---

## 5) Validated Operational Problems

## OP-1: VCS config contract mismatch is still present in live call paths

### Evidence

`VCSConfig.to_dict()` emits flat Aether fields:

```python
# shared/utils/config.py
@dataclass
class VCSConfig:
    provider: str = "github"
    aether_base_url: str = "http://localhost:3000"
    aether_repo_id: str | None = None
    aether_agent_id: str = "mx-swarm"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "aether_base_url": self.aether_base_url,
            "aether_repo_id": self.aether_repo_id,
            "aether_agent_id": self.aether_agent_id,
        }
```

VCS factory expects nested `aether` object:

```python
# shared/adapters/__init__.py
if provider == "aether":
    aether_cfg = config.get("aether", {})
    return AetherVCS(
        base_url=aether_cfg.get("base_url", "http://localhost:8100/v1"),
        repo_id=aether_cfg.get("repo_id"),
        agent_id=aether_cfg.get("agent_id", "mx-swarm"),
    )
```

`cli/main.py` passes `config.vcs.to_dict()` directly in `sync_spec_with_aether()`.

### Impact

- Runtime can silently fall back to defaults (`repo_id=None`, default URL), depending on call path.
- Preflight can produce false confidence if different code paths pass different shapes.

### Severity

High (contract correctness across control-plane).

---

## OP-2: Worker can acknowledge tasks even when no code artifact is produced

### Evidence

```python
# worker/service.py
changed_files, notes = await self._implement_task(repo, task)

if not changed_files:
    print(f"[{self.worker_id}] No changes made for task {task.id}")
    await self.queue.ack(message.receipt_handle)
    return True
```

Also observed in logs:

- `Found 0 file blocks in response`
- `No changes made for task ...`
- followed by ack behavior.

### Impact

- False positive completion semantics.
- Queue drains while requirements may remain unimplemented.
- Judge can see progress metrics detached from actual shipped changes.

### Severity

High (integrity of completion semantics).

---

## OP-3: STOP lifecycle is partial; swarm process can stall in paused loops

### Evidence

Judge STOP path sets `paused = True` and breaks its own loop:

```python
# judge/service.py
if report.decision == JudgeDecision.STOP:
    config = MXConfig.load()
    config.paused = True
    config.save()
    print("[Judge] Swarm stopped - DoD met")
    break
```

Worker loop on paused state:

```python
# worker/service.py
if config.paused:
    print(f"[{self.worker_id}] Paused, waiting...")
    await asyncio.sleep(30)
    continue
```

CLI run loop waits on `shutdown_event` only (signal-driven), not on judge termination/state convergence:

```python
# cli/main.py
await shutdown_event.wait()
```

### Impact

- Judge can stop itself while workers keep sleeping forever.
- Top-level process appears "running" until manual interrupt.

### Severity

High (operational control-plane reliability).

---

## OP-4: Azure queue settlement is not implemented

### Evidence

```python
# shared/adapters/queue.py
async def ack(self, receipt_handle: str) -> None:
    # Azure requires the original message object
    # This is handled via the _original attribute
    pass  # Completed via context manager in real implementation

async def nack(self, receipt_handle: str, delay: int = 0) -> None:
    # Azure handles this via abandon() on the message
    pass
```

### Impact

- For Azure provider, success/failure settlement is effectively undefined.
- Message lock behavior and retry semantics diverge from expected queue contract.

### Severity

Critical for Azure mode.

---

## OP-5: Queue semantics are abstracted as uniform, but backend guarantees differ materially

### Evidence in code

- GCP adapter supports explicit `ack` and `modify_ack_deadline` (nack behavior emulation).
- Azure adapter stubs settlement methods.
- `get_queue_depth()` returns `-1` for cloud backends in current implementation.

### External source constraints

- Pub/Sub pull returns `ackId`; acknowledge removes message best-effort, but expired ack deadline can still redeliver.
- Azure PeekLock requires explicit completion/abandon/dead-letter semantics.

### Impact

- The system currently treats queue semantics as uniform when they are not.
- Reliability assumptions can break by provider.

### Severity

High.

---

## OP-6: Telemetry quality gap (placeholder/synthetic metrics)

### Evidence

In CLI heartbeat:

```python
# cli/main.py
prs_opened = tasks_completed if vcs_provider == "github" else 0
submissions_opened = tasks_completed if vcs_provider == "aether" else 0
```

In Judge telemetry snapshot:

```python
# judge/service.py
return TelemetrySnapshot(
    ci_health_percent=self.state.last_progress,  # Placeholder
    tasks_completed_1h=metrics.total_tasks,
    tasks_nacked_1h=metrics.retry_cascades,
    queue_depth=0,  # Need real queue depth
    prs_open_total=0  # Need real PR count
)
```

### Impact

- Dashboard/operator perception can diverge from real system state.
- Governor decisions are at risk if fed low-fidelity telemetry.

### Severity

Medium-high.

---

## OP-7: Real logs show VCS health instability and retry exhaustion

### Evidence from `live_swarm.log`

Repeated patterns:

- `VCS submission failed: Aether submission failed after 3 attempts: Aether health check failed`
- `Task ... exceeded max retries`
- long sequences of `Paused, waiting...`

### Impact

- External dependency instability (Aether) currently causes broad task churn.
- Retry policy can consume cycles without forward progress.

### Severity

High (operational reliability).

---

## OP-8: Packaging/runtime expectations still drift

### Evidence

- `AGENTS.md` expects editable install `pip install -e ".[dev]"`.
- `pyproject.toml` runtime dependencies are minimal (`anthropic`, `httpx`).
- `requirements.txt` includes FastAPI/uvicorn/aiohttp/dev tooling.

### Impact

- Different install paths can lead to environment skew and "works on one setup" behavior.

### Severity

Medium.

---

## 6) Research-Backed Directional Hypotheses (Beyond Project MX)

Below are explicit hypotheses and why they are plausible based on external sources.

## H1: Multi-window multi-burn-rate SLO alerting should replace threshold-only judge heuristics

Rationale:

- Google SRE Workbook recommends multi-window, multi-burn-rate alerting as the most viable option for balancing precision, recall, and reset behavior.
- Current judge logic is mostly threshold/heuristic-driven and does not enforce error-budget style response policies.

Expected result:

- Fewer false alarms and earlier detection of materially threatening failure modes.

---

## H2: Queue settlement must become provider-native and explicit, not abstract/no-op

Rationale:

- Pub/Sub and Service Bus have different acknowledgment/lock semantics.
- Treating them as identical causes hidden correctness bugs and duplicate/lost-work edge cases.

Expected result:

- Deterministic retry behavior and better cross-cloud parity.

---

## H3: Task completion should be artifact-gated, not "attempt-gated"

Rationale:

- SWE-bench highlights that real software issue resolution requires coherent multi-file edits and executable verification.
- Current "no-change => ack" branch breaks that contract.

Expected result:

- Completion metrics align with real, reviewable code outcomes.

---

## H4: Integrate reflective retry memory to reduce repetitive failure loops

Rationale:

- Reflexion shows linguistic feedback and episodic memory can improve iterative problem solving without weight updates.
- Current retries are mostly exponential backoff without robust semantic adaptation.

Expected result:

- Better second/third-attempt quality and fewer repeated dead-end patches.

---

## H5: Tool invocation policy should be explicit and validated

Rationale:

- Toolformer demonstrates measurable gains when model learns structured API/tool usage decisions.
- Project MX already uses tools (git, queue, VCS adapters), but without a formal tool-call policy quality layer.

Expected result:

- Lower hallucination, better deterministic interaction with external systems.

---

## H6: Distributed state conflict handling should use CRDT principles for eventual convergence

Rationale:

- CRDT formalization provides sufficient conditions for eventual convergence under weak network conditions.
- Multi-agent status/state updates currently rely on ad hoc file/log sync patterns.

Expected result:

- Fewer state races, cleaner reconciliation between planner/worker/judge/dashboard state.

---

## H7: GitHub integration should distinguish Checks API vs Statuses API capability boundaries

Rationale:

- GitHub Checks write operations are GitHub App-oriented.
- Commit Statuses remain simpler and widely usable for CI result signaling.

Expected result:

- Correct auth model and fewer integration dead-ends for CI signal publishing.

---

## 7) Recommended Architecture Adjustments (Near-term)

## 7.1 Define and enforce a strict Task Lifecycle FSM

States:

- `queued`
- `claimed`
- `in_progress`
- `artifact_produced`
- `verified`
- `submitted`
- `settled_success`
- `settled_retry`
- `dead_letter`

Rule: task cannot transition to success without `artifact_produced` + `verified`.

## 7.2 Make queue settlement adapter-complete per provider

- Implement Azure `complete_message` for ack path.
- Implement Azure `abandon_message` and optional dead-letter on retry exhaustion.
- Keep receipt token to original message mapping in adapter state (or wrapper).

## 7.3 Repair VCS config contract once, centrally

Option A: Normalize all configs to nested schema before factory.
Option B: Factory accepts both flat and nested for backward compatibility.

Either way: one canonical schema must be documented and tested.

## 7.4 Add orchestrator convergence logic

- `cmd_run` should terminate when judge reaches STOP and all workers are paused or gracefully stopped.
- Avoid indefinite wait on external signal only.

## 7.5 Replace synthetic dashboard counters with source-of-truth metrics

- Count opened PRs/submissions from VCS adapter responses.
- Track queue depth with provider-aware strategy (where unavailable, mark unknown explicitly).
- Expose confidence level in telemetry payload (measured/estimated/placeholder).

## 7.6 Expand tests where risk is highest

Highest-priority new tests:

1. VCS config shape compatibility matrix.
2. Worker no-change path should `nack` with reason, not `ack` success.
3. Judge STOP should converge full swarm shutdown in `cmd_run`.
4. Azure settlement integration tests.
5. Dashboard heartbeat values traceable to real events.

---

## 8) 7-Day Stabilization Plan (Investor Week)

## Day 1: Contract hardening

- Fix VCS config contract mismatch.
- Add compatibility tests (flat + nested).
- Deliverable: deterministic adapter creation across CLI/worker/dashboard.

## Day 2: Completion semantics

- Change no-change branch from success ack to retry/diagnostic path.
- Add explicit failure reason taxonomy.
- Deliverable: no silent success without artifact.

## Day 3: Stop/convergence control-plane

- Implement judge-to-orchestrator shutdown propagation.
- Ensure workers exit cleanly after STOP.
- Deliverable: `mx run` exits deterministically on DoD/STOP.

## Day 4: Azure settlement completion

- Implement `ack`/`nack` in Azure adapter correctly.
- Add adapter-specific integration tests.
- Deliverable: provider parity for core settlement semantics.

## Day 5: Telemetry truthfulness

- Replace synthetic PR/submission metrics with event-based counters.
- Mark unknown metrics explicitly.
- Deliverable: dashboard trustworthy enough for live investor demo.

## Day 6: Reliability policy integration

- Add burn-rate based alert policy (initial static params).
- Wire judge escalation to error-budget style actions.
- Deliverable: objective reliability governance.

## Day 7: Dry-run + evidence pack

- Run end-to-end rehearsal with one realistic PRD.
- Produce evidence report: success rate, retries, mean time to settle, false-success count (target 0).
- Deliverable: investor-week metrics deck + reproducible runbook.

---

## 9) Peer Review Questions for Google Deep Think

1. Is the proposed task lifecycle FSM sufficient, or should we include explicit compensation states for partially applied diffs?
2. Should Project MX prioritize deterministic local execution first, then cloud portability, or can both be matured in parallel?
3. Is CRDT-backed shared state justified now, or is an append-only event log with deterministic reducers enough for v1 reliability?
4. What is the minimum telemetry set required to claim "operationally reliable" in an agentic coding platform?
5. Which benchmark strategy is best for proving progress: SWE-bench style external benchmark, internal PRD-to-PR benchmark, or hybrid?

---

## 10) Code Excerpts (Core Evidence)

### 10.1 VCS config serialization (flat)

```python
# shared/utils/config.py
def to_dict(self) -> dict:
    return {
        "provider": self.provider,
        "aether_base_url": self.aether_base_url,
        "aether_repo_id": self.aether_repo_id,
        "aether_agent_id": self.aether_agent_id,
    }
```

### 10.2 VCS adapter factory expectation (nested)

```python
# shared/adapters/__init__.py
if provider == "aether":
    aether_cfg = config.get("aether", {})
    return AetherVCS(
        base_url=aether_cfg.get("base_url", "http://localhost:8100/v1"),
        repo_id=aether_cfg.get("repo_id"),
        agent_id=aether_cfg.get("agent_id", "mx-swarm"),
    )
```

### 10.3 Worker no-change success path

```python
# worker/service.py
if not changed_files:
    print(f"[{self.worker_id}] No changes made for task {task.id}")
    await self.queue.ack(message.receipt_handle)
    return True
```

### 10.4 Judge STOP behavior

```python
# judge/service.py
if report.decision == JudgeDecision.STOP:
    config = MXConfig.load()
    config.paused = True
    config.save()
    print("[Judge] Swarm stopped - DoD met")
    break
```

### 10.5 CLI run waits on external shutdown signal

```python
# cli/main.py
# Wait for shutdown or completion
await shutdown_event.wait()
```

### 10.6 Azure queue settlement stubs

```python
# shared/adapters/queue.py
async def ack(self, receipt_handle: str) -> None:
    pass

async def nack(self, receipt_handle: str, delay: int = 0) -> None:
    pass
```

---

## 11) External Source Base (Beyond Project MX)

### Reliability / SRE

- SRE Workbook: Alerting on SLOs
  - https://sre.google/workbook/alerting-on-slos/
- SRE Workbook: Example Error Budget Policy
  - https://sre.google/workbook/error-budget-policy/

### Queue semantics

- Google Pub/Sub exactly-once delivery
  - https://cloud.google.com/pubsub/docs/exactly-once-delivery
- Google Pub/Sub REST pull method
  - https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions/pull
- Google Pub/Sub REST acknowledge method
  - https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions/acknowledge
- Azure Service Bus message settlement model
  - https://learn.microsoft.com/en-us/azure/service-bus-messaging/message-transfers-locks-settlement
- Azure Service Bus Python queue quickstart (`complete_message` usage)
  - https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-python-how-to-use-queues

### GitHub integration boundaries

- GitHub Checks API (check runs)
  - https://docs.github.com/en/rest/checks/runs
- GitHub Commit Statuses API
  - https://docs.github.com/en/rest/commits/statuses

### Observability conventions

- OpenTelemetry service semantic conventions (`service.name` fallback behavior)
  - https://opentelemetry.io/docs/specs/semconv/resource/service/

### Distributed systems / agent research

- CRDT foundational report (Shapiro et al., 2011)
  - https://hal.inria.fr/inria-00555588/document
- SWE-bench
  - https://arxiv.org/abs/2310.06770
- Reflexion
  - https://arxiv.org/abs/2303.11366
- Toolformer
  - https://arxiv.org/abs/2302.04761

---

## 12) Final Assessment

Project MX should continue, but under a reliability-first charter for the next phase.

If you present this as "already operationally robust," reviewers will find holes quickly.
If you present it as "a strong architecture with explicit reliability debt now being retired with measurable milestones," that is accurate and defensible.

The project is not overhyped technically. It is under-constrained operationally. Fixing that is feasible in days, not months, if scope remains strict.

