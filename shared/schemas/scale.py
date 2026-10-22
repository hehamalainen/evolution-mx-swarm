"""Stable schema for MX scaling manifests and reports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class ScaleLaneMix:
    deterministic_pct: float
    noise_pct: float
    sampled_pct: float


@dataclass(slots=True)
class ScaleScenarioConfig:
    dependency_depth_max: int
    invalid_contract_pct: float
    transient_failure_pct: float
    deterministic_failure_pct: float
    escalation_failure_pct: float
    settlement_delay_pct: float


@dataclass(slots=True)
class ScaleRunManifest:
    run_id: str
    phase: str
    generated_at: str = field(default_factory=_utc_now)
    seed: int = 0
    logical_lanes: int = 0
    logical_workers: int = 0
    active_concurrency: int = 0
    sink_mode: str = "aether"
    model_mix: dict[str, Any] = field(default_factory=dict)
    lane_mix: ScaleLaneMix = field(
        default_factory=lambda: ScaleLaneMix(
            deterministic_pct=0.75,
            noise_pct=0.20,
            sampled_pct=0.05,
        )
    )
    scenario: ScaleScenarioConfig = field(
        default_factory=lambda: ScaleScenarioConfig(
            dependency_depth_max=4,
            invalid_contract_pct=0.03,
            transient_failure_pct=0.08,
            deterministic_failure_pct=0.06,
            escalation_failure_pct=0.03,
            settlement_delay_pct=0.07,
        )
    )
    topology: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScaleFailureVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    structural_pass: bool = True
    loss_budget_pass: bool = True


@dataclass(slots=True)
class ScaleMetricsSummary:
    verified_landings: int = 0
    total_terminal_tasks: int = 0
    false_success_rate: float | None = None
    blocked_dependency_ratio: float | None = None
    convergence_time_seconds: float = 0.0
    batch_wall_clock_seconds: float = 0.0
    throughput_tasks_per_minute: float = 0.0
    governor_interventions: int | None = None
    cost_per_success_usd: float | None = None
    queue_depth_p95: float | None = None
    sink_settlement_latency_p95_s: float = 0.0
    retry_cascade_count: int | None = None
    dlq_volume: int = 0
    dead_letter_ratio: float = 0.0
    dead_letter_ratio_limit: float = 0.14
    duplicate_terminal_settlements: int | None = None
    zombie_blocked_tasks: int | None = None
    sink_truth_divergence_count: int | None = None
    unstable_governor_oscillation_count: int | None = None
    unmeasured_metrics: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScaleCapabilityDelta:
    lanes_observed: int = 0
    lanes_updated: int = 0
    avg_pass_rate: float = 0.0
    avg_escalation_rate: float = 0.0
    avg_transient_failure_rate: float = 0.0


@dataclass(slots=True)
class ScaleRunReport:
    manifest: ScaleRunManifest
    metrics: ScaleMetricsSummary
    verdict: ScaleFailureVerdict
    capability_delta: ScaleCapabilityDelta
    hold_reason_distribution: dict[str, int] = field(default_factory=dict)
    dead_letter_reason_distribution: dict[str, int] = field(default_factory=dict)
    latency_by_lane: dict[str, float] = field(default_factory=dict)
    queue_depth_distribution: dict[str, int] = field(default_factory=dict)
    governor_summary: dict[str, Any] = field(default_factory=dict)
    sink_summary: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "metrics": asdict(self.metrics),
            "verdict": asdict(self.verdict),
            "capability_delta": asdict(self.capability_delta),
            "hold_reason_distribution": dict(self.hold_reason_distribution),
            "dead_letter_reason_distribution": dict(self.dead_letter_reason_distribution),
            "latency_by_lane": dict(self.latency_by_lane),
            "queue_depth_distribution": dict(self.queue_depth_distribution),
            "governor_summary": dict(self.governor_summary),
            "sink_summary": dict(self.sink_summary),
            "artifacts": dict(self.artifacts),
            "notes": list(self.notes),
        }

    def save(self, report_dir: str | Path) -> Path:
        path = Path(report_dir)
        path.mkdir(parents=True, exist_ok=True)
        json_path = path / "scale_report.json"
        json_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return json_path
