"""Regression tests for worker reconciliation backlog persistence and serialization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.schemas import NackReason, TaskContract, TaskState, TaskType
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


def _make_worker(reconciliation_path: Path, max_attempts: int = 3, worker_id: str = "worker-test") -> Worker:
    config = MXConfig()
    config.vcs.settlement_retry_max = max_attempts
    worker = Worker(
        config=config,
        worker_id=worker_id,
        queue=_StubQueue(),
        llm=_StubLLM(),
    )
    worker._reconciliation_path = str(reconciliation_path)
    worker._vcs = MagicMock()
    return worker


@pytest.mark.asyncio
async def test_reconcile_pending_persists_attempts_each_iteration_and_dead_letters(tmp_path: Path) -> None:
    """Ordinary pending responses must persist attempt increments on every poll and stop at max_attempts."""
    backlog_file = tmp_path / "reconciliation_backlog.json"
    worker = _make_worker(backlog_file, max_attempts=4)
    worker._vcs.get_status = AsyncMock(return_value={"status": "pending"})

    task = TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Ensure pending attempts persist",
    )
    task.id = "task-recon-1"
    await worker._enqueue_reconciliation_item(task, submission_id="sub-123", detail="pending ledger")

    for expected_attempt in range(1, 5):
        await worker._reconcile_backlog_once()
        persisted = worker._load_reconciliation_backlog()
        assert len(persisted) == 1
        assert persisted[0]["attempts"] == expected_attempt
        assert persisted[0]["last_status"] == "pending"
        if expected_attempt < 4:
            assert persisted[0]["status"] == "pending"
        else:
            assert persisted[0]["status"] == "dead_letter"
            assert persisted[0]["state"] == TaskState.DEAD_LETTER.value
            assert persisted[0]["reason"] == NackReason.ERR_LEDGER_UNAVAILABLE.value

    # Subsequent reconciliation pass does not poll dead_letter items again
    await worker._reconcile_backlog_once()
    assert worker._vcs.get_status.await_count == 4


@pytest.mark.asyncio
async def test_reconcile_merges_updates_without_overwriting_concurrent_enqueues(tmp_path: Path) -> None:
    """Concurrent enqueue while reconciliation is polling must not be overwritten on save."""
    backlog_file = tmp_path / "reconciliation_backlog.json"
    worker_a = _make_worker(backlog_file, max_attempts=3, worker_id="worker-a")
    worker_b = _make_worker(backlog_file, max_attempts=3, worker_id="worker-b")

    task_a = TaskContract.create(task_type=TaskType.FEATURE, goal="First task")
    task_a.id = "task-a"
    task_b = TaskContract.create(task_type=TaskType.FEATURE, goal="Second task")
    task_b.id = "task-b"
    await worker_a._enqueue_reconciliation_item(task_a, submission_id="sub-a", detail="queued a")

    async def _poll_and_concurrently_enqueue(submission_id: str) -> dict[str, str]:
        assert submission_id == "sub-a"
        await worker_b._enqueue_reconciliation_item(task_b, submission_id="sub-b", detail="queued b")
        return {"status": "pending"}

    worker_a._vcs.get_status = AsyncMock(side_effect=_poll_and_concurrently_enqueue)
    await worker_a._reconcile_backlog_once()

    persisted = worker_a._load_reconciliation_backlog()
    by_task = {entry["task_id"]: entry for entry in persisted}
    assert set(by_task.keys()) == {"task-a", "task-b"}
    assert by_task["task-a"]["attempts"] == 1
    assert by_task["task-b"]["attempts"] == 0
