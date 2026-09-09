#!/usr/bin/env python3
"""Finish the login synchronously using the saved code_hash.

Inputs (written by the agent from the user's messages):
  data/auth_state.json      — must contain phone + code_hash (from a CODE_WAIT)
  data/auth_reply_code.txt  — the 6-digit code
  data/auth_reply_pw.txt    — 2FA cloud password (only if 2FA is on)

Prints DONE <user> on success, or NEEDS_2FA / an error.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
from organizer.telegram.client import env_credentials  # noqa: E402
from telethon import errors, TelegramClient  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402


def _e(*names):
    for n in names:
        cls = getattr(errors, n, None)
        if cls:
            return cls
    return RuntimeError


SessionPasswordNeeded = _e("SessionPasswordNeeded", "SessionPasswordNeededError")
PhoneCodeInvalid = _e("PhoneCodeInvalid", "PhoneCodeInvalidError")
PhoneCodeExpired = _e("PhoneCodeExpired", "PhoneCodeExpiredError")

DATA = ROOT / "data"
ENV = ROOT / ".env"


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


def main():
    state = json.loads((DATA / "auth_state.json").read_text())
    phone, code_hash = state["phone"], state["code_hash"]
    code = (DATA / "auth_reply_code.txt").read_text().strip()
    pw_file = DATA / "auth_reply_pw.txt"
    pw = pw_file.read_text().strip() if pw_file.exists() else None
    api_id, api_hash, _ = env_credentials()
    sess = StringSession()
    client = TelegramClient(sess, api_id, api_hash,
                            system_version="Linux", device_model="ArchiveOrganizer")

    async def run():
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except SessionPasswordNeeded:
            if not pw:
                await client.disconnect()
                print("NEEDS_2FA")
                return
            await client.sign_in(password=pw)
        except PhoneCodeInvalid as e:
            await client.disconnect()
            print(f"CODE_INVALID: {e}")
            return
        except PhoneCodeExpired:
            await client.disconnect()
            print("CODE_EXPIRED")
            return
        me = await client.get_me()
        save_session_string(sess)
        (DATA / "auth_state.json").write_text(
            json.dumps({"state": "DONE", "user": me.username or str(me.id)}, indent=1))
        print(f"DONE {me.username or me.id}")
        await client.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    main()
