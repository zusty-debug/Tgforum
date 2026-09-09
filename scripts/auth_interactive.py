#!/usr/bin/env python3
"""Interactive Telegram login without a TTY (sandbox-friendly).

The sandbox has no keyboard, so the script uses two files as a bridge
with the agent, which relays messages with the user:

  data/auth_state.json  — current state (what the agent reads)
  data/auth_reply.txt   — user's reply (the agent writes it here)

Flow: CODE sent to phone → user pastes code (agent writes reply file)
→ [2FA password if enabled] → StringSession saved into .env.

Usage:  python scripts/auth_interactive.py
        (phone number is read from data/auth_reply.txt first)
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from organizer.telegram.client import env_credentials  # noqa: E402
from telethon import errors, TelegramClient  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

# telethon renamed some RPC error classes across versions — resolve both
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

STATE = ROOT / "data" / "auth_state.json"
REPLY = ROOT / "data" / "auth_reply.txt"
ENV = ROOT / ".env"


def log(msg: str):
    print(f"[auth] {msg}", flush=True)


def set_state(**kw):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(kw, indent=1, ensure_ascii=False))
    log(f"state → {kw.get('state')}")


def consume_reply(label: str, timeout: int = 3600) -> str:
    deadline = time.time() + timeout
    next_beat = time.time() + 60
    while time.time() < deadline:
        if REPLY.exists():
            text = REPLY.read_text().strip()
            REPLY.unlink()
            if text:
                log(f"received {label} reply")
                return text
        if time.time() > next_beat:
            next_beat = time.time() + 60
            log(f"still waiting for {label} …")
        time.sleep(1)
    raise TimeoutError(f"no {label} reply within {timeout}s")


def save_session_string(sess: StringSession):
    value = str(sess)
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    out, replaced = [], False
    for ln in lines:
        if ln.startswith("TELEGRAM_STRING_SESSION="):
            out.append(f"TELEGRAM_STRING_SESSION={value}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(f"TELEGRAM_STRING_SESSION={value}")
    ENV.write_text("\n".join(out) + "\n")
    log("string session saved to .env")


async def main():
    api_id, api_hash, _ = env_credentials()
    sess = StringSession()
    client = TelegramClient(sess, api_id, api_hash,
                            system_version="Linux", device_model="ArchiveOrganizer")
    await client.connect()
    log(f"connected to Telegram DC (api_id {api_id})")

    if await client.is_user_authorized():
        save_session_string(sess)
        me = await client.get_me()
        set_state(state="DONE", user=me.username or str(me.id))
        await client.disconnect()
        return

    # ── phone ──
    set_state(state="PHONE_WAIT")
    phone = consume_reply("phone number")
    phone = re.sub(r"[^\d+]", "", phone)
    if not phone.startswith("+"):
        log(f"phone {phone!r} lacks '+' — assuming +91 (Indian number)")
        phone = "+91" + phone
    log(f"requesting code for {phone}")

    # ── code (retry on invalid/expired) ──
    need_2fa = False
    while True:
        sent = await client.send_code_request(phone)
        code_hash = getattr(sent, "phone_code_hash", sent)
        set_state(state="CODE_WAIT", phone=phone, code_hash=code_hash)
        log("code sent to the user's Telegram — waiting for paste-back")
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
            await client.disconnect()
            return

    # ── 2FA password ──
    if need_2fa:
        set_state(state="PASSWORD_WAIT")
        log("2FA enabled — waiting for cloud password")
        password = consume_reply("2FA password")
        await client.sign_in(password=password)

    me = await client.get_me()
    save_session_string(sess)
    set_state(state="DONE", user=me.username or str(me.id), id=me.id)
    log(f"AUTHENTICATED as {me.username or me.id}")
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        set_state(state="ERROR", detail=f"{type(e).__name__}: {e}"[:300])
        raise
