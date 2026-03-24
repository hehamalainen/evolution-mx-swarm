from datetime import UTC, datetime, timedelta

import worker.service as worker_service
from shared.schemas import TaskContract, TaskState, TaskType
from shared.utils.config import MXConfig
from worker.service import Worker


class _StubQueue:
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class _StubLLM:
    async def chat(self, *args, **kwargs):
        raise AssertionError("LLM should not be called in this unit test")


class _RegistryCapture:
    def __init__(self) -> None:
        self.events = []

    def record_settlement_event(self, event):
        self.events.append(event)
        return True


def test_worker_emits_passive_capability_event_on_terminal_settlement(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    registry = _RegistryCapture()
    monkeypatch.setattr(worker_service, "get_capability_registry", lambda **kwargs: registry)

    worker = Worker(
        config=MXConfig(),
        worker_id="worker-test",
        queue=_StubQueue(),
        llm=_StubLLM(),
    )
    task = TaskContract.create(task_type=TaskType.FEATURE, goal="Implement feature")
    task.attempt_id = "attempt-1"
    task.state = TaskState.SUBMITTED_PENDING_LEDGER
    task.last_error = "All connection attempts failed"
    worker._attempt_started_at[(task.id, task.attempt_id)] = datetime.now(UTC) - timedelta(seconds=4)
    worker._attempt_llm_cost_usd[(task.id, task.attempt_id)] = 0.42

    worker._set_state(task, TaskState.SETTLED_ACK)

    assert len(registry.events) == 1
    event = registry.events[0]
    assert event.task_id == task.id
    assert event.outcome == "success"
    assert event.terminal_state == TaskState.SETTLED_ACK.value
    assert event.cost_usd == 0.42
    assert event.latency_s >= 0.0
    assert event.detail == "All connection attempts failed"
    assert event.stream_id == task.stream.value
    assert event.model_tier == task.model_tier.value
