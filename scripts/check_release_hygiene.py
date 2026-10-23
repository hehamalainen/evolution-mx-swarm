#!/usr/bin/env python3
"""Release hygiene scanner for credential material and release-boundary enforcement."""

from __future__ import annotations

import argparse
import hashlib
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
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ),
)

# SHA-256 digests of historical credential tokens, 32-char suffixes, and 12-char prefixes
# from ancestor commits (001b73c6 and d66d392b). Stored as one-way hashes so no plaintext
# credential fragment ever appears in the scanner source.
REVOKED_CREDENTIAL_FRAGMENT_SHA256: frozenset[str] = frozenset(
    {
        "208dbb34231a027a6bc2c8ce7881fa0ebc9ad807c3fb9e9400e24c49047c47b2",
        "2786ee25d2e8640b4e5346995c7163fb5e2e0c76acc63bc2ab7cb64a60fd58f8",
        "35b79c4a49cd93bfe4a7d4329413be7cb63eac459bd90d030a4a64c19f74460e",
        "3ee74c9c6a7e056692c5b6b0f29ee47b7049d176c47ed2b86c4d4dd03502e99d",
        "40d319406fdb190626b1eb0d492403e6f14a5626e2f31b85a052e1bf5b39fabd",
        "56b231cd76e12ea2674c17caec3a55bceb9a5c4d24df818a5cecc4118c86ff4e",
        "8b3e0bc117a524edf80953434f5bd094e34bc4778bdd30ba1f4c09a11bbb78f2",
        "c851942e07bed572c5dadf7ee34a34052ec06d38aa0b6f39f7dd666413ad5ee4",
        "dc1070ac2c4f1a85c582d12d8da024874e785f910ddd77547fe3ba8f87b19205",
    }
)

TOKEN_CANDIDATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{12,}")
CONCAT_JOIN_PATTERN = re.compile(r"""['"]\s*\+?\s*['"]""")
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


def _has_revoked_credential_fragment(text: str) -> bool:
    for match in TOKEN_CANDIDATE_PATTERN.finditer(text):
        token = match.group(0)
        candidates = {token, token[:12]}
        if len(token) >= 32:
            candidates.add(token[-32:])
        for candidate in candidates:
            digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            if digest in REVOKED_CREDENTIAL_FRAGMENT_SHA256:
                return True
    return False


def scan_text_for_credentials(text: str, rel_path: str) -> list[dict[str, object]]:
    """Scan a single text payload for live-format provider tokens, private keys, or historical fragments."""
    findings: list[dict[str, object]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        collapsed_line = CONCAT_JOIN_PATTERN.sub("", line)
        for kind, pattern in PROVIDER_TOKEN_PATTERNS:
            if pattern.search(line) or pattern.search(collapsed_line):
                findings.append(
                    {
                        "path": rel_path,
                        "line": line_no,
                        "kind": kind,
                    }
                )
        if _has_revoked_credential_fragment(line) or _has_revoked_credential_fragment(collapsed_line):
            findings.append(
                {
                    "path": rel_path,
                    "line": line_no,
                    "kind": "revoked_historical_credential_fragment",
                }
            )

    # Also check multi-line concatenated string literals across the whole file
    collapsed_full = CONCAT_JOIN_PATTERN.sub("", text)
    existing_kinds = {str(item["kind"]) for item in findings}
    for kind, pattern in PROVIDER_TOKEN_PATTERNS:
        if kind not in existing_kinds and pattern.search(collapsed_full):
            findings.append({"path": rel_path, "line": 1, "kind": kind})
    if (
        "revoked_historical_credential_fragment" not in existing_kinds
        and _has_revoked_credential_fragment(collapsed_full)
    ):
        findings.append(
            {
                "path": rel_path,
                "line": 1,
                "kind": "revoked_historical_credential_fragment",
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


EXPLICIT_FORBIDDEN_PATHS: frozenset[str] = frozenset(
    {
        *FORBIDDEN_CREDENTIAL_PATHS,
        "dashboard_v2/public/x-logo.png",
        "dashboard_v2/public/xai-logo.png",
        "dashboard_v2/public/opengraph.jpg",
    }
)


def is_path_allowed(rel_path: str, allowlist: dict[str, object]) -> tuple[bool, str]:
    """Return (allowed, violation_kind) for a repository-relative path."""
    allowed_root = set(allowlist.get("allowed_root_files", []))  # type: ignore[arg-type]
    allowed_dirs = set(allowlist.get("allowed_top_level_directories", []))  # type: ignore[arg-type]
    allowed_reports = set(allowlist.get("allowed_reports", []))  # type: ignore[arg-type]
    forbidden_exts = tuple(allowlist.get("forbidden_extensions", []))  # type: ignore[arg-type]
    forbidden_prefixes = tuple(allowlist.get("forbidden_path_prefixes", []))  # type: ignore[arg-type]

    if rel_path in EXPLICIT_FORBIDDEN_PATHS:
        return False, "forbidden_credential_or_asset_file"
    if any(rel_path.endswith(str(ext)) for ext in forbidden_exts):
        return False, "forbidden_extension"
    if any(rel_path.startswith(str(prefix)) for prefix in forbidden_prefixes):
        return False, "forbidden_prefix"
    if "/" not in rel_path:
        if rel_path not in allowed_root:
            return False, "unapproved_root_file"
        return True, ""

    top_dir = rel_path.split("/", 1)[0]
    if top_dir == "reports":
        if rel_path not in allowed_reports:
            return False, "unapproved_report_file"
        return True, ""
    if top_dir not in allowed_dirs:
        return False, "unapproved_directory"
    return True, ""


def scan_release_boundary(
    repo_root: Path = REPO_ROOT,
    allowlist_path: Path | None = None,
) -> list[dict[str, object]]:
    """Verify tracked files conform to RELEASE_ALLOWLIST.json and contain no private paths."""
    target_allowlist = allowlist_path or (repo_root / "RELEASE_ALLOWLIST.json")
    if not target_allowlist.is_file():
        return [{"path": "RELEASE_ALLOWLIST.json", "line": 1, "kind": "missing_allowlist"}]

    allowlist = json.loads(target_allowlist.read_text(encoding="utf-8"))
    findings: list[dict[str, object]] = []
    for rel_path in list_tracked_files(repo_root):
        ok, kind = is_path_allowed(rel_path, allowlist)
        if not ok:
            findings.append({"path": rel_path, "line": 1, "kind": kind})

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


def scan_git_history(
    repo_root: Path = REPO_ROOT,
    allowlist_path: Path | None = None,
) -> list[dict[str, object]]:
    """Scan all reachable git commits and blobs for disallowed paths, credentials, and private paths."""
    target_allowlist = allowlist_path or (repo_root / "RELEASE_ALLOWLIST.json")
    if not target_allowlist.is_file():
        return [{"path": "RELEASE_ALLOWLIST.json", "line": 1, "kind": "missing_allowlist"}]
    allowlist = json.loads(target_allowlist.read_text(encoding="utf-8"))

    findings: list[dict[str, object]] = []
    shallow_proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--is-shallow-repository"],
        check=True,
        capture_output=True,
        text=True,
    )
    if shallow_proc.stdout.strip().lower() == "true" or (repo_root / ".git" / "shallow").exists():
        findings.append({"path": ".git/shallow", "line": 1, "kind": "shallow_repository_history"})

    rev_proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-list", "--all", "--objects"],
        check=True,
        capture_output=True,
        text=True,
    )
    raw_log_proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "log",
            "--all",
            "--raw",
            "--no-abbrev",
            "-m",
            "--root",
            "--pretty=format:",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    seen_path_kinds: set[tuple[str, str]] = set()
    historical_paths: set[str] = set()
    oid_to_paths: dict[str, set[str]] = {}
    all_oids: list[str] = []
    seen_oids: set[str] = set()

    for line in rev_proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(" ", 1)
        oid = parts[0].strip()
        if oid and oid not in seen_oids:
            seen_oids.add(oid)
            all_oids.append(oid)
        if len(parts) == 2:
            rel_path = parts[1].strip()
            if rel_path:
                oid_to_paths.setdefault(oid, set()).add(rel_path)

    for line in raw_log_proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith(":"):
            continue
        meta_part, sep, path_part = stripped.partition("\t")
        if not sep or not path_part:
            continue
        # Handle rename/copy tab-separated paths (status R/C has old_path\tnew_path)
        paths_in_entry = [p.strip() for p in path_part.split("\t") if p.strip()]
        meta_tokens = meta_part.split()
        # Format: :<old_mode> <new_mode> <old_oid> <new_oid> <status>
        entry_oids = [tok for tok in meta_tokens[2:4] if set(tok) != {"0"}]
        for rel_path in paths_in_entry:
            historical_paths.add(rel_path)
            for entry_oid in entry_oids:
                oid_to_paths.setdefault(entry_oid, set()).add(rel_path)

    if not all_oids:
        return findings

    batch_input = "".join(f"{oid}\n" for oid in all_oids).encode("utf-8")
    batch_proc = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input=batch_input,
        check=True,
        capture_output=True,
    )
    stream = batch_proc.stdout
    pos = 0
    total_len = len(stream)
    while pos < total_len:
        nl = stream.find(b"\n", pos)
        if nl == -1:
            break
        header = stream[pos:nl].decode("utf-8", errors="replace").strip()
        pos = nl + 1
        header_parts = header.split()
        if len(header_parts) < 3:
            continue
        oid, obj_type, size_str = header_parts[0], header_parts[1], header_parts[2]
        size = int(size_str)
        payload = stream[pos : pos + size]
        pos += size + 1  # skip trailing newline
        if obj_type == "blob":
            for rel_path in oid_to_paths.get(oid, set()):
                historical_paths.add(rel_path)
            primary_path = sorted(oid_to_paths.get(oid, {""}))[0]
            label = f"{oid[:8]}:{primary_path}"
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
        elif obj_type in ("commit", "tag"):
            label = f"{obj_type}:{oid[:8]}"
            text = payload.decode("utf-8", errors="replace")
        else:
            continue

        for item in scan_text_for_credentials(text, label):
            findings.append(item)
        for line_no, text_line in enumerate(text.splitlines(), start=1):
            if PRIVATE_PATH_PATTERN.search(text_line):
                findings.append(
                    {
                        "path": label,
                        "line": line_no,
                        "kind": "history_private_path",
                    }
                )
                break

    for rel_path in sorted(historical_paths):
        ok, kind = is_path_allowed(rel_path, allowlist)
        if not ok and (rel_path, kind) not in seen_path_kinds:
            seen_path_kinds.add((rel_path, kind))
            findings.append({"path": rel_path, "line": 1, "kind": f"history_{kind}"})

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify MX-Swarm release credential and boundary hygiene.")
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to scan.",
    )
    parser.add_argument(
        "--scan-history",
        action="store_true",
        help="Also scan all reachable git history objects and paths.",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    cred_findings = scan_tracked_credentials(repo_root)
    boundary_findings = scan_release_boundary(repo_root)
    history_findings = scan_git_history(repo_root) if args.scan_history else []
    findings = [*cred_findings, *boundary_findings, *history_findings]
    if findings:
        print(json.dumps({"ok": False, "findings": findings}, indent=2))
        return 1

    print(json.dumps({"ok": True, "findings": []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
