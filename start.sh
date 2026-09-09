#!/usr/bin/env bash
# PaaS entrypoint — every step is idempotent and resume-safe, so this is
# safe to run on every deploy. New files arriving in the source channel
# are picked up automatically (incremental scan).
set -euo pipefail
echo "── scan (incremental) ─────────────────────────────"
python main.py scan
echo "── classify ────────────────────────────────────────"
python main.py classify
echo "── sync-seq (one dump at a time, resumes from checkpoint) ────"
python main.py sync-seq
echo "── rebuild-index ───────────────────────────────────"
python main.py rebuild-index
echo "✔ pipeline pass complete — sleeping until next deploy"
tail -f /dev/null
