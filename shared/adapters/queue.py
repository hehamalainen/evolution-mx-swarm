"""Queue adapter abstraction for MX Swarm.

Provides cloud-agnostic queue interface with implementations for:
- GCP Pub/Sub
- Azure Service Bus
- In-memory (local dev)
"""

import asyncio
import json
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shared.schemas import NackReason, TaskContract, TaskState
from shared.utils.metrics import append_trace_span
from shared.utils.task_registry import load_task_snapshot, snapshot_task


@dataclass
class QueueMessage:
    """Wrapper for queue messages with metadata."""
    id: str
    body: str
    receipt_handle: str  # For ack/nack
    receive_count: int
    enqueued_at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    provider: str = "memory"
    lease_expires_at: datetime | None = None
    _original: Any | None = None  # Backend-native message for providers needing explicit settle calls


class QueueAdapter(ABC):
    """Abstract base class for queue adapters."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to queue service."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to queue service."""
        pass

    @abstractmethod
    async def publish(self, task: TaskContract, priority: int = 5) -> str:
        """
        Publish a task to the queue.

        Args:
            task: TaskContract to publish
            priority: Message priority (1-10, higher = more urgent)

        Returns:
            Message ID
        """
        pass

    @abstractmethod
    async def receive(
        self,
        max_messages: int = 1,
        visibility_timeout: int = 300,
    ) -> list[QueueMessage]:
        """
        Receive messages from the queue.

        Args:
            max_messages: Maximum number of messages to receive
            visibility_timeout: Seconds before message becomes visible again

        Returns:
            List of QueueMessage objects
        """
        pass

    @abstractmethod
    async def ack(self, receipt_handle: str) -> None:
        """Acknowledge message (remove from queue)."""
        pass

    @abstractmethod
    async def nack(self, receipt_handle: str, reason: str, delay: int = 0) -> None:
        """
        Negative acknowledge (return to queue for retry).

        Args:
            receipt_handle: Message receipt handle
            reason: Machine-readable failure reason
            delay: Seconds to delay before message is visible again
        """
        pass

    @abstractmethod
    async def dead_letter(self, receipt_handle: str, reason: str) -> None:
        """Move message to dead-letter state."""
        pass

    @abstractmethod
    async def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        """Extend queue lease/visibility timeout for an in-flight message."""
        pass

    @abstractmethod
    async def get_queue_depth(self) -> int:
        """Get approximate number of messages in queue."""
        pass

    @abstractmethod
    async def purge(self) -> None:
        """Remove all messages from queue (use with caution)."""
        pass

    def supports_depth(self) -> bool:
        """Whether queue depth introspection is supported."""
        return False

    def supports_dead_letter(self) -> bool:
        """Whether native dead-letter semantics are supported."""
        return False

    async def publish_batch(self, tasks: list[TaskContract]) -> list[str]:
        """Publish multiple tasks. Default implementation calls publish() in parallel."""
        results = await asyncio.gather(*[self.publish(t) for t in tasks])
        return list(results)


class InMemoryQueue(QueueAdapter):
    """
    In-memory queue for local development and testing.

    Thread-safe implementation with visibility timeout support.
    Uses singleton pattern per queue name to ensure all components share the same queue.
    """

    # Singleton instances by queue name
    _instances: dict[tuple[str, str], "InMemoryQueue"] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, name: str = "mx-tasks", repo_path: str = "."):
        """Return existing instance for queue name, or create new one."""
        key = (name, os.path.abspath(repo_path))
        with cls._instance_lock:
            if key not in cls._instances:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instances[key] = instance
            return cls._instances[key]

    def __init__(self, name: str = "mx-tasks", repo_path: str = "."):
        # Only initialize once per instance
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.name = name
        self.repo_path = os.path.abspath(repo_path)
        self._queue: deque = deque()
        self._inflight: dict[str, tuple[QueueMessage, float]] = {}
        self._dead_letter: deque[QueueMessage] = deque()
        self._lock = threading.Lock()
        self._connected = False

    def _task_state_event_path(self) -> str:
        return os.path.join(self.repo_path, ".runtime", "task_state_events.jsonl")

    def _emit_task_event(
        self,
        task: TaskContract,
        state_from: TaskState,
        state_to: TaskState,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        payload = {
            "event_id": f"evt-{uuid.uuid4().hex[:16]}",
            "trace_id": task.trace_id,
            "task_id": task.id,
            "attempt_id": task.attempt_id,
            "state_from": state_from.value,
            "state_to": state_to.value,
            "reason": reason,
            "detail": detail,
            "provider": "memory",
            "timestamp": datetime.now(UTC).isoformat(),
            "actor_id": f"queue:{self.name}",
        }
        event_path = self._task_state_event_path()
        os.makedirs(os.path.dirname(event_path), exist_ok=True)
        with open(event_path, "a") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _dependency_status(self, msg: QueueMessage) -> tuple[list[str], list[str]]:
        try:
            task = TaskContract.from_json(msg.body)
        except Exception:
            return [], []

        blocked: list[str] = []
        failed: list[str] = []
        for prerequisite_task_id in task.prerequisite_task_ids:
            snapshot = load_task_snapshot(prerequisite_task_id, repo_path=self.repo_path)
            if snapshot is None:
                blocked.append(prerequisite_task_id)
                continue
            if snapshot.state in {TaskState.SETTLED_NACK, TaskState.DEAD_LETTER}:
                failed.append(prerequisite_task_id)
                continue
            if snapshot.state != TaskState.SETTLED_ACK:
                blocked.append(prerequisite_task_id)
        return blocked, failed

    def _fail_dependency_task(self, msg: QueueMessage, failed_by: list[str]) -> None:
        try:
            task = TaskContract.from_json(msg.body)
        except Exception:
            return

        state_from = task.state
        detail = f"Prerequisite task(s) failed terminally: {', '.join(failed_by)}"
        if task.can_transition_to(TaskState.DEAD_LETTER):
            task.transition_to(TaskState.DEAD_LETTER)
        else:
            task.state = TaskState.DEAD_LETTER
        task.last_error = detail
        task.settlement_reason = NackReason.ERR_PREREQUISITE_FAILED
        msg.body = task.to_json()
        msg.attributes["dlq_reason"] = NackReason.ERR_PREREQUISITE_FAILED.value
        msg.attributes["dlq_at"] = datetime.now(UTC).isoformat()
        snapshot_task(task, repo_path=self.repo_path, actor_id=f"queue:{self.name}")
        self._emit_task_event(
            task,
            state_from,
            TaskState.DEAD_LETTER,
            reason=NackReason.ERR_PREREQUISITE_FAILED.value,
            detail=detail,
        )
        append_trace_span(
            trace_id=task.trace_id,
            service="queue",
            operation="dependency_failed",
            actor_id=f"queue:{self.name}",
            task_id=task.id,
            attempt_id=task.attempt_id,
            attributes={"failed_by_task_ids": failed_by},
        )
        self._dead_letter.append(msg)

    def _sync_dependency_state(self, msg: QueueMessage, blocked_by: list[str]) -> None:
        try:
            task = TaskContract.from_json(msg.body)
        except Exception:
            return

        detail = (
            f"Waiting on prerequisite task(s): {', '.join(blocked_by)}"
            if blocked_by
            else None
        )
        if blocked_by and task.state != TaskState.BLOCKED:
            state_from = task.state
            if task.can_transition_to(TaskState.BLOCKED):
                task.transition_to(TaskState.BLOCKED)
            else:
                task.state = TaskState.BLOCKED
            task.last_error = detail
            msg.body = task.to_json()
            snapshot_task(task, repo_path=self.repo_path, actor_id=f"queue:{self.name}")
            self._emit_task_event(
                task,
                state_from,
                TaskState.BLOCKED,
                reason="DEPENDENCY_WAIT",
                detail=detail,
            )
            append_trace_span(
                trace_id=task.trace_id,
                service="queue",
                operation="dependency_blocked",
                actor_id=f"queue:{self.name}",
                task_id=task.id,
                attempt_id=task.attempt_id,
                attributes={"blocked_by_task_ids": blocked_by},
            )
            return

        if not blocked_by and task.state == TaskState.BLOCKED:
            prev_state = task.state
            if task.can_transition_to(TaskState.QUEUED):
                task.transition_to(TaskState.QUEUED)
            else:
                task.state = TaskState.QUEUED
            task.last_error = None
            msg.body = task.to_json()
            snapshot_task(task, repo_path=self.repo_path, actor_id=f"queue:{self.name}")
            self._emit_task_event(
                task,
                prev_state,
                TaskState.QUEUED,
                reason="DEPENDENCY_READY",
            )
            append_trace_span(
                trace_id=task.trace_id,
                service="queue",
                operation="dependency_unblocked",
                actor_id=f"queue:{self.name}",
                task_id=task.id,
                attempt_id=task.attempt_id,
                attributes={"blocked_by_task_ids": []},
            )

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def publish(self, task: TaskContract, priority: int = 5) -> str:
        msg_id = str(uuid.uuid4())
        message = QueueMessage(
            id=msg_id,
            body=task.to_json(),
            receipt_handle="",  # Set on receive
            receive_count=0,
            enqueued_at=datetime.now(UTC),
            attributes={
                "priority": priority,
                "task_id": task.id,
                "trace_id": task.trace_id,
                "attempt_id": task.attempt_id,
                "intent_anchor_id": task.intent_anchor_id,
                "provider": "memory",
            },
            provider="memory",
        )
        with self._lock:
            # Insert based on priority (higher priority at front)
            inserted = False
            for i, (_, existing) in enumerate(self._queue):
                if message.attributes["priority"] > existing.attributes.get("priority", 5):
                    self._queue.insert(i, (priority, message))
                    inserted = True
                    break
            if not inserted:
                self._queue.append((priority, message))
        return msg_id

    async def receive(
        self,
        max_messages: int = 1,
        visibility_timeout: int = 300,
    ) -> list[QueueMessage]:
        self._return_expired_messages()

        messages: list[QueueMessage] = []
        with self._lock:
            inflight_task_ids = {
                inflight_msg.attributes.get("task_id")
                for inflight_msg, _ in self._inflight.values()
                if inflight_msg.attributes.get("task_id")
            }
            inspected = 0
            max_inspect = len(self._queue)

            while (
                len(messages) < max_messages
                and self._queue
                and inspected < max_inspect
            ):
                priority, msg = self._queue.popleft()
                inspected += 1
                task_id = msg.attributes.get("task_id")

                # Single-flight guard: do not deliver same task_id concurrently.
                if task_id and task_id in inflight_task_ids:
                    self._queue.append((priority, msg))
                    continue

                blocked_by, failed_by = self._dependency_status(msg)
                if failed_by:
                    self._fail_dependency_task(msg, failed_by)
                    continue
                self._sync_dependency_state(msg, blocked_by)
                if blocked_by:
                    msg.attributes["blocked_by_task_ids"] = blocked_by
                    self._queue.append((priority, msg))
                    continue
                msg.attributes.pop("blocked_by_task_ids", None)

                receipt = str(uuid.uuid4())
                msg.receipt_handle = receipt
                msg.receive_count += 1
                expiry_ts = time.time() + visibility_timeout
                msg.lease_expires_at = datetime.fromtimestamp(expiry_ts, tz=UTC)
                self._inflight[receipt] = (msg, expiry_ts)
                if task_id:
                    inflight_task_ids.add(task_id)
                messages.append(msg)
        return messages

    async def ack(self, receipt_handle: str) -> None:
        with self._lock:
            if receipt_handle in self._inflight:
                del self._inflight[receipt_handle]

    async def nack(self, receipt_handle: str, reason: str, delay: int = 0) -> None:
        if not reason:
            raise ValueError("nack reason is required")
        with self._lock:
            if receipt_handle in self._inflight:
                msg, _ = self._inflight.pop(receipt_handle)
                msg.receipt_handle = ""
                msg.attributes["last_nack_reason"] = reason
                # Add back to queue after delay (simplified - immediate for in-memory)
                priority = msg.attributes.get("priority", 5)
                self._queue.append((priority, msg))

    async def dead_letter(self, receipt_handle: str, reason: str) -> None:
        if not reason:
            raise ValueError("dead-letter reason is required")
        with self._lock:
            if receipt_handle in self._inflight:
                msg, _ = self._inflight.pop(receipt_handle)
                msg.receipt_handle = ""
                msg.attributes["dlq_reason"] = reason
                msg.attributes["dlq_at"] = datetime.now(UTC).isoformat()
                self._dead_letter.append(msg)

    async def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        with self._lock:
            if receipt_handle in self._inflight:
                msg, _ = self._inflight[receipt_handle]
                expiry_ts = time.time() + seconds
                msg.lease_expires_at = datetime.fromtimestamp(expiry_ts, tz=UTC)
                self._inflight[receipt_handle] = (msg, expiry_ts)

    async def get_queue_depth(self) -> int:
        self._return_expired_messages()
        with self._lock:
            return len(self._queue)

    async def get_dead_letter_depth(self) -> int:
        with self._lock:
            return len(self._dead_letter)

    async def purge(self) -> None:
        with self._lock:
            self._queue.clear()
            self._inflight.clear()
            self._dead_letter.clear()

    def supports_depth(self) -> bool:
        return True

    def supports_dead_letter(self) -> bool:
        return True

    def _return_expired_messages(self) -> None:
        """Return messages whose visibility timeout has expired."""
        now = time.time()
        with self._lock:
            expired = [
                (handle, msg)
                for handle, (msg, expiry) in self._inflight.items()
                if expiry < now
            ]
            for handle, msg in expired:
                del self._inflight[handle]
                msg.receipt_handle = ""
                msg.lease_expires_at = None
                priority = msg.attributes.get("priority", 5)
                self._queue.appendleft((priority, msg))


class GCPPubSubQueue(QueueAdapter):
    """
    Google Cloud Pub/Sub queue adapter.

    Requires:
        - google-cloud-pubsub package
        - GOOGLE_APPLICATION_CREDENTIALS or workload identity
    """

    def __init__(
        self,
        project_id: str,
        topic_id: str = "mx-tasks",
        subscription_id: str = "mx-worker-sub",
    ):
        self.project_id = project_id
        self.topic_id = topic_id
        self.subscription_id = subscription_id
        self._publisher: Any | None = None
        self._subscriber: Any | None = None
        self._topic_path: str | None = None
        self._subscription_path: str | None = None

    async def connect(self) -> None:
        try:
            from google.cloud import pubsub_v1
        except ImportError:
            raise ImportError(
                "google-cloud-pubsub required. Install with: pip install google-cloud-pubsub"
            )

        self._publisher = pubsub_v1.PublisherClient()
        self._subscriber = pubsub_v1.SubscriberClient()
        publisher = self._publisher
        subscriber = self._subscriber
        if publisher is None or subscriber is None:
            raise RuntimeError("Failed to initialize Pub/Sub clients")

        self._topic_path = publisher.topic_path(self.project_id, self.topic_id)
        self._subscription_path = subscriber.subscription_path(
            self.project_id, self.subscription_id
        )

        # Ensure topic and subscription exist
        try:
            publisher.create_topic(request={"name": self._topic_path})
        except Exception:
            pass  # Topic already exists

        try:
            subscriber.create_subscription(
                request={
                    "name": self._subscription_path,
                    "topic": self._topic_path,
                    "ack_deadline_seconds": 300,
                }
            )
        except Exception:
            pass  # Subscription already exists

    async def disconnect(self) -> None:
        if self._publisher:
            self._publisher.close()
        if self._subscriber:
            self._subscriber.close()

    async def publish(self, task: TaskContract, priority: int = 5) -> str:
        if not self._publisher or not self._topic_path:
            raise RuntimeError("Queue not connected")

        data = task.to_json().encode("utf-8")
        future = self._publisher.publish(
            self._topic_path,
            data,
            task_id=task.id,
            priority=str(priority),
            trace_id=task.trace_id,
            attempt_id=task.attempt_id,
            intent_anchor_id=task.intent_anchor_id,
            provider="gcp",
        )
        return str(future.result())

    async def receive(
        self,
        max_messages: int = 1,
        visibility_timeout: int = 300,
    ) -> list[QueueMessage]:
        if not self._subscriber or not self._subscription_path:
            raise RuntimeError("Queue not connected")

        from google.cloud.pubsub_v1.types import PullRequest

        response = self._subscriber.pull(
            request=PullRequest(
                subscription=self._subscription_path,
                max_messages=max_messages,
            ),
            timeout=30,
        )

        messages = []
        for received in response.received_messages:
            publish_time = received.message.publish_time
            lease_expires_at = datetime.fromtimestamp(time.time() + visibility_timeout, tz=UTC)
            msg = QueueMessage(
                id=received.message.message_id,
                body=received.message.data.decode("utf-8"),
                receipt_handle=received.ack_id,
                receive_count=received.delivery_attempt or 1,
                enqueued_at=publish_time.replace(tzinfo=UTC),
                attributes=dict(received.message.attributes),
                provider="gcp",
                lease_expires_at=lease_expires_at,
            )
            messages.append(msg)
            await self.extend_visibility(received.ack_id, visibility_timeout)

        return messages

    async def ack(self, receipt_handle: str) -> None:
        if not self._subscriber or not self._subscription_path:
            raise RuntimeError("Queue not connected")

        self._subscriber.acknowledge(
            request={
                "subscription": self._subscription_path,
                "ack_ids": [receipt_handle],
            }
        )

    async def nack(self, receipt_handle: str, reason: str, delay: int = 0) -> None:
        if not self._subscriber or not self._subscription_path:
            raise RuntimeError("Queue not connected")
        if not reason:
            raise ValueError("nack reason is required")

        self._subscriber.modify_ack_deadline(
            request={
                "subscription": self._subscription_path,
                "ack_ids": [receipt_handle],
                "ack_deadline_seconds": delay,
            }
        )

    async def dead_letter(self, receipt_handle: str, reason: str) -> None:
        if not reason:
            raise ValueError("dead-letter reason is required")
        # Pub/Sub dead-letter routing is configured at subscription level.
        # For now we settle the message to avoid infinite hot-loop retries.
        await self.ack(receipt_handle)

    async def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        if not self._subscriber or not self._subscription_path:
            raise RuntimeError("Queue not connected")
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        self._subscriber.modify_ack_deadline(
            request={
                "subscription": self._subscription_path,
                "ack_ids": [receipt_handle],
                "ack_deadline_seconds": seconds,
            }
        )

    async def get_queue_depth(self) -> int:
        # Pub/Sub doesn't have direct queue depth API
        # Would need monitoring API for accurate count
        return -1  # Unknown

    async def purge(self) -> None:
        if not self._subscriber or not self._subscription_path:
            raise RuntimeError("Queue not connected")

        from google.cloud.pubsub_v1.types import SeekRequest
        from google.protobuf.timestamp_pb2 import Timestamp

        # Seek to now to skip all existing messages
        now = Timestamp()
        now.FromDatetime(datetime.now(UTC))
        self._subscriber.seek(
            request=SeekRequest(
                subscription=self._subscription_path,
                time=now,
            )
        )

    def supports_dead_letter(self) -> bool:
        # Native DLQ is subscription-config dependent and not guaranteed by this adapter.
        return False


class AzureServiceBusQueue(QueueAdapter):
    """
    Azure Service Bus queue adapter.

    Requires:
        - azure-servicebus package
        - Connection string or managed identity
    """

    def __init__(
        self,
        connection_string: str | None = None,
        namespace: str | None = None,
        queue_name: str = "mx-tasks",
    ):
        self.connection_string = connection_string
        self.namespace = namespace
        self.queue_name = queue_name
        self._client: Any | None = None
        self._sender: Any | None = None
        self._receiver: Any | None = None
        self._inflight: dict[str, Any] = {}

    async def connect(self) -> None:
        try:
            from azure.identity.aio import DefaultAzureCredential
            from azure.servicebus.aio import ServiceBusClient
        except ImportError:
            raise ImportError(
                "azure-servicebus required. Install with: pip install azure-servicebus azure-identity"
            )

        if self.connection_string:
            self._client = ServiceBusClient.from_connection_string(self.connection_string)
        else:
            credential = DefaultAzureCredential()
            self._client = ServiceBusClient(
                fully_qualified_namespace=f"{self.namespace}.servicebus.windows.net",
                credential=credential,
            )

        client = self._client
        if client is None:
            raise RuntimeError("Failed to initialize Azure Service Bus client")

        self._sender = client.get_queue_sender(self.queue_name)
        self._receiver = client.get_queue_receiver(self.queue_name)

    async def disconnect(self) -> None:
        if self._sender:
            await self._sender.close()
        if self._receiver:
            await self._receiver.close()
        if self._client:
            await self._client.close()
        self._inflight.clear()

    async def publish(self, task: TaskContract, priority: int = 5) -> str:
        if not self._sender:
            raise RuntimeError("Queue not connected")

        from azure.servicebus import ServiceBusMessage

        msg = ServiceBusMessage(
            body=task.to_json(),
            application_properties={
                "task_id": task.id,
                "priority": priority,
                "trace_id": task.trace_id,
                "attempt_id": task.attempt_id,
                "intent_anchor_id": task.intent_anchor_id,
                "provider": "azure",
            },
        )
        await self._sender.send_messages(msg)
        return task.id

    async def receive(
        self,
        max_messages: int = 1,
        visibility_timeout: int = 300,
    ) -> list[QueueMessage]:
        if not self._receiver:
            raise RuntimeError("Queue not connected")

        received = await self._receiver.receive_messages(
            max_message_count=max_messages,
            max_wait_time=30,
        )

        messages = []
        for msg in received:
            lock_token = str(msg.lock_token)
            queue_msg = QueueMessage(
                id=str(msg.message_id),
                body=str(msg),
                receipt_handle=lock_token,
                receive_count=msg.delivery_count,
                enqueued_at=msg.enqueued_time_utc,
                attributes=dict(msg.application_properties or {}),
                provider="azure",
                lease_expires_at=getattr(msg, "locked_until_utc", None),
            )
            messages.append(queue_msg)
            self._inflight[lock_token] = msg

        return messages

    async def ack(self, receipt_handle: str) -> None:
        if not self._receiver:
            raise RuntimeError("Queue not connected")
        msg = self._inflight.pop(receipt_handle, None)
        if msg is None:
            return
        await self._receiver.complete_message(msg)

    async def nack(self, receipt_handle: str, reason: str, delay: int = 0) -> None:
        if not self._receiver:
            raise RuntimeError("Queue not connected")
        if not reason:
            raise ValueError("nack reason is required")
        msg = self._inflight.pop(receipt_handle, None)
        if msg is None:
            return
        await self._receiver.abandon_message(msg)

    async def dead_letter(self, receipt_handle: str, reason: str) -> None:
        if not self._receiver:
            raise RuntimeError("Queue not connected")
        if not reason:
            raise ValueError("dead-letter reason is required")
        msg = self._inflight.pop(receipt_handle, None)
        if msg is None:
            return
        await self._receiver.dead_letter_message(msg, reason=reason)

    async def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        if not self._receiver:
            raise RuntimeError("Queue not connected")
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        msg = self._inflight.get(receipt_handle)
        if msg is None:
            return
        await self._receiver.renew_message_lock(msg)

    async def get_queue_depth(self) -> int:
        # Would need management client for this
        return -1

    async def purge(self) -> None:
        if not self._receiver:
            raise RuntimeError("Queue not connected")

        # Receive and discard all messages
        while True:
            messages = await self._receiver.receive_messages(max_message_count=100, max_wait_time=5)
            if not messages:
                break
            for msg in messages:
                await self._receiver.complete_message(msg)

    def supports_dead_letter(self) -> bool:
        return True


def create_queue_adapter(config: dict) -> QueueAdapter:
    """
    Factory function to create queue adapter based on config.

    Config example:
    {
        "provider": "gcp",  # or "azure", "memory"
        "gcp": {
            "project_id": "my-project",
            "topic_id": "mx-tasks",
            "subscription_id": "mx-worker-sub"
        },
        "azure": {
            "connection_string": "...",
            "queue_name": "mx-tasks"
        }
    }
    """
    provider = config.get("provider", "memory")

    if provider == "memory":
        return InMemoryQueue(
            config.get("queue_name", "mx-tasks"),
            repo_path=config.get("repo_path", "."),
        )

    elif provider == "gcp":
        gcp_config = config.get("gcp", {})
        return GCPPubSubQueue(
            project_id=gcp_config["project_id"],
            topic_id=gcp_config.get("topic_id", "mx-tasks"),
            subscription_id=gcp_config.get("subscription_id", "mx-worker-sub"),
        )

    elif provider == "azure":
        azure_config = config.get("azure", {})
        return AzureServiceBusQueue(
            connection_string=azure_config.get("connection_string"),
            namespace=azure_config.get("namespace"),
            queue_name=azure_config.get("queue_name", "mx-tasks"),
        )

    else:
        raise ValueError(f"Unknown queue provider: {provider}")
