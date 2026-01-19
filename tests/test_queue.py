"""Tests for MX Swarm queue adapters."""

import pytest
import asyncio

from shared.adapters import InMemoryQueue, create_queue_adapter
from shared.schemas import TaskContract, TaskType


@pytest.fixture
def queue():
    """Create an in-memory queue for testing."""
    return InMemoryQueue("test-queue")


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
        await queue.nack(messages[0].receipt_handle)

        # Message should be back in queue
        depth = await queue.get_queue_depth()
        assert depth == 1

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
        await queue.nack(messages[0].receipt_handle)

        # Second receive
        messages = await queue.receive()
        assert messages[0].receive_count == 2

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
