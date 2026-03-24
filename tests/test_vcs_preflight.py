from fastapi.testclient import TestClient

from dashboard import api
from shared.utils.config import MXConfig


def test_vcs_preflight_github_missing_token(monkeypatch) -> None:
    config = MXConfig()
    config.github.repo_owner = "owner"
    config.github.repo_name = "repo"

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("MX_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(api, "load_config", lambda *_args, **_kwargs: config)

    client = TestClient(api.app)
    response = client.post("/api/control/vcs_preflight", json={"vcs_target": "github"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["message"] == "Missing GitHub token"


def test_vcs_preflight_aether_missing_repo_id(monkeypatch) -> None:
    config = MXConfig()
    config.vcs.provider = "aether"
    config.vcs.aether_base_url = "http://localhost:8100"
    config.vcs.aether_repo_id = None

    monkeypatch.setattr(api, "load_config", lambda *_args, **_kwargs: config)

    client = TestClient(api.app)
    response = client.post("/api/control/vcs_preflight", json={"vcs_target": "aether"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["message"] == "Missing Aether repo id"
