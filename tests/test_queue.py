"""Tests for MX Swarm queue adapters."""

import json

import pytest

from shared.adapters import InMemoryQueue, create_queue_adapter
from shared.schemas import NackReason, TaskContract, TaskState, TaskType
from shared.utils.task_registry import snapshot_task


@pytest.fixture
async def queue():
    """Create an in-memory queue for testing."""
    q = InMemoryQueue("test-queue")
    await q.purge()  # Ensure empty state (singleton persistence)
    return q


@pytest.fixture
def sample_task():
    """Create a sample task for testing."""
    return TaskContract.create(
        task_type=TaskType.FEATURE,
        goal="Test task for queue",
        priority=5,
    )


class TestInMemoryQueue:
    """Tests for InMemoryQueue."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self, queue):
        """Test queue connection lifecycle."""
        await queue.connect()
        assert queue._connected is True

        await queue.disconnect()
        assert queue._connected is False

    @pytest.mark.asyncio
    async def test_publish_and_receive(self, queue, sample_task):
        """Test publishing and receiving messages."""
        await queue.connect()

        # Publish
        msg_id = await queue.publish(sample_task)
        assert msg_id is not None

        # Check queue depth
        depth = await queue.get_queue_depth()
        assert depth == 1

        # Receive
        messages = await queue.receive(max_messages=1)
        assert len(messages) == 1
        assert messages[0].body == sample_task.to_json()

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_ack_removes_message(self, queue, sample_task):
        """Test that ack removes message from queue."""
        await queue.connect()

        await queue.publish(sample_task)
        messages = await queue.receive()

        # Ack the message
        await queue.ack(messages[0].receipt_handle)

        # Queue should be empty
        depth = await queue.get_queue_depth()
        assert depth == 0

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_nack_returns_message(self, queue, sample_task):
        """Test that nack returns message to queue."""
        await queue.connect()

        await queue.publish(sample_task)
        messages = await queue.receive()

        # Nack the message
        await queue.nack(messages[0].receipt_handle, reason="ERR_TASK_FAILED")

        # Message should be back in queue
        depth = await queue.get_queue_depth()
        assert depth == 1

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_nack_records_reason_metadata(self, queue, sample_task):
        """Test that nack reason is attached to message metadata."""
        await queue.connect()

        await queue.publish(sample_task)
        messages = await queue.receive()

        await queue.nack(messages[0].receipt_handle, reason="ERR_SYNTAX")

        redelivered = await queue.receive()
        assert redelivered[0].attributes.get("last_nack_reason") == "ERR_SYNTAX"

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_nack_preserves_updated_task_payload(self, queue, sample_task):
        """Updated in-flight message body should survive local redelivery."""
        await queue.connect()

        await queue.publish(sample_task)
        messages = await queue.receive()
        message = messages[0]

        task = TaskContract.from_json(message.body)
        task.previous_failure_detail = "Expected fenced file block."
        message.body = task.to_json()

        await queue.nack(message.receipt_handle, reason="ERR_NO_DIFF")

        redelivered = await queue.receive()
        redelivered_task = TaskContract.from_json(redelivered[0].body)
        assert redelivered_task.previous_failure_detail == "Expected fenced file block."

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_dead_letter_moves_message_out_of_queue(self, queue, sample_task):
        """Test dead-letter transition for in-memory queue."""
        await queue.connect()

        await queue.publish(sample_task)
        messages = await queue.receive()

        await queue.dead_letter(messages[0].receipt_handle, reason="ERR_SYNTAX")

        assert await queue.get_queue_depth() == 0
        assert await queue.get_dead_letter_depth() == 1

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_priority_ordering(self, queue):
        """Test that higher priority messages are received first."""
        await queue.connect()

        # Publish tasks with different priorities
        low_task = TaskContract.create(TaskType.FEATURE, "Low priority", priority=1)
        high_task = TaskContract.create(TaskType.FEATURE, "High priority", priority=9)
        mid_task = TaskContract.create(TaskType.FEATURE, "Mid priority", priority=5)

        await queue.publish(low_task, priority=1)
        await queue.publish(high_task, priority=9)
        await queue.publish(mid_task, priority=5)

        # Receive should give highest priority first
        messages = await queue.receive(max_messages=3)

        received_priorities = [m.attributes["priority"] for m in messages]
        assert received_priorities == [9, 5, 1]

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_receive_count_increments(self, queue, sample_task):
        """Test that receive count increments on redelivery."""
        await queue.connect()

        await queue.publish(sample_task)

        # First receive
        messages = await queue.receive()
        assert messages[0].receive_count == 1

        # Nack to return
        await queue.nack(messages[0].receipt_handle, reason="ERR_TASK_FAILED")

        # Second receive
        messages = await queue.receive()
        assert messages[0].receive_count == 2

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_single_flight_prevents_duplicate_task_delivery(self, queue):
        """In-memory receive should not deliver same task_id concurrently."""
        await queue.connect()

        task = TaskContract.create(TaskType.FEATURE, "Same task", priority=5)
        await queue.publish(task)
        await queue.publish(task)

        first = await queue.receive(max_messages=1)
        assert len(first) == 1

        # Duplicate with same task_id remains queued while first attempt is in-flight.
        second = await queue.receive(max_messages=1)
        assert second == []
        assert await queue.get_queue_depth() == 1

        await queue.ack(first[0].receipt_handle)
        third = await queue.receive(max_messages=1)
        assert len(third) == 1
        assert third[0].attributes.get("task_id") == task.id

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_receive_skips_tasks_with_unsettled_prerequisites(self, tmp_path):
        """Dependent tasks should remain queued until prerequisite tasks settle."""
        queue = InMemoryQueue(f"test-deps-{tmp_path.name}", repo_path=str(tmp_path))
        await queue.purge()
        await queue.connect()

        prerequisite = TaskContract.create(TaskType.FEATURE, "Base task")
        dependent = TaskContract.create(
            TaskType.FEATURE,
            "Dependent task",
            prerequisite_task_ids=[prerequisite.id],
            prerequisite_requirement_ids=["REQ-BASE"],
        )

        await queue.publish(dependent)
        await queue.publish(prerequisite)

        first = await queue.receive(max_messages=2)
        assert len(first) == 1
        assert TaskContract.from_json(first[0].body).id == prerequisite.id
        assert await queue.get_queue_depth() == 1
        task_event_path = tmp_path / ".runtime" / "task_state_events.jsonl"
        logged_events = [
            json.loads(line)
            for line in task_event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(
            event["state_to"] == TaskState.BLOCKED.value and event["reason"] == "DEPENDENCY_WAIT"
            for event in logged_events
        )

        prerequisite.transition_to(TaskState.CLAIMED)
        prerequisite.transition_to(TaskState.EXECUTING)
        prerequisite.transition_to(TaskState.ARTIFACT_READY)
        prerequisite.transition_to(TaskState.VERIFIED)
        prerequisite.transition_to(TaskState.SETTLED_ACK)
        snapshot_task(prerequisite, repo_path=str(tmp_path), actor_id="test")
        await queue.ack(first[0].receipt_handle)

        second = await queue.receive(max_messages=1)
        assert len(second) == 1
        assert TaskContract.from_json(second[0].body).id == dependent.id
        assert second[0].attributes.get("blocked_by_task_ids") is None
        logged_events = [
            json.loads(line)
            for line in task_event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(
            event["state_from"] == TaskState.BLOCKED.value
            and event["state_to"] == TaskState.QUEUED.value
            and event["reason"] == "DEPENDENCY_READY"
            for event in logged_events
        )

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_failed_prerequisite_dead_letters_dependent_task(self, tmp_path):
        """Dependent tasks should terminalize when a prerequisite fails permanently."""
        queue = InMemoryQueue(f"test-deps-fail-{tmp_path.name}", repo_path=str(tmp_path))
        await queue.purge()
        await queue.connect()

        prerequisite = TaskContract.create(TaskType.FEATURE, "Base task")
        dependent = TaskContract.create(
            TaskType.FEATURE,
            "Dependent task",
            prerequisite_task_ids=[prerequisite.id],
            prerequisite_requirement_ids=["REQ-BASE"],
        )

        await queue.publish(dependent)
        await queue.publish(prerequisite)

        first = await queue.receive(max_messages=2)
        assert len(first) == 1
        assert TaskContract.from_json(first[0].body).id == prerequisite.id

        prerequisite.transition_to(TaskState.CLAIMED)
        prerequisite.transition_to(TaskState.SETTLED_NACK)
        prerequisite.settlement_reason = NackReason.ERR_CHECK_FAIL
        snapshot_task(prerequisite, repo_path=str(tmp_path), actor_id="test")
        await queue.ack(first[0].receipt_handle)

        second = await queue.receive(max_messages=1)
        assert second == []
        assert await queue.get_queue_depth() == 0
        assert await queue.get_dead_letter_depth() == 1

        task_event_path = tmp_path / ".runtime" / "task_state_events.jsonl"
        logged_events = [
            json.loads(line)
            for line in task_event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(
            event["state_to"] == TaskState.DEAD_LETTER.value
            and event["reason"] == NackReason.ERR_PREREQUISITE_FAILED.value
            for event in logged_events
        )

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_purge(self, queue):
        """Test queue purge."""
        await queue.connect()

        # Add multiple tasks
        for i in range(5):
            task = TaskContract.create(TaskType.FEATURE, f"Task {i}")
            await queue.publish(task)

        assert await queue.get_queue_depth() == 5

        # Purge
        await queue.purge()

        assert await queue.get_queue_depth() == 0

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_batch_publish(self, queue):
        """Test publishing multiple tasks at once."""
        await queue.connect()

        tasks = [
            TaskContract.create(TaskType.FEATURE, f"Task {i}")
            for i in range(10)
        ]

        msg_ids = await queue.publish_batch(tasks)

        assert len(msg_ids) == 10
        assert await queue.get_queue_depth() == 10

        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_extend_visibility_updates_lease_expiry(self, queue, sample_task):
        """Lease extension should update in-flight expiry metadata."""
        await queue.connect()
        await queue.publish(sample_task)
        messages = await queue.receive()
        message = messages[0]
        before = message.lease_expires_at

        await queue.extend_visibility(message.receipt_handle, 600)
        refreshed = queue._inflight[message.receipt_handle][0]  # type: ignore[attr-defined]

        assert before is not None
        assert refreshed.lease_expires_at is not None
        assert refreshed.lease_expires_at > before
        await queue.disconnect()

    @pytest.mark.asyncio
    async def test_queue_capabilities_reflect_memory_adapter(self, queue):
        await queue.connect()
        assert queue.supports_depth() is True
        assert queue.supports_dead_letter() is True
        await queue.disconnect()


class TestQueueFactory:
    """Tests for queue adapter factory."""

    def test_create_memory_queue(self):
        """Test creating in-memory queue."""
        config = {"provider": "memory", "queue_name": "test"}
        queue = create_queue_adapter(config)

        assert isinstance(queue, InMemoryQueue)
        assert queue.name == "test"

    def test_create_default_queue(self):
        """Test creating queue with default config."""
        config = {}
        queue = create_queue_adapter(config)

        assert isinstance(queue, InMemoryQueue)

    def test_invalid_provider(self):
        """Test that invalid provider raises error."""
        config = {"provider": "invalid"}

        with pytest.raises(ValueError, match="Unknown queue provider"):
            create_queue_adapter(config)
