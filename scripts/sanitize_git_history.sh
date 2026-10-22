#!/usr/bin/env bash
# Sanitize historical credentials and out-of-scope blobs in an isolated mirror clone.
# NOTE: Rewriting repository history or refs requires explicit owner approval.
set -euo pipefail

if [[ "${1:-}" != "--i-have-owner-approval" ]]; then
  cat >&2 <<'EOF'
Refusing to rewrite git history without explicit approval.
Usage (after revoking exposed tokens and obtaining owner approval):
  ./scripts/sanitize_git_history.sh --i-have-owner-approval <source-repo> <output-mirror-dir>

Historical paths targeted for removal when approved:
  - reg_output.txt
  - molt_client.py
  - scripts/build_ris_graph.py
  - scripts/ris_collector.py
  - scripts/zoo_theory_analyzer.py
  - evidence/aether.db
  - evidence/final_canary_evidence.json
  - evidence/canary_evidence_20260720t214602z.json
  - data/epstein_analytics.db
  - vault/actor_map/
EOF
  exit 2
fi

SOURCE_REPO="${2:?source repository path required}"
OUTPUT_MIRROR="${3:?output mirror path required}"

git clone --no-local --mirror "$SOURCE_REPO" "$OUTPUT_MIRROR"
git -C "$OUTPUT_MIRROR" filter-repo --invert-paths \
  --path reg_output.txt \
  --path molt_client.py \
  --path scripts/build_ris_graph.py \
  --path scripts/ris_collector.py \
  --path scripts/zoo_theory_analyzer.py \
  --path evidence/aether.db \
  --path evidence/final_canary_evidence.json \
  --path evidence/canary_evidence_20260720t214602z.json \
  --path data/epstein_analytics.db \
  --path-glob 'vault/actor_map/*'
