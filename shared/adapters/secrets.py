"""Secrets adapter abstraction for MX Swarm.

Provides cloud-agnostic secrets interface with implementations for:
- GCP Secret Manager
- Azure Key Vault
- Environment variables (local dev)
"""

from abc import ABC, abstractmethod
from typing import Optional
import os


class SecretsAdapter(ABC):
    """Abstract base class for secrets adapters."""

    @abstractmethod
    async def connect(self) -> None:
        """Initialize connection to secrets service."""
        pass

    @abstractmethod
    async def get_secret(self, name: str, version: str = "latest") -> str:
        """
        Retrieve a secret value.

        Args:
            name: Secret name/key
            version: Secret version (default: latest)

        Returns:
            Secret value as string

        Raises:
            KeyError: If secret not found
        """
        pass

    @abstractmethod
    async def set_secret(self, name: str, value: str) -> None:
        """
        Store a secret value.

        Args:
            name: Secret name/key
            value: Secret value
        """
        pass

    @abstractmethod
    async def delete_secret(self, name: str) -> None:
        """Delete a secret."""
        pass

    @abstractmethod
    async def list_secrets(self) -> list[str]:
        """List all secret names."""
        pass


class EnvSecretsAdapter(SecretsAdapter):
    """
    Environment variable-based secrets for local development.

    Optionally loads from .env file.
    """

    def __init__(self, prefix: str = "MX_", env_file: Optional[str] = None):
        self.prefix = prefix
        self.env_file = env_file
        self._cache: dict[str, str] = {}

    async def connect(self) -> None:
        # Load from .env file if specified
        if self.env_file and os.path.exists(self.env_file):
            with open(self.env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        self._cache[key] = value

        # Also load from environment
        for key, value in os.environ.items():
            if key.startswith(self.prefix):
                self._cache[key] = value

    async def get_secret(self, name: str, version: str = "latest") -> str:
        # Try with prefix first
        prefixed = f"{self.prefix}{name}"
        if prefixed in self._cache:
            return self._cache[prefixed]
        if prefixed in os.environ:
            return os.environ[prefixed]

        # Try without prefix
        if name in self._cache:
            return self._cache[name]
        if name in os.environ:
            return os.environ[name]

        raise KeyError(f"Secret not found: {name}")

    async def set_secret(self, name: str, value: str) -> None:
        prefixed = f"{self.prefix}{name}"
        self._cache[prefixed] = value
        os.environ[prefixed] = value

    async def delete_secret(self, name: str) -> None:
        prefixed = f"{self.prefix}{name}"
        self._cache.pop(prefixed, None)
        self._cache.pop(name, None)
        os.environ.pop(prefixed, None)
        os.environ.pop(name, None)

    async def list_secrets(self) -> list[str]:
        secrets = set()
        for key in self._cache:
            if key.startswith(self.prefix):
                secrets.add(key[len(self.prefix):])
            else:
                secrets.add(key)
        return sorted(secrets)


class GCPSecretManager(SecretsAdapter):
    """
    Google Cloud Secret Manager adapter.

    Requires:
        - google-cloud-secret-manager package
        - GOOGLE_APPLICATION_CREDENTIALS or workload identity
    """

    def __init__(self, project_id: str):
        self.project_id = project_id
        self._client = None

    async def connect(self) -> None:
        try:
            from google.cloud import secretmanager
        except ImportError:
            raise ImportError(
                "google-cloud-secret-manager required. Install with: "
                "pip install google-cloud-secret-manager"
            )

        self._client = secretmanager.SecretManagerServiceClient()

    async def get_secret(self, name: str, version: str = "latest") -> str:
        if not self._client:
            raise RuntimeError("Secrets manager not connected")

        secret_path = f"projects/{self.project_id}/secrets/{name}/versions/{version}"

        try:
            response = self._client.access_secret_version(request={"name": secret_path})
            return response.payload.data.decode("utf-8")
        except Exception as e:
            if "NOT_FOUND" in str(e):
                raise KeyError(f"Secret not found: {name}")
            raise

    async def set_secret(self, name: str, value: str) -> None:
        if not self._client:
            raise RuntimeError("Secrets manager not connected")

        parent = f"projects/{self.project_id}"
        secret_id = name

        # Try to create secret first
        try:
            self._client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        except Exception:
            pass  # Secret already exists

        # Add version
        secret_path = f"projects/{self.project_id}/secrets/{name}"
        self._client.add_secret_version(
            request={
                "parent": secret_path,
                "payload": {"data": value.encode("utf-8")},
            }
        )

    async def delete_secret(self, name: str) -> None:
        if not self._client:
            raise RuntimeError("Secrets manager not connected")

        secret_path = f"projects/{self.project_id}/secrets/{name}"
        try:
            self._client.delete_secret(request={"name": secret_path})
        except Exception:
            pass  # Secret doesn't exist

    async def list_secrets(self) -> list[str]:
        if not self._client:
            raise RuntimeError("Secrets manager not connected")

        parent = f"projects/{self.project_id}"
        secrets = []

        for secret in self._client.list_secrets(request={"parent": parent}):
            # Extract name from path
            name = secret.name.split("/")[-1]
            secrets.append(name)

        return secrets


class AzureKeyVault(SecretsAdapter):
    """
    Azure Key Vault adapter.

    Requires:
        - azure-keyvault-secrets package
        - azure-identity package
    """

    def __init__(self, vault_url: str):
        """
        Args:
            vault_url: Key Vault URL (e.g., https://myvault.vault.azure.net/)
        """
        self.vault_url = vault_url
        self._client = None

    async def connect(self) -> None:
        try:
            from azure.keyvault.secrets import SecretClient
            from azure.identity import DefaultAzureCredential
        except ImportError:
            raise ImportError(
                "azure-keyvault-secrets required. Install with: "
                "pip install azure-keyvault-secrets azure-identity"
            )

        credential = DefaultAzureCredential()
        self._client = SecretClient(vault_url=self.vault_url, credential=credential)

    async def get_secret(self, name: str, version: str = "latest") -> str:
        if not self._client:
            raise RuntimeError("Key Vault not connected")

        try:
            # Key Vault doesn't support "latest" keyword, use None
            ver = None if version == "latest" else version
            secret = self._client.get_secret(name, ver)
            return secret.value
        except Exception as e:
            if "SecretNotFound" in str(e):
                raise KeyError(f"Secret not found: {name}")
            raise

    async def set_secret(self, name: str, value: str) -> None:
        if not self._client:
            raise RuntimeError("Key Vault not connected")

        self._client.set_secret(name, value)

    async def delete_secret(self, name: str) -> None:
        if not self._client:
            raise RuntimeError("Key Vault not connected")

        try:
            poller = self._client.begin_delete_secret(name)
            poller.wait()
        except Exception:
            pass  # Secret doesn't exist

    async def list_secrets(self) -> list[str]:
        if not self._client:
            raise RuntimeError("Key Vault not connected")

        secrets = []
        for secret in self._client.list_properties_of_secrets():
            secrets.append(secret.name)
        return secrets


def create_secrets_adapter(config: dict) -> SecretsAdapter:
    """
    Factory function to create secrets adapter based on config.

    Config example:
    {
        "provider": "gcp",  # or "azure", "env"
        "gcp": {
            "project_id": "my-project"
        },
        "azure": {
            "vault_url": "https://myvault.vault.azure.net/"
        },
        "env": {
            "prefix": "MX_",
            "env_file": ".env"
        }
    }
    """
    provider = config.get("provider", "env")

    if provider == "env":
        env_config = config.get("env", {})
        return EnvSecretsAdapter(
            prefix=env_config.get("prefix", "MX_"),
            env_file=env_config.get("env_file"),
        )

    elif provider == "gcp":
        gcp_config = config.get("gcp", {})
        return GCPSecretManager(project_id=gcp_config["project_id"])

    elif provider == "azure":
        azure_config = config.get("azure", {})
        return AzureKeyVault(vault_url=azure_config["vault_url"])

    else:
        raise ValueError(f"Unknown secrets provider: {provider}")
