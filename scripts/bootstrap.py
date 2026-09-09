#!/usr/bin/env python3
"""One-shot detached bootstrap: auth → verify chats → scan source.

Survives the user being slow (waits on reply files) and the sandbox
recycling (every step is checkpointed on disk; re-run to continue).

Reply files (agent writes from user messages):
  data/auth_reply.txt     — next expected input (phone or code)
  data/auth_reply_pw.txt  — 2FA cloud password (pre-staged, read only at 2FA step)
Outputs:
  data/session.txt, .env TELEGRAM_STRING_SESSION, data/verify.json,
  data/archive.db (scan checkpoint), data/auth_state.json
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path("/home/user/telegram-archive-organizer")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from telethon import TelegramClient, errors  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

DATA = ROOT / "data"
STATE = DATA / "auth_state.json"
REPLY = DATA / "auth_reply.txt"
PWFILE = DATA / "auth_reply_pw.txt"


def _e(*names):
    for n in names:
        cls = getattr(errors, n, None)
        if cls:
            return cls
    return RuntimeError


PhoneCodeInvalid = _e("PhoneCodeInvalid", "PhoneCodeInvalidError")
PhoneCodeExpired = _e("PhoneCodeExpired", "PhoneCodeExpiredError")
SessionPasswordNeeded = _e("SessionPasswordNeeded", "SessionPasswordNeededError")
PhoneNumberBanned = _e("PhoneNumberBanned", "PhoneNumberBannedError")


def log(m: str):
    print(f"[bootstrap] {m}", flush=True)


def set_state(**kw):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(kw, indent=1, ensure_ascii=False))
    log(f"state → {kw.get('state')}")


def consume_reply(label: str, timeout: int = 7200, pw: bool = False) -> str:
    src = PWFILE if pw else REPLY
    deadline = time.time() + timeout
    beat = time.time() + 60
    while time.time() < deadline:
        if src.exists():
            text = src.read_text().strip()
            src.unlink()
            if text:
                log(f"received {label}")
                return text
        if time.time() > beat:
            beat = time.time() + 60
            log(f"still waiting for {label} …")
        time.sleep(1)
    raise TimeoutError(f"no {label} within {timeout}s")


def load_or_new_session() -> StringSession:
    val = ""
    if (ROOT / ".env").exists():
        for ln in (ROOT / ".env").read_text().splitlines():
            if ln.startswith("TELEGRAM_STRING_SESSION="):
                val = ln.split("=", 1)[1].strip()
    if val:
        try:
            return StringSession(val)
        except ValueError:
            log("stored session string invalid — starting fresh")
    return StringSession()


def save_session(sess: StringSession):
    value = sess.save()
    if not value or value[0] != "1":
        raise RuntimeError(f"session serialize failed: {value[:20]!r}")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "session.txt").write_text(value)
    lines = (ROOT / ".env").read_text().splitlines()
    out, rep = [], False
    for ln in lines:
        if ln.startswith("TELEGRAM_STRING_SESSION="):
            out.append(f"TELEGRAM_STRING_SESSION={value}")
            rep = True
        else:
            out.append(ln)
    if not rep:
        out.append(f"TELEGRAM_STRING_SESSION={value}")
    (ROOT / ".env").write_text("\n".join(out) + "\n")
    log(f"session saved (len {len(value)})")


async def ensure_auth(client: TelegramClient):
    if await client.is_user_authorized():
        log("already authorized — skipping login")
        return
    set_state(state="PHONE_WAIT")
    phone = consume_reply("phone number")
    phone = re.sub(r"[^\d+]", "", phone)
    if not phone.startswith("+"):
        log("phone lacks '+' — assuming +91")
        phone = "+91" + phone
    log(f"requesting code for {phone}")
    need_2fa = False
    while True:
        sent = await client.send_code_request(phone)
        code_hash = getattr(sent, "phone_code_hash", sent)
        set_state(state="CODE_WAIT", phone=phone, code_hash=code_hash)
        log("code sent — waiting for paste-back")
        code = consume_reply("code")
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
            break
        except SessionPasswordNeeded:
            need_2fa = True
            break
        except PhoneCodeInvalid as e:
            set_state(state="CODE_INVALID", detail=str(e)[:200])
        except PhoneCodeExpired:
            set_state(state="CODE_EXPIRED")
        except PhoneNumberBanned as e:
            set_state(state="BANNED", detail=str(e)[:200])
            raise
    if need_2fa:
        set_state(state="PASSWORD_WAIT")
        log("2FA on — reading cloud password from staged file")
        pw = consume_reply("2FA password", pw=True)
        await client.sign_in(password=pw)
    me = await client.get_me()
    save_session(client.session)
    set_state(state="AUTHENTICATED", user=me.username or str(me.id), id=me.id)


async def verify(client: TelegramClient) -> dict:
    from organizer.telegram.client import verify_forum_permissions
    res: dict = {}
    src = int(os.environ["SOURCE_CHAT_ID"])
    try:
        ent = await client.get_entity(src)
        res["source"] = {"ok": True, "title": getattr(ent, "title", str(src)),
                         "type": "channel" if getattr(ent, "broadcast", False) else "group"}
    except Exception as e:
        res["source"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    for i in (1, 2, 3):
        aid = int(os.environ[f"ARCHIVE_{i}_CHAT_ID"])
        try:
            info = await verify_forum_permissions(client, aid)
            res[f"archive_{i}"] = {"ok": True, **info}
        except Exception as e:
            res[f"archive_{i}"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    (DATA / "verify.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    log("verify: " + json.dumps({k: v["ok"] for k, v in res.items()}))
    return res


async def main():
    set_state(state="STARTING")
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    sess = load_or_new_session()
    client = TelegramClient(sess, api_id, api_hash,
                            system_version="Linux", device_model="ArchiveOrganizer")
    await client.connect()
    log("connected to Telegram DC")

    await ensure_auth(client)
    res = await verify(client)
    if not (res["source"]["ok"] and all(res[f"archive_{i}"]["ok"] for i in (1, 2, 3))):
        set_state(state="VERIFY_FAILED")
        log("verification failed — NOT scanning. See data/verify.json")
        await client.disconnect()
        return

    from organizer.db import DB
    from organizer.telegram.scanner import scan_source
    db = DB(ROOT / "data" / "archive.db")
    src = int(os.environ["SOURCE_CHAT_ID"])
    set_state(state="SCANNING")
    log("scan started (checkpointed; safe to interrupt)")
    n = await scan_source(client, src, db)
    total = db.count_source()
    db.close()
    set_state(state="SCAN_DONE", new_files=n, total=total)
    log(f"scan complete: {n} new, {total} total")
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc()
        set_state(state="ERROR", detail=f"{type(e).__name__}: {e}"[:300])
