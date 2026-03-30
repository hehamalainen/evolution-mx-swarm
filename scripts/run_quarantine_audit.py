#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = REPO_ROOT / "reports" / "quarantine_audit"
SCAN_ROOTS = ("boss", "shared", "worker", "judge", "planner", "cli", "dashboard", "scripts", "docs", "vault", "temp_analysis", "data")
IGNORE_DIRS = {".git", ".venv", ".runtime", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "reports", "node_modules", "dist"}
KEYWORD_RE = re.compile(r"\b(moltbook|actor_map|antigravity)\b", re.IGNORECASE)
SECRET_RE = re.compile(r"moltbook_sk_[A-Za-z0-9_-]{20,}")
PATH_ONLY_PREFIXES = ("vault/actor_map/", "temp_analysis/", "data/")


def _classify(path: Path) -> str:
    path_str = path.as_posix()
    if path_str.startswith(("vault/actor_map/", "temp_analysis/", "data/")):
        return "move"
    if path_str in {
        "boss/agent_boss.py",
        "shared/utils/bazaar_client.py",
        "dashboard/api.py",
    }:
        return "review"
    if path_str.startswith("scripts/") and any(
        token in path.name
        for token in (
            "ris",
            "real_task_loop",
            "full_loop_orchestrator",
            "zoo_theory",
            "content_analytics",
            "message_trends",
            "snapshot_ris_graph",
            "build_ris_graph",
        )
    ):
        return "move"
    return "keep"


def _mask_secret(secret: str) -> str:
    if len(secret) <= 10:
        return "***"
    return f"{secret[:6]}***{secret[-4:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory MX vs Moltbook boundary surface.")
    parser.add_argument(
        "--report-root",
        default=str(REPORT_ROOT),
        help="Report root directory.",
    )
    parser.add_argument(
        "--fail-on-secrets",
        action="store_true",
        help="Exit non-zero if live Moltbook credentials are detected.",
    )
    args = parser.parse_args()

    findings: list[dict[str, object]] = []
    secret_hits: list[dict[str, str]] = []
    counts = {"move": 0, "review": 0, "keep": 0}

    for root_name in SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in IGNORE_DIRS for part in path.parts):
                continue
            rel = path.relative_to(REPO_ROOT)
            rel_posix = rel.as_posix()
            if rel_posix.startswith(PATH_ONLY_PREFIXES):
                classification = _classify(rel)
                counts[classification] += 1
                findings.append(
                    {
                        "path": rel_posix,
                        "classification": classification,
                    }
                )
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if KEYWORD_RE.search(content) or "actor_map" in rel_posix:
                classification = _classify(rel)
                counts[classification] += 1
                findings.append(
                    {
                        "path": rel_posix,
                        "classification": classification,
                    }
                )
            for hit in SECRET_RE.findall(content):
                secret_hits.append(
                    {
                        "path": rel_posix,
                        "secret": _mask_secret(hit),
                    }
                )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir = Path(args.report_root) / stamp
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": counts,
        "findings": findings,
        "secret_hits": secret_hits,
    }
    (report_dir / "quarantine_audit.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [
        "# MX Quarantine Audit",
        "",
        f"- move: `{counts['move']}`",
        f"- review: `{counts['review']}`",
        f"- keep: `{counts['keep']}`",
        f"- secret_hits: `{len(secret_hits)}`",
        "",
        "## Secret Hits",
    ]
    if secret_hits:
        lines.extend([f"- `{hit['path']}` -> `{hit['secret']}`" for hit in secret_hits])
    else:
        lines.append("- none")
    (report_dir / "quarantine_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Quarantine audit written to {report_dir}")
    return 1 if secret_hits and args.fail_on_secrets else 0


if __name__ == "__main__":
    raise SystemExit(main())
