#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

echo "Starting RIS Continuous Collection Loop..."

while true; do
    echo "Running collection cycle at $(date)"
    python3 scripts/ris_collector.py || echo "Cycle failed. Retrying next cycle."
    # Sleep for 5 minutes between cycles
    sleep 300
done
