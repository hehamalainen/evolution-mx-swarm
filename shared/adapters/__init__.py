"""Cloud adapters for MX Swarm."""

from .queue import (
    AzureServiceBusQueue,
    GCPPubSubQueue,
    InMemoryQueue,
    QueueAdapter,
    QueueMessage,
    create_queue_adapter,
)
from .secrets import (
    AzureKeyVault,
    EnvSecretsAdapter,
    GCPSecretManager,
    SecretsAdapter,
    create_secrets_adapter,
)

__all__ = [
    "QueueAdapter",
    "QueueMessage",
    "InMemoryQueue",
    "GCPPubSubQueue",
    "AzureServiceBusQueue",
    "create_queue_adapter",
    "SecretsAdapter",
    "EnvSecretsAdapter",
    "GCPSecretManager",
    "AzureKeyVault",
    "create_secrets_adapter",
]
