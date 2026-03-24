"""SQLite-backed capability registry for passive lane learning."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from types import MappingProxyType

from shared.schemas.org_topology import CapabilityVector

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.path.join(".runtime", "mx_capabilities.sqlite")
_REGISTRIES: dict[str, CapabilityRegistry] = {}
_REGISTRY_LOCK = threading.Lock()
_TRANSIENT_TASK_FAILED_MARKERS = (
    "all connection attempts failed",
    "connection refused",
    "connection reset",
    "temporarily unavailable",
    "temporary failure",
    "timed out",
    "timeout",
    "rate limit",
    "operation not permitted",
    "network",
)


class TerminalOutcomeClass(str, Enum):
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
    TRANSIENT_SYSTEM_FAILURE = "TRANSIENT_SYSTEM_FAILURE"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    POLICY_FAILURE = "POLICY_FAILURE"
    LANDING_FAILURE = "LANDING_FAILURE"


@dataclass(slots=True)
class TerminalSettlementEvent:
    """Terminal settlement truth emitted by the execution/landing path."""

    task_id: str
    attempt_id: str
    trace_id: str
    lane_key: str
    rig_id: str
    stream_id: str
    shard_id: str
    model_tier: str
    outcome: str
    terminal_state: str
    reason_code: str | None = None
    detail: str | None = None
    task_class: str | None = None
    toolchain: str | None = None
    cost_usd: float = 0.0
    latency_s: float = 0.0
    false_success: bool = False
    outcome_class: str | None = None
    learning_weight: float | None = None
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: f"capevt-{uuid.uuid4().hex[:16]}")

    @property
    def terminal_key(self) -> str:
        return f"{self.task_id}:{self.attempt_id}"


class CapabilityRegistry:
    """Cold-path writer plus in-memory routing snapshot."""

    def __init__(self, db_path: str, refresh_interval_s: float = 5.0):
        self.db_path = os.path.abspath(db_path)
        self.refresh_interval_s = refresh_interval_s
        self.alpha = 0.15
        self._memory_snapshot: Mapping[str, CapabilityVector] = MappingProxyType({})
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None

        self._init_db()
        self._refresh_snapshot()
        self._refresh_thread = threading.Thread(
            target=self._background_refresh_loop,
            name=f"capability-registry:{os.path.basename(self.db_path)}",
            daemon=True,
        )
        self._refresh_thread.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lane_capabilities (
                    lane_key TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    last_updated_ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS terminal_events (
                    event_id TEXT PRIMARY KEY,
                    terminal_key TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    lane_key TEXT NOT NULL,
                    rig_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    shard_id TEXT NOT NULL,
                    model_tier TEXT NOT NULL,
                    task_class TEXT,
                    toolchain TEXT,
                    outcome TEXT NOT NULL,
                    terminal_state TEXT NOT NULL,
                    reason_code TEXT,
                    detail TEXT,
                    outcome_class TEXT NOT NULL DEFAULT 'CAPABILITY_FAILURE',
                    learning_weight REAL NOT NULL DEFAULT 1.0,
                    cost_usd REAL NOT NULL,
                    latency_s REAL NOT NULL,
                    false_success INTEGER NOT NULL DEFAULT 0,
                    timestamp REAL NOT NULL
                )
                """
            )
            self._ensure_column(conn, "terminal_events", "detail", "TEXT")
            self._ensure_column(
                conn,
                "terminal_events",
                "outcome_class",
                "TEXT NOT NULL DEFAULT 'CAPABILITY_FAILURE'",
            )
            self._ensure_column(
                conn,
                "terminal_events",
                "learning_weight",
                "REAL NOT NULL DEFAULT 1.0",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_terminal_events_lane_ts ON terminal_events (lane_key, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_terminal_events_outcome_ts ON terminal_events (outcome, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_terminal_events_task_attempt ON terminal_events (task_id, attempt_id)"
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _background_refresh_loop(self) -> None:
        while not self._stop_event.wait(self.refresh_interval_s):
            try:
                self._refresh_snapshot()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("Capability registry refresh failed: %s", exc)

    def _refresh_snapshot(self) -> None:
        snapshot: dict[str, CapabilityVector] = {}
        with self._connect() as conn:
            rows = conn.execute("SELECT lane_key, data_json FROM lane_capabilities").fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["data_json"]))
                snapshot[str(row["lane_key"])] = CapabilityVector(**payload)
            except Exception:
                continue
        self._memory_snapshot = MappingProxyType(snapshot)

    def get_routing_snapshot(self) -> Mapping[str, CapabilityVector]:
        """Return the latest immutable in-memory snapshot."""
        return self._memory_snapshot

    def record_settlement_event(self, event: TerminalSettlementEvent) -> bool:
        """Persist a terminal event and update lane capabilities once per attempt."""
        if event.outcome_class is None or event.learning_weight is None:
            outcome_class, learning_weight = classify_terminal_event(event)
            event.outcome_class = outcome_class
            event.learning_weight = learning_weight

        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO terminal_events (
                    event_id,
                    terminal_key,
                    task_id,
                    attempt_id,
                    trace_id,
                    lane_key,
                    rig_id,
                    stream_id,
                    shard_id,
                    model_tier,
                    task_class,
                    toolchain,
                    outcome,
                    terminal_state,
                    reason_code,
                    detail,
                    outcome_class,
                    learning_weight,
                    cost_usd,
                    latency_s,
                    false_success,
                    timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.terminal_key,
                    event.task_id,
                    event.attempt_id,
                    event.trace_id,
                    event.lane_key,
                    event.rig_id,
                    event.stream_id,
                    event.shard_id,
                    event.model_tier,
                    event.task_class,
                    event.toolchain,
                    event.outcome,
                    event.terminal_state,
                    event.reason_code,
                    event.detail,
                    event.outcome_class,
                    float(event.learning_weight if event.learning_weight is not None else 1.0),
                    float(event.cost_usd or 0.0),
                    float(event.latency_s or 0.0),
                    1 if event.false_success else 0,
                    float(event.timestamp or time.time()),
                ),
            )
            if cursor.rowcount == 0:
                return False

            row = conn.execute(
                "SELECT data_json FROM lane_capabilities WHERE lane_key = ?",
                (event.lane_key,),
            ).fetchone()
            capability = (
                CapabilityVector(**json.loads(str(row["data_json"])))
                if row is not None
                else CapabilityVector()
            )
            self._apply_ema_math(capability, event)
            conn.execute(
                """
                INSERT INTO lane_capabilities (lane_key, data_json, last_updated_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(lane_key) DO UPDATE SET
                    data_json = excluded.data_json,
                    last_updated_ts = excluded.last_updated_ts
                """,
                (
                    event.lane_key,
                    json.dumps(asdict(capability), sort_keys=True),
                    float(capability.last_updated_ts),
                ),
            )

        self._refresh_snapshot()
        return True

    def _apply_ema_math(self, capability: CapabilityVector, event: TerminalSettlementEvent) -> None:
        if event.false_success:
            capability.false_success_penalty_multiplier *= 0.1
            capability.last_updated_ts = float(event.timestamp or time.time())
            return

        outcome_class = event.outcome_class or classify_terminal_event(event)[0]
        learning_weight = float(event.learning_weight if event.learning_weight is not None else 1.0)
        learning_weight = max(0.0, min(1.0, learning_weight))

        transient_observed = (
            1.0 if outcome_class == TerminalOutcomeClass.TRANSIENT_SYSTEM_FAILURE.value else 0.0
        )
        capability.transient_failure_rate = self._ema(
            capability.transient_failure_rate,
            transient_observed,
            self.alpha,
        )

        effective_alpha = self.alpha * learning_weight
        if effective_alpha <= 0.0:
            capability.last_updated_ts = float(event.timestamp or time.time())
            return

        is_success = 1.0 if event.outcome == "success" else 0.0
        is_escalated = 1.0 if event.outcome == "escalated" else 0.0

        capability.pass_rate = self._ema(capability.pass_rate, is_success, effective_alpha)
        capability.escalation_rate = self._ema(
            capability.escalation_rate,
            is_escalated,
            effective_alpha,
        )

        if is_success:
            capability.cost_per_success_usd = self._ema(
                capability.cost_per_success_usd,
                float(event.cost_usd or 0.0),
                effective_alpha,
            )
            capability.settlement_latency_ema_s = self._ema(
                capability.settlement_latency_ema_s,
                float(event.latency_s or 0.0),
                effective_alpha,
            )

        if event.task_class:
            current = capability.task_class_strengths.get(event.task_class, 0.5)
            capability.task_class_strengths[event.task_class] = self._ema(
                current,
                is_success,
                effective_alpha,
            )

        if event.toolchain:
            current = capability.toolchain_strengths.get(event.toolchain, 0.5)
            capability.toolchain_strengths[event.toolchain] = self._ema(
                current,
                is_success,
                effective_alpha,
            )

        capability.last_updated_ts = float(event.timestamp or time.time())

    def _ema(self, current: float, observed: float, alpha: float) -> float:
        return ((1 - alpha) * current) + (alpha * observed)

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._refresh_thread and self._refresh_thread.is_alive():
            self._refresh_thread.join(timeout=1.0)


def capability_registry_db_path(*, repo_path: str = ".") -> str:
    return os.path.abspath(os.path.join(repo_path, _DEFAULT_DB_PATH))


def get_capability_registry(
    *,
    repo_path: str = ".",
    db_path: str | None = None,
    refresh_interval_s: float = 5.0,
) -> CapabilityRegistry:
    resolved = os.path.abspath(db_path or capability_registry_db_path(repo_path=repo_path))
    with _REGISTRY_LOCK:
        registry = _REGISTRIES.get(resolved)
        if registry is None:
            registry = CapabilityRegistry(resolved, refresh_interval_s=refresh_interval_s)
            _REGISTRIES[resolved] = registry
        return registry


def shutdown_capability_registry(*, repo_path: str = ".", db_path: str | None = None) -> None:
    resolved = os.path.abspath(db_path or capability_registry_db_path(repo_path=repo_path))
    with _REGISTRY_LOCK:
        registry = _REGISTRIES.pop(resolved, None)
    if registry is not None:
        registry.shutdown()


def shutdown_all_capability_registries() -> None:
    with _REGISTRY_LOCK:
        registries = list(_REGISTRIES.values())
        _REGISTRIES.clear()
    for registry in registries:
        registry.shutdown()


def classify_terminal_event(event: TerminalSettlementEvent) -> tuple[str, float]:
    """Classify a terminal event for shadow capability learning."""
    if event.false_success:
        return (TerminalOutcomeClass.LANDING_FAILURE.value, 1.0)
    if event.outcome == "success" and event.terminal_state == "settled_ack":
        return (TerminalOutcomeClass.VERIFIED_SUCCESS.value, 1.0)

    reason = (event.reason_code or "").strip()
    detail = (event.detail or "").lower()

    if reason in {"ERR_NO_DIFF", "ERR_SYNTAX", "ERR_REFUSAL", "ERR_CHECK_FAIL"}:
        return (TerminalOutcomeClass.CAPABILITY_FAILURE.value, 1.0)
    if reason in {"ERR_POST_LANDING_VERIFY", "ERR_RECON_DIVERGENCE"}:
        return (TerminalOutcomeClass.LANDING_FAILURE.value, 1.0)
    if reason in {"ERR_RATE_LIMIT", "ERR_QUEUE_LEASE", "ERR_LEDGER_UNAVAILABLE"}:
        return (TerminalOutcomeClass.TRANSIENT_SYSTEM_FAILURE.value, 0.0)
    if reason == "ERR_PREREQUISITE_FAILED":
        return (TerminalOutcomeClass.DEPENDENCY_FAILURE.value, 0.0)
    if reason == "ERR_TASK_FAILED":
        if any(marker in detail for marker in _TRANSIENT_TASK_FAILED_MARKERS):
            return (TerminalOutcomeClass.TRANSIENT_SYSTEM_FAILURE.value, 0.0)
        return (TerminalOutcomeClass.TRANSIENT_SYSTEM_FAILURE.value, 0.0)

    return (TerminalOutcomeClass.CAPABILITY_FAILURE.value, 1.0)
