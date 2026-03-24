import pytest

from shared.adapters import AetherVCS, GitHubVCS, create_vcs_adapter
from shared.utils import MockGitHubAPI


def test_create_vcs_adapter_accepts_flat_aether_shape() -> None:
    adapter = create_vcs_adapter(
        {
            "provider": "aether",
            "base_url": "http://localhost:8100/v1/",
            "repo_id": "repo-flat",
            "agent_id": "agent-flat",
        }
    )

    assert isinstance(adapter, AetherVCS)
    assert adapter.base_url == "http://localhost:8100"
    assert adapter.repo_id == "repo-flat"
    assert adapter.agent_id == "agent-flat"


def test_create_vcs_adapter_accepts_nested_aether_shape() -> None:
    adapter = create_vcs_adapter(
        {
            "provider": "aether",
            "aether": {
                "base_url": "http://localhost:3000",
                "repo_id": "repo-nested",
                "agent_id": "agent-nested",
            },
        }
    )

    assert isinstance(adapter, AetherVCS)
    assert adapter.base_url == "http://localhost:3000"
    assert adapter.repo_id == "repo-nested"
    assert adapter.agent_id == "agent-nested"


def test_create_vcs_adapter_github_uses_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")

    adapter = create_vcs_adapter(
        {
            "provider": "github",
            "owner": "acme",
            "repo": "mx",
        }
    )

    assert isinstance(adapter, GitHubVCS)
    assert adapter.github.token == "env-token"


def test_create_vcs_adapter_github_without_token_uses_mock_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    adapter = create_vcs_adapter(
        {
            "provider": "github",
            "owner": "acme",
            "repo": "mx",
        }
    )

    assert isinstance(adapter, GitHubVCS)
    assert isinstance(adapter.github, MockGitHubAPI)


def test_create_vcs_adapter_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown VCS provider: invalid"):
        create_vcs_adapter({"provider": "invalid"})
