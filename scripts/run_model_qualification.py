#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from scripts.run_gate2_local_parallel import execute_parallel_corpus, load_slice_specs
from shared.utils import MXConfig
from shared.utils.qualification import load_qualification_corpus, summarize_qualification


def _write_report(
    *,
    report_dir: Path,
    corpus_name: str,
    corpus_path: Path,
    model_label: str,
    results: list[object],
    summary: object,
) -> None:
    json_path = report_dir / "model_qualification.json"
    md_path = report_dir / "model_qualification.md"

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus_name": corpus_name,
        "corpus_path": str(corpus_path),
        "model_label": model_label,
        "summary": summary.to_dict(),
        "runs": [asdict(result) for result in results],
    }
    with open(json_path, "w") as handle:
        json.dump(payload, handle, indent=2)

    lines = [
        "# Model Qualification Report",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- corpus_name: `{corpus_name}`",
        f"- corpus_path: `{corpus_path}`",
        f"- model_label: `{model_label}`",
        f"- passed: `{summary.passed}`",
        "",
        "## Summary",
        "",
        f"- expected_runs: `{summary.expected_runs}`",
        f"- completed_runs: `{summary.completed_runs}`",
        f"- total_slice_attempts: `{summary.total_slice_attempts}`",
        f"- format_compliance_rate: `{summary.format_compliance_rate:.2%}`",
        f"- deterministic_acceptance_rate: `{summary.deterministic_acceptance_rate:.2%}`",
        f"- false_success_rate: `{summary.false_success_rate}`",
        f"- duplicate_settlement_events: `{summary.duplicate_settlement_events}`",
        f"- duplicate_claim_events: `{summary.duplicate_claim_events}`",
        f"- lifecycle_bug_events: `{summary.lifecycle_bug_events}`",
        "",
    ]
    if summary.failure_reasons:
        lines.extend(["## Failures", ""])
        for reason in summary.failure_reasons:
            lines.append(f"- `{reason}`")
        lines.append("")

    with open(md_path, "w") as handle:
        handle.write("\n".join(lines))


def _model_safe_worker_cap(config: MXConfig) -> int:
    provider = (config.llm.provider or "").lower()
    model = (config.llm.model or "").lower()
    if provider == "ollama" and "7b" in model:
        return 1
    return max(1, config.worker.max_workers)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reusable narrow-task model qualification.")
    parser.add_argument(
        "--corpus",
        default=str(REPO_ROOT / "qualification" / "corpora" / "gate2_local_open_weight.json"),
        help="Path to qualification corpus manifest.",
    )
    parser.add_argument("--runs", type=int, default=None, help="Override corpus default run count.")
    parser.add_argument("--workers", type=int, default=None, help="Override corpus default worker count.")
    parser.add_argument("--timeout-seconds", type=int, default=None, help="Override corpus timeout.")
    parser.add_argument("--model-label", default="unspecified-model", help="Label stored in the report.")
    args = parser.parse_args()

    corpus_path = Path(args.corpus).resolve()
    corpus = load_qualification_corpus(corpus_path)
    slices = load_slice_specs(str(corpus_path))
    runs = args.runs or corpus.default_runs
    requested_workers = args.workers or corpus.default_workers
    config = MXConfig.load(str(REPO_ROOT / "mx.config.json"))
    workers = min(requested_workers, _model_safe_worker_cap(config))
    timeout_seconds = args.timeout_seconds or corpus.timeout_seconds

    report_dir = REPO_ROOT / "reports" / "model_qualification" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir.mkdir(parents=True, exist_ok=True)

    results = asyncio.run(
        execute_parallel_corpus(
            runs=runs,
            worker_count=workers,
            timeout_seconds=timeout_seconds,
            report_dir=report_dir,
            slices=slices,
            retry_max=0,
        )
    )
    summary = summarize_qualification(corpus, results, expected_runs=runs)
    _write_report(
        report_dir=report_dir,
        corpus_name=corpus.name,
        corpus_path=corpus_path,
        model_label=args.model_label,
        results=results,
        summary=summary,
    )
    print(f"Model qualification report written to {report_dir}")
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
