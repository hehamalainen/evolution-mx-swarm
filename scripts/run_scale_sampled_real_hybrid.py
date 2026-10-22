#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_aether_live_proof import _copy_worktree
from simulation.scale_harness import run_hybrid_control_plane_simulation, run_isolated_worker_load


def _build_payload(
    *,
    logical_lanes: int,
    seed: int,
    sampled_real_workers: int,
    active_concurrency: int,
    slice_key: str,
    timeout_seconds: int,
    sink_mode: str,
    sampled_real_settlement_p95_limit: float,
    control_plane_report: dict[str, Any],
    sampled_real_report: dict[str, Any],
) -> dict[str, Any]:
    sampled_real_share = sampled_real_workers / max(logical_lanes, 1)
    control_plane_verdict = control_plane_report["verdict"]
    control_plane_metrics = control_plane_report["metrics"]
    sampled_real_verdict = sampled_real_report["verdict"]
    sampled_real_metrics = sampled_real_report["metrics"]

    reasons: list[str] = []
    if sampled_real_share > 0.05:
        reasons.append(f"sampled_real_share_exceeded:{sampled_real_share:.6f}>0.050000")
    if not control_plane_verdict["structural_pass"]:
        reasons.append("control_plane_structural_fail")
    if not control_plane_verdict["loss_budget_pass"]:
        reasons.append("control_plane_loss_budget_fail")
    if not sampled_real_verdict["passed"]:
        reasons.append("sampled_real_worker_batch_fail")
    if sampled_real_metrics["dlq_volume"] > 0:
        reasons.append(f"sampled_real_dlq_detected:{sampled_real_metrics['dlq_volume']}")
    if (sampled_real_metrics.get("sink_truth_divergence_count") or 0) > 0:
        reasons.append("sampled_real_sink_truth_divergence")
    if sampled_real_metrics["sink_settlement_latency_p95_s"] > sampled_real_settlement_p95_limit:
        reasons.append(
            "sampled_real_settlement_latency_exceeded:"
            f"{sampled_real_metrics['sink_settlement_latency_p95_s']:.6f}>{sampled_real_settlement_p95_limit:.6f}"
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest": {
            "phase": "sampled_real_hybrid",
            "logical_lanes": logical_lanes,
            "seed": seed,
            "sampled_real_workers": sampled_real_workers,
            "sampled_real_share": sampled_real_share,
            "active_concurrency": active_concurrency,
            "slice": slice_key,
            "timeout_seconds": timeout_seconds,
            "sink_mode": sink_mode,
            "sampled_real_settlement_p95_limit": sampled_real_settlement_p95_limit,
        },
        "overall_verdict": {
            "passed": not reasons,
            "reasons": reasons,
        },
        "control_plane": {
            "run_id": control_plane_report["manifest"]["run_id"],
            "structural_pass": control_plane_verdict["structural_pass"],
            "loss_budget_pass": control_plane_verdict["loss_budget_pass"],
            "verified_landings": control_plane_metrics["verified_landings"],
            "total_terminal_tasks": control_plane_metrics["total_terminal_tasks"],
            "dead_letter_ratio": control_plane_metrics["dead_letter_ratio"],
            "dead_letter_ratio_limit": control_plane_metrics["dead_letter_ratio_limit"],
            "dlq_volume": control_plane_metrics["dlq_volume"],
            "hold_reason_distribution": control_plane_report["hold_reason_distribution"],
            "dead_letter_reason_distribution": control_plane_report["dead_letter_reason_distribution"],
        },
        "sampled_real": {
            "run_id": sampled_real_report["manifest"]["run_id"],
            "passed": sampled_real_verdict["passed"],
            "verified_landings": sampled_real_metrics["verified_landings"],
            "total_terminal_tasks": sampled_real_metrics["total_terminal_tasks"],
            "dlq_volume": sampled_real_metrics["dlq_volume"],
            "dead_letter_ratio": sampled_real_metrics["dead_letter_ratio"],
            "sink_settlement_latency_p95_s": sampled_real_metrics["sink_settlement_latency_p95_s"],
            "batch_wall_clock_seconds": sampled_real_metrics["batch_wall_clock_seconds"],
            "throughput_tasks_per_minute": sampled_real_metrics["throughput_tasks_per_minute"],
            "sink_truth_divergence_count": sampled_real_metrics["sink_truth_divergence_count"],
        },
        "artifacts": {
            "control_plane_run_id": control_plane_report["manifest"]["run_id"],
            "sampled_real_run_id": sampled_real_report["manifest"]["run_id"],
        },
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    manifest = payload["manifest"]
    overall = payload["overall_verdict"]
    control = payload["control_plane"]
    sampled = payload["sampled_real"]
    lines = [
        "# MX Sampled-Real Hybrid Report",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- phase: `{manifest['phase']}`",
        f"- logical_lanes: `{manifest['logical_lanes']}`",
        f"- seed: `{manifest['seed']}`",
        f"- sampled_real_workers: `{manifest['sampled_real_workers']}`",
        f"- sampled_real_share: `{manifest['sampled_real_share']}`",
        f"- active_concurrency: `{manifest['active_concurrency']}`",
        f"- sink_mode: `{manifest['sink_mode']}`",
        f"- overall_passed: `{overall['passed']}`",
        f"- overall_reasons: `{overall['reasons']}`",
        "",
        "## Control Plane",
        f"- run_id: `{control['run_id']}`",
        f"- structural_pass: `{control['structural_pass']}`",
        f"- loss_budget_pass: `{control['loss_budget_pass']}`",
        f"- verified_landings: `{control['verified_landings']}`",
        f"- total_terminal_tasks: `{control['total_terminal_tasks']}`",
        f"- dead_letter_ratio: `{control['dead_letter_ratio']}`",
        f"- dead_letter_ratio_limit: `{control['dead_letter_ratio_limit']}`",
        f"- dlq_volume: `{control['dlq_volume']}`",
        f"- dead_letter_reason_distribution: `{control['dead_letter_reason_distribution']}`",
        "",
        "## Sampled Real",
        f"- run_id: `{sampled['run_id']}`",
        f"- passed: `{sampled['passed']}`",
        f"- verified_landings: `{sampled['verified_landings']}`",
        f"- total_terminal_tasks: `{sampled['total_terminal_tasks']}`",
        f"- dlq_volume: `{sampled['dlq_volume']}`",
        f"- dead_letter_ratio: `{sampled['dead_letter_ratio']}`",
        f"- sink_settlement_latency_p95_s: `{sampled['sink_settlement_latency_p95_s']}`",
        f"- batch_wall_clock_seconds: `{sampled['batch_wall_clock_seconds']}`",
        f"- throughput_tasks_per_minute: `{sampled['throughput_tasks_per_minute']}`",
        f"- sink_truth_divergence_count: `{sampled['sink_truth_divergence_count']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bounded sampled-real hybrid MX scale extension.")
    parser.add_argument("--logical-lanes", type=int, default=1000, help="Logical lane count.")
    parser.add_argument("--seed", type=int, default=23, help="Deterministic RNG seed.")
    parser.add_argument(
        "--sampled-real-workers",
        type=int,
        default=50,
        help="Real logical workers in the capped batch.",
    )
    parser.add_argument("--active-concurrency", type=int, default=4, help="Maximum real worker concurrency.")
    parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run for real workers.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Per-proof timeout.")
    parser.add_argument(
        "--sink-mode",
        choices=("aether", "mock_github"),
        default="aether",
        help="Sink mode for the sampled-real worker batch.",
    )
    parser.add_argument(
        "--sampled-real-settlement-p95-limit",
        type=float,
        default=10.0,
        help="Maximum acceptable sampled-real sink settlement p95 in seconds.",
    )
    parser.add_argument(
        "--report-root",
        default=str(REPO_ROOT / "reports" / "scale_sampled_real_hybrid"),
        help="Report root directory.",
    )
    args = parser.parse_args()

    control_plane_report = run_hybrid_control_plane_simulation(
        logical_lanes=args.logical_lanes,
        seed=args.seed,
    ).to_dict()

    with tempfile.TemporaryDirectory(prefix="mx_scale_sampled_real_template_") as temp_dir:
        template_root = Path(temp_dir) / "source"
        _copy_worktree(REPO_ROOT, template_root)
        sampled_real_report = asyncio.run(
            run_isolated_worker_load(
                logical_workers=args.sampled_real_workers,
                active_concurrency=args.active_concurrency,
                slice_key=args.slice,
                timeout_seconds=args.timeout_seconds,
                repo_root=REPO_ROOT,
                source_root=template_root,
                sink_mode=args.sink_mode,
            )
        ).to_dict()

    payload = _build_payload(
        logical_lanes=args.logical_lanes,
        seed=args.seed,
        sampled_real_workers=args.sampled_real_workers,
        active_concurrency=args.active_concurrency,
        slice_key=args.slice,
        timeout_seconds=args.timeout_seconds,
        sink_mode=args.sink_mode,
        sampled_real_settlement_p95_limit=args.sampled_real_settlement_p95_limit,
        control_plane_report=control_plane_report,
        sampled_real_report=sampled_real_report,
    )
    report_root = Path(args.report_root)
    report_dir = report_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "scale_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (report_dir / "scale_report.md").write_text(_render_markdown(payload), encoding="utf-8")
    print(f"Scale sampled-real hybrid report written to {report_dir}")
    return 0 if payload["overall_verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
