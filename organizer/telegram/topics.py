"""Forum topic manager — create-or-reuse by exact title (idempotent).

Rerunning never produces 'Database', 'Database 2', 'Database 3': the
local destinations table is checked first, then the live forum topic
list, and only then is a new topic created. topic_id is stored
permanently per (archive, title).
"""
from __future__ import annotations

import logging

from ._tl import CreateForumTopicRequest, GetForumTopicsRequest

log = logging.getLogger("organizer.telegram")


async def list_topics(client, chat_id) -> dict[str, int]:
    out: dict[str, int] = {}
    seen_ids: set[int] = set()
    offset_topic = 0
    for _ in range(200):  # hard cap: 200 pages × 100 topics
        res = await client(GetForumTopicsRequest(peer=chat_id, offset_date=None,
                                                 offset_id=0, offset_topic=offset_topic,
                                                 limit=100))
        if not res.topics:
            break
        new = 0
        for t in res.topics:
            if t.id not in seen_ids:
                seen_ids.add(t.id)
                new += 1
            out[t.title] = t.id
        # 'General' (id 1) is returned by the DC regardless of offset on
        # single-topic forums — stop when a page adds no new topics
        if not new:
            break
        offset_topic = res.topics[-1].id
    return out


async def ensure_topic(client, chat_id, title, icon_color, db, archive_id) -> int:
    row = db.find_destination(archive_id, title)
    if row and row["topic_id"]:
        return row["topic_id"]
    try:
        existing = await list_topics(client, chat_id)
    except Exception as e:  # defensive — fall through to create
        log.warning("list_topics(%s) failed: %s", chat_id, e)
        existing = {}
    topic_id = existing.get(title)
    if topic_id is None:
        res = await client(CreateForumTopicRequest(peer=chat_id,
                                                   title=title[:128],
                                                   icon_color=icon_color))
        # telethon ≥1.41 returns Updates, not Message: the topic is the
        # service message inside UpdateNewChannelMessage (or .message)
        topic_id = getattr(res, "id", None)
        if topic_id is None:
            for u in (getattr(res, "updates", None) or []):
                m = getattr(u, "message", None)
                if m is not None and getattr(m, "id", None):
                    topic_id = m.id
                    break
                nft = getattr(u, "new_forum_topic", None)
                if nft is not None:
                    topic_id = getattr(getattr(nft, "topic", None), "id", None) \
                        or getattr(nft, "id", None)
                    break
        if topic_id is None:
            raise RuntimeError(f"cannot extract topic id from {type(res).__name__}")
        log.info("created topic '%s' (id %d) in %s", title, topic_id, chat_id)
    db.save_destination(archive_id, chat_id, topic_id, title)
    return topic_id
