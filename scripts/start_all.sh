#!/usr/bin/env bash
# ONE-COMMAND (re)START of everything in this sandbox:
#   1. sync watchdog      — keeps the sequential sync alive (resumes from DB)
#   2. status file writer — out/DASHBOARD.md + out/progress.json every ~25s
#   3. local dashboard    — http://0.0.0.0:8000 (sandbox preview, if available)
#   4. git progress push  — pushes out/progress.json to the repo every 60s
#                           (only effective once git remote + token are set)
#
# Safe to run repeatedly: each service is a no-op if already running.
cd "$(dirname "$0")/.."
mkdir -p logs out

up() { pgrep -f "$1" >/dev/null 2>&1; }

if up "scripts/sync_supervis[o]r.sh"; then
  echo "  supervisor: already running"
else
  ( setsid nohup bash scripts/sync_supervisor.sh >> logs/supervisor.log 2>&1 & )
  echo "  supervisor: started (it starts the sync if needed)"
fi
if up "scripts/status_fi[l]e.py"; then
  echo "  status writer: already running"
else
  ( setsid nohup python3 -u scripts/status_file.py >> logs/status_file.log 2>&1 & )
  echo "  status writer: started"
fi
if up "scripts/dashboar[d].py"; then
  echo "  local dashboard: already running (port 8000)"
else
  ( setsid nohup python3 -u scripts/dashboard.py >> logs/dashboard.log 2>&1 & )
  echo "  local dashboard: started (port 8000)"
fi
if up "scripts/push_loo[p].sh"; then
  echo "  git push loop: already running"
else
  ( setsid nohup bash scripts/push_loop.sh >> logs/push.log 2>&1 & )
  echo "  git push loop: started (needs GIT_PUSH_URL — see README)"
fi
echo "all services ensured — check logs/ for details"
