#!/usr/bin/env bash
# PaaS entrypoint — every step is idempotent and resume-safe, so this is
# safe to run on every deploy/restart on JustRunMy, Railway, Render, or a VPS.
set -euo pipefail
cd "$(dirname "$0")"

DB_PATH="${DB_PATH:-/data/archive.db}"
SES_NAME="${SESSION_FILE:-/data/tg_sess}"     # Telethon session name (file = $SES_NAME.session)
SES_DIR="$(dirname "$SES_NAME")"
REPO_ROOT="$(pwd)"

# ── 1. Restore the progress DB on first boot (repo → persistent volume) ────
#    The repo holds data/archive.db as an LFS object. The slim Docker image
#    may not contain it at all (.dockerignore), so try in order:
#      a) real file in the image working tree → copy
#      b) LFS pointer in the image → git lfs pull
#      c) nothing in the image → sparse partial-clone the one file via the
#         token URL in GIT_PUSH_URL
mkdir -p "$(dirname "$DB_PATH")"
is_pointer() { head -c 60 "$1" 2>/dev/null | grep -q "git-lfs"; }

if [ ! -s "$DB_PATH" ]; then
  echo "── DB bootstrap: $DB_PATH missing ───────────────────────"
  restored=""
  if [ -s "data/archive.db" ] && ! is_pointer "data/archive.db"; then
    cp "data/archive.db" "$DB_PATH"
    restored="repo copy (real LFS file)"
  elif [ -s "data/archive.db" ] && is_pointer "data/archive.db"; then
    echo "    repo copy is an LFS pointer — trying git lfs pull…"
    if command -v git-lfs >/dev/null 2>&1 \
       && git lfs pull --include="data/archive.db" 2>/dev/null \
       && [ -s "data/archive.db" ] && ! is_pointer "data/archive.db"; then
      cp "data/archive.db" "$DB_PATH"
      restored="git lfs pull"
    fi
  fi
  if [ -z "$restored" ] && [ -n "${GIT_PUSH_URL:-}" ] && command -v git-lfs >/dev/null 2>&1; then
    echo "    fetching the DB from GitHub (sparse LFS clone)…"
    TDIR="$(mktemp -d)"
    if git clone -q --depth 1 --filter=blob:none --sparse "$GIT_PUSH_URL" "$TDIR" 2>/dev/null \
       && ( cd "$TDIR" && git sparse-checkout set data 2>/dev/null \
            && git lfs pull --include="data/archive.db" 2>/dev/null \
            && [ -s data/archive.db ] && ! is_pointer "data/archive.db" ); then
      cp "$TDIR/data/archive.db" "$DB_PATH"
      restored="sparse LFS clone"
    fi
    rm -rf "$TDIR"
  fi
  if [ -n "$restored" ]; then
    echo "    restored via $restored"
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

# status writer → out/progress.json. When the dashboard runs in the same
# container (EMBED_STATUS=1) it renders this itself — don't double-render.
if [ "${EMBED_STATUS:-0}" != "1" ] && ! up "scripts/status_fi[l]e.py"; then
  ( setsid nohup python3 -u scripts/status_file.py >> logs/status_file.log 2>&1 & )
  echo "── status writer started (out/progress.json every ~25s) ──"
fi

# git push loop → keeps the repo's progress.json fresh (only when the image
# actually contains a git repo)
if ! up "scripts/push_loo[p].sh"; then
  if [ -d .git ] && [ -n "${GIT_PUSH_URL:-}" ]; then
    ( setsid nohup bash scripts/push_loop.sh >> logs/push.log 2>&1 & )
    echo "── git push loop started (progress → GitHub every 60s) ──"
  else
    echo "── git push loop skipped (no git repo in image) ──"
  fi
fi

# live dashboard → http://0.0.0.0:${PORT:-8080} — progress + /auth web login
if ! up "deploy/dashboard/app.p[y]"; then
  ( setsid nohup env PROGRESS_FILE="$(pwd)/out/progress.json" \
      EMBED_STATUS=1 \
      PORT="${PORT:-8080}" \
      python3 -u deploy/dashboard/app.py >> logs/dashboard_web.log 2>&1 & )
  echo "── live dashboard started on 0.0.0.0:${PORT:-8080} (progress + /auth) ──"
fi

# ── 3. Session gate: wait for the web login (phone → OTP) if none exists ───
if [ -z "${TELEGRAM_STRING_SESSION:-}" ] \
   && [ ! -f "$SES_NAME.session" ] && [ ! -f "$SES_DIR/session_ready" ]; then
  echo "── NO TELEGRAM SESSION YET ───────────────────────────────────────────"
  echo "   Open your app URL and go to /auth"
  echo "   → enter your phone number → enter the code Telegram sends you"
  echo "   (→ cloud password if 2FA is on). The sync starts within ~10s."
  while true; do
    if [ -f "$SES_NAME.session" ] && [ -f "$SES_DIR/session_ready" ]; then
      break
    fi
    sleep 10
  done
  echo "── session ready — starting pipeline ──"
fi

# ── 4. The pipeline (foreground — platform sees its logs) ──────────────────
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
