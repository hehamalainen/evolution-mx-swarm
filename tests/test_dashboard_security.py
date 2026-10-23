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

    # 2. Untrusted website Origin is rejected even if token header is supplied
    with pytest.raises(WebSocketDisconnect) as exc_info_origin:
        with client.websocket_connect(
            "/ws",
            headers={
                "Authorization": "Bearer ws-secret-token",
                "Origin": "https://evil.example",
            },
        ):
            pass
    assert exc_info_origin.value.code == 1008

    # 3. Authenticated WebSocket with loopback Origin on matching port succeeds and receives initial status
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


def test_dashboard_session_cookie_login_rejects_cross_port_and_url_query_tokens(
    monkeypatch,
) -> None:
    from pathlib import Path

    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "session-secret-token")
    client = TestClient(dashboard_api.app, base_url="http://127.0.0.1:8080")

    # Reusable secret in URL query parameters is rejected on HTTP and WebSocket
    assert client.get("/?token=session-secret-token").status_code == 400
    assert client.get("/api/status?token=session-secret-token").status_code == 400
    with pytest.raises(WebSocketDisconnect) as ws_query_exc:
        with client.websocket_connect(
            "/ws?token=session-secret-token",
            headers={"Origin": "http://127.0.0.1:8080"},
        ):
            pass
    assert ws_query_exc.value.code == 1008

    # Exchange credential via POST /api/auth/session for an opaque HttpOnly session cookie
    login_resp = client.post(
        "/api/auth/session",
        json={"token": "session-secret-token"},
        headers={"Origin": "http://127.0.0.1:8080"},
    )
    assert login_resp.status_code == 200
    cookie_val = client.cookies.get("mx_dashboard_session")
    assert cookie_val
    assert cookie_val != "session-secret-token"

    # Same-port loopback Origin succeeds with session cookie on HTTP mutation and WebSocket
    same_port_hb = client.post(
        "/api/swarm/heartbeat",
        json={"workers_active": 2, "queue_depth": 0, "is_running": True},
        headers={"Origin": "http://127.0.0.1:8080"},
    )
    assert same_port_hb.status_code == 200

    with client.websocket_connect(
        "ws://127.0.0.1:8080/ws",
        headers={"Origin": "http://127.0.0.1:8080"},
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "status"

    # Cross-port localhost Origin (http://127.0.0.1:9999) is rejected even with valid session cookie
    cross_port_hb = client.post(
        "/api/swarm/heartbeat",
        json={"workers_active": 2, "queue_depth": 0, "is_running": True},
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    assert cross_port_hb.status_code == 403

    with pytest.raises(WebSocketDisconnect) as ws_cross_port_exc:
        with client.websocket_connect(
            "ws://127.0.0.1:8080/ws",
            headers={"Origin": "http://127.0.0.1:9999"},
        ):
            pass
    assert ws_cross_port_exc.value.code == 1008

    # Verify frontend sources do not read or append ?token= in URLs
    repo_root = Path(__file__).resolve().parents[1]
    index_html = (repo_root / "dashboard" / "index.html").read_text(encoding="utf-8")
    v2_adapter = (repo_root / "dashboard_v2" / "src" / "lib" / "api_adapter.ts").read_text(
        encoding="utf-8"
    )
    assert "?token=" not in index_html
    assert "params.get('token')" not in index_html
    assert "params.get('token')" not in v2_adapter


def test_dashboard_sign_in_flow_status_session_expiry_and_frontend_wiring(
    monkeypatch,
) -> None:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]

    # 1. Without MX_DASHBOARD_TOKEN configured, loopback session is immediately authenticated
    monkeypatch.delenv("MX_DASHBOARD_TOKEN", raising=False)
    loopback_client = TestClient(dashboard_api.app, base_url="http://127.0.0.1:8080")
    no_token_status = loopback_client.get("/api/auth/status")
    assert no_token_status.status_code == 200
    assert no_token_status.json() == {
        "requires_auth": False,
        "authenticated": True,
        "mode": "loopback",
    }

    # 2. With MX_DASHBOARD_TOKEN configured, fresh browser session is unauthenticated on /api/auth/status
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "browser-flow-secret")
    browser_client = TestClient(dashboard_api.app, base_url="http://127.0.0.1:8080")
    pre_login_status = browser_client.get("/api/auth/status")
    assert pre_login_status.status_code == 200
    assert pre_login_status.json() == {
        "requires_auth": True,
        "authenticated": False,
        "mode": "unauthenticated",
    }
    assert browser_client.get("/api/status").status_code == 401

    # 3. Wrong token via login form fails and leaves session unauthenticated
    bad_login = browser_client.post(
        "/api/auth/session",
        json={"token": "wrong-token"},
        headers={"Origin": "http://127.0.0.1:8080"},
    )
    assert bad_login.status_code == 401
    assert browser_client.get("/api/auth/status").json()["authenticated"] is False

    # 4. Valid token via login form establishes cookie session and unlocks /api/status + /ws
    good_login = browser_client.post(
        "/api/auth/session",
        json={"token": "browser-flow-secret"},
        headers={"Origin": "http://127.0.0.1:8080"},
    )
    assert good_login.status_code == 200
    post_login_status = browser_client.get("/api/auth/status")
    assert post_login_status.status_code == 200
    assert post_login_status.json() == {
        "requires_auth": True,
        "authenticated": True,
        "mode": "cookie",
    }
    assert browser_client.get("/api/status").status_code == 200

    # 5. Session expiry / logout returns /api/auth/status to authenticated=False and /api/status to 401
    dashboard_api._ACTIVE_DASHBOARD_SESSIONS.clear()
    expired_status = browser_client.get("/api/auth/status")
    assert expired_status.status_code == 200
    assert expired_status.json()["authenticated"] is False
    assert browser_client.get("/api/status").status_code == 401

    # 6. Verify both dashboard frontends wire token sign-in forms to loginDashboardSession,
    # gate startup on /api/auth/status, and recover from 401/1008 session expiry
    index_html = (repo_root / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert '<div class="modal" id="auth-modal">' in index_html
    assert '<div class="modal-content" style="max-width: 440px;">' in index_html
    assert "modal-overlay" not in index_html
    assert 'id="dashboard-auth-form"' in index_html
    assert 'id="dashboard-token-input"' in index_html
    assert "submitDashboardLogin" in index_html
    assert "await loginDashboardSession(token)" in index_html
    assert "checkDashboardAuthStatus()" in index_html
    assert "initDashboard();" in index_html
    assert "res.status === 401" in index_html
    assert "event.code === 1008" in index_html

    v2_app = (repo_root / "dashboard_v2" / "src" / "App.tsx").read_text(encoding="utf-8")
    v2_adapter = (repo_root / "dashboard_v2" / "src" / "lib" / "api_adapter.ts").read_text(
        encoding="utf-8"
    )
    assert 'id="v2-dashboard-auth-form"' in v2_app
    assert 'id="v2-dashboard-token-input"' in v2_app
    assert "await loginDashboardSession(tokenInput)" in v2_app
    assert "useDashboardAuthStatusQuery()" in v2_app
    assert "AUTH_REQUIRED_EVENT" in v2_app
    assert "notifyAuthRequiredIfUnauthorized(res)" in v2_adapter

    # 7. Execute the actual bundled JavaScript from dashboard/index.html in a fresh browser VM
    import shutil
    import subprocess

    node_bin = shutil.which("node")
    if node_bin:
        script_body = index_html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        harness = r"""
const fs = require('fs');
const vm = require('vm');
const scriptCode = fs.readFileSync(process.argv[1], 'utf8');

let authCookieValid = false;
const apiCalls = [];
const wsInstances = [];
const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      value: '',
      textContent: '',
      innerHTML: '',
      className: '',
      style: {},
      addEventListener() {},
      removeEventListener() {},
      classes: new Set(),
      classList: {
        add(c) { elements[id].classes.add(c); },
        remove(c) { elements[id].classes.delete(c); },
        contains(c) { return elements[id].classes.has(c); },
      },
      getContext() {
        return {
          clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
          createLinearGradient() { return { addColorStop() {} }; },
        };
      },
    };
  }
  return elements[id];
}

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.closed = false;
    wsInstances.push(this);
  }
  send() {}
  close() { this.closed = true; this.readyState = 3; }
}
FakeWebSocket.OPEN = 1;

async function fakeFetch(url, init = {}) {
  apiCalls.push({ url, method: init.method || 'GET' });
  if (url === '/api/auth/status') {
    return {
      ok: true,
      status: 200,
      async json() { return { requires_auth: true, authenticated: authCookieValid, mode: authCookieValid ? 'cookie' : 'unauthenticated' }; },
    };
  }
  if (url === '/api/auth/session') {
    const body = JSON.parse(init.body || '{}');
    if (body.token === 'browser-flow-secret') {
      authCookieValid = true;
      return { ok: true, status: 200, async json() { return { status: 'authenticated' }; } };
    }
    return { ok: false, status: 401, async json() { return { detail: 'Invalid token' }; } };
  }
  if (!authCookieValid) {
    return { ok: false, status: 401, async json() { return { detail: 'Unauthorized' }; } };
  }
  return { ok: true, status: 200, async json() { return {}; } };
}

const windowObj = {
  location: { protocol: 'http:', host: '127.0.0.1:8080' },
  fetch: fakeFetch,
};
const sandbox = {
  window: windowObj,
  fetch: (...args) => windowObj.fetch(...args),
  WebSocket: FakeWebSocket,
  document: {
    getElementById: getEl,
    querySelectorAll: () => [],
  },
  console: { log() {}, error() {}, warn() {} },
  setInterval: () => 1,
  clearInterval: () => {},
  setTimeout: () => 1,
  Date,
  Math,
  JSON,
};

(async () => {
  vm.runInNewContext(scriptCode, sandbox);
  await new Promise((r) => setTimeout(r, 20));

  // Fresh unauthenticated session: modal is active, no WebSocket opened, no /api/status fetched
  if (!getEl('auth-modal').classList.contains('active')) throw new Error('Expected auth-modal active on unauthenticated load');
  if (wsInstances.length !== 0) throw new Error('Expected 0 WebSockets before login');

  // Wrong token keeps modal active
  getEl('dashboard-token-input').value = 'wrong-token';
  await sandbox.submitDashboardLogin({ preventDefault() {} });
  if (!getEl('auth-modal').classList.contains('active')) throw new Error('Expected auth-modal to remain active on wrong token');
  if (wsInstances.length !== 0) throw new Error('Expected 0 WebSockets after wrong token');

  // Valid token logs in, hides modal, opens WebSocket, and starts polling
  getEl('dashboard-token-input').value = 'browser-flow-secret';
  await sandbox.submitDashboardLogin({ preventDefault() {} });
  await new Promise((r) => setTimeout(r, 20));
  if (getEl('auth-modal').classList.contains('active')) throw new Error('Expected auth-modal hidden after valid login');
  if (wsInstances.length !== 1) throw new Error('Expected 1 WebSocket after valid login');
  if (!apiCalls.some((c) => c.url === '/api/status')) throw new Error('Expected /api/status fetch after login');

  // Simulate session expiry: next /api/status returns 401 -> re-opens auth-modal and closes WebSocket
  authCookieValid = false;
  await sandbox.window.fetch('/api/status');
  if (!getEl('auth-modal').classList.contains('active')) throw new Error('Expected auth-modal to reopen on 401 session expiry');
  if (!wsInstances[0].closed) throw new Error('Expected WebSocket to close on session expiry');
  console.log('FRONTEND_VM_OK');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as f:
            f.write(script_body)
            f.flush()
            proc = subprocess.run(
                [node_bin, "-e", harness, f.name],
                capture_output=True,
                text=True,
                check=False,
            )
        assert proc.returncode == 0, f"Frontend VM test failed: {proc.stderr}\n{proc.stdout}"
        assert "FRONTEND_VM_OK" in proc.stdout


@pytest.mark.asyncio
async def test_original_dashboard_real_browser_sign_in_visibility_and_expiry_recovery(
    tmp_path,
) -> None:
    import asyncio
    import json
    import os
    import shutil
    import socket
    import subprocess
    import sys
    import urllib.request
    from pathlib import Path

    node_bin = shutil.which("node")
    chrome_candidates = [
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    chrome_bin = next((c for c in chrome_candidates if c and os.path.exists(c)), None)
    if not chrome_bin or not node_bin:
        pytest.skip("Real Chromium/Chrome browser binary or node not available in environment")

    def get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    server_port = get_free_port()
    cdp_port = get_free_port()
    repo_root = Path(__file__).resolve().parents[1]

    server_env = os.environ.copy()
    server_env["MX_DASHBOARD_TOKEN"] = "real-browser-secret"
    server_env["MX_DASHBOARD_PORT"] = str(server_port)
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "dashboard.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(server_port),
            "--log-level",
            "warning",
        ],
        cwd=str(repo_root),
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    import tempfile

    chrome_proc = None
    short_profile_dir = tempfile.mkdtemp(prefix="mx-chr-", dir="/tmp")
    try:
        # Wait for dashboard server to listen
        server_ready = False
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server_port}/api/auth/status", timeout=0.5
                ) as resp:
                    if resp.status == 200:
                        server_ready = True
                        break
            except Exception:
                await asyncio.sleep(0.1)
        assert server_ready, "Dashboard server failed to start for browser test"

        chrome_proc = subprocess.Popen(
            [
                chrome_bin,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                f"--user-data-dir={short_profile_dir}",
                f"--remote-debugging-port={cdp_port}",
                f"http://127.0.0.1:{server_port}/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ws_debugger_url = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{cdp_port}/json", timeout=0.5
                ) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                    page_target = next(
                        (
                            t
                            for t in targets
                            if t.get("type") == "page"
                            and f"127.0.0.1:{server_port}" in t.get("url", "")
                        ),
                        None,
                    )
                    if page_target and page_target.get("webSocketDebuggerUrl"):
                        ws_debugger_url = page_target["webSocketDebuggerUrl"]
                        break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        assert ws_debugger_url, "Failed to connect to headless Chrome CDP page target"

        cdp_driver = r"""
const wsUrl = process.argv[1];
const ws = new WebSocket(wsUrl);
let msgId = 0;
const pending = new Map();

ws.addEventListener('message', (event) => {
  const payload = JSON.parse(event.data);
  if (payload.id && pending.has(payload.id)) {
    const { resolve, reject } = pending.get(payload.id);
    pending.delete(payload.id);
    const res = payload.result && payload.result.result;
    if ((payload.result && payload.result.exceptionDetails) || (res && res.subtype === 'error')) {
      reject(new Error(JSON.stringify(payload)));
    } else {
      resolve(res ? res.value : undefined);
    }
  }
});

function cdpEval(expression) {
  return new Promise((resolve, reject) => {
    msgId += 1;
    pending.set(msgId, { resolve, reject });
    ws.send(JSON.stringify({
      id: msgId,
      method: 'Runtime.evaluate',
      params: { expression, awaitPromise: true, returnByValue: true },
    }));
  });
}

ws.addEventListener('open', async () => {
  try {
    let state = null;
    for (let i = 0; i < 40; i++) {
      state = await cdpEval(`(() => {
        const modal = document.getElementById('auth-modal');
        const input = document.getElementById('dashboard-token-input');
        const btn = document.getElementById('dashboard-auth-submit');
        if (!modal || !input || !btn) return null;
        const modalStyle = window.getComputedStyle(modal);
        const inputStyle = window.getComputedStyle(input);
        const inputRect = input.getBoundingClientRect();
        const btnRect = btn.getBoundingClientRect();
        return {
          modalActive: modal.classList.contains('active'),
          modalDisplay: modalStyle.display,
          inputDisplay: inputStyle.display,
          inputVisibility: inputStyle.visibility,
          inputWidth: inputRect.width,
          inputHeight: inputRect.height,
          btnWidth: btnRect.width,
          btnHeight: btnRect.height,
        };
      })()`);
      if (state && state.modalActive) break;
      await new Promise((r) => setTimeout(r, 100));
    }

    const badState = await cdpEval(`(async () => {
      const input = document.getElementById('dashboard-token-input');
      input.value = 'wrong-token';
      await submitDashboardLogin(new Event('submit', { cancelable: true }));
      const modal = document.getElementById('auth-modal');
      const msg = document.getElementById('auth-modal-message');
      const rect = input.getBoundingClientRect();
      return {
        modalDisplay: window.getComputedStyle(modal).display,
        inputWidth: rect.width,
        message: msg ? msg.textContent : '',
      };
    })()`);

    const goodState = await cdpEval(`(async () => {
      const input = document.getElementById('dashboard-token-input');
      input.value = 'real-browser-secret';
      await submitDashboardLogin(new Event('submit', { cancelable: true }));
      for (let i = 0; i < 30; i++) {
        const statusEl = document.getElementById('connection-status');
        if (statusEl && statusEl.textContent.includes('Connected')) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      const modal = document.getElementById('auth-modal');
      const statusEl = document.getElementById('connection-status');
      const rect = input.getBoundingClientRect();
      return {
        modalDisplay: window.getComputedStyle(modal).display,
        inputWidth: rect.width,
        statusText: statusEl ? statusEl.textContent.trim() : '',
      };
    })()`);

    const expiredState = await cdpEval(`(async () => {
      await fetch('/api/auth/logout', { method: 'POST' });
      await window.fetch('/api/status');
      const modal = document.getElementById('auth-modal');
      const input = document.getElementById('dashboard-token-input');
      const rect = input.getBoundingClientRect();
      return {
        modalDisplay: window.getComputedStyle(modal).display,
        inputWidth: rect.width,
        inputHeight: rect.height,
      };
    })()`);

    console.log(JSON.stringify({ state, badState, goodState, expiredState }));
    ws.close();
    process.exit(0);
  } catch (err) {
    console.error(err);
    process.exit(1);
  }
});
"""
        cdp_proc = subprocess.run(
            [node_bin, "-e", cdp_driver, ws_debugger_url],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert cdp_proc.returncode == 0, f"CDP browser check failed: {cdp_proc.stderr}\n{cdp_proc.stdout}"
        results = json.loads(cdp_proc.stdout.strip().splitlines()[-1])

        # 1. Fresh unauthenticated browser session: modal and input/submit are visibly rendered
        state = results["state"]
        assert state is not None
        assert state["modalActive"] is True
        assert state["modalDisplay"] == "flex"
        assert state["inputDisplay"] != "none"
        assert state["inputVisibility"] == "visible"
        assert state["inputWidth"] > 0 and state["inputHeight"] > 0
        assert state["btnWidth"] > 0 and state["btnHeight"] > 0

        # 2. Invalid token rejected, modal and input remain visibly rendered
        bad_state = results["badState"]
        assert bad_state["modalDisplay"] == "flex"
        assert bad_state["inputWidth"] > 0
        assert "Invalid MX_DASHBOARD_TOKEN" in bad_state["message"]

        # 3. Valid token hides modal (display: none, width: 0) and connects WebSocket
        good_state = results["goodState"]
        assert good_state["modalDisplay"] == "none"
        assert good_state["inputWidth"] == 0
        assert "Connected" in good_state["statusText"]

        # 4. Session expiry re-opens modal and makes input visible again
        expired_state = results["expiredState"]
        assert expired_state["modalDisplay"] == "flex"
        assert expired_state["inputWidth"] > 0 and expired_state["inputHeight"] > 0
    finally:
        if chrome_proc is not None:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        shutil.rmtree(short_profile_dir, ignore_errors=True)


def test_dashboard_allows_legitimate_remote_same_origin_and_rejects_mismatched_remote_origins(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MX_DASHBOARD_TOKEN", "remote-secret-token")
    remote_client = TestClient(dashboard_api.app, base_url="https://dashboard.example")

    login = remote_client.post(
        "/api/auth/session",
        json={"token": "remote-secret-token"},
        headers={"Origin": "https://dashboard.example"},
    )
    assert login.status_code == 200

    status_resp = remote_client.get(
        "/api/status",
        headers={"Origin": "https://dashboard.example"},
    )
    assert status_resp.status_code == 200

    hb_resp = remote_client.post(
        "/api/swarm/heartbeat",
        json={"workers_active": 1, "queue_depth": 0, "is_running": True},
        headers={"Origin": "https://dashboard.example"},
    )
    assert hb_resp.status_code == 200

    with remote_client.websocket_connect(
        "wss://dashboard.example/ws",
        headers={"Origin": "https://dashboard.example"},
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "status"

    # Also verify header-authenticated WebSocket with Host: dashboard.example on relative /ws
    with remote_client.websocket_connect(
        "/ws",
        headers={
            "Host": "dashboard.example",
            "Origin": "https://dashboard.example",
            "Authorization": "Bearer remote-secret-token",
        },
    ) as ws2:
        msg2 = ws2.receive_json()
        assert msg2["type"] == "status"

    # Scheme downgrade (http://dashboard.example) and cross-port (https://dashboard.example:9443) are rejected
    downgrade = remote_client.post(
        "/api/swarm/heartbeat",
        json={"workers_active": 1},
        headers={"Origin": "http://dashboard.example"},
    )
    assert downgrade.status_code == 403

    remote_cross_port = remote_client.post(
        "/api/swarm/heartbeat",
        json={"workers_active": 1},
        headers={"Origin": "https://dashboard.example:9443"},
    )
    assert remote_cross_port.status_code == 403



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

