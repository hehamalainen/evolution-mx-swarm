import asyncio
import sys
from datetime import UTC, datetime
from typing import TextIO

import aiohttp


class RemoteLogStream:
    """
    Captures stdout/stderr and streams logs to a remote dashboard.
    """
    def __init__(self, dashboard_url: str, source_id: str = "cli"):
        self.dashboard_url = dashboard_url.rstrip("/")
        self.source_id = source_id
        self.buffer: list[dict[str, str]] = []
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.is_running = False
        self._task: asyncio.Task[None] | None = None

    async def _flush_loop(self) -> None:
        """Periodically flush buffer to dashboard."""
        async with aiohttp.ClientSession() as session:
            while self.is_running:
                if self.buffer:
                    # Swap buffer
                    batch = self.buffer[:]
                    self.buffer = []

                    try:
                        await session.post(
                            f"{self.dashboard_url}/api/logs/ingest",
                            json={"logs": batch, "source": self.source_id},
                            timeout=aiohttp.ClientTimeout(total=2)
                        )
                    except Exception:
                        # Fallback to original stderr if logging fails
                        pass

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
