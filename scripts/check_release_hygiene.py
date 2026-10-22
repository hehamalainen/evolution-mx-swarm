#!/usr/bin/env python3
"""Release hygiene scanner for credential material and release-boundary enforcement."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = REPO_ROOT / "RELEASE_ALLOWLIST.json"

PROVIDER_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("moltbook_api_key", re.compile(r"moltbook_sk_[A-Za-z0-9_-]{8,}")),
    ("moltbook_claim_token", re.compile(r"moltbook_claim_[A-Za-z0-9_-]{8,}")),
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_project_key", re.compile(r"sk-proj-[A-Za-z0-9_-]{16,}")),
    ("github_pat", re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
)

PRIVATE_PATH_PATTERN = re.compile(r"/Users/[A-Za-z0-9._-]+")

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
        "run_temp.py",
        "tests/test_run_suite.py",
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


def scan_release_boundary(
    repo_root: Path = REPO_ROOT,
    allowlist_path: Path | None = None,
) -> list[dict[str, object]]:
    """Verify tracked files conform to RELEASE_ALLOWLIST.json and contain no private paths."""
    target_allowlist = allowlist_path or (repo_root / "RELEASE_ALLOWLIST.json")
    if not target_allowlist.is_file():
        return [{"path": "RELEASE_ALLOWLIST.json", "line": 1, "kind": "missing_allowlist"}]

    allowlist = json.loads(target_allowlist.read_text(encoding="utf-8"))
    allowed_root = set(allowlist.get("allowed_root_files", []))
    allowed_dirs = set(allowlist.get("allowed_top_level_directories", []))
    allowed_reports = set(allowlist.get("allowed_reports", []))
    forbidden_exts = tuple(allowlist.get("forbidden_extensions", []))
    forbidden_prefixes = tuple(allowlist.get("forbidden_path_prefixes", []))

    findings: list[dict[str, object]] = []
    for rel_path in list_tracked_files(repo_root):
        if any(rel_path.endswith(ext) for ext in forbidden_exts):
            findings.append({"path": rel_path, "line": 1, "kind": "forbidden_extension"})
        if any(rel_path.startswith(prefix) for prefix in forbidden_prefixes):
            findings.append({"path": rel_path, "line": 1, "kind": "forbidden_prefix"})

        if "/" not in rel_path:
            if rel_path not in allowed_root:
                findings.append({"path": rel_path, "line": 1, "kind": "unapproved_root_file"})
        else:
            top_dir = rel_path.split("/", 1)[0]
            if top_dir == "reports":
                if rel_path not in allowed_reports:
                    findings.append({"path": rel_path, "line": 1, "kind": "unapproved_report_file"})
            elif top_dir not in allowed_dirs:
                findings.append({"path": rel_path, "line": 1, "kind": "unapproved_directory"})

        file_path = repo_root / rel_path
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            if PRIVATE_PATH_PATTERN.search(line):
                findings.append({"path": rel_path, "line": line_no, "kind": "private_path"})

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify MX-Swarm release credential and boundary hygiene.")
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to scan.",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    cred_findings = scan_tracked_credentials(repo_root)
    boundary_findings = scan_release_boundary(repo_root)
    findings = [*cred_findings, *boundary_findings]
    if findings:
        print(json.dumps({"ok": False, "findings": findings}, indent=2))
        return 1

    print(json.dumps({"ok": True, "findings": []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
