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

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_aether_live_proof import _copy_worktree
from simulation.scale_harness import run_isolated_worker_load


def _render_markdown(payload: dict) -> str:
    metrics = payload["metrics"]
    verdict = payload["verdict"]
    manifest = payload["manifest"]
    lines = [
        "# MX Scale Worker-Load Report",
        "",
        f"- generated_at: `{manifest['generated_at']}`",
        f"- run_id: `{manifest['run_id']}`",
        f"- logical_workers: `{manifest['logical_workers']}`",
        f"- active_concurrency: `{manifest['active_concurrency']}`",
        f"- phase: `{manifest['phase']}`",
        f"- sink_mode: `{manifest['sink_mode']}`",
        f"- passed: `{verdict['passed']}`",
        f"- verified_landings: `{metrics['verified_landings']}`",
        f"- total_terminal_tasks: `{metrics['total_terminal_tasks']}`",
        f"- convergence_time_seconds: `{metrics['convergence_time_seconds']}`",
        f"- batch_wall_clock_seconds: `{metrics['batch_wall_clock_seconds']}`",
        f"- throughput_tasks_per_minute: `{metrics['throughput_tasks_per_minute']}`",
        f"- sink_settlement_latency_p95_s: `{metrics['sink_settlement_latency_p95_s']}`",
        f"- dlq_volume: `{metrics['dlq_volume']}`",
        f"- verdict_reasons: `{verdict['reasons']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the canned worker-load scale harness.")
    parser.add_argument("--logical-workers", type=int, default=100, help="Logical worker count.")
    parser.add_argument("--active-concurrency", type=int, default=10, help="Maximum concurrent proof workers.")
    parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Per-proof timeout.")
    parser.add_argument(
        "--sink-mode",
        choices=("mock_github", "aether"),
        default="mock_github",
        help="Sink mode for isolated proofs. Use mock_github during Aether soak windows.",
    )
    parser.add_argument(
        "--report-root",
        default=str(REPO_ROOT / "reports" / "scale_worker_load"),
        help="Report root directory.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="mx_scale_worker_template_") as temp_dir:
        template_root = Path(temp_dir) / "source"
        _copy_worktree(REPO_ROOT, template_root)
        report = asyncio.run(
            run_isolated_worker_load(
                logical_workers=args.logical_workers,
                active_concurrency=args.active_concurrency,
                slice_key=args.slice,
                timeout_seconds=args.timeout_seconds,
                repo_root=REPO_ROOT,
                source_root=template_root,
                sink_mode=args.sink_mode,
            )
        )
    report_root = Path(args.report_root)
    report_dir = report_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    (report_dir / "scale_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (report_dir / "scale_report.md").write_text(_render_markdown(payload), encoding="utf-8")
    print(f"Scale worker-load report written to {report_dir}")
    return 0 if report.verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
