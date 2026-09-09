"""Pre-create every planned topic in all three archives.

Visible progress for the user (topics appear in the app immediately),
and de-risks the big sync. No files are copied — topic creation is not
blocked by the source channel's 'restrict saving content' flag.

Idempotent: existing topics (by title) are reused, never duplicated.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from telethon import errors

from main import load_config, archives_from_env
from organizer.db import DB
from organizer.planner import Planner
from organizer.telegram.client import build_client, verify_forum_permissions
from organizer.telegram.topics import ensure_topic, list_topics


async def precreate(client, db, archive, topics, delay=0.4):
    created = skipped = 0
    try:
        existing = set((await list_topics(client, archive["chat_id"])).keys())
    except Exception as e:
        print(f"[{archive['id']}] list_topics failed ({e}) — creating from scratch", flush=True)
        existing = set()
    for title, tp in topics.items():
        if title in existing:
            skipped += 1
            continue
        ok = False
        for attempt in range(3):
            try:
                await ensure_topic(client, archive["chat_id"], title,
                                   tp.icon_color, db, archive["id"])
                ok = True
                break
            except errors.FloodWaitError as e:
                wait = int(e.seconds) + 3
                print(f"[{archive['id']}] flood wait {wait}s on '{title}'…", flush=True)
                await asyncio.sleep(wait)
            except Exception as e:
                print(f"[{archive['id']}] FAILED '{title}' (attempt {attempt + 1}): {e}", flush=True)
                await asyncio.sleep(8)
        if ok:
            created += 1
            if created % 25 == 0:
                print(f"[{archive['id']}] {created} topics created, {skipped} reused…", flush=True)
        await asyncio.sleep(delay)
    print(f"[{archive['id']}] DONE: {created} created, {skipped} already existed", flush=True)


async def main():
    cfg = load_config("config.yaml")
    archives = archives_from_env()
    db = DB(os.environ.get("DB_PATH", os.path.join(ROOT, "data", "archive.db")))
    groups = db.load_groups_full()
    dup_of = db.load_dedup()
    planner = Planner(cfg, archives)
    id_map = {id(g): g._db_id for g in groups}
    plan = planner.plan(groups, id_map, dup_of)
    topics = plan["topics"]
    print(f"planned topics: {len(topics)} — pre-creating in {len(archives)} archives", flush=True)

    client = build_client()
    await client.start()

    async def one(a):
        info = await verify_forum_permissions(client, a["chat_id"])
        print(f"✔ {info['title']} ready — building topics…", flush=True)
        await precreate(client, db, a, topics)

    try:
        await asyncio.gather(*(one(a) for a in archives))
    finally:
        await client.disconnect()
        db.close()
    print("ALL TOPICS PRE-CREATED", flush=True)


asyncio.run(main())
