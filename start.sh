#!/usr/bin/env bash
# PaaS entrypoint — every step is idempotent and resume-safe, so this is
# safe to run on every deploy/restart on Railway, JustRunMy, Render, or a VPS.
set -euo pipefail
cd "$(dirname "$0")"

DB_PATH="${DB_PATH:-/data/archive.db}"
REPO_ROOT="$(pwd)"

# ── 1. Restore the progress DB on first boot (repo DB → persistent volume) ──
#    The DB in the repo is an LFS object. Depending on how the platform cloned
#    the repo, the working tree may contain either the real 66MB file or a
#    133-byte LFS pointer. Handle both, plus a token-based direct download.
mkdir -p "$(dirname "$DB_PATH")"
is_pointer() { head -c 60 "$1" 2>/dev/null | grep -q "git-lfs"; }

if [ ! -s "$DB_PATH" ]; then
  echo "── DB bootstrap: $DB_PATH missing ───────────────────────"
  if [ -s "data/archive.db" ] && ! is_pointer "data/archive.db"; then
    cp "data/archive.db" "$DB_PATH"
    echo "    restored from repo copy (real LFS file)"
  elif [ -s "data/archive.db" ] && is_pointer "data/archive.db"; then
    echo "    repo copy is an LFS pointer — trying git lfs pull…"
    if command -v git-lfs >/dev/null 2>&1 && git lfs pull --include="data/archive.db" 2>/dev/null; then
      cp "data/archive.db" "$DB_PATH"
      echo "    restored via git lfs pull"
    else
      # Last resort: sparse partial-clone just the DB file (LFS) using the
      # token URL in GIT_PUSH_URL (which the push loop needs anyway).
      if [ -n "${GIT_PUSH_URL:-}" ] && command -v git-lfs >/dev/null 2>&1; then
        TDIR="$(mktemp -d)"
        if git clone -q --depth 1 --filter=blob:none --sparse "$GIT_PUSH_URL" "$TDIR" 2>/dev/null \
           && ( cd "$TDIR" && git sparse-checkout set data 2>/dev/null \
                && git lfs pull --include="data/archive.db" 2>/dev/null \
                && [ -s data/archive.db ] && ! is_pointer "data/archive.db" ); then
          cp "$TDIR/data/archive.db" "$DB_PATH"
          echo "    restored via sparse LFS clone"
        fi
        rm -rf "$TDIR"
      fi
    fi
  fi
fi

if [ ! -s "$DB_PATH" ] || is_pointer "$DB_PATH"; then
  echo "!! DB bootstrap FAILED — $DB_PATH is missing or still an LFS pointer." >&2
  echo "!! Refusing to start with an empty DB (would break dedup / double-copy)." >&2
  echo "!! If you see this line, paste it to the assistant and we fix it together." >&2
  exit 1
fi
echo "── DB ready: $DB_PATH ($(du -h "$DB_PATH" | cut -f1)) — sync will resume from checkpoint ──"

# ── 2. Background services (no-op-safe) ────────────────────────────────────
mkdir -p logs out
up() { pgrep -f "$1" >/dev/null 2>&1; }

# status writer → out/progress.json (the live progress bus)
if up "scripts/status_fi[l]e.py"; then :; else
  ( setsid nohup python3 -u scripts/status_file.py >> logs/status_file.log 2>&1 & )
  echo "── status writer started (out/progress.json every ~25s) ──"
fi

# git push loop → keeps the deployed dashboard fed (needs GIT_PUSH_URL)
if up "scripts/push_loo[p].sh"; then :; else
  if [ -n "${GIT_PUSH_URL:-}" ]; then
    ( setsid nohup bash scripts/push_loop.sh >> logs/push.log 2>&1 & )
    echo "── git push loop started (progress → GitHub every 60s) ──"
  else
    echo "── git push loop SKIPPED (set GIT_PUSH_URL to feed the dashboard) ──"
  fi
fi

# ── 3. The pipeline (foreground — platform sees its logs) ──────────────────
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
