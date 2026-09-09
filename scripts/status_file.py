"""File-based live dashboard.

The platform now blocks direct clicks on the sandbox HTTP URL (it demands an
'e2b-traffic-access-token' header a browser can't send). So the live
dashboard is this FILE: it re-renders out/DASHBOARD.md every ~25 seconds.
Open DASHBOARD.md from the workspace files and refresh to see new numbers.

Usage:  python3 scripts/status_file.py
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import dashboard as D  # reuse stats()/flood_state()/fmt_bytes

OUT = os.path.join(ROOT, "out", "DASHBOARD.md")
LOG = os.path.join(ROOT, "logs", "sync.log")
ARCHIVE_NAMES = {"archive_1": "Dump 1", "archive_2": "Dump 2", "archive_3": "Dump 3"}
ORDER = ["archive_1", "archive_2", "archive_3"]


def current_dump() -> str | None:
    """Latest 'starting sync' marker in the log → which dump is being filled."""
    try:
        with open(LOG, "r", errors="replace") as f:
            tail = f.readlines()[-400:]
    except OSError:
        return None
    for line in reversed(tail):
        m = re.search(r"\[(archive_\d)\] starting sync", line)
        if m:
            return m.group(1)
    return None


def recent_failures() -> list[str]:
    import sqlite3
    try:
        con = sqlite3.connect(D.DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT archive_id, operation, topic_title, last_error, last_attempt_at "
            "FROM processing_jobs WHERE status='FAILED' "
            "ORDER BY last_attempt_at DESC LIMIT 10"
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            when = (r["last_attempt_at"] or "")[:19].replace("T", " ")
            out.append(f"{when} · {ARCHIVE_NAMES.get(r['archive_id'], r['archive_id'])} · "
                       f"{r['operation']} '{(r['topic_title'] or '')[:40]}' · "
                       f"{(r['last_error'] or '')[:80]}")
        return out
    except Exception:
        return []


def fmt_eta(s):
    if s is None:
        return "—"
    s = int(s)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return "".join(parts)


def bar(done, total, width=22):
    if total <= 0:
        return ""
    p = min(1.0, done / total)
    filled = int(p * width)
    return "█" * filled + "░" * (width - filled) + f" {p*100:5.1f}%"


def render():
    st = D.stats()
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["now"]))
    arch = st["archives"]
    cur = current_dump()
    flood = st["flood"]
    uni = st["unique"]

    lines = []
    lines.append("# 📦 Archive Sync — LIVE")
    lines.append("")
    lines.append(f"_auto-refreshes every ~25 s — just refresh this file · "
                 f"updated {now}_")
    lines.append("")
    alive = st["sync_alive"]
    lines.append(f"**Sync process:** {'🟢 running' if alive else '🔴 NOT running (check logs/sync.log)'}")
    if cur:
        pos = ORDER.index(cur) + 1 if cur in ORDER else 0
        lines.append(f"**Now filling:** {ARCHIVE_NAMES.get(cur, cur)} "
                     f"(dump {pos} of 3 — one at a time, as planned)")
    else:
        lines.append("**Now filling:** — (starting up…)")
    if st.get("complete"):
        lines.append("")
        lines.append("## ✅ ALL DONE — all 3 dumps complete")
    lines.append("")

    # overall (unique files — tracks the lead dump in sequential mode)
    lines.append("## Progress (lead dump)")
    lines.append("")
    lines.append(f"| | files | size |")
    lines.append(f"|---|---|---|")
    lines.append(f"| done | **{uni['done_files']:,}** / {uni['total_files']:,} | "
                 f"{D.fmt_bytes(uni['done_bytes'])} / {D.fmt_bytes(uni['total_bytes'])} |")
    lines.append(f"| left | {uni['remaining_files']:,} | {D.fmt_bytes(uni['remaining_bytes'])} |")
    lines.append("")
    lines.append(bar(uni["done_files"], uni["total_files"]))
    lines.append("")
    rate = st["pace_files_per_sec"]
    rpm = rate * 60
    lines.append(f"**Pace:** {rpm:.0f} files/min "
                 f"(ETA to finish this dump {fmt_eta(st['eta_seconds'])} — "
                 f"the other two dumps then run at roughly the same pace, "
                 f"on cached references)")
    lines.append("")

    # per-dump table
    lines.append("## The 3 dumps")
    lines.append("")
    lines.append("| dump | files done | size copied | failed |")
    lines.append("|---|---|---|---|")
    for a in ORDER:
        d = arch.get(a, {"done": 0, "failed": 0, "bytes": 0})
        mark = "⏳" if a == cur else ("✔" if d["done"] >= uni["total_files"] else "·")
        lines.append(f"| {mark} {ARCHIVE_NAMES[a]} | {d['done']:,} / {uni['total_files']:,} "
                     f"| {D.fmt_bytes(d.get('bytes', 0))} | {d['failed']} |")
    lines.append("")

    # telegram rate limit
    lines.append("## ⏱ Telegram rate limit")
    lines.append("")
    act = flood.get("active")
    if act:
        lines.append(f"🛑 **currently waiting: {act['remaining_s']}s left** "
                     f"({ARCHIVE_NAMES.get(act['archive'], act['archive'])}, "
                     f"{act['what']})")
    else:
        lines.append("🟢 no active wait — sending at full pace")
    rec = flood.get("recent") or []
    if rec:
        lines.append("")
        lines.append("recent waits:")
        for r in rec[:6]:
            lines.append(f"- {r['time']} · {ARCHIVE_NAMES.get(r['archive'], r['archive'])} · "
                         f"{r['seconds']}s · {r['what']}")
    lines.append(f"- seconds spent waiting (last hour): {flood.get('waited_last_hour_s', 0)}")
    lines.append("")

    # failures
    fails = recent_failures()
    lines.append("## ❌ Recent failures")
    lines.append("")
    if fails:
        for f_ in fails[:10]:
            lines.append(f"- {f_}")
    else:
        lines.append("none 🎉")
    lines.append("")

    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, OUT)

    # ── progress.json — the live progress bus for the deployable dashboard ──
    try:
        j = {
            "ts": st["now"],
            "updated": now,
            "sync_alive": alive,
            "mode": "sequential",
            "current_dump": cur,
            "complete": bool(st.get("complete")),
            "unique": uni,
            "archives": {k: {
                "done": v.get("done", 0),
                "failed": v.get("failed", 0),
                "inflight": v.get("inflight", 0),
                "bytes": v.get("bytes", 0),
                "total": v.get("total"),
            } for k, v in arch.items()},
            "review_saved_done": st.get("saved_review_done", 0),
            "pace_files_per_min": round(st.get("pace_files_per_sec", 0) * 60, 1),
            "eta_seconds": st.get("eta_seconds"),
            "flood": {
                "active": flood.get("active"),
                "recent": (flood.get("recent") or [])[:6],
                "waited_last_hour_s": flood.get("waited_last_hour_s", 0),
            },
            "topics": st.get("topics", {}),
            "failures": fails[:10],
        }
        jtmp = os.path.join(ROOT, "out", "progress.json.tmp")
        with open(jtmp, "w") as f:
            json.dump(j, f)
        os.replace(jtmp, os.path.join(ROOT, "out", "progress.json"))
    except Exception as e:
        print(f"progress.json error: {e}", flush=True)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print(f"status file dashboard → {OUT} (every 25s)", flush=True)
    while True:
        try:
            render()
        except Exception as e:
            print(f"render error: {e}", flush=True)
        time.sleep(25)


if __name__ == "__main__":
    main()
