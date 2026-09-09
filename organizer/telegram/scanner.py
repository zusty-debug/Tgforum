"""Streaming metadata ingest — no file downloads, checkpointed, resumable.

Reads message metadata oldest→newest and upserts it into SQLite. The
watermark (last message id) persists between runs, so re-running the scan
picks up only NEW files (incremental) and a crash never restarts from zero.
"""
from __future__ import annotations

import logging

log = logging.getLogger("organizer.telegram")


def _attr_filename(doc):
    """Filename lives in DocumentAttributeFilename (telethon >= 1.41 raw types)."""
    for a in (getattr(doc, "attributes", None) or []):
        name = getattr(a, "file_name", None)
        if name:
            return name
    return None


def _media_info(msg):
    """Extract (file_id, filename, size, mime, media_type) or None."""
    media = msg.media
    if media is None:
        return None
    doc = getattr(media, "document", None)
    if doc is not None:
        # doc.file_id is the sendable string; doc.id is an internal long
        # (storing it breaks send_file — "not a valid file ID")
        fid = getattr(doc, "file_id", None) or str(doc.id)
        return (fid, _attr_filename(doc), doc.size, doc.mime_type, "document")
    photo = getattr(media, "photo", None)
    if photo is not None:
        fid = getattr(photo, "file_id", None) or str(getattr(photo, "id", ""))
        return (fid or None, None, 0, "image/jpeg", "photo")
    return None


async def scan_source(client, source_id, db, commit_every: int = 200) -> int:
    ent = await client.get_entity(source_id)
    username = getattr(ent, "username", None)
    if username:
        base_link = f"https://t.me/{username}"
    else:
        base_link = f"https://t.me/c/{abs(source_id)}"

    last = int(db.get_setting("scan_offset_id", 0) or 0)
    log.info("scanning chat %s from offset %d", source_id, last)
    scanned = 0
    commit_batch = 0
    last_seen = last
    async for msg in client.iter_messages(source_id, reverse=True, offset_id=last):
        info = _media_info(msg)
        if info is None:
            continue
        file_id, name, size, mime, mtype = info
        if not file_id:
            continue
        ext = name.rsplit(".", 1)[-1].lower() if name and "." in name else ""
        db.upsert_source_message(
            chat_id=source_id, message_id=msg.id, file_id=file_id,
            filename=name or f"(no name) {msg.id}", extension=ext, mime_type=mime,
            size=size or 0, date=msg.date.timestamp() if msg.date else None,
            caption=msg.message,
            media_group_id=getattr(msg, "grouped_id", None)
                           or getattr(msg, "media_group_id", None),
            media_type=mtype, link=f"{base_link}/{msg.id}")
        commit_batch += 1
        scanned += 1
        last_seen = msg.id
        if commit_batch >= commit_every:
            db.conn.commit()
            db.set_setting("scan_offset_id", last_seen)
            log.info("scan checkpoint: %d new files (offset %d)", scanned, last_seen)
            commit_batch = 0
    db.conn.commit()
    if last_seen > last:
        db.set_setting("scan_offset_id", last_seen)
    log.info("scan complete: %d files this pass", scanned)
    return scanned
