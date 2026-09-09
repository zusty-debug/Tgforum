#!/usr/bin/env bash
# Push a fresh RESUMABLE SNAPSHOT: the SQLite DB (via Git LFS) + progress +
# code. Run this occasionally (e.g. every 2-4h) and/or before you step away.
# Even if the running host dies, `git clone` + `bash start.sh` resumes the
# sync from this snapshot (worst case it redoes the files copied since the
# last snapshot — dedup-safe, no duplicates).
#
# Needs git-lfs installed and a token (see push_progress.sh for GIT_PUSH_URL).
set -euo pipefail
cd "$(dirname "$0")/.."

git lfs install --local >/dev/null 2>&1 || { echo "git-lfs not available — skipping DB snapshot"; exit 0; }

git add data/archive.db out/progress.json
git add -u 2>/dev/null || true
if git diff --cached --quiet; then
  echo "DB snapshot unchanged — nothing to push"; exit 0
fi
git commit -q -m "snapshot: resumable DB + progress"
if [ -n "${GIT_PUSH_URL:-}" ]; then
  git push -q "$GIT_PUSH_URL" HEAD
else
  git push -q origin HEAD
fi
echo "DB snapshot pushed ($(du -h data/archive.db | cut -f1))"
