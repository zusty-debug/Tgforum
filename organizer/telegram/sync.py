"""Archive sync worker — silent copies, resume-safe, flood-aware.

Files are sent with send_file(file_id): Telegram resolves the file
server-side for the same account — no re-download, no re-upload, and no
'Forwarded from …' label (the requirement: source stays hidden).

Each archive paces itself (sync_delay_seconds); archives run in
parallel under one client. Every operation is a row in processing_jobs,
so a crash mid-way resumes exactly where it stopped — no duplicates.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import os
import time
import zlib

from telethon import errors

from ..planner import topic_icon
from .topics import ensure_topic

log = logging.getLogger("organizer.telegram")

# msg_id -> resolved media (Document/Photo). The SAME source message is
# copied to all 3 dumps, so each one should be looked up only ONCE.
# Entries are small InputDocument-ish metadata objects.
resolve_cache: dict[int, object] = {}

# ── batch prefetch for numeric file_ids ─────────────────────────────
# Source file_ids are internal numeric ids → each one normally needs a
# get_messages lookup. Instead of 91,935 one-by-one calls, we fetch the
# next batches of KNOWN message ids in chunks of 50 (1 call per 50
# copies). Poisoned ids (batch failed) fall back to per-copy resolution.
_prefetch: dict = {"ids": None, "pos": 0, "up_to": 0, "skip": set()}
_prefetch_lock: asyncio.Lock | None = None
_INF = 10**18


def seed_prefetch(views: list[dict]) -> int:
    """Fill the batch list with all uncached numeric msg_ids (sorted)."""
    global _prefetch_lock
    if _prefetch["ids"] is None:
        ids = sorted({m["msg_id"] for v in views for m in v["members"]
                      if m["file_id"] and m["file_id"].isdigit()
                      and m["msg_id"] not in resolve_cache})
        _prefetch["ids"] = ids
        _prefetch["pos"] = 0
        _prefetch["up_to"] = (ids[0] - 1) if ids else _INF
        if _prefetch_lock is None:
            _prefetch_lock = asyncio.Lock()
    return len(_prefetch["ids"])


def _next_batch(pos: int, chunk: int) -> tuple[list[int], int]:
    ids, skip = _prefetch["ids"], _prefetch["skip"]
    out = []
    while pos < len(ids) and len(out) < chunk:
        if ids[pos] not in skip:
            out.append(ids[pos])
        pos += 1
    return out, pos


async def ensure_prefetched(client, src_chat_id: int, target: int,
                            chunk: int = 50, pacer=None) -> None:
    """Ensure prefetch ids up to `target` (inclusive) are in resolve_cache."""
    if _prefetch["ids"] is None or target <= _prefetch["up_to"]:
        return
    consecutive_floods = 0
    consecutive_net = 0
    async with _prefetch_lock:
        while _prefetch["up_to"] < target:
            pre_pos = _prefetch["pos"]
            batch, _prefetch["pos"] = _next_batch(pre_pos, chunk)
            if not batch:
                _prefetch["up_to"] = _INF  # exhausted / all poisoned
                break
            if pacer is not None:
                await pacer.throttle()
            try:
                # bounded: a dead TCP connection must never hang the run
                msgs = await asyncio.wait_for(
                    client.get_messages(src_chat_id, ids=batch), timeout=90)
            except errors.FloodWaitError as e:
                # Flood waits are TRANSIENT: sleep, then retry the SAME batch.
                # Never poison a batch for a flood (that would force slow
                # per-copy lookups), and never spin without sleeping (that
                # keeps the account's flood state hot forever).
                wait = min(int(e.seconds) + 2, 120)
                consecutive_floods += 1
                if pacer is not None:
                    pacer.on_flood(int(e.seconds) + 2)
                if consecutive_floods >= 3:
                    log.warning("prefetch: %d consecutive flood waits — "
                                "account cooling down 300s (msg %d)",
                                consecutive_floods, batch[0])
                    await asyncio.sleep(300)
                    consecutive_floods = 0
                else:
                    log.warning("prefetch flood wait %ss at msg %d — "
                                "retrying after %ss", int(e.seconds), batch[0], wait)
                    await asyncio.sleep(wait)
                _prefetch["pos"] = pre_pos  # rewind: same batch
                continue
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                # transient network trouble: retry the same batch a few times,
                # then fall back to per-copy resolution for it.
                consecutive_net += 1
                if consecutive_net >= 5:
                    log.warning("prefetch: 5 consecutive network errors — "
                                "per-copy fallback for batch at msg %d", batch[0])
                    _prefetch["skip"].update(batch)
                    _prefetch["up_to"] = max(_prefetch["up_to"], batch[0] - 1)
                    consecutive_net = 0
                    continue
                log.warning("prefetch network error at msg %d (%s) — "
                            "retrying batch after 5s", batch[0], type(e).__name__)
                await asyncio.sleep(5)
                _prefetch["pos"] = pre_pos  # rewind: same batch
                continue
            except errors.RPCError as e:
                log.warning("prefetch RPC error for %d ids at msg %d: %s "
                            "— per-copy fallback for these", len(batch), batch[0], e)
                _prefetch["skip"].update(batch)
                _prefetch["up_to"] = max(_prefetch["up_to"], batch[0] - 1)
                continue
            consecutive_floods = 0
            consecutive_net = 0
            got = msgs if isinstance(msgs, (list, tuple)) else [msgs]
            for msg in got:
                if msg is None:
                    continue
                media = getattr(msg, "media", None)
                if media is None:
                    continue
                doc = getattr(media, "document", None) or getattr(media, "photo", None)
                if doc is not None:
                    resolve_cache[msg.id] = doc
            _prefetch["up_to"] = max(_prefetch["up_to"], batch[-1])


def _source_chat_id() -> int | None:
    src = os.environ.get("SOURCE_CHAT_ID", "").strip()
    return int(src) if src else None


def _media_of(fetched):
    if isinstance(fetched, str):
        return fetched
    media = getattr(fetched, "media", None)
    if media is not None:
        doc = getattr(media, "document", None)
        if doc is not None:
            return doc  # telethon reuses the file — no re-upload
        photo = getattr(media, "photo", None)
        if photo is not None:
            return photo
    raise RuntimeError("no media in fetched message")


async def get_payload(worker: "SyncWorker", m: dict) -> object:
    """Sendable payload for a member: real file_id string, cached
    Document, batch-prefetched Document, or a fresh per-copy resolve.

    Never stalls long: the prefetch ratchet is pulled only a small horizon
    ahead; a member far beyond it (e.g. a scattered review-group part) is
    resolved with ONE neighborhood batch instead of ratcheting the whole
    gap (which would mean hours of dead time).
    """
    fid = m["file_id"]
    if len(fid) >= 30 and not fid.isdigit():
        return fid
    cached = resolve_cache.get(m["msg_id"])
    if cached is not None:
        return cached
    src = _source_chat_id()
    if src is None:
        raise RuntimeError("SOURCE_CHAT_ID not set — cannot resolve "
                           f"numeric file_id for msg {m['msg_id']}")
    if m["msg_id"] <= _prefetch["up_to"] + 2000:
        await ensure_prefetched(worker.client, src, m["msg_id"],
                                pacer=worker.pacer)
        cached = resolve_cache.get(m["msg_id"])
        if cached is not None:
            return cached
    # Far member: resolve a small neighborhood in ONE call. Review groups
    # cluster in source space, so this usually covers the rest of the group.
    ids = _prefetch["ids"] or []
    if ids:
        lo = bisect.bisect_left(ids, m["msg_id"] - 15)
        hi = bisect.bisect_right(ids, m["msg_id"] + 15)
        window = ids[lo:hi]
    else:
        window = [m["msg_id"]]
    fetched = await worker._with_retry(
        lambda: worker.client.get_messages(src, ids=window),
        f"resolve msg {m['msg_id']} +{max(0, len(window) - 1)} neighbors")
    got = fetched if isinstance(fetched, (list, tuple)) else [fetched]
    for msg in got:
        if msg is None:
            continue
        media = getattr(msg, "media", None)
        if media is None:
            continue
        doc = getattr(media, "document", None) or getattr(media, "photo", None)
        if doc is not None:
            resolve_cache[msg.id] = doc
    cached = resolve_cache.get(m["msg_id"])
    if cached is None:
        raise RuntimeError(f"no media in source message {m['msg_id']}")
    return cached


def topic_job_key(title: str) -> int:
    """Stable pseudo message-id for topic-level jobs (CREATE_TOPIC)."""
    return -zlib.crc32((title or "").encode("utf-8"))


def summary_job_key(title: str, gid: int) -> int:
    return -zlib.crc32(f"{title}#{gid}".encode("utf-8"))


class GlobalPacer:
    """ONE shared pacing slot for all archive workers.

    Telegram rate-limits the *account*, not each bot. Three workers bursting
    in parallel (each with its own small delay) trip the heavy FLOOD_WAIT
    cooldowns (5-9 min), which dominate the total run time. Every write —
    file copies, topic creation, summary/marker messages — passes through
    this single slot, spaced `gap` seconds apart across ALL workers.

    If Telegram answers with a long flood wait (>= 60s), the gap doubles
    (held for 10 min). After 10 min without a long flood it eases back to
    the base gap. Net effect: the account stays under the throttle line and
    the effective throughput is far higher than burst-then-cooldown.
    """

    def __init__(self, gap: float = 1.5, max_gap: float = 10.0):
        self.base = float(gap)
        self.max_gap = float(max_gap)
        self.gap = self.base
        self.next_t = 0.0
        self.hold_until = 0.0
        self.recent: list[float] = []
        self._lock: asyncio.Lock | None = None

    def _lock_for(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def throttle(self) -> None:
        """Wait until this worker holds the shared slot, then release it."""
        if time.monotonic() > self.hold_until and self.gap > self.base:
            self.gap = self.base  # long-enough quiet period — ease off
        lock = self._lock_for()
        async with lock:
            now = time.monotonic()
            start = max(self.next_t, now)
            self.next_t = start + self.gap
        wait = start - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    def on_flood(self, wait_s: int) -> None:
        """Called when Telegram imposed a flood wait of wait_s seconds.

        Graduated response:
          >= 60s  → gap ×2, held 10 min (heavy flood)
          >= 20s  → gap ×1.5, held 5 min
          < 20s   → counts; 5+ small ones in 5 min → gap ×1.25, held 5 min
        After 10 quiet minutes the gap eases back to base (in throttle()).
        """
        now = time.monotonic()
        self.recent = [t for t in self.recent if now - t < 300]
        self.recent.append(now)
        if wait_s >= 60:
            self.gap = min(self.max_gap, max(self.gap * 2, self.base * 2))
            self.hold_until = now + 600
            log.info("global pace raised to %.1fs for 10 min (flood wait %ss)",
                     self.gap, wait_s)
        elif wait_s >= 20:
            self.gap = min(self.max_gap, self.gap * 1.5)
            self.hold_until = now + 300
            log.info("global pace raised to %.1fs for 5 min (flood wait %ss)",
                     self.gap, wait_s)
        elif len(self.recent) >= 5:
            self.gap = min(self.max_gap, self.gap * 1.25)
            self.hold_until = now + 300
            self.recent.clear()
            log.info("global pace raised to %.1fs for 5 min "
                     "(5+ small floods in 5 min, last wait %ss)", self.gap, wait_s)


class SyncWorker:
    def __init__(self, client, db, cfg, views: list[dict], archive: dict,
                 limit: int | None = None, pacer: GlobalPacer | None = None):
        self.client = client
        self.db = db
        self.cfg = cfg or {}
        self.views = views[:limit] if limit else views
        self.archive = archive
        self.pacer = pacer
        self.proc = self.cfg.get("processing", {})
        self.delay = float(self.proc.get("sync_delay_seconds", 1.2))
        self.retries = int(self.proc.get("max_retries", 5))
        self.backoff_base = float(self.proc.get("backoff_base_seconds", 5))
        self.backoff_max = float(self.proc.get("backoff_max_seconds", 300))
        # Bounded RPC time: telethon has NO request timeout by default, so a
        # half-dead TCP connection would hang a copy FOREVER (observed: a
        # process idle in select for 34+ min on one send_file).
        self.req_timeout = float(self.proc.get("request_timeout_seconds", 90))
        self.done = 0
        self.failed = 0
        self.skipped = 0

    # ── retry wrapper ───────────────────────────────────────────
    async def _with_retry(self, fn, what: str):
        for attempt in range(self.retries):
            try:
                if self.pacer is not None:
                    await self.pacer.throttle()  # shared account-wide slot
                return await asyncio.wait_for(fn(), timeout=self.req_timeout)
            except errors.FloodWaitError as e:
                wait = int(e.seconds) + 2
                log.warning("[%s] flood wait %ss (%s)", self.archive["id"], wait, what)
                if self.pacer is not None:
                    self.pacer.on_flood(wait)
                await asyncio.sleep(wait)
            except (errors.RPCError, ConnectionError, OSError, asyncio.TimeoutError) as e:
                wait = min(self.backoff_max, self.backoff_base * (2 ** attempt))
                log.warning("[%s] %s on %s — dropping connection, retry in %ss",
                            self.archive["id"], type(e).__name__, what, wait)
                # A hung/dead connection never heals on its own — force a
                # fresh one for the next attempt.
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(wait)
        raise RuntimeError(f"max retries exceeded: {what}")

    # ── main loop ───────────────────────────────────────────────
    async def run(self):
        log.info("[%s] starting sync of %d groups → %s",
                 self.archive["id"], len(self.views), self.archive["chat_id"])
        n = seed_prefetch(self.views)
        if n:
            log.info("[%s] batch prefetch armed for %d source messages",
                     self.archive["id"], n)
        for v in self.views:
            if v["to_saved"]:
                await self._sync_saved(v)
            elif v["topic_title"]:
                await self._sync_group(v)
        log.info("[%s] pass complete: %d copied, %d skipped-already-done, %d failed",
                 self.archive["id"], self.done, self.skipped, self.failed)

    async def _sync_group(self, v: dict):
        arch, chat = self.archive["id"], self.archive["chat_id"]
        title = v["topic_title"]

        tkey = topic_job_key(title)
        job = self.db.ensure_job(arch, tkey, "CREATE_TOPIC", v["gid"], title)
        if job["status"] == "SUCCESS" and job["dest_message_id"]:
            topic_id = job["dest_message_id"]
            self.skipped += 1
        else:
            try:
                topic_id = await self._with_retry(
                    lambda: ensure_topic(client=self.client, chat_id=chat,
                                         title=title, icon_color=topic_icon(title),
                                         db=self.db, archive_id=arch),
                    f"create topic '{title}'")
                self.db.set_job(arch, tkey, "CREATE_TOPIC", "SUCCESS",
                                dest_message_id=topic_id)
                self.done += 1
            except Exception as e:
                self.db.set_job(arch, tkey, "CREATE_TOPIC", "FAILED", error=str(e)[:500])
                self.failed += 1
                log.error("[%s] topic creation failed '%s': %s", arch, title, e)
                return
        v["topic_id"] = topic_id

        # review marker message (Review topic only)
        if v.get("marker"):
            mkey = summary_job_key(title, v["gid"]) - 1
            job = self.db.ensure_job(arch, mkey, "POST_SUMMARY", v["gid"], title)
            if job["status"] != "SUCCESS":
                try:
                    msg = await self._with_retry(
                        lambda: self.client.send_message(chat, v["marker"],
                                                         reply_to=topic_id, silent=True),
                        "review marker")
                    self.db.set_job(arch, mkey, "POST_SUMMARY", "SUCCESS",
                                    dest_message_id=msg.id)
                except Exception as e:
                    self.db.set_job(arch, mkey, "POST_SUMMARY", "FAILED", error=str(e)[:500])

        for m in v["members"]:
            ok = await self._copy_member(v, m)
            if ok:
                self.skipped += 1  # counts both fresh copies and resume-skips

        if v["summary"]:
            skey = summary_job_key(title, v["gid"])
            job = self.db.ensure_job(arch, skey, "POST_SUMMARY", v["gid"], title)
            if job["status"] != "SUCCESS":
                try:
                    msg = await self._with_retry(
                        lambda: self.client.send_message(chat, v["summary"],
                                                         reply_to=topic_id, silent=True),
                        "summary message")
                    try:  # pin it (per requirement) — non-fatal if denied
                        await self.client.pin_message(chat, msg.id)
                    except Exception as pe:
                        log.warning("[%s] pin failed: %s", arch, pe)
                    self.db.set_job(arch, skey, "POST_SUMMARY", "SUCCESS",
                                    dest_message_id=msg.id)
                    self.done += 1
                except Exception as e:
                    self.db.set_job(arch, skey, "POST_SUMMARY", "FAILED", error=str(e)[:500])
                    self.failed += 1
                    log.error("[%s] summary failed for '%s': %s", arch, title, e)

    async def _copy_member(self, v: dict, m: dict) -> bool:
        arch = self.archive["id"]
        job = self.db.ensure_job(arch, m["msg_id"], "COPY_FILE", v["gid"],
                                 v["topic_title"])
        if job["status"] == "SUCCESS":
            return True  # already done in a previous run
        if job["status"] == "FAILED" and (job["attempts"] or 0) >= self.retries:
            log.error("[%s] giving up on file msg %d after %d attempts",
                      arch, m["msg_id"], job["attempts"])
            self.failed += 1
            return False
        if not m["file_id"]:
            self.db.set_job(arch, m["msg_id"], "COPY_FILE", "FAILED",
                            error="no file_id in source metadata")
            self.failed += 1
            return False
        self.db.set_job(arch, m["msg_id"], "COPY_FILE", "RUNNING", bump_attempt=True)

        try:
            payload = await get_payload(self, m)
            msg = await self._with_retry(
                lambda: self.client.send_file(
                    self.archive["chat_id"], payload,
                    reply_to=v["topic_id"], silent=True),
                f"copy msg {m['msg_id']}")
            self.db.set_job(arch, m["msg_id"], "COPY_FILE", "SUCCESS",
                            dest_message_id=msg.id)
            self.done += 1
            # (pacing is handled by the shared GlobalPacer)
            return True
        except Exception as e:
            self.db.set_job(arch, m["msg_id"], "COPY_FILE", "FAILED", error=str(e)[:500])
            self.failed += 1
            log.error("[%s] copy failed msg %d: %s", arch, m["msg_id"], e)
            return False

    async def _sync_saved(self, v: dict):
        """Review flow: copy files to the owner's Saved Messages."""
        arch = self.archive["id"]
        if getattr(self, "_me", None) is None:
            try:
                self._me = await self.client.get_entity("me")
            except Exception as e:
                log.error("[%s] cannot resolve 'me' for saved review: %s", arch, e)
                return
        me = self._me
        note = (f"🔎 REVIEW — {v['name']} (confidence {v['confidence']:.2f})\n"
                f"Reason: {v['review_reason'] or 'unresolvable — needs admin decision'}\n"
                f"Approve: python main.py approve {v['gid']} --action accept")
        for m in v["members"]:
            job = self.db.ensure_job(arch, m["msg_id"], "REVIEW_SAVED", v["gid"],
                                     "Saved Messages")
            if job["status"] == "SUCCESS":
                continue
            if not m["file_id"]:
                continue
            self.db.set_job(arch, m["msg_id"], "REVIEW_SAVED", "RUNNING", bump_attempt=True)
            try:
                payload = await get_payload(self, m)
                await self._with_retry(
                    lambda: self.client.send_file(me, payload, silent=True),
                    f"saved review msg {m['msg_id']}")
                self.db.set_job(arch, m["msg_id"], "REVIEW_SAVED", "SUCCESS")
                self.done += 1
            except Exception as e:
                self.db.set_job(arch, m["msg_id"], "REVIEW_SAVED", "FAILED",
                                error=str(e)[:500])
                self.failed += 1
        # one note per reviewed group
        nkey = summary_job_key("saved", v["gid"])
        job = self.db.ensure_job(arch, nkey, "POST_SUMMARY", v["gid"], "Saved Messages")
        if job["status"] != "SUCCESS":
            try:
                msg = await self._with_retry(
                    lambda: self.client.send_message(me, note, silent=True),
                    "saved review note")
                self.db.set_job(arch, nkey, "POST_SUMMARY", "SUCCESS",
                                dest_message_id=msg.id)
            except Exception as e:
                self.db.set_job(arch, nkey, "POST_SUMMARY", "FAILED", error=str(e)[:500])


async def run_archive(client, db, cfg, views, archive, limit=None,
                      pacer: GlobalPacer | None = None):
    worker = SyncWorker(client, db, cfg, views, archive, limit, pacer=pacer)
    await worker.run()
    return worker.done, worker.failed
