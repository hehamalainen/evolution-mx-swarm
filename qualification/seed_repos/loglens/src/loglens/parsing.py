"""Parsing helpers for the structured log fixture format."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\S+) "
    r"level=(?P<level>[A-Z]+) "
    r"component=(?P<component>[a-z0-9_-]+) "
    r"duration_ms=(?P<duration_ms>\d+) "
    r'message="(?P<message>.*)"$'
)


@dataclass(frozen=True)
class LogEntry:
    """Normalized representation of one structured log line."""

    timestamp: datetime
    timestamp_text: str
    level: str
    component: str
    duration_ms: int
    message: str
    raw_line: str


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def parse_line(line: str) -> LogEntry:
    """Parse a single fixture line into a typed record."""
    match = LINE_PATTERN.match(line.strip())
    if match is None:
        raise ValueError(f"invalid log line: {line}")

    timestamp_text = match.group("timestamp")
    return LogEntry(
        timestamp=_parse_timestamp(timestamp_text),
        timestamp_text=timestamp_text,
        level=match.group("level"),
        component=match.group("component"),
        duration_ms=int(match.group("duration_ms")),
        message=match.group("message"),
        raw_line=line.strip(),
    )


def load_entries(path: str) -> list[LogEntry]:
    """Read all non-empty log lines from disk."""
    source = Path(path)
    lines = [
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [parse_line(line) for line in lines]
