#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from simulation.scale_harness import run_hybrid_control_plane_simulation


def _fmt_metric(value: object) -> str:
    return "unmeasured" if value is None else str(value)


def _render_markdown(payload: dict) -> str:
    metrics = payload["metrics"]
    verdict = payload["verdict"]
    manifest = payload["manifest"]
    governor = payload["governor_summary"]
    lines = [
        "# MX Scale Control-Plane Report",
        "",
        f"- generated_at: `{manifest['generated_at']}`",
        f"- run_id: `{manifest['run_id']}`",
        f"- logical_lanes: `{manifest['logical_lanes']}`",
        f"- phase: `{manifest['phase']}`",
        f"- sink_mode: `{payload.get('sink_summary', {}).get('mode', manifest.get('sink_mode'))}`",
        f"- passed: `{verdict['passed']}`",
        f"- verified_landings: `{_fmt_metric(metrics['verified_landings'])}`",
        f"- total_terminal_tasks: `{_fmt_metric(metrics['total_terminal_tasks'])}`",
        f"- false_success_rate: `{_fmt_metric(metrics['false_success_rate'])}`",
        f"- blocked_dependency_ratio: `{_fmt_metric(metrics['blocked_dependency_ratio'])}`",
        f"- convergence_time_seconds: `{_fmt_metric(metrics['convergence_time_seconds'])}`",
        f"- batch_wall_clock_seconds: `{_fmt_metric(metrics['batch_wall_clock_seconds'])}`",
        f"- throughput_tasks_per_minute: `{_fmt_metric(metrics['throughput_tasks_per_minute'])}`",
        f"- governor_interventions: `{_fmt_metric(metrics['governor_interventions'])}`",
        f"- cost_per_success_usd: `{_fmt_metric(metrics['cost_per_success_usd'])}`",
        f"- queue_depth_p95: `{_fmt_metric(metrics['queue_depth_p95'])}`",
        f"- sink_settlement_latency_p95_s: `{_fmt_metric(metrics['sink_settlement_latency_p95_s'])}`",
        f"- retry_cascade_count: `{_fmt_metric(metrics['retry_cascade_count'])}`",
        f"- dlq_volume: `{_fmt_metric(metrics['dlq_volume'])}`",
        f"- duplicate_terminal_settlements: `{_fmt_metric(metrics['duplicate_terminal_settlements'])}`",
        f"- zombie_blocked_tasks: `{_fmt_metric(metrics['zombie_blocked_tasks'])}`",
        f"- sink_truth_divergence_count: `{_fmt_metric(metrics['sink_truth_divergence_count'])}`",
        f"- unstable_governor_oscillation_count: `{_fmt_metric(metrics['unstable_governor_oscillation_count'])}`",
        f"- unmeasured_metrics: `{metrics.get('unmeasured_metrics', [])}`",
        f"- verdict_reasons: `{verdict['reasons']}`",
        f"- governor_summary: `{governor}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the 1000-lane hybrid MX control-plane scale harness.")
    parser.add_argument("--logical-lanes", type=int, default=1000, help="Logical lane count.")
    parser.add_argument("--logical-tasks", type=int, default=None, help="Optional logical task override.")
    parser.add_argument("--seed", type=int, default=7, help="Deterministic RNG seed.")
    parser.add_argument(
        "--report-root",
        default=str(REPO_ROOT / "reports" / "scale_control_plane"),
        help="Report root directory.",
    )
    args = parser.parse_args()

    report = run_hybrid_control_plane_simulation(
        logical_lanes=args.logical_lanes,
        logical_tasks=args.logical_tasks,
        seed=args.seed,
    )

    report_root = Path(args.report_root)
    report_dir = report_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    (report_dir / "scale_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (report_dir / "scale_report.md").write_text(_render_markdown(payload), encoding="utf-8")
    print(f"Scale control-plane report written to {report_dir}")
    return 0 if report.verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
