"""Cloud adapters for MX Swarm."""

import os

from .aether_vcs import AetherVCS, create_aether_vcs
from .github_vcs import GitHubVCS
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
from .vcs_adapter import SubmitResult, VCSAdapter


def create_vcs_adapter(config: dict) -> VCSAdapter:
    """Factory function to create a VCS adapter from config.

    Args:
        config: Dictionary with at least 'provider' and optionally nested config.
            Example for Aether:
            {
                "provider": "aether",
                "aether": {
                    "base_url": "http://localhost:8100/v1",
                    "repo_id": "repo_abc123",
                    "agent_id": "mx-swarm"
                }
            }
            Example for GitHub:
            {
                "provider": "github",
                "token": "...",
                "owner": "owner",
                "repo": "repo",
                "default_branch": "main"
            }

    Returns:
        VCSAdapter instance (AetherVCS or GitHubVCS)
    """
    provider = config.get("provider", "github")

    if provider == "aether":
        aether_cfg = config.get("aether", {})
        return AetherVCS(
            base_url=aether_cfg.get("base_url", "http://localhost:8100/v1"),
            repo_id=aether_cfg.get("repo_id"),
            agent_id=aether_cfg.get("agent_id", "mx-swarm"),
        )
    else:
        # Default to GitHub
        token = config.get("token") or os.environ.get("GITHUB_TOKEN", "")
        return GitHubVCS(
            token=token,
            owner=config.get("owner", ""),
            repo=config.get("repo", ""),
            default_branch=config.get("default_branch", "main"),
        )


__all__ = [
    # Queue
    "QueueAdapter",
    "QueueMessage",
    "InMemoryQueue",
    "GCPPubSubQueue",
    "AzureServiceBusQueue",
    "create_queue_adapter",
    # Secrets
    "SecretsAdapter",
    "EnvSecretsAdapter",
    "GCPSecretManager",
    "AzureKeyVault",
    "create_secrets_adapter",
    # VCS
    "VCSAdapter",
    "SubmitResult",
    "GitHubVCS",
    "AetherVCS",
    "create_aether_vcs",
    "create_vcs_adapter",
]

