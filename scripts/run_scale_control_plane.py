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
        f"- passed: `{verdict['passed']}`",
        f"- verified_landings: `{metrics['verified_landings']}`",
        f"- total_terminal_tasks: `{metrics['total_terminal_tasks']}`",
        f"- false_success_rate: `{metrics['false_success_rate']}`",
        f"- blocked_dependency_ratio: `{metrics['blocked_dependency_ratio']}`",
        f"- convergence_time_seconds: `{metrics['convergence_time_seconds']}`",
        f"- batch_wall_clock_seconds: `{metrics['batch_wall_clock_seconds']}`",
        f"- throughput_tasks_per_minute: `{metrics['throughput_tasks_per_minute']}`",
        f"- governor_interventions: `{metrics['governor_interventions']}`",
        f"- cost_per_success_usd: `{metrics['cost_per_success_usd']}`",
        f"- queue_depth_p95: `{metrics['queue_depth_p95']}`",
        f"- sink_settlement_latency_p95_s: `{metrics['sink_settlement_latency_p95_s']}`",
        f"- retry_cascade_count: `{metrics['retry_cascade_count']}`",
        f"- dlq_volume: `{metrics['dlq_volume']}`",
        f"- zombie_blocked_tasks: `{metrics['zombie_blocked_tasks']}`",
        f"- unstable_governor_oscillation_count: `{metrics['unstable_governor_oscillation_count']}`",
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
