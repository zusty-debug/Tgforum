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
import asyncio
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "10000"))
REFRESH = float(os.environ.get("REFRESH", "30"))
PROGRESS_FILE = os.environ.get("PROGRESS_FILE", "").strip()
GIT_REPO = os.environ.get("GIT_REPO", "").strip()
GIT_TOKEN = os.environ.get("GIT_TOKEN", "").strip()
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main").strip() or "main"
WORKDIR = os.environ.get("REPO_DIR", "/tmp/progress-repo") or "/tmp/progress-repo"
EMBED_STATUS = os.environ.get("EMBED_STATUS", "").strip() == "1"

# Session file base (Telethon appends ".session"); must match start.sh /
# organizer.telegram.client.session_target().
SESSION_NAME = (os.environ.get("SESSION_FILE", "").strip()
                or ("/data/tg_sess" if os.path.isdir("/data") else "organizer_session"))

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


# ── Embedded progress renderer (single-container mode) ─────────────────────
def _status_refresher():
    """Render out/progress.json (+ DASHBOARD.md) every 25s straight from the
    DB — replaces the separate status_file.py process when the dashboard and
    the sync live in the same container (keeps RAM down on the free tier)."""
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(root, "scripts"))
    try:
        import status_file as SF
    except Exception as e:
        _log(f"embedded status disabled: {e}")
        return
    _log("embedded status renderer on (progress.json every 25s)")
    while True:
        try:
            SF.render()
        except Exception as e:
            _log(f"status render error: {e}")
        time.sleep(25)


# ── Web login: phone → OTP code → (2FA password) → session file on volume ──
_AUTH = {"state": "idle", "error": "", "info": "", "client": None,
         "loop": None, "lock": threading.Lock()}


def _auth_loop_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _AUTH["loop"] = loop
    loop.run_forever()


def _auth_run(coro, timeout=150):
    fut = asyncio.run_coroutine_threadsafe(coro, _AUTH["loop"])
    return fut.result(timeout=timeout)


async def _auth_mark_finished(client, who: str):
    try:
        client.session.save()          # persist the auth key NOW
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(SESSION_NAME) or ".", exist_ok=True)
        with open(os.path.join(os.path.dirname(SESSION_NAME) or ".", "session_ready"), "w") as f:
            f.write(json.dumps({"who": who, "ts": int(time.time())}))
    except Exception:
        pass
    await client.disconnect()
    with _AUTH["lock"]:
        _AUTH.update(state="done", client=None, error="",
                     info=f"logged in as {who} — session saved. The sync starts within ~10s.")


async def _auth_send_code(phone: str):
    from telethon import TelegramClient
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    client = TelegramClient(SESSION_NAME, api_id, api_hash,
                            system_version="Linux", device_model="ArchiveOrganizer")
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            await _auth_mark_finished(client, me.first_name or str(me.id))
            return
        await client.send_code_request(phone)
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise
    with _AUTH["lock"]:
        _AUTH.update(client=client, state="code_sent", error="",
                     info=f"code sent to {phone}")


async def _auth_sign_in(step: str, value: str):
    from telethon import errors as terrors
    client = _AUTH["client"]
    if client is None:
        raise RuntimeError("no login in progress — start again with your phone number")
    try:
        if step == "code":
            await client.sign_in_phone_code(value)
        else:
            await client.sign_in_password(value)
        me = await client.get_me()
        await _auth_mark_finished(client, me.first_name or str(me.id))
    except terrors.PasswordHashInvalidError:
        if step == "code":
            with _AUTH["lock"]:
                _AUTH.update(state="need_password", error="",
                             info="This account has 2FA on — enter your cloud password")
        else:
            with _AUTH["lock"]:
                _AUTH.update(state="need_password", error="Wrong cloud password — try again")
    except terrors.PhoneCodeInvalidError:
        with _AUTH["lock"]:
            _AUTH.update(state="code_sent", error="That code wasn't right — enter the latest code Telegram sent")
    except terrors.PhoneCodeEmptyError:
        with _AUTH["lock"]:
            _AUTH.update(state="code_sent", error="Code field was empty — enter the 5-digit code")
    except terrors.FloodWaitError as e:
        with _AUTH["lock"]:
            _AUTH.update(error=f"Telegram rate limit — wait {e.seconds}s and try again")


def _auth_html() -> str:
    with _AUTH["lock"]:
        state, err, info = _AUTH["state"], _AUTH["error"], _AUTH["info"]

    def card(body):
        return (f'<!doctype html><html><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>Archive Sync — Login</title><style>'
                f'body{{background:#0b1020;color:#e8ecf5;font-family:system-ui,sans-serif;'
                f'margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}}'
                f'.c{{background:#131a2e;border:1px solid #232d4a;border-radius:14px;padding:26px;max-width:400px;width:100%}}'
                f'h1{{font-size:20px;margin:0 0 6px}}p{{color:#8b96ad;font-size:14px;line-height:1.45}}'
                f'input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #2b3757;'
                f'background:#0f1526;color:#e8ecf5;font-size:16px;margin:10px 0}}'
                f'button{{width:100%;padding:12px;border:0;border-radius:10px;background:#3b82f6;color:#fff;'
                f'font-size:16px;font-weight:600;cursor:pointer}}'
                f'.err{{background:#2f0e14;border:1px solid #7f1d1d;color:#f87171;padding:10px 12px;'
                f'border-radius:10px;font-size:14px}}.ok{{background:#0e2f22;border:1px solid #14532d;'
                f'color:#4ade80;padding:10px 12px;border-radius:10px;font-size:14px}}'
                f'.a{{color:#3b82f6;font-size:13px;text-decoration:none}}</style></head><body>'
                f'<div class="c">{body}</div></body></html>')

    if state == "done":
        return card('<h1>✅ Logged in</h1>'
                    f'<div class="ok">{info}</div>'
                    '<p>Go back to <a class="a" href="/">/</a> to watch progress.</p>')
    if state == "code_sent":
        body = ('<h1>📲 Enter the code</h1>'
                f'<p>{info}</p>')
        if err:
            body += f'<div class="err">{err}</div>'
        body += ('<form method="POST" action="/auth/code">'
                 '<input name="code" inputmode="numeric" autocomplete="one-time-code" '
                 'placeholder="5-digit code from Telegram" required>'
                 '<button type="submit">Verify code</button></form>'
                 '<p><a class="a" href="/auth?back=1">change phone number</a></p>')
        return card(body)
    if state == "need_password":
        body = ('<h1>🔐 Cloud password</h1>'
                f'<p>{info}</p>')
        if err:
            body += f'<div class="err">{err}</div>'
        body += ('<form method="POST" action="/auth/password">'
                 '<input name="password" type="password" placeholder="2FA cloud password" required>'
                 '<button type="submit">Unlock</button></form>')
        return card(body)
    # idle — phone form
    body = ('<h1>🔑 Login to Telegram</h1>'
            '<p>Enter the phone number of your Telegram account (with country code, '
            'e.g. <code>+213…</code>). Telegram will send you a login code — '
            'enter it here. No password needed unless you have 2FA.</p>')
    if err:
        body += f'<div class="err">{err}</div>'
    body += ('<form method="POST" action="/auth/phone">'
             '<input name="phone" inputmode="tel" placeholder="+213542067735" required>'
             '<button type="submit">Send code</button></form>'
             '<p>After login the session is saved to this server — it survives '
             'restarts, so you only do this once.</p>')
    return card(body)


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
        elif path == "/auth":
            if "back" in self.path:          # "change phone number"
                with _AUTH["lock"]:
                    _AUTH.update(state="idle", error="", info="", client=None)
            self._send(200, _auth_html(), "text/html; charset=utf-8")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/auth/"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode()) if length else {}
            value = (form.get(path.split("/")[-1]) or [""])[0].strip()
        except Exception:
            self._send(400, json.dumps({"error": "bad request"}))
            return

        if path == "/auth/phone":
            if not re.fullmatch(r"\+?\d{7,15}", value):
                with _AUTH["lock"]:
                    _AUTH.update(state="idle",
                                 error="That doesn't look like a phone number — use country code + number, e.g. +213542067735",
                                 info="")
                self.send_response(303)
                self.send_header("Location", "/auth")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                _auth_run(_auth_send_code(value))
            except Exception as e:
                with _AUTH["lock"]:
                    _AUTH.update(state="idle", error=f"Could not send code: {e}", info="")
        elif path in ("/auth/code", "/auth/password"):
            if not value:
                with _AUTH["lock"]:
                    _AUTH.update(error="Field is empty — enter the code/password and press the button")
            else:
                try:
                    _auth_run(_auth_sign_in("code" if path == "/auth/code" else "password", value))
                except Exception as e:
                    with _AUTH["lock"]:
                        _AUTH.update(error=f"{e}")
        else:
            self._send(404, json.dumps({"error": "not found"}))
            return
        # redirect back to the auth page (shows the new state)
        self.send_response(303)
        self.send_header("Location", "/auth")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    t = threading.Thread(target=_updater, daemon=True)
    t.start()
    if EMBED_STATUS:
        threading.Thread(target=_status_refresher, daemon=True).start()
    # web login (phone → OTP) — enabled when Telegram credentials are present
    if os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH"):
        try:
            import telethon  # noqa: F401
            threading.Thread(target=_auth_loop_thread, daemon=True).start()
            _log(f"web login enabled → /auth (session file: {SESSION_NAME}.session)")
        except ImportError:
            _log("telethon not installed — /auth disabled")
    # immediate first load so the page isn't empty on cold start
    _refresh_once()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    _log(f"dashboard on 0.0.0.0:{PORT} (mode={PROGRESS_FILE and 'local' or (GIT_REPO and 'git' or 'none')}, "
         f"refresh={REFRESH:g}s, embed_status={EMBED_STATUS})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
