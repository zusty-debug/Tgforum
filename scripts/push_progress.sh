#!/usr/bin/env bash
# Push the latest progress (out/progress.json) + any code changes to git so
# the deployed dashboard can pick them up. Idempotent — no-op if nothing new.
#
# Auth: set GIT_PUSH_URL to a token URL, e.g.
#   GIT_PUSH_URL=https://x-access-token:ghp_XXXX@github.com/you/repo.git
# ...or have `origin` already authenticated (ssh key / credential helper)
# and just leave GIT_PUSH_URL unset to push to origin.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f out/progress.json ] || { echo "no out/progress.json yet — nothing to push"; exit 0; }

git add out/progress.json
# code changes only — NEVER stage the live DB here (that's push_db_snapshot.sh's
# job; the DB changes every few seconds and is a 66MB LFS object)
git add -u -- . ':!data' 2>/dev/null || true

if git diff --cached --quiet; then
  exit 0   # nothing new
fi

note=$(python3 -c "
import json
try:
    d=json.load(open('out/progress.json')); u=d['unique']
    print(f\"{u['done_files']}/{u['total_files']} files · {d.get('pace_files_per_min',0):.0f} fpm · dump {d.get('current_dump') or '?'}\")
except Exception:
    print('progress update')
" 2>/dev/null || echo "progress update")
git commit -q -m "progress: $note"

if [ -n "${GIT_PUSH_URL:-}" ]; then
  TARGET="$GIT_PUSH_URL"
else
  TARGET="origin"
fi
if ! git push -q "$TARGET" HEAD 2>/dev/null; then
  # someone else pushed first (or history diverged) — rebase and retry once
  git pull --rebase -q "$TARGET" main 2>/dev/null || true
  git push -q "$TARGET" HEAD || echo "push failed (will retry next minute)"
fi
echo "pushed: $note"
