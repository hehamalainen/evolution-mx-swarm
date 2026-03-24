#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MX_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
DEFAULT_FIXTURE = REPO_ROOT / "proof_fixtures" / "jsonpeek_canned_completion.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the middle-layer MX worker -> Aether proof with canned completion.")
    parser.add_argument("--slice", default="jsonpeek", help="Qualification slice to run.")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="Proof timeout.")
    parser.add_argument("--aether-base-url", default="http://127.0.0.1:8100", help="Aether base URL.")
    parser.add_argument("--repo-name", default=None, help="Optional Aether repository name.")
    parser.add_argument("--run-id", default=None, help="Explicit MX run identifier.")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="Canned completion fixture JSON.")
    args = parser.parse_args()

    cmd = [
        str(MX_VENV_PYTHON),
        "scripts/run_aether_live_proof.py",
        "--slice",
        args.slice,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--aether-base-url",
        args.aether_base_url,
        "--report-root",
        str(REPO_ROOT / "reports" / "aether_worker_proof"),
        "--canned-completion-file",
        str(Path(args.fixture).resolve()),
    ]
    if args.repo_name:
        cmd.extend(["--repo-name", args.repo_name])
    if args.run_id:
        cmd.extend(["--run-id", args.run_id])

    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
