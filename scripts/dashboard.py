#!/usr/bin/env python3
"""Live sync dashboard — stdlib only, zero dependencies.

Serves:
  /           → auto-refreshing HTML dashboard (JS polls every 3 s)
  /api/stats  → JSON stats read straight from the live SQLite DB

Shows: files copied / total / remaining, bytes transferred / remaining,
per-forum progress bars, pace + ETA, topics created, failures, and
whether the sync process is still alive.
"""
import json
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(ROOT, "data", "archive.db"))
SYNC_LOG = os.path.join(ROOT, "logs", "sync.log")
PORT = int(os.environ.get("DASHBOARD_PORT", "8000"))

_FLOOD_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ WARNING organizer\.telegram: "
    r"\[(archive_\d)\] flood wait (\d+)s \(([^)]*)\)")


def flood_state() -> dict:
    """Parse the sync log tail for Telegram flood-wait events.

    Returns the currently-active wait (with live countdown) plus the most
    recent waits and total time spent waiting in the last hour.
    """
    try:
        with open(SYNC_LOG, "r", errors="ignore") as f:
            tail = f.readlines()[-300:]
    except OSError:
        return {"active": None, "recent": [], "waited_last_hour_s": 0}

    now_wall = time.time()
    events = []
    for line in tail:
        m = _FLOOD_RE.match(line)
        if not m:
            continue
        ts_s, arch, secs, what = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        try:
            ts = time.mktime(time.strptime(ts_s, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        events.append({"ts": ts, "archive": arch, "seconds": secs, "what": what})

    active = None
    waited = 0
    for e in events:
        elapsed = now_wall - e["ts"]
        if elapsed < e["seconds"] and e["seconds"] - elapsed > 1:
            # latest such event wins
            if active is None or e["ts"] > active["ts"]:
                active = e
        if now_wall - e["ts"] < 3600:
            waited += e["seconds"]

    recent = []
    for e in events[-6:][::-1]:
        remaining = max(0, e["seconds"] - (now_wall - e["ts"]))
        recent.append({
            "time": time.strftime("%H:%M:%S", time.localtime(e["ts"])),
            "archive": e["archive"], "seconds": e["seconds"],
            "what": e["what"], "remaining_s": int(remaining) if remaining > 1 else 0,
        })
    if active is not None:
        return {"active": {
                    "archive": active["archive"],
                    "what": active["what"],
                    "total_s": active["seconds"],
                    "remaining_s": int(active["seconds"] - (now_wall - active["ts"])),
                },
                "recent": recent, "waited_last_hour_s": int(waited)}
    return {"active": None, "recent": recent, "waited_last_hour_s": int(waited)}

# in-memory pace tracking: (monotonic, cumulative_done_files)
_PACE = []


def sync_alive() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="ignore")
        except OSError:
            continue
            if ("sync-all" in cmd or "sync-seq" in cmd) and ("python" in cmd or "main.py" in cmd):
                return True
    return False


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:,.1f} PB"


def stats() -> dict:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    q = lambda s, *a: conn.execute(s, a).fetchall()

    total_files = q("""SELECT COUNT(*) FROM group_members lm
                       WHERE lm.duplicate_of_message_id IS NULL""")[0][0]
    total_bytes = q("""SELECT COALESCE(SUM(sm.file_size),0) FROM group_members lm
                       JOIN source_messages sm ON sm.source_message_id = lm.source_message_id
                       WHERE lm.duplicate_of_message_id IS NULL""")[0][0]

    per = {}
    for r in q("""SELECT j.archive_id,
                         SUM(CASE WHEN j.status='SUCCESS' THEN 1 ELSE 0 END) AS done,
                         SUM(CASE WHEN j.status='FAILED'  THEN 1 ELSE 0 END) AS failed,
                         SUM(CASE WHEN j.status IN ('RUNNING','PENDING') THEN 1 ELSE 0 END) AS inflight
                  FROM processing_jobs j WHERE j.operation='COPY_FILE' GROUP BY 1"""):
        per[r["archive_id"]] = {
            "done": r["done"] or 0, "failed": r["failed"] or 0, "inflight": r["inflight"] or 0,
        }
    # per-archive transferred bytes (count each forum's copy)
    for r in q("""SELECT j.archive_id, COALESCE(SUM(sm.file_size),0) AS b
                  FROM processing_jobs j
                  JOIN source_messages sm ON sm.source_message_id = j.source_message_id
                  WHERE j.operation='COPY_FILE' AND j.status='SUCCESS'
                  GROUP BY 1"""):
        per.setdefault(r["archive_id"], {"done": 0, "failed": 0, "inflight": 0})
        per[r["archive_id"]]["bytes"] = r["b"]

    done_unique = q("""SELECT COUNT(DISTINCT j.source_message_id) FROM processing_jobs j
                       WHERE j.operation='COPY_FILE' AND j.status='SUCCESS'""")[0][0]
    done_unique_bytes = q("""SELECT COALESCE(SUM(sm.file_size),0) FROM processing_jobs j
                             JOIN source_messages sm ON sm.source_message_id = j.source_message_id
                             WHERE j.operation='COPY_FILE' AND j.status='SUCCESS'
                               AND j.source_message_id IN (
                                 SELECT MIN(source_message_id) FROM processing_jobs
                                 WHERE operation='COPY_FILE' AND status='SUCCESS'
                                 GROUP BY source_message_id)""")[0][0]

    topics = {}
    for r in q("SELECT archive_id, COUNT(*) FROM destinations GROUP BY 1"):
        topics[r["archive_id"]] = r[1]

    saved_done = q("""SELECT COUNT(DISTINCT j.source_message_id) FROM processing_jobs j
                      WHERE j.operation IN ('POST_SUMMARY','COPY_FILE')
                        AND j.topic_title LIKE '%REVIEW%'""")[0][0]

    alive = sync_alive()
    now = time.monotonic()
    _PACE.append((now, done_unique))
    _PACE[:] = [p for p in _PACE if now - p[0] < 300]
    rate = 0.0
    if len(_PACE) >= 2 and _PACE[-1][0] > _PACE[0][0]:
        rate = max(0.0, (_PACE[-1][1] - _PACE[0][1]) / (_PACE[-1][0] - _PACE[0][0]))
    remaining = max(0, total_files - done_unique)
    eta_s = int(remaining / rate) if rate > 0.01 else None

    total_done_all = sum(p["done"] for p in per.values())
    total_failed_all = sum(p.get("failed", 0) for p in per.values())
    return {
        "now": int(time.time()),
        "sync_alive": alive,
        "complete": (not alive) and total_done_all + total_failed_all >= total_files * len(per) and total_files > 0,
        "flood": flood_state(),
        "unique": {
            "total_files": total_files,
            "done_files": done_unique,
            "remaining_files": remaining,
            "total_bytes": total_bytes,
            "done_bytes": done_unique_bytes,
            "remaining_bytes": total_bytes - done_unique_bytes,
        },
        "archives": {k: {**v, "total": total_files,
                         "total_bytes": total_bytes} for k, v in sorted(per.items())},
        "topics": topics,
        "totals_all_forums": {"done": total_done_all, "failed": total_failed_all},
        "pace_files_per_sec": round(rate, 2),
        "eta_seconds": eta_s,
        "saved_review_done": saved_done,
    }


HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Archive Sync — Live</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{background:#0b1020;color:#e8ecf5;font-family:system-ui,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#8b96ad;font-size:13px;margin-bottom:18px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
 .card{background:#131a2e;border:1px solid #232d4a;border-radius:12px;padding:16px}
 .big{font-size:30px;font-weight:700;margin:6px 0 2px}
 .lbl{color:#8b96ad;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
 .bar{height:10px;background:#1d2745;border-radius:6px;overflow:hidden;margin-top:10px}
 .bar i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#22d3ee);width:0}
 .row{display:flex;justify-content:space-between;font-size:13px;margin-top:8px;color:#aab4cc}
 .ok{color:#34d399}.warn{color:#fbbf24}.bad{color:#f87171}.dim{color:#64748b}
 .pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
 .live{background:#0e3b2e;color:#34d399}.dead{background:#3b1220;color:#f87171}
 .done-banner{background:linear-gradient(90deg,#0e3b2e,#123a4a);border:1px solid #1f6f54;border-radius:12px;padding:14px 16px;margin-top:16px;font-size:16px}
 table{width:100%;border-collapse:collapse;font-size:14px}
 td{padding:8px 10px;border-bottom:1px solid #1e2946}
 td:first-child{color:#8b96ad}
</style></head><body>
<h1>📦 Telegram Archive Sync — Live Dashboard</h1>
<div class="sub">Source: Car Dump 3 → Dumps 1/2/3 &nbsp;·&nbsp; auto-refreshes every 3 s &nbsp;·&nbsp; <span id="upd" class="dim"></span></div>
<div id="pillbox"></div>
<div class="grid" style="margin-top:14px">
  <div class="card"><div class="lbl">Files copied (unique)</div><div class="big" id="f">–</div>
    <div class="bar"><i id="fb"></i></div>
    <div class="row"><span id="fr">remaining: –</span><span id="fp">–%</span></div></div>
  <div class="card"><div class="lbl">Data transferred</div><div class="big" id="b">–</div>
    <div class="bar"><i id="bb"></i></div>
    <div class="row"><span id="br">remaining: –</span></div></div>
  <div class="card"><div class="lbl">Pace / ETA</div><div class="big" id="p">–</div>
    <div class="row"><span id="eta">–</span></div>
    <div class="row"><span>failures: <span id="fail" class="bad">–</span></span></div></div>
  <div class="card"><div class="lbl">Topics created</div><div class="big" id="t">–</div>
    <div class="row"><span id="td">per forum</span></div>
    <div class="row"><span>review items saved: <span id="sv">–</span></span></div></div>
  <div class="card"><div class="lbl">⏱ Telegram rate limit (flood wait)</div><div class="big" id="fw">–</div>
    <div class="row"><span id="fwsub">–</span></div>
    <div id="fwlist" style="font-size:12px;color:#8b96ad;margin-top:10px;line-height:1.7"></div></div>
</div>
<div class="grid" style="margin-top:14px">
  <div class="card"><div class="lbl">Dump 1</div><div class="big" id="a1">–</div><div class="bar"><i id="a1b"></i></div>
    <div class="row"><span id="a1s">–</span></div></div>
  <div class="card"><div class="lbl">Dump 2</div><div class="big" id="a2">–</div><div class="bar"><i id="a2b"></i></div>
    <div class="row"><span id="a2s">–</span></div></div>
  <div class="card"><div class="lbl">Dump 3</div><div class="big" id="a3">–</div><div class="bar"><i id="a3b"></i></div>
    <div class="row"><span id="a3s">–</span></div></div>
</div>
<div id="donebox"></div>
<script>
function fmtB(n){if(n==null)return'–';const u=['B','KB','MB','GB','TB','PB'];let i=0;
 while(n>=1024&&i<u.length-1){n/=1024;i++;}return n.toFixed(1)+' '+u[i];}
function fmtEta(s){if(s==null)return'computing…';const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
 return (h?h+'h ':'')+(m?m+'m ':(h?'':'0m '))+Math.floor(s%60)+'s';}
async function tick(){
 try{
  const r=await fetch('/api/stats');const d=await r.json();
  const u=d.unique,tot=u.total_files||1;
  const pct=(u.done_files/tot*100);
  document.getElementById('f').textContent=u.done_files.toLocaleString()+' / '+tot.toLocaleString();
  document.getElementById('fb').style.width=pct.toFixed(2)+'%';
  document.getElementById('fp').textContent=pct.toFixed(2)+'%';
  document.getElementById('fr').textContent='remaining: '+u.remaining_files.toLocaleString();
  document.getElementById('b').textContent=fmtB(u.done_bytes);
  document.getElementById('bb').style.width=(u.total_bytes?u.done_bytes/u.total_bytes*100:0).toFixed(2)+'%';
  document.getElementById('br').textContent='of '+fmtB(u.total_bytes)+' · remaining '+fmtB(u.remaining_bytes);
  document.getElementById('p').textContent=(d.pace_files_per_sec||0)+' files/s';
  document.getElementById('eta').textContent='ETA: '+fmtEta(d.eta_seconds);
  document.getElementById('fail').textContent=d.totals_all_forums.failed;
  const fl=d.flood||{recent:[]};
  if(fl.active){
    document.getElementById('fw').textContent=fl.active.remaining_s+' s left';
    document.getElementById('fw').className='big warn';
    document.getElementById('fwsub').textContent='rate-limit pause — resumes automatically after this countdown';
  }else{
    document.getElementById('fw').textContent='copying';
    document.getElementById('fw').className='big ok';
    document.getElementById('fwsub').textContent='limit budget OK · paused '+fl.waited_last_hour_s+'s in the last hour';
  }
  document.getElementById('fwlist').innerHTML=(fl.recent||[]).map(r=>
    '<div>'+r.time+' · '+r.archive+' · wait '+r.seconds+'s ('+r.what+')'+
    (r.remaining_s>1?' — <b class="warn">'+r.remaining_s+'s left</b>':'')+'</div>').join('')||'<div class="dim">no rate-limit pauses yet</div>';
  const tv=Object.values(d.topics);
  document.getElementById('t').textContent=tv.length?Math.max(...tv).toLocaleString():'–';
  document.getElementById('sv').textContent=(d.saved_review_done||0).toLocaleString();
  let doneAll=0,failAll=0;
  const map={archive_1:'1',archive_2:'2',archive_3:'3'};
  for(const [k,v] of Object.entries(d.archives)){
    const n=map[k];if(!n)continue;
    doneAll+=v.done;failAll+=v.failed;
    const p=(v.total? v.done/v.total*100:0);
    document.getElementById('a'+n).textContent=v.done.toLocaleString()+' / '+v.total.toLocaleString();
    document.getElementById('a'+n+'b').style.width=p.toFixed(2)+'%';
    document.getElementById('a'+n+'s').textContent=fmtB(v.bytes||0)+' · failed '+v.failed;
  }
  document.getElementById('pillbox').innerHTML=
    (d.sync_alive?'<span class="pill live">● SYNC RUNNING</span>':'<span class="pill dead">● SYNC STOPPED</span>')
    + (d.complete?' <span class="pill live">✔ ALL DONE</span>':'');
  document.getElementById('donebox').innerHTML=d.complete?
    '<div class="done-banner">✔ <b>SYNC COMPLETE</b> — '+u.done_files.toLocaleString()+' files × 3 forums, '+fmtB(u.done_bytes)+' each. You can close this page.</div>':'';
  document.getElementById('upd').textContent='last update '+new Date(d.now*1000).toLocaleTimeString();
 }catch(e){}
}
tick();setInterval(tick,3000);
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/stats"):
            body = json.dumps(stats()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"dashboard on 0.0.0.0:{PORT}", flush=True)
    srv.serve_forever()
