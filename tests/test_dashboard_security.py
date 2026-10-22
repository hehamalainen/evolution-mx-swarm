from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dashboard.api as dashboard_api
from shared.utils.config import MXConfig


def test_dashboard_bind_rejects_non_loopback_without_token(monkeypatch) -> None:
    monkeypatch.delenv("MX_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MX_DASHBOARD_TOKEN"):
        dashboard_api.assert_dashboard_bind_safe("0.0.0.0")

    dashboard_api.assert_dashboard_bind_safe("127.0.0.1")
    dashboard_api.assert_dashboard_bind_safe("localhost")


def test_dashboard_bind_allows_non_loopback_when_token_configured(monkeypatch) -> None:
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "test-control-secret")
    dashboard_api.assert_dashboard_bind_safe("0.0.0.0")


def test_dashboard_control_routes_require_token_when_configured(monkeypatch) -> None:
    config = MXConfig()
    monkeypatch.setattr(dashboard_api, "load_config", lambda *_a, **_kw: config)
    monkeypatch.setattr(dashboard_api, "save_config", lambda *_a, **_kw: None)
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "secret-dashboard-token")

    client = TestClient(dashboard_api.app)

    # Read-only status endpoint remains accessible
    assert client.get("/api/status").status_code == 200

    # Mutating control endpoints reject unauthenticated or wrong-token requests
    unauth = client.post("/api/config/update", json={"retry_max": 2})
    assert unauth.status_code == 401

    wrong = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong.status_code == 401

    # Valid Bearer token succeeds
    authed = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers={"Authorization": "Bearer secret-dashboard-token"},
    )
    assert authed.status_code == 200
    assert authed.json()["status"] == "updated"


def test_dashboard_control_routes_reject_forwarded_or_cross_origin_requests(monkeypatch) -> None:
    config = MXConfig()
    monkeypatch.setattr(dashboard_api, "load_config", lambda *_a, **_kw: config)
    monkeypatch.setattr(dashboard_api, "save_config", lambda *_a, **_kw: None)
    monkeypatch.delenv("MX_DASHBOARD_TOKEN", raising=False)

    client = TestClient(dashboard_api.app)

    forwarded = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers={"X-Forwarded-For": "192.0.2.10"},
    )
    assert forwarded.status_code == 403

    cross_origin = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers={"Origin": "https://attacker.example"},
    )
    assert cross_origin.status_code == 403
