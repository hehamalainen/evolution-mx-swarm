import os

from worker.service import Worker
from shared.utils.config import MXConfig


class _StubQueue:
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class _StubLLM:
    pass


class _StubVCS:
    pass


def _make_worker(config: MXConfig) -> Worker:
    return Worker(
        config=config,
        worker_id="test-worker",
        queue=_StubQueue(),
        llm=_StubLLM(),
        vcs=_StubVCS(),
    )


def test_repo_url_aether_mode_ignores_token(monkeypatch) -> None:
    config = MXConfig()
    config.vcs.provider = "aether"
    config.github.token = "token"
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.chdir(os.getcwd())

    worker = _make_worker(config)
    assert worker._get_repo_url() == os.getcwd()


def test_repo_url_github_mode_with_token(monkeypatch) -> None:
    config = MXConfig()
    config.vcs.provider = "github"
    config.github.repo_owner = "owner"
    config.github.repo_name = "repo"
    config.github.token = "token"
    monkeypatch.chdir(os.getcwd())

    worker = _make_worker(config)
    assert worker._get_repo_url() == "https://github.com/owner/repo.git"


def test_repo_url_github_mode_without_token(monkeypatch) -> None:
    config = MXConfig()
    config.vcs.provider = "github"
    config.github.repo_owner = "owner"
    config.github.repo_name = "repo"
    config.github.token = None
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(os.getcwd())

    worker = _make_worker(config)
    assert worker._get_repo_url() == os.getcwd()
