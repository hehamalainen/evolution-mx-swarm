#!/usr/bin/env bash
# Sanitize historical credentials, removed brand assets, and out-of-scope blobs in an isolated mirror clone.
# NOTE: Rewriting repository history or refs requires explicit owner approval.
set -euo pipefail

if [[ "${1:-}" != "--i-have-owner-approval" ]]; then
  cat >&2 <<'EOF'
Refusing to rewrite git history without explicit approval.
Usage (after revoking exposed tokens and obtaining owner approval):
  ./scripts/sanitize_git_history.sh --i-have-owner-approval <source-repo> <output-mirror-dir>

When approved, this script clones an isolated mirror and purges all historical paths outside
RELEASE_ALLOWLIST.json, including:
  - reg_output.txt, molt_client.py, scripts/build_ris_graph.py, scripts/ris_collector.py, scripts/zoo_theory_analyzer.py
  - evidence/*, data/*, vault/*, temp_analysis/*, .runtime/*, .agents/*
  - dashboard_v2/public/x-logo.png, dashboard_v2/public/xai-logo.png, dashboard_v2/public/opengraph.jpg
  - all historical reports/* and root scratch files not present in RELEASE_ALLOWLIST.json
EOF
  exit 2
fi

SOURCE_REPO="${2:?source repository path required}"
OUTPUT_MIRROR="${3:?output mirror path required}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALLOWLIST_FILE="${SCRIPT_DIR}/../RELEASE_ALLOWLIST.json"

git clone --no-local --mirror "$SOURCE_REPO" "$OUTPUT_MIRROR"

REMOVE_PATHS_FILE="$(mktemp)"
trap 'rm -f "$REMOVE_PATHS_FILE"' EXIT

python3 - "$OUTPUT_MIRROR" "$ALLOWLIST_FILE" "$REMOVE_PATHS_FILE" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

mirror_dir = Path(sys.argv[1])
allowlist = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
out_file = Path(sys.argv[3])

allowed_root = set(allowlist.get("allowed_root_files", []))
allowed_dirs = set(allowlist.get("allowed_top_level_directories", []))
allowed_reports = set(allowlist.get("allowed_reports", []))
forbidden_exts = tuple(allowlist.get("forbidden_extensions", []))
forbidden_prefixes = tuple(allowlist.get("forbidden_path_prefixes", []))
explicit_forbidden = {
    "reg_output.txt",
    "molt_client.py",
    "scripts/build_ris_graph.py",
    "scripts/ris_collector.py",
    "scripts/zoo_theory_analyzer.py",
    "dashboard_v2/public/x-logo.png",
    "dashboard_v2/public/xai-logo.png",
    "dashboard_v2/public/opengraph.jpg",
    "run_temp.py",
    "tests/test_run_suite.py",
}

proc = subprocess.run(
    ["git", "--git-dir", str(mirror_dir), "log", "--all", "--pretty=format:", "--name-only"],
    check=True,
    capture_output=True,
    text=True,
)
all_historical_paths = {line.strip() for line in proc.stdout.splitlines() if line.strip()}

to_remove = set()
for rel_path in sorted(all_historical_paths):
    if rel_path in explicit_forbidden:
        to_remove.add(rel_path)
        continue
    if any(rel_path.endswith(ext) for ext in forbidden_exts):
        to_remove.add(rel_path)
        continue
    if any(rel_path.startswith(prefix) for prefix in forbidden_prefixes):
        to_remove.add(rel_path)
        continue
    if "/" not in rel_path:
        if rel_path not in allowed_root:
            to_remove.add(rel_path)
    else:
        top_dir = rel_path.split("/", 1)[0]
        if top_dir == "reports":
            if rel_path not in allowed_reports:
                to_remove.add(rel_path)
        elif top_dir not in allowed_dirs:
            to_remove.add(rel_path)

out_file.write_text("\n".join(sorted(to_remove)) + "\n", encoding="utf-8")
PY

git -C "$OUTPUT_MIRROR" filter-repo --invert-paths --paths-from-file "$REMOVE_PATHS_FILE" --force
