from __future__ import annotations

import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import dashboard.api as dashboard_api
from shared.utils.config import MXConfig
from shared.utils.remote_logger import (
    RemoteLogStream,
    build_dashboard_auth_headers,
    post_dashboard_json_sync,
)


def test_dashboard_bind_rejects_non_loopback_without_token(monkeypatch) -> None:
    monkeypatch.delenv("MX_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MX_DASHBOARD_TOKEN"):
        dashboard_api.assert_dashboard_bind_safe("0.0.0.0")

    dashboard_api.assert_dashboard_bind_safe("127.0.0.1")
    dashboard_api.assert_dashboard_bind_safe("localhost")


def test_dashboard_bind_allows_non_loopback_when_token_configured(monkeypatch) -> None:
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "test-control-secret")
    dashboard_api.assert_dashboard_bind_safe("0.0.0.0")


def test_dashboard_api_reads_and_mutations_require_token_when_configured(monkeypatch) -> None:
    config = MXConfig()
    monkeypatch.setattr(dashboard_api, "load_config", lambda *_a, **_kw: config)
    monkeypatch.setattr(dashboard_api, "save_config", lambda *_a, **_kw: None)
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "secret-dashboard-token")

    client = TestClient(dashboard_api.app)

    # Sensitive read endpoints (/api/logs, /api/status) require authentication when token is set
    assert client.get("/api/logs").status_code == 401
    assert client.get("/api/status").status_code == 401

    # Mutating control endpoints reject unauthenticated or wrong-token requests
    unauth = client.post("/api/config/update", json={"retry_max": 2})
    assert unauth.status_code == 401

    wrong = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong.status_code == 401

    # Valid Bearer token succeeds for both reads and mutations
    auth_headers = {"Authorization": "Bearer secret-dashboard-token"}
    assert client.get("/api/logs", headers=auth_headers).status_code == 200
    assert client.get("/api/status", headers=auth_headers).status_code == 200

    authed = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers=auth_headers,
    )
    assert authed.status_code == 200
    assert authed.json()["status"] == "updated"


def test_dashboard_routes_reject_forwarded_or_cross_origin_requests(monkeypatch) -> None:
    config = MXConfig()
    monkeypatch.setattr(dashboard_api, "load_config", lambda *_a, **_kw: config)
    monkeypatch.setattr(dashboard_api, "save_config", lambda *_a, **_kw: None)
    monkeypatch.delenv("MX_DASHBOARD_TOKEN", raising=False)

    client = TestClient(dashboard_api.app)

    forwarded_mut = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers={"X-Forwarded-For": "192.0.2.10"},
    )
    assert forwarded_mut.status_code == 403

    forwarded_logs = client.get(
        "/api/logs",
        headers={"X-Forwarded-For": "192.0.2.10"},
    )
    assert forwarded_logs.status_code == 403

    cross_origin_mut = client.post(
        "/api/config/update",
        json={"retry_max": 2},
        headers={"Origin": "https://attacker.example"},
    )
    assert cross_origin_mut.status_code == 403

    cross_origin_logs = client.get(
        "/api/logs",
        headers={"Origin": "https://attacker.example"},
    )
    assert cross_origin_logs.status_code == 403


def test_dashboard_websocket_enforces_token_and_origin_validation(monkeypatch) -> None:
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "ws-secret-token")
    client = TestClient(dashboard_api.app)

    # 1. Unauthenticated WebSocket is rejected with 1008
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 1008

    # 2. Untrusted website Origin is rejected even if token is supplied
    with pytest.raises(WebSocketDisconnect) as exc_info_origin:
        with client.websocket_connect(
            "/ws?token=ws-secret-token",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert exc_info_origin.value.code == 1008

    # 3. Authenticated WebSocket with loopback Origin succeeds and receives initial status
    with client.websocket_connect(
        "/ws",
        headers={
            "Authorization": "Bearer ws-secret-token",
            "Origin": "http://127.0.0.1:8080",
        },
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "status"

    # 4. Even without MX_DASHBOARD_TOKEN configured, untrusted website Origin is rejected
    monkeypatch.delenv("MX_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(WebSocketDisconnect) as exc_info_no_token_evil:
        with client.websocket_connect(
            "/ws",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert exc_info_no_token_evil.value.code == 1008


@pytest.mark.asyncio
async def test_bundled_clients_send_dashboard_token_and_surface_http_errors(monkeypatch) -> None:
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "bundled-client-secret")
    client = TestClient(dashboard_api.app)

    # Verify build_dashboard_auth_headers populates both Bearer and X-MX-Dashboard-Token
    headers = build_dashboard_auth_headers({"Content-Type": "application/json"})
    assert headers["Authorization"] == "Bearer bundled-client-secret"
    assert headers["X-MX-Dashboard-Token"] == "bundled-client-secret"

    # Authenticated heartbeat using bundled client headers succeeds against dashboard API
    hb_resp = client.post(
        "/api/swarm/heartbeat",
        json={"workers_active": 3, "queue_depth": 1, "is_running": True},
        headers=headers,
    )
    assert hb_resp.status_code == 200

    # RemoteLogStream sends auth headers and surfaces HTTP >= 400 errors on stderr
    class FakeResponse:
        def __init__(self, status: int) -> None:
            self.status = status

        async def __aenter__(self) -> FakeResponse:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    captured_calls: list[dict[str, Any]] = []

    class FakeSession:
        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: object) -> FakeResponse:
            captured_calls.append({"url": url, "json": json, "headers": headers})
            forwarded_resp = client.post("/api/logs/ingest", json=json, headers=headers)
            return FakeResponse(forwarded_resp.status_code)

    stream = RemoteLogStream("http://127.0.0.1:8080", source_id="worker-test")
    err_buf = io.StringIO()
    stream.original_stderr = err_buf
    stream.write_log("authenticated log line", "info")
    status = await stream.flush_once(FakeSession())  # type: ignore[arg-type]
    assert status == 200
    assert captured_calls[-1]["headers"]["Authorization"] == "Bearer bundled-client-secret"
    assert err_buf.getvalue() == ""

    # Wrong token surfaces explicit HTTP 401 error on original_stderr
    bad_stream = RemoteLogStream(
        "http://127.0.0.1:8080",
        source_id="worker-test",
        token="wrong-token",
    )
    bad_err_buf = io.StringIO()
    bad_stream.original_stderr = bad_err_buf
    bad_stream.write_log("unauthenticated log line", "error")
    bad_status = await bad_stream.flush_once(FakeSession())  # type: ignore[arg-type]
    assert bad_status == 401
    assert "HTTP 401" in bad_err_buf.getvalue()

    # Sync helper also surfaces HTTP errors on error_stream when urllib raises HTTPError
    import urllib.error

    def fake_urlopen(req, timeout=2.0):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sync_err = io.StringIO()
    sync_status = post_dashboard_json_sync(
        "http://127.0.0.1:8080/api/swarm/heartbeat",
        {"workers_active": 1},
        token="wrong-token",
        error_stream=sync_err,
    )
    assert sync_status == 401
    assert "HTTP 401" in sync_err.getvalue()

