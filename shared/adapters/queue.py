"""Queue adapter abstraction for MX Swarm.

Provides cloud-agnostic queue interface with implementations for:
- GCP Pub/Sub
- Azure Service Bus
- In-memory (local dev)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable, Any
import json
import asyncio
import uuid
from collections import deque
import threading
import time

from shared.schemas import TaskContract


@dataclass
class QueueMessage:
    """Wrapper for queue messages with metadata."""
    id: str
    body: str
    receipt_handle: str  # For ack/nack
    receive_count: int
    enqueued_at: datetime
    attributes: dict


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
    async def nack(self, receipt_handle: str, delay: int = 0) -> None:
        """
        Negative acknowledge (return to queue for retry).

        Args:
            receipt_handle: Message receipt handle
            delay: Seconds to delay before message is visible again
        """
        pass

    @abstractmethod
    async def get_queue_depth(self) -> int:
        """Get approximate number of messages in queue."""
        pass

    @abstractmethod
    async def purge(self) -> None:
        """Remove all messages from queue (use with caution)."""
        pass

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
    _instances: dict[str, "InMemoryQueue"] = {}
    _instance_lock = threading.Lock()

    def __new__(cls, name: str = "mx-tasks"):
        """Return existing instance for queue name, or create new one."""
        with cls._instance_lock:
            if name not in cls._instances:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instances[name] = instance
            return cls._instances[name]

    def __init__(self, name: str = "mx-tasks"):
        # Only initialize once per instance
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.name = name
        self._queue: deque = deque()
        self._inflight: dict[str, tuple[QueueMessage, float]] = {}
        self._lock = threading.Lock()
        self._connected = False

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
            enqueued_at=datetime.utcnow(),
            attributes={"priority": priority, "task_id": task.id},
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

        messages = []
        with self._lock:
            for _ in range(min(max_messages, len(self._queue))):
                if self._queue:
                    _, msg = self._queue.popleft()
                    receipt = str(uuid.uuid4())
                    msg.receipt_handle = receipt
                    msg.receive_count += 1
                    self._inflight[receipt] = (msg, time.time() + visibility_timeout)
                    messages.append(msg)
        return messages

    async def ack(self, receipt_handle: str) -> None:
        with self._lock:
            if receipt_handle in self._inflight:
                del self._inflight[receipt_handle]

    async def nack(self, receipt_handle: str, delay: int = 0) -> None:
        with self._lock:
            if receipt_handle in self._inflight:
                msg, _ = self._inflight.pop(receipt_handle)
                msg.receipt_handle = ""
                # Add back to queue after delay (simplified - immediate for in-memory)
                priority = msg.attributes.get("priority", 5)
                self._queue.append((priority, msg))

    async def get_queue_depth(self) -> int:
        self._return_expired_messages()
        with self._lock:
            return len(self._queue)

    async def purge(self) -> None:
        with self._lock:
            self._queue.clear()
            self._inflight.clear()

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
        self._publisher = None
        self._subscriber = None
        self._topic_path = None
        self._subscription_path = None

    async def connect(self) -> None:
        try:
            from google.cloud import pubsub_v1
        except ImportError:
            raise ImportError(
                "google-cloud-pubsub required. Install with: pip install google-cloud-pubsub"
            )

        self._publisher = pubsub_v1.PublisherClient()
        self._subscriber = pubsub_v1.SubscriberClient()
        self._topic_path = self._publisher.topic_path(self.project_id, self.topic_id)
        self._subscription_path = self._subscriber.subscription_path(
            self.project_id, self.subscription_id
        )

        # Ensure topic and subscription exist
        try:
            self._publisher.create_topic(request={"name": self._topic_path})
        except Exception:
            pass  # Topic already exists

        try:
            self._subscriber.create_subscription(
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
        if not self._publisher:
            raise RuntimeError("Queue not connected")

        data = task.to_json().encode("utf-8")
        future = self._publisher.publish(
            self._topic_path,
            data,
            task_id=task.id,
            priority=str(priority),
        )
        return future.result()

    async def receive(
        self,
        max_messages: int = 1,
        visibility_timeout: int = 300,
    ) -> list[QueueMessage]:
        if not self._subscriber:
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
            msg = QueueMessage(
                id=received.message.message_id,
                body=received.message.data.decode("utf-8"),
                receipt_handle=received.ack_id,
                receive_count=received.delivery_attempt or 1,
                enqueued_at=received.message.publish_time.replace(tzinfo=None),
                attributes=dict(received.message.attributes),
            )
            messages.append(msg)

        return messages

    async def ack(self, receipt_handle: str) -> None:
        if not self._subscriber:
            raise RuntimeError("Queue not connected")

        self._subscriber.acknowledge(
            request={
                "subscription": self._subscription_path,
                "ack_ids": [receipt_handle],
            }
        )

    async def nack(self, receipt_handle: str, delay: int = 0) -> None:
        if not self._subscriber:
            raise RuntimeError("Queue not connected")

        self._subscriber.modify_ack_deadline(
            request={
                "subscription": self._subscription_path,
                "ack_ids": [receipt_handle],
                "ack_deadline_seconds": delay,
            }
        )

    async def get_queue_depth(self) -> int:
        # Pub/Sub doesn't have direct queue depth API
        # Would need monitoring API for accurate count
        return -1  # Unknown

    async def purge(self) -> None:
        if not self._subscriber:
            raise RuntimeError("Queue not connected")

        from google.cloud.pubsub_v1.types import SeekRequest
        from google.protobuf.timestamp_pb2 import Timestamp
        import datetime

        # Seek to now to skip all existing messages
        now = Timestamp()
        now.FromDatetime(datetime.datetime.utcnow())
        self._subscriber.seek(
            request=SeekRequest(
                subscription=self._subscription_path,
                time=now,
            )
        )


class AzureServiceBusQueue(QueueAdapter):
    """
    Azure Service Bus queue adapter.

    Requires:
        - azure-servicebus package
        - Connection string or managed identity
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        namespace: Optional[str] = None,
        queue_name: str = "mx-tasks",
    ):
        self.connection_string = connection_string
        self.namespace = namespace
        self.queue_name = queue_name
        self._client = None
        self._sender = None
        self._receiver = None

    async def connect(self) -> None:
        try:
            from azure.servicebus.aio import ServiceBusClient
            from azure.identity.aio import DefaultAzureCredential
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

        self._sender = self._client.get_queue_sender(self.queue_name)
        self._receiver = self._client.get_queue_receiver(self.queue_name)

    async def disconnect(self) -> None:
        if self._sender:
            await self._sender.close()
        if self._receiver:
            await self._receiver.close()
        if self._client:
            await self._client.close()

    async def publish(self, task: TaskContract, priority: int = 5) -> str:
        if not self._sender:
            raise RuntimeError("Queue not connected")

        from azure.servicebus import ServiceBusMessage

        msg = ServiceBusMessage(
            body=task.to_json(),
            application_properties={"task_id": task.id, "priority": priority},
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
            queue_msg = QueueMessage(
                id=str(msg.message_id),
                body=str(msg),
                receipt_handle=msg.lock_token,
                receive_count=msg.delivery_count,
                enqueued_at=msg.enqueued_time_utc,
                attributes=dict(msg.application_properties or {}),
            )
            messages.append(queue_msg)
            # Store original message for ack/nack
            queue_msg._original = msg

        return messages

    async def ack(self, receipt_handle: str) -> None:
        # Azure requires the original message object
        # This is handled via the _original attribute
        pass  # Completed via context manager in real implementation

    async def nack(self, receipt_handle: str, delay: int = 0) -> None:
        # Azure handles this via abandon() on the message
        pass

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
        return InMemoryQueue(config.get("queue_name", "mx-tasks"))

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
