"""Telethon user-client factory + startup permission verification.

Credentials come from the environment (.env) — never from code, never
logged. The selected account must be owner/admin of the target forums
with "Manage topics" rights; we verify that at startup and fail with a
clear error otherwise.
"""
from __future__ import annotations

import os

from telethon import TelegramClient
from telethon.sessions import StringSession

DEVICE = dict(system_version="Linux", device_model="ArchiveOrganizer")


def env_credentials():
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    session = os.environ.get("TELEGRAM_STRING_SESSION", "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH are missing in .env")
    return int(api_id), api_hash, session


def build_client():
    api_id, api_hash, session = env_credentials()
    sess = StringSession(session) if session else "organizer_session"
    client = TelegramClient(sess, api_id, api_hash, **DEVICE)
    # Surface every flood wait >5s as FloodWaitError to our retry/pacer
    # layer (telethon's default auto-sleeps waits <60s and hides them).
    # The global pacer then adapts the whole-account pace to the pressure.
    try:
        client.flood_sleep_threshold = 5
    except Exception:
        pass
    return client


async def interactive_auth():
    """Run the phone/code/2FA flow; returns (string_session, identity)."""
    api_id, api_hash, _ = env_credentials()
    sess = StringSession()
    client = TelegramClient(sess, api_id, api_hash, **DEVICE)
    await client.start()
    me = await client.get_me()
    await client.disconnect()
    return sess, (me.username or str(me.id))


async def verify_forum_permissions(client, chat_id) -> dict:
    """Verify: chat exists · is a forum supergroup · account can manage topics.

    Raises PermissionError with a clear message on any failure.
    """
    from ._tl import GetForumTopicsRequest

    ent = await client.get_entity(chat_id)
    if not (getattr(ent, "megagroup", False) or getattr(ent, "is_group", False)):
        raise PermissionError(
            f"{chat_id} is not a group/supergroup — it cannot host forum topics")

    forum = True
    try:
        await client(GetForumTopicsRequest(peer=ent, offset_date=None,
                                           offset_id=0, offset_topic=0, limit=1))
    except Exception:
        forum = False
    if not forum:
        raise PermissionError(
            f"{getattr(ent, 'title', chat_id)} is not in forum mode — "
            "enable Topics in group settings (or create a new forum group)")

    me = await client.get_me()
    can_topics = False
    try:
        perms = await client.get_permissions(ent, me.id)
        if perms is not None:
            if perms.is_creator:
                can_topics = True  # creator has every permission
            else:
                part = getattr(perms, "participant", None)
                ap = getattr(part, "permissions", None)
                if ap is not None:
                    can_topics = bool(getattr(ap, "manage_topics", False))
                else:
                    priv = [str(p) for p in (getattr(part, "privileges", None) or [])]
                    can_topics = "manage_topics" in priv
    except Exception:
        pass
    if not can_topics:
        raise PermissionError(
            f"account lacks 'Manage topics' permission in "
            f"{getattr(ent, 'title', chat_id)} — grant it (or use the owner account)")

    return {"chat_id": chat_id, "title": getattr(ent, "title", str(chat_id)),
            "forum": True, "manage_topics": True}
