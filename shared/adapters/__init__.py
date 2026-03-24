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
from .vcs_adapter import LandingVerificationResult, SubmitResult, VCSAdapter


def _normalize_aether_base_url(base_url: str | None) -> str:
    """Normalize Aether base URL to API root (without trailing /v1)."""
    candidate = (base_url or "").strip().rstrip("/")
    if not candidate:
        return "http://localhost:3000"
    if candidate.endswith("/v1"):
        return candidate[:-3]
    return candidate


def create_vcs_adapter(config: dict) -> VCSAdapter:
    """Factory function to create a VCS adapter from config.

    Args:
        config: Dictionary with at least 'provider' and optionally nested config.
            Example for Aether:
            {
                "provider": "aether",
                "aether": {
                    "base_url": "http://localhost:8100",
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
    provider = str(config.get("provider", "github")).strip().lower()

    if provider == "aether":
        aether_cfg = config.get("aether", {}) if isinstance(config.get("aether"), dict) else {}
        base_url = (
            config.get("aether_base_url")
            or config.get("base_url")
            or aether_cfg.get("base_url")
        )
        repo_id = (
            config.get("aether_repo_id")
            or config.get("repo_id")
            or aether_cfg.get("repo_id")
        )
        agent_id = (
            config.get("aether_agent_id")
            or config.get("agent_id")
            or aether_cfg.get("agent_id")
            or "mx-swarm"
        )

        return AetherVCS(
            base_url=_normalize_aether_base_url(base_url),
            repo_id=repo_id,
            agent_id=agent_id,
        )

    if provider == "github":
        token = config.get("token") or os.environ.get("GITHUB_TOKEN", "")
        return GitHubVCS(
            token=token,
            owner=config.get("owner", ""),
            repo=config.get("repo", ""),
            default_branch=config.get("default_branch", "main"),
        )

    raise ValueError(f"Unknown VCS provider: {provider}")


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
    "LandingVerificationResult",
    "GitHubVCS",
    "AetherVCS",
    "create_aether_vcs",
    "create_vcs_adapter",
]
