#!/usr/bin/env bash
# Sanitize historical credentials, removed brand assets, and out-of-scope blobs into a clean repository.
# NOTE: Rewriting repository history or refs requires explicit owner approval.
set -euo pipefail

if [[ "${1:-}" != "--i-have-owner-approval" ]]; then
  cat >&2 <<'EOF'
Refusing to rewrite git history without explicit approval.
Usage (after revoking exposed tokens and obtaining owner approval):
  ./scripts/sanitize_git_history.sh --i-have-owner-approval <source-repo> <output-repo-dir>

When approved, this script builds a sanitized repository containing only publication refs and:
  1. Removes all historical paths outside RELEASE_ALLOWLIST.json (including reg_output.txt,
     molt_client.py, collector scripts, *.db databases, evidence/*, data/*, vault/*,
     removed brand logos in dashboard_v2/public/*, and non-allowlisted reports/*).
  2. Scrubs or replaces any historical blob on allowed paths (such as 0c55c201:tests/test_release_hygiene.py)
     that matches credential patterns, split/concatenated credential literals, or revoked credential
     SHA-256 fragment fingerprints.
  3. Verifies the resulting repository with:
     python3 scripts/check_release_hygiene.py --repo-root <output-repo-dir> --scan-history
EOF
  exit 2
fi

SOURCE_REPO="${2:?source repository path required}"
OUTPUT_REPO="${3:?output repository path required}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ALLOWLIST_FILE="${REPO_ROOT}/RELEASE_ALLOWLIST.json"

rm -rf "$OUTPUT_REPO"
mkdir -p "$OUTPUT_REPO"
git init -q "$OUTPUT_REPO"

python3 - "$SOURCE_REPO" "$OUTPUT_REPO" "$ALLOWLIST_FILE" "$REPO_ROOT" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

source_repo = Path(sys.argv[1]).resolve()
output_repo = Path(sys.argv[2]).resolve()
allowlist_path = Path(sys.argv[3]).resolve()
scanner_root = Path(sys.argv[4]).resolve()

sys.path.insert(0, str(scanner_root))
from scripts.check_release_hygiene import (  # noqa: E402
    PRIVATE_PATH_PATTERN,
    is_path_allowed,
    scan_text_for_credentials,
)

allowlist = json.loads(allowlist_path.read_text(encoding="utf-8"))

# Load clean HEAD blobs from source_repo for fallback replacement when an allowed path
# had a credential leak or private path in an earlier commit.
head_ls = subprocess.run(
    ["git", "-C", str(source_repo), "ls-tree", "-r", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()

head_blobs: dict[str, bytes] = {}
for line in head_ls:
    if not line.strip():
        continue
    meta, rel_path = line.split("\t", 1)
    oid = meta.split()[2]
    ok, _ = is_path_allowed(rel_path, allowlist)
    if ok:
        blob_bytes = subprocess.run(
            ["git", "-C", str(source_repo), "cat-file", "blob", oid],
            check=True,
            capture_output=True,
        ).stdout
        head_blobs[rel_path] = blob_bytes

# Determine the chronological commit list along HEAD
rev_list = subprocess.run(
    ["git", "-C", str(source_repo), "rev-list", "--reverse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()

# Batch-cache sanitized blobs by (source_blob_oid, rel_path) -> new_blob_oid in output_repo
sanitized_blob_cache: dict[tuple[str, str], str] = {}


def sanitize_blob(source_oid: str, rel_path: str) -> str:
    cache_key = (source_oid, rel_path)
    if cache_key in sanitized_blob_cache:
        return sanitized_blob_cache[cache_key]

    raw = subprocess.run(
        ["git", "-C", str(source_repo), "cat-file", "blob", source_oid],
        check=True,
        capture_output=True,
    ).stdout

    replacement = raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    if text is not None:
        cred_hits = scan_text_for_credentials(text, rel_path)
        has_private_path = bool(PRIVATE_PATH_PATTERN.search(text))
        if cred_hits or has_private_path:
            if rel_path in head_blobs:
                replacement = head_blobs[rel_path]
            else:
                scrubbed = PRIVATE_PATH_PATTERN.sub("<REDACTED_LOCAL_PATH>", text)
                scrubbed_lines = []
                for line in scrubbed.splitlines():
                    if scan_text_for_credentials(line, rel_path):
                        scrubbed_lines.append("# [REDACTED HISTORICAL CREDENTIAL]")
                    else:
                        scrubbed_lines.append(line)
                replacement = ("\n".join(scrubbed_lines) + "\n").encode("utf-8")

    new_oid = (
        subprocess.run(
            ["git", "-C", str(output_repo), "hash-object", "-w", "--stdin"],
            input=replacement,
            check=True,
            capture_output=True,
        )
        .stdout.decode("utf-8")
        .strip()
    )
    sanitized_blob_cache[cache_key] = new_oid
    return new_oid


last_tree_oid: str | None = None
last_new_commit: str | None = None

for old_commit in rev_list:
    # Read commit metadata
    commit_raw = subprocess.run(
        ["git", "-C", str(source_repo), "cat-file", "commit", old_commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    header_part, _, message = commit_raw.partition("\n\n")
    author_name = "MX Swarm Team"
    author_email = "noreply@mx-swarm.local"
    author_date = "1700000000 +0000"
    committer_name = author_name
    committer_email = author_email
    committer_date = author_date

    for hline in header_part.splitlines():
        if hline.startswith("author "):
            m = re.match(r"^author (.+) <([^>]+)> (.+)$", hline)
            if m:
                author_name, author_email, author_date = m.group(1), m.group(2), m.group(3)
        elif hline.startswith("committer "):
            m = re.match(r"^committer (.+) <([^>]+)> (.+)$", hline)
            if m:
                committer_name, committer_email, committer_date = m.group(1), m.group(2), m.group(3)

    tree_entries = subprocess.run(
        ["git", "-C", str(source_repo), "ls-tree", "-r", old_commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    index_lines: list[str] = []
    for entry in tree_entries:
        if not entry.strip():
            continue
        meta, rel_path = entry.split("\t", 1)
        ok, _ = is_path_allowed(rel_path, allowlist)
        if not ok:
            continue
        mode, obj_type, oid = meta.split()
        if obj_type != "blob":
            continue
        clean_oid = sanitize_blob(oid, rel_path)
        index_lines.append(f"{mode} blob {clean_oid}\t{rel_path}")

    if not index_lines:
        continue

    # Build tree in output_repo using git update-index + write-tree
    subprocess.run(
        ["git", "-C", str(output_repo), "read-tree", "--empty"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(output_repo), "update-index", "--index-info"],
        input="\n".join(index_lines) + "\n",
        check=True,
        text=True,
    )
    new_tree_oid = subprocess.run(
        ["git", "-C", str(output_repo), "write-tree"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Skip commits whose sanitized tree is identical to the previous sanitized commit
    if new_tree_oid == last_tree_oid and last_new_commit is not None:
        continue

    cmd = ["git", "-C", str(output_repo), "commit-tree", new_tree_oid]
    if last_new_commit is not None:
        cmd.extend(["-p", last_new_commit])

    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": author_date,
        "GIT_COMMITTER_NAME": committer_name,
        "GIT_COMMITTER_EMAIL": committer_email,
        "GIT_COMMITTER_DATE": committer_date,
    }
    last_new_commit = subprocess.run(
        cmd,
        input=message,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    last_tree_oid = new_tree_oid

assert last_new_commit is not None, "No commits produced during history sanitization"

for branch in ("feature/aether-sprint-g-sync", "release/public-prototype-rc1", "main"):
    subprocess.run(
        ["git", "-C", str(output_repo), "update-ref", f"refs/heads/{branch}", last_new_commit],
        check=True,
    )
subprocess.run(
    ["git", "-C", str(output_repo), "symbolic-ref", "HEAD", "refs/heads/feature/aether-sprint-g-sync"],
    check=True,
)
subprocess.run(
    ["git", "-C", str(output_repo), "checkout", "-f", "feature/aether-sprint-g-sync"],
    check=True,
    capture_output=True,
)
subprocess.run(
    ["git", "-C", str(output_repo), "gc", "--prune=now", "--quiet"],
    check=True,
)
PY

python3 "${REPO_ROOT}/scripts/check_release_hygiene.py" --repo-root "$OUTPUT_REPO" --scan-history
