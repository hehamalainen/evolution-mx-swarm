import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, TextIO

try:
    import aiohttp  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]


def build_dashboard_auth_headers(
    extra_headers: dict[str, str] | None = None,
    token: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = dict(extra_headers or {})
    resolved_token = (
        token if token is not None else os.environ.get("MX_DASHBOARD_TOKEN", "")
    ).strip()
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
        headers["X-MX-Dashboard-Token"] = resolved_token
    return headers


def post_dashboard_json_sync(
    url: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    timeout: float = 2.0,
    error_stream: TextIO | None = None,
) -> int | None:
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        return None
    headers = build_dashboard_auth_headers(
        {"Content-Type": "application/json"},
        token=token,
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            status = int(getattr(resp, "status", 200))
            if status >= 400 and error_stream is not None:
                error_stream.write(
                    f"[dashboard-client] {url} returned HTTP {status}\n"
                )
            return status
    except urllib.error.HTTPError as exc:
        if error_stream is not None:
            error_stream.write(
                f"[dashboard-client] {url} rejected request with HTTP {exc.code}\n"
            )
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        return None


class RemoteLogStream:
    """
    Captures stdout/stderr and streams logs to a remote dashboard.
    """
    def __init__(
        self,
        dashboard_url: str,
        source_id: str = "cli",
        token: str | None = None,
    ):
        self.dashboard_url = dashboard_url.rstrip("/")
        self.source_id = source_id
        self.token = token
        self.buffer: list[dict[str, str]] = []
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.is_running = False
        self._task: asyncio.Task[None] | None = None

    def _build_headers(self) -> dict[str, str]:
        return build_dashboard_auth_headers(token=self.token)

    async def flush_once(
        self, session: Any = None
    ) -> int | None:
        if not self.buffer:
            return None
        batch = self.buffer[:]
        self.buffer = []
        url = f"{self.dashboard_url}/api/logs/ingest"
        payload = {"logs": batch, "source": self.source_id}
        headers = self._build_headers()

        if session is not None:
            try:
                timeout_val = (
                    aiohttp.ClientTimeout(total=2)
                    if aiohttp is not None
                    else 2.0
                )
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_val,
                ) as response:
                    if response.status >= 400:
                        self.original_stderr.write(
                            f"[RemoteLogStream] {url} rejected logs with HTTP {response.status}\n"
                        )
                    return int(response.status)
            except Exception:
                return None

        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: post_dashboard_json_sync(
                url,
                payload,
                token=self.token,
                timeout=2.0,
                error_stream=self.original_stderr,
            ),
        )

    async def _flush_loop(self) -> None:
        """Periodically flush buffer to dashboard."""
        if aiohttp is not None:
            async with aiohttp.ClientSession() as session:
                while self.is_running:
                    if self.buffer:
                        await self.flush_once(session)
                    await asyncio.sleep(0.5)
        else:
            while self.is_running:
                if self.buffer:
                    await self.flush_once(None)
                await asyncio.sleep(0.5)

    def write_log(self, message: str, level: str = "info") -> None:
        """Add log to buffer."""
        timestamp = datetime.now(UTC).strftime("%H.%M.%S")
        self.buffer.append({
            "timestamp": timestamp,
            "message": message,
            "level": level,
            "event_type": "SYS_CONFIG"
        })

    class StreamWrapper:
        def __init__(self, stream: TextIO, logger: "RemoteLogStream", level: str):
            self.stream = stream
            self.logger = logger
            self.level = level

        def write(self, data: str) -> None:
            self.stream.write(data)
            self.stream.flush()
            if data.strip():
                self.logger.write_log(data.strip(), self.level)

        def flush(self) -> None:
            self.stream.flush()

    def start(self) -> None:
        self.is_running = True
        self._task = asyncio.create_task(self._flush_loop())
        sys.stdout = self.StreamWrapper(self.original_stdout, self, "info")
        sys.stderr = self.StreamWrapper(self.original_stderr, self, "error")

    async def stop(self):
        self.is_running = False
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
        if self._task:
            await self._task
