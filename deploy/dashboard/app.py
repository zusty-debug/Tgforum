#!/usr/bin/env python3
"""Live progress dashboard — stdlib only, deploys to Render or Railway.

It shows progress of the Telegram archive sync in a browser at a stable URL.
The dashboard does NOT run the sync — it just displays progress, which it
keeps fresh from one of two sources (set via env):

  PROGRESS_FILE=/path/to/progress.json   → local mode: tail that file.
  GIT_REPO=... GIT_TOKEN=...             → git mode: shallow-clone + `git pull`
                                            of the repo and read its
                                            progress.json (LFS skipped).

Env:
  PORT          (default 10000)
  REFRESH       seconds between refreshes (default 30)
  GIT_BRANCH    branch to track (default main)

Run:  python3 app.py
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "10000"))
REFRESH = float(os.environ.get("REFRESH", "30"))
PROGRESS_FILE = os.environ.get("PROGRESS_FILE", "").strip()
GIT_REPO = os.environ.get("GIT_REPO", "").strip()
GIT_TOKEN = os.environ.get("GIT_TOKEN", "").strip()
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main").strip() or "main"
WORKDIR = os.environ.get("REPO_DIR", "/tmp/progress-repo") or "/tmp/progress-repo"

_lock = threading.Lock()
_STATE = {"progress": None, "mode": None, "error": None, "last_update": None}


def _log(msg):
    print(f"[dashboard] {msg}", flush=True)


def _auth_url(repo):
    """Embed the token so `git` can pull a private repo non-interactively."""
    if not GIT_TOKEN or "://" not in repo:
        return repo
    scheme, rest = repo.split("://", 1)
    if "@" in rest.split("/", 1)[0]:
        return repo  # already has creds
    return f"{scheme}://x-access-token:{GIT_TOKEN}@{rest}"


def _run(cmd, cwd=None, timeout=120):
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"          # don't download the big DB
    env["GIT_TERMINAL_PROMPT"] = "0"          # never hang on a password prompt
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True)


def _git_pull():
    url = _auth_url(GIT_REPO)
    if not os.path.isdir(os.path.join(WORKDIR, ".git")):
        os.makedirs(os.path.dirname(WORKDIR) or ".", exist_ok=True)
        _log(f"cloning {GIT_REPO} ({GIT_BRANCH}) → {WORKDIR}")
        r = _run(["git", "clone", "--depth", "1", "--branch", GIT_BRANCH,
                  url, WORKDIR])
        if r.returncode != 0:
            raise RuntimeError(f"clone failed: {r.stderr.strip()[:300]}")
    else:
        r = _run(["git", "-C", WORKDIR, "fetch", "--depth", "1", "origin",
                  GIT_BRANCH], timeout=90)
        if r.returncode == 0:
            r = _run(["git", "-C", WORKDIR, "reset", "--hard",
                      f"origin/{GIT_BRANCH}"])
        if r.returncode != 0:
            raise RuntimeError(f"pull failed: {r.stderr.strip()[:300]}")
    p = os.path.join(WORKDIR, "progress.json")
    if not os.path.exists(p):
        raise RuntimeError("progress.json not found in repo")
    with open(p) as f:
        return json.load(f)


def _local_read():
    with open(PROGRESS_FILE) as f:
        return json.load(f)


def _refresh_once():
    try:
        if PROGRESS_FILE:
            data = _local_read()
            mode = "local"
        elif GIT_REPO:
            data = _git_pull()
            mode = "git"
        else:
            _log("no PROGRESS_FILE or GIT_REPO set — nothing to show")
            return
        with _lock:
            _STATE["progress"] = data
            _STATE["mode"] = mode
            _STATE["error"] = None
            _STATE["last_update"] = time.time()
    except Exception as e:
        with _lock:
            _STATE["error"] = str(e)


def _updater():
    while True:
        _refresh_once()
        time.sleep(REFRESH)


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archive Sync — Live</title>
<style>
 body{background:#0b1020;color:#e8ecf5;font-family:system-ui,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#8b96ad;font-size:13px;margin-bottom:18px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
 .card{background:#131a2e;border:1px solid #232d4a;border-radius:12px;padding:16px}
 .big{font-size:30px;font-weight:700;margin:6px 0 2px}
 .lbl{color:#8b96ad;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
 .bar{height:12px;background:#1c2540;border-radius:8px;overflow:hidden;margin:10px 0 4px}
 .bar>i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#22d3ee)}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #1c2540}
 th{color:#8b96ad;font-weight:600;font-size:12px}
 .ok{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}
 .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
 .pill.on{background:#0e2f22;color:#4ade80;border:1px solid #14532d}
 .pill.off{background:#2f0e14;color:#f87171;border:1px solid #7f1d1d}
 .muted{color:#8b96ad;font-size:13px}
 code{background:#0f1526;padding:1px 6px;border-radius:6px;font-size:12px}
 .small{font-size:12px;color:#8b96ad}
 ul{margin:8px 0 0;padding-left:18px}
</style></head>
<body>
 <h1>📦 Archive Sync — Live</h1>
 <div class="sub" id="sub">connecting…</div>
 <div class="grid">
   <div class="card">
     <div class="lbl">Status</div>
     <div style="margin-top:8px"><span class="pill off" id="alive">…</span>
     &nbsp;<span id="dump" class="muted"></span></div>
   </div>
   <div class="card">
     <div class="lbl">Files (lead dump)</div>
     <div class="big" id="files">—</div>
     <div class="bar"><i id="fbar" style="width:0%"></i></div>
     <div class="small" id="fpct">—</div>
   </div>
   <div class="card">
     <div class="lbl">Size copied</div>
     <div class="big" id="bytes" style="font-size:24px">—</div>
     <div class="small" id="bytesof">—</div>
   </div>
   <div class="card">
     <div class="lbl">Pace / ETA</div>
     <div class="big" id="pace" style="font-size:24px">—</div>
     <div class="small" id="eta">—</div>
   </div>
 </div>

 <div class="grid" style="margin-top:14px">
   <div class="card">
     <div class="lbl">The 3 dumps</div>
     <table style="margin-top:10px">
       <thead><tr><th>dump</th><th>files</th><th>size</th><th>failed</th></tr></thead>
       <tbody id="dumps"></tbody>
     </table>
   </div>
   <div class="card">
     <div class="lbl">⏱ Telegram rate limit</div>
     <div id="flood" style="margin-top:10px" class="muted">—</div>
     <ul id="floodrecent" class="small"></ul>
   </div>
 </div>

 <div class="card" style="margin-top:14px">
   <div class="lbl">❌ Recent failures</div>
   <div id="fails" class="muted" style="margin-top:8px">—</div>
 </div>

<script>
const NAMES={archive_1:"Dump 1",archive_2:"Dump 2",archive_3:"Dump 3"};
function fmtBytes(n){if(n==null)return"—";const u=["B","KB","MB","GB","TB","PB"];
 let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return n.toFixed(n>=100||i<2?0:1)+" "+u[i];}
function fmtEta(s){if(s==null)return"—";s=Math.floor(s);
 const d=Math.floor(s/86400);s%=86400;const h=Math.floor(s/3600);s%=3600;
 const m=Math.floor(s/60);return (d?d+"d ":"")+(h?h+"h ":"")+m+"m";}
function esc(x){return (x==null?"":String(x)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
async function tick(){
 try{
  const r=await fetch("/progress.json",{cache:"no-store"});
  const j=await r.json();
  if(!j||!j.unique){document.getElementById("sub").textContent="waiting for first progress report…";return;}
  render(j);
 }catch(e){document.getElementById("sub").textContent="refresh error: "+e;}
}
function render(j){
 const u=j.unique; const alive=j.sync_alive;
 const sub=document.getElementById("sub");
 sub.textContent=`updated ${j.updated||""} · source: ${j.mode||"?"}`+(j.error?" · ⚠ "+j.error:"");
 const a=document.getElementById("alive");
 a.textContent=alive?"🟢 sync running":"🔴 sync stopped";
 a.className="pill "+(alive?"on":"off");
 document.getElementById("dump").textContent =
   j.current_dump? "now filling "+NAMES[j.current_dump]||j.current_dump : "";
 document.getElementById("files").textContent=(u.done_files||0).toLocaleString()+" / "+(u.total_files||0).toLocaleString();
 const pct=u.total_files?100*u.done_files/u.total_files:0;
 document.getElementById("fbar").style.width=pct.toFixed(2)+"%";
 document.getElementById("fpct").textContent=pct.toFixed(2)+"% complete";
 document.getElementById("bytes").textContent=fmtBytes(u.done_bytes);
 document.getElementById("bytesof").textContent="of "+fmtBytes(u.total_bytes);
 document.getElementById("pace").textContent=(j.pace_files_per_min||0).toFixed(0)+" files/min";
 document.getElementById("eta").textContent="ETA (this dump): "+fmtEta(j.eta_seconds);
 // dumps
 const tb=document.getElementById("dumps");tb.innerHTML="";
 for(const k of ["archive_1","archive_2","archive_3"]){
  const d=j.archives[k]||{done:0,failed:0,bytes:0};
  const tr=document.createElement("tr");
  const mark=j.current_dump===k?"⏳":"·";
  tr.innerHTML=`<td>${mark} ${NAMES[k]}</td><td>${(d.done||0).toLocaleString()}</td>
    <td>${fmtBytes(d.bytes)}</td><td class="${d.failed?"bad":"ok"}">${d.failed||0}</td>`;
  tb.appendChild(tr);
 }
 // flood
 const f=j.flood||{};const fl=document.getElementById("flood");
 if(f.active){fl.innerHTML=`<span class="warn">🛑 waiting ${f.active.remaining_s}s</span>
   <span class="small">(${NAMES[f.active.archive]||f.active.archive}, ${esc(f.active.what)})</span>`;}
 else fl.innerHTML=`<span class="ok">🟢 no active wait</span>
   <span class="small">· waited ${f.waited_last_hour_s||0}s in last hour</span>`;
 const fr=document.getElementById("floodrecent");fr.innerHTML="";
 (f.recent||[]).slice(0,5).forEach(x=>{
   const li=document.createElement("li");
   li.textContent=`${x.time} · ${NAMES[x.archive]||x.archive} · ${x.seconds}s · ${x.what}`;
   fr.appendChild(li);
 });
 // fails
 const fl2=document.getElementById("fails");
 const fs=j.failures||[];
 fl2.innerHTML=fs.length?fs.map(x=>esc(x)).join("<br>"):"none 🎉";
 if(j.complete)sub.textContent+=" · ✅ ALL DONE";
}
tick();setInterval(tick,3000);
</script>
</body></html>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep logs quiet

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/progress.json":
            with _lock:
                prog = _STATE["progress"]
                payload = dict(prog) if prog else {}
                payload["_dashboard"] = {
                    "mode": _STATE["mode"],
                    "error": _STATE["error"],
                    "last_update": _STATE["last_update"],
                    "now": int(time.time()),
                }
            self._send(200, json.dumps(payload))
        elif path == "/healthz":
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main():
    t = threading.Thread(target=_updater, daemon=True)
    t.start()
    # immediate first load so the page isn't empty on cold start
    _refresh_once()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    _log(f"dashboard on 0.0.0.0:{PORT} (mode={PROGRESS_FILE and 'local' or (GIT_REPO and 'git' or 'none')}, "
         f"refresh={REFRESH:g}s)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
