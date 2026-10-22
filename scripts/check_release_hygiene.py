#!/usr/bin/env python3
"""Release hygiene scanner for credential material and release-boundary enforcement."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PROVIDER_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("moltbook_api_key", re.compile(r"moltbook_sk_[A-Za-z0-9_-]{8,}")),
    ("moltbook_claim_token", re.compile(r"moltbook_claim_[A-Za-z0-9_-]{8,}")),
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_project_key", re.compile(r"sk-proj-[A-Za-z0-9_-]{16,}")),
    ("github_pat", re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
)

FORBIDDEN_CREDENTIAL_PATHS: frozenset[str] = frozenset(
    {
        "reg_output.txt",
        ".env",
        ".env.local",
        "credentials.json",
        "molt_client.py",
        "scripts/build_ris_graph.py",
        "scripts/ris_collector.py",
        "scripts/zoo_theory_analyzer.py",
    }
)


def list_tracked_files(repo_root: Path) -> list[str]:
    """Return tracked repository paths relative to repo_root."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def scan_text_for_credentials(text: str, rel_path: str) -> list[dict[str, object]]:
    """Scan a single text payload for live-format provider tokens."""
    findings: list[dict[str, object]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PROVIDER_TOKEN_PATTERNS:
            if pattern.search(line):
                findings.append(
                    {
                        "path": rel_path,
                        "line": line_no,
                        "kind": kind,
                    }
                )
    return findings


def scan_tracked_credentials(repo_root: Path = REPO_ROOT) -> list[dict[str, object]]:
    """Scan all tracked files in repo_root for forbidden credential files or tokens."""
    findings: list[dict[str, object]] = []
    for rel_path in list_tracked_files(repo_root):
        if rel_path in FORBIDDEN_CREDENTIAL_PATHS:
            findings.append({"path": rel_path, "line": 1, "kind": "forbidden_credential_file"})
        file_path = repo_root / rel_path
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text_for_credentials(content, rel_path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify MX-Swarm release credential hygiene.")
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to scan.",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    findings = scan_tracked_credentials(repo_root)
    if findings:
        print(json.dumps({"ok": False, "findings": findings}, indent=2))
        return 1

    print(json.dumps({"ok": True, "findings": []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
