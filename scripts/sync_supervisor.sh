#!/usr/bin/env bash
# Watchdog: keeps the sequential sync alive. If it ever dies (crash, OOM,
# bad network), it is started again — the sync resumes exactly where it
# stopped from the DB checkpoint (no duplicates).
#
# Run:  setsid nohup bash scripts/sync_supervisor.sh >> logs/supervisor.log 2>&1 &
cd "$(dirname "$0")/.."
mkdir -p logs
GAP="${SYNC_GLOBAL_GAP:-1.5}"
echo "[$(date '+%F %T')] supervisor up (gap=${GAP}s)"
while true; do
  if ! pgrep -f "main.py sync-[s]eq" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] sync not running — starting (resumes from DB checkpoint)"
    echo "=== $(date '+%F %T') supervisor: sync (re)started, gap ${GAP}s ===" >> logs/sync.log
    ( SYNC_GLOBAL_GAP="$GAP" setsid nohup python3 -u main.py sync-seq >> logs/sync.log 2>&1 & )
    sleep 10
  fi
  sleep 60
done
