#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from scripts.run_gate2_local_parallel import execute_parallel_corpus, load_slice_specs
from shared.schemas import ModelTier, RoutingTarget
from shared.utils import MXConfig
from shared.utils.metrics import get_budget_snapshot


def _resolve_frontier_llm_override(args: argparse.Namespace) -> dict[str, object]:
    config = MXConfig.load_or_create(str(REPO_ROOT / "mx.config.json"))
    if config.frontier_llm:
        payload = config.frontier_llm.to_dict()
    else:
        payload = config.llm.to_dict()

    if args.provider:
        payload["provider"] = args.provider
    if args.model:
        payload["model"] = args.model
    if args.base_url:
        payload["base_url"] = args.base_url
    if args.api_key:
        payload["api_key"] = args.api_key
    return payload


def _write_report(
    report_dir: Path,
    *,
    slice_key: str,
    frontier_llm: dict[str, object],
    result_payload: dict[str, object],
    budget_snapshot: dict[str, object],
) -> None:
    report_path = report_dir / "frontier_smoke.md"
    payload_path = report_dir / "frontier_smoke.json"

    with open(payload_path, "w") as handle:
        json.dump(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "slice_key": slice_key,
                "frontier_llm": frontier_llm,
                "result": result_payload,
                "budget_snapshot": budget_snapshot,
            },
            handle,
            indent=2,
        )

    lines = [
        "# Frontier Smoke Report",
        "",
        f"- generated_at: `{datetime.now(UTC).isoformat()}`",
        f"- slice_key: `{slice_key}`",
        f"- frontier_provider: `{frontier_llm.get('provider')}`",
        f"- frontier_model: `{frontier_llm.get('model')}`",
        f"- frontier_budget_consumed_usd: `{budget_snapshot.get('frontier_spend_today_usd')}`",
        f"- frontier_freeze_status: `{budget_snapshot.get('frontier_frozen')}`",
        "",
        "## Result",
        "",
        f"- status: `{result_payload['status']}`",
        f"- model_tier: `{result_payload['model_tier']}`",
        f"- estimated_cost_bucket: `{result_payload['estimated_cost_bucket']}`",
        f"- false_success_rate: `{result_payload['false_success_rate']}`",
        f"- duplicate_settlement_events: `{result_payload['duplicate_settlement_events']}`",
        f"- queue_depth_end: `{result_payload['queue_depth_end']}`",
        f"- dead_letter_depth_end: `{result_payload['dead_letter_depth_end']}`",
        f"- run_dir: `{result_payload['run_dir']}`",
        f"- log_path: `{result_payload['log_path']}`",
    ]
    with open(report_path, "w") as handle:
        handle.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a frontier-tier smoke proof on one constrained slice.")
    parser.add_argument("--slice", default="jsonpeek", help="Slice key to run.")
    parser.add_argument("--corpus", default=None, help="Optional corpus manifest.")
    parser.add_argument("--provider", default=None, help="Override frontier provider.")
    parser.add_argument("--model", default=None, help="Override frontier model.")
    parser.add_argument("--base-url", default=None, help="Override frontier base URL.")
    parser.add_argument("--api-key", default=None, help="Override frontier API key.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Timeout per run.")
    args = parser.parse_args()

    slices = [slice_spec for slice_spec in load_slice_specs(args.corpus) if slice_spec.key == args.slice]
    if not slices:
        raise SystemExit(f"Unknown slice key: {args.slice}")

    frontier_llm = _resolve_frontier_llm_override(args)
    config = MXConfig.load_or_create(str(REPO_ROOT / "mx.config.json"))
    if config.budget.frontier_daily_usd_cap <= 0 or config.budget.max_frontier_concurrency <= 0:
        raise SystemExit("Frontier smoke requires configured frontier budget cap and frontier concurrency.")
    report_dir = REPO_ROOT / "reports" / "frontier_smoke" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)

    results = asyncio.run(
        execute_parallel_corpus(
            runs=1,
            worker_count=1,
            timeout_seconds=args.timeout_seconds,
            report_dir=report_dir,
            slices=slices,
            retry_max=0,
            frontier_llm_override=frontier_llm,
            task_model_tier=ModelTier.FRONTIER,
            routing_target=RoutingTarget.FRONTIER,
            routing_reason="frontier_smoke",
        )
    )
    result = results[0]
    budget_snapshot = get_budget_snapshot(config.budget)
    _write_report(
        report_dir,
        slice_key=args.slice,
        frontier_llm=frontier_llm,
        result_payload={
            "status": result.status,
            "model_tier": ModelTier.FRONTIER.value,
            "estimated_cost_bucket": "high",
            "false_success_rate": result.false_success_rate,
            "duplicate_settlement_events": result.duplicate_settlement_events,
            "queue_depth_end": result.queue_depth_end,
            "dead_letter_depth_end": result.dead_letter_depth_end,
            "run_dir": result.run_dir,
            "log_path": result.log_path,
        },
        budget_snapshot=budget_snapshot,
    )
    print(f"Frontier smoke report written to {report_dir}")
    print(
        "Frontier budget "
        f"spent=${budget_snapshot.get('frontier_spend_today_usd', 0.0)} "
        f"freeze={budget_snapshot.get('frontier_frozen', False)} "
        f"cost_per_success=${budget_snapshot.get('cost_per_success_usd', 0.0)}"
    )
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
