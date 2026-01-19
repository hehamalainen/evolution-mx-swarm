"""Configuration management for MX Swarm."""

from dataclasses import dataclass, field
from typing import Optional
import json
import os


@dataclass
class QueueConfig:
    """Queue configuration."""
    provider: str = "memory"  # memory, gcp, azure

    # GCP config
    gcp_project_id: Optional[str] = None
    gcp_topic_id: str = "mx-tasks"
    gcp_subscription_id: str = "mx-worker-sub"

    # Azure config
    azure_connection_string: Optional[str] = None
    azure_namespace: Optional[str] = None
    azure_queue_name: str = "mx-tasks"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "gcp": {
                "project_id": self.gcp_project_id,
                "topic_id": self.gcp_topic_id,
                "subscription_id": self.gcp_subscription_id,
            },
            "azure": {
                "connection_string": self.azure_connection_string,
                "namespace": self.azure_namespace,
                "queue_name": self.azure_queue_name,
            },
        }


@dataclass
class SecretsConfig:
    """Secrets configuration."""
    provider: str = "env"  # env, gcp, azure

    # Env config
    env_prefix: str = "MX_"
    env_file: Optional[str] = ".env"

    # GCP config
    gcp_project_id: Optional[str] = None

    # Azure config
    azure_vault_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "env": {
                "prefix": self.env_prefix,
                "env_file": self.env_file,
            },
            "gcp": {
                "project_id": self.gcp_project_id,
            },
            "azure": {
                "vault_url": self.azure_vault_url,
            },
        }


@dataclass
class LLMConfig:
    """LLM configuration."""
    provider: str = "anthropic"  # anthropic, openai, ollama
    model: str = "claude-sonnet-4-20250514"
    api_key: Optional[str] = None  # Will use env var if not set
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.7

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


@dataclass
class WorkerConfig:
    """Worker configuration."""
    max_workers: int = 10
    timebox_minutes: int = 60
    retry_max: int = 3
    poll_interval_seconds: int = 5
    visibility_timeout_seconds: int = 300

    def to_dict(self) -> dict:
        return {
            "max_workers": self.max_workers,
            "timebox_minutes": self.timebox_minutes,
            "retry_max": self.retry_max,
            "poll_interval_seconds": self.poll_interval_seconds,
            "visibility_timeout_seconds": self.visibility_timeout_seconds,
        }


@dataclass
class JudgeConfig:
    """Judge configuration."""
    interval_minutes: int = 60
    failure_rate_threshold: float = 0.3
    no_progress_timeout_minutes: int = 60
    auto_reset_enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "interval_minutes": self.interval_minutes,
            "failure_rate_threshold": self.failure_rate_threshold,
            "no_progress_timeout_minutes": self.no_progress_timeout_minutes,
            "auto_reset_enabled": self.auto_reset_enabled,
        }


@dataclass
class GitHubConfig:
    """GitHub configuration."""
    repo_owner: str = ""
    repo_name: str = ""
    default_branch: str = "main"
    token: Optional[str] = None  # Will use GITHUB_TOKEN env var if not set
    auto_merge_low_risk: bool = False

    def to_dict(self) -> dict:
        return {
            "repo_owner": self.repo_owner,
            "repo_name": self.repo_name,
            "default_branch": self.default_branch,
            "token": self.token,
            "auto_merge_low_risk": self.auto_merge_low_risk,
        }


@dataclass
class BudgetConfig:
    """Budget and limits configuration."""
    daily_usd_cap: float = 100.0
    max_llm_calls_per_task: int = 50
    max_tokens_per_task: int = 100000

    def to_dict(self) -> dict:
        return {
            "daily_usd_cap": self.daily_usd_cap,
            "max_llm_calls_per_task": self.max_llm_calls_per_task,
            "max_tokens_per_task": self.max_tokens_per_task,
        }


@dataclass
class MXConfig:
    """Main MX Swarm configuration."""
    version: str = "1.0.0"
    queue: QueueConfig = field(default_factory=QueueConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    # State
    paused: bool = False

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "queue": self.queue.to_dict(),
            "secrets": self.secrets.to_dict(),
            "llm": self.llm.to_dict(),
            "worker": self.worker.to_dict(),
            "judge": self.judge.to_dict(),
            "github": self.github.to_dict(),
            "budget": self.budget.to_dict(),
            "paused": self.paused,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "MXConfig":
        config = cls()
        config.version = data.get("version", "1.0.0")
        config.paused = data.get("paused", False)

        if "queue" in data:
            q = data["queue"]
            config.queue = QueueConfig(
                provider=q.get("provider", "memory"),
                gcp_project_id=q.get("gcp", {}).get("project_id"),
                gcp_topic_id=q.get("gcp", {}).get("topic_id", "mx-tasks"),
                gcp_subscription_id=q.get("gcp", {}).get("subscription_id", "mx-worker-sub"),
                azure_connection_string=q.get("azure", {}).get("connection_string"),
                azure_namespace=q.get("azure", {}).get("namespace"),
                azure_queue_name=q.get("azure", {}).get("queue_name", "mx-tasks"),
            )

        if "secrets" in data:
            s = data["secrets"]
            config.secrets = SecretsConfig(
                provider=s.get("provider", "env"),
                env_prefix=s.get("env", {}).get("prefix", "MX_"),
                env_file=s.get("env", {}).get("env_file", ".env"),
                gcp_project_id=s.get("gcp", {}).get("project_id"),
                azure_vault_url=s.get("azure", {}).get("vault_url"),
            )

        if "llm" in data:
            l = data["llm"]
            config.llm = LLMConfig(
                provider=l.get("provider", "anthropic"),
                model=l.get("model", "claude-sonnet-4-20250514"),
                api_key=l.get("api_key"),
                base_url=l.get("base_url"),
                max_tokens=l.get("max_tokens", 4096),
                temperature=l.get("temperature", 0.7),
            )

        if "worker" in data:
            w = data["worker"]
            config.worker = WorkerConfig(
                max_workers=w.get("max_workers", 10),
                timebox_minutes=w.get("timebox_minutes", 60),
                retry_max=w.get("retry_max", 3),
                poll_interval_seconds=w.get("poll_interval_seconds", 5),
                visibility_timeout_seconds=w.get("visibility_timeout_seconds", 300),
            )

        if "judge" in data:
            j = data["judge"]
            config.judge = JudgeConfig(
                interval_minutes=j.get("interval_minutes", 60),
                failure_rate_threshold=j.get("failure_rate_threshold", 0.3),
                no_progress_timeout_minutes=j.get("no_progress_timeout_minutes", 60),
                auto_reset_enabled=j.get("auto_reset_enabled", True),
            )

        if "github" in data:
            g = data["github"]
            config.github = GitHubConfig(
                repo_owner=g.get("repo_owner", ""),
                repo_name=g.get("repo_name", ""),
                default_branch=g.get("default_branch", "main"),
                token=g.get("token"),
                auto_merge_low_risk=g.get("auto_merge_low_risk", False),
            )

        if "budget" in data:
            b = data["budget"]
            config.budget = BudgetConfig(
                daily_usd_cap=b.get("daily_usd_cap", 100.0),
                max_llm_calls_per_task=b.get("max_llm_calls_per_task", 50),
                max_tokens_per_task=b.get("max_tokens_per_task", 100000),
            )

        return config

    @classmethod
    def from_json(cls, json_str: str) -> "MXConfig":
        return cls.from_dict(json.loads(json_str))

    def save(self, path: str = "mx.config.json") -> None:
        """Save config to file."""
        with open(path, "w") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str = "mx.config.json") -> "MXConfig":
        """Load config from file."""
        if not os.path.exists(path):
            return cls()  # Return default config

        with open(path) as f:
            return cls.from_json(f.read())

    @classmethod
    def load_or_create(cls, path: str = "mx.config.json") -> "MXConfig":
        """Load config from file or create default."""
        if os.path.exists(path):
            return cls.load(path)

        config = cls()
        config.save(path)
        return config
