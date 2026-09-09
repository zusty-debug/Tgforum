#!/usr/bin/env bash
# Keep pushing progress to git every INTERVAL seconds (default 60) so the
# deployed dashboard stays live. Run it under nohup/setsid:
#   setsid nohup bash scripts/push_loop.sh >/dev/null 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL="${PUSH_INTERVAL:-60}"
while true; do
  bash scripts/push_progress.sh || echo "push failed: $?"
  sleep "$INTERVAL"
done
