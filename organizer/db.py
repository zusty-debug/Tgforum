"""SQLite persistence layer — WAL, batched, resumable, crash-safe."""
from __future__ import annotations
from contextlib import contextmanager

import json
import os
import sqlite3
from pathlib import Path

from .models import LogicalGroup, SourceFile
from .normalize import prepare_file
from .partparser import parse_filename

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class DB:
    def __init__(self, path: str | os.PathLike):
        path = str(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.conn.commit()
        self._in_batch = False

    @contextmanager
    def transaction(self):
        """Run many writes under ONE commit (10-100x faster for bulk loads)."""
        self._in_batch = True
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._in_batch = False

    def _commit(self):
        if not self._in_batch:
            self.conn.commit()

    def close(self):
        self._commit()
        self.conn.close()

    # ── settings ────────────────────────────────────────────────
    def set_setting(self, key: str, value):
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self._commit()

    def get_setting(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ── source messages ─────────────────────────────────────────
    def upsert_source_message(self, *, chat_id, message_id, file_id, filename,
                              extension, mime_type, size, date, caption,
                              media_group_id, media_type, link):
        self.conn.execute(
            """INSERT INTO source_messages
               (source_chat_id, source_message_id, file_id, filename, extension,
                mime_type, file_size, message_date, caption, media_group_id,
                media_type, message_link)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_chat_id, source_message_id) DO UPDATE SET
                file_id=excluded.file_id, filename=excluded.filename,
                extension=excluded.extension, mime_type=excluded.mime_type,
                file_size=excluded.file_size, message_date=excluded.message_date,
                caption=excluded.caption, media_group_id=excluded.media_group_id,
                media_type=excluded.media_type, message_link=excluded.message_link""",
            (chat_id, message_id, file_id, filename, extension, mime_type, size,
             date, caption, media_group_id, media_type, link))

    def count_source(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM source_messages").fetchone()["c"]

    def iter_source_files(self, batch: int = 5000):
        cur = self.conn.execute("SELECT * FROM source_messages ORDER BY source_message_id")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            yield rows

    def load_files(self) -> list[SourceFile]:
        files: list[SourceFile] = []
        for rows in self.iter_source_files():
            for r in rows:
                f = SourceFile(
                    message_id=r["source_message_id"], chat_id=r["source_chat_id"],
                    file_id=r["file_id"], filename=r["filename"] or "",
                    size=r["file_size"], date=r["message_date"], mime=r["mime_type"],
                    caption=r["caption"], media_group_id=r["media_group_id"])
                f.part = parse_filename(f.filename)
                prepare_file(f)
                files.append(f)
        return files

    # ── classification ──────────────────────────────────────────
    def reset_classification(self):
        self.conn.executescript(
            "DELETE FROM group_members; DELETE FROM logical_groups; "
            "DELETE FROM review_items; DELETE FROM deduplication;")
        self._commit()

    def insert_group(self, g: LogicalGroup) -> int:
        cur = self.conn.execute(
            """INSERT INTO logical_groups
               (canonical_name, normalized_name, category, country, country_code,
                country_confidence, country_reasons, group_confidence, group_reasons,
                is_multipart, part_count, total_size, status, review_reason,
                quarantine_reason, missing_parts, partial_start)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (g.name, g.norm, g.category,
             g.country.name if g.country else None,
             g.country.code if g.country else None,
             g.country.confidence if g.country else None,
             json.dumps(g.country.reasons, ensure_ascii=False) if g.country else None,
             g.confidence, json.dumps(g.reasons, ensure_ascii=False),
             1 if g.is_multipart else 0,
             len(g.part_numbers), g.total_size, g.status, g.review_reason,
             g.quarantine_reason, json.dumps(g.missing_parts),
             1 if g.partial_start else 0))
        gid = cur.lastrowid
        for m in g.members:
            self.conn.execute(
                """INSERT OR REPLACE INTO group_members
                   (logical_group_id, source_message_id, part_number, part_label,
                    assignment_confidence)
                   VALUES(?,?,?,?,?)""",
                (gid, m.message_id, m.part.part_number, m.part.part_label, g.confidence))
        self._commit()
        return gid

    def mark_duplicates(self, dup_of: dict):
        """Flag duplicate member rows. ONE update per dup (message id is
        unique across groups) — the old per-group × per-dup version was
        O(groups × dups) ≈ 10^9 updates at scale."""
        rows = [(canon, m_id) for m_id, (canon, _c, _t) in dup_of.items()]
        self.conn.executemany(
            "UPDATE group_members SET duplicate_of_message_id=? "
            "WHERE source_message_id=?", rows)
        self._commit()

    def insert_dedup(self, dup_of: dict):
        for dup_id, (canon, conf, typ) in dup_of.items():
            self.conn.execute(
                """INSERT OR REPLACE INTO deduplication
                   (fingerprint, fingerprint_type, canonical_message_id,
                    duplicate_message_id, confidence)
                   VALUES(?,?,?,?,?)""",
                (f"{typ}:{canon}", typ, canon, dup_id, conf))
        self._commit()

    def load_dedup(self) -> dict:
        rows = self.conn.execute(
            "SELECT canonical_message_id c, duplicate_message_id d, "
            "confidence cf, fingerprint_type t FROM deduplication").fetchall()
        return {r["d"]: (r["c"], r["cf"], r["t"]) for r in rows}

    def load_groups_full(self) -> list[LogicalGroup]:
        rows = self.conn.execute(
            """SELECT lg.*, sm.source_chat_id, sm.source_message_id AS mid,
                      sm.file_id, sm.filename, sm.mime_type,
                      sm.file_size, sm.message_date, sm.caption, sm.media_group_id,
                      gm.part_number, gm.part_label, gm.duplicate_of_message_id
               FROM logical_groups lg
               JOIN group_members gm ON gm.logical_group_id = lg.id
               JOIN source_messages sm ON sm.source_message_id = gm.source_message_id
               ORDER BY lg.id, COALESCE(gm.part_number, 999999), sm.source_message_id"""
        ).fetchall()
        groups: dict[int, dict] = {}
        for r in rows:
            g = groups.setdefault(r["id"], {"row": r, "members": []})
            f = SourceFile(
                message_id=r["mid"], chat_id=r["source_chat_id"], file_id=r["file_id"],
                filename=r["filename"] or "", size=r["file_size"], date=r["message_date"],
                mime=r["mime_type"], caption=r["caption"],
                media_group_id=r["media_group_id"])
            f.part = parse_filename(f.filename)
            prepare_file(f)
            g["members"].append(f)
        out: list[LogicalGroup] = []
        for gid, g in groups.items():
            row = g["row"]
            lg = LogicalGroup(
                name=row["canonical_name"], norm=row["normalized_name"] or "",
                members=g["members"], is_multipart=bool(row["is_multipart"]),
                part_numbers=sorted({m.part.part_number for m in g["members"]
                                     if m.part.has_part()}),
                missing_parts=json.loads(row["missing_parts"] or "[]"),
                partial_start=bool(row["partial_start"]),
                total_size=row["total_size"] or 0,
                confidence=row["group_confidence"] or 0.0,
                reasons=json.loads(row["group_reasons"] or "[]"),
                status=row["status"], review_reason=row["review_reason"],
                quarantine_reason=row["quarantine_reason"],
                category=row["category"] or "Miscellaneous")
            lg._db_id = gid  # type: ignore[attr-defined]
            if row["country"]:
                from .models import CountryResult
                lg.country = CountryResult(
                    row["country_code"], row["country"], row["country_confidence"] or 0.0,
                    json.loads(row["country_reasons"] or "[]"))
            out.append(lg)
        out.sort(key=lambda g: g.members[0].message_id)
        return out

    # ── review items ────────────────────────────────────────────
    def insert_review_item(self, kind, payload, recommended, group_id=None, msg_id=None):
        self.conn.execute(
            "INSERT INTO review_items(kind, logical_group_id, source_message_id, "
            "payload, recommended_action) VALUES(?,?,?,?,?)",
            (kind, group_id, msg_id, json.dumps(payload, ensure_ascii=False), recommended))
        self._commit()

    def get_review_item(self, rid):
        return self.conn.execute("SELECT * FROM review_items WHERE id=?", (rid,)).fetchone()

    def list_review_items(self, kind=None, status="OPEN", limit=200):
        q, args = "SELECT * FROM review_items WHERE status=?", [status]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY id LIMIT ?"
        args.append(limit)
        return self.conn.execute(q, args).fetchall()

    def set_review_status(self, rid, status, action):
        self.conn.execute("UPDATE review_items SET status=?, action_taken=? WHERE id=?",
                          (status, action, rid))
        self._commit()

    def set_group_status(self, gid, status, name=None, quarantine_reason=None):
        self.conn.execute(
            """UPDATE logical_groups SET status=?, review_reason=NULL,
               quarantine_reason=COALESCE(?, quarantine_reason),
               canonical_name=COALESCE(?, canonical_name), updated_at=datetime('now')
               WHERE id=?""", (status, quarantine_reason, name, gid))
        self._commit()

    # ── destinations & jobs ─────────────────────────────────────
    def find_destination(self, archive_id, title):
        return self.conn.execute(
            "SELECT * FROM destinations WHERE archive_id=? AND topic_title=?",
            (archive_id, title)).fetchone()

    def save_destination(self, archive_id, chat_id, topic_id, title,
                         group_id=None, status="READY"):
        self.conn.execute(
            """INSERT INTO destinations
               (archive_id, telegram_chat_id, topic_id, topic_title,
                logical_group_id, status)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(archive_id, topic_title) DO UPDATE SET
                topic_id=COALESCE(excluded.topic_id, destinations.topic_id),
                status=excluded.status""",
            (archive_id, chat_id, topic_id, title, group_id, status))
        self._commit()

    def topic_counts(self, archive_id) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT topic_title) c FROM destinations "
            "WHERE archive_id=? AND topic_id IS NOT NULL", (archive_id,)).fetchone()["c"]

    def job(self, archive_id, msg_id, op):
        return self.conn.execute(
            "SELECT * FROM processing_jobs WHERE archive_id=? AND "
            "source_message_id=? AND operation=?", (archive_id, msg_id, op)).fetchone()

    def ensure_job(self, archive_id, msg_id, op, group_id, topic_title):
        row = self.job(archive_id, msg_id, op)
        if row:
            if row["topic_title"] != topic_title:
                self.conn.execute(
                    "UPDATE processing_jobs SET topic_title=?, logical_group_id=? WHERE id=?",
                    (topic_title, group_id, row["id"]))
                self._commit()
            return self.job(archive_id, msg_id, op)
        self.conn.execute(
            "INSERT INTO processing_jobs(archive_id, source_message_id, "
            "logical_group_id, operation, topic_title, status) VALUES(?,?,?,?,?,'PENDING')",
            (archive_id, msg_id, group_id, op, topic_title))
        self._commit()
        return self.job(archive_id, msg_id, op)

    def set_job(self, archive_id, msg_id, op, status, error=None,
                dest_message_id=None, bump_attempt=False):
        sets = ["status=?", "last_error=?", "dest_message_id=?",
                "last_attempt_at=datetime('now')"]
        params: list = [status, error, dest_message_id]
        if bump_attempt:
            sets.append("attempts=attempts+1")
        if status == "SUCCESS":
            sets.append("completed_at=datetime('now')")
        sql = ("UPDATE processing_jobs SET " + ", ".join(sets) +
               " WHERE archive_id=? AND source_message_id=? AND operation=?")
        self.conn.execute(sql, params + [archive_id, msg_id, op])
        self._commit()

    def job_counts(self, archive_id=None):
        q, args = "SELECT status, COUNT(*) c FROM processing_jobs", []
        if archive_id:
            q += " WHERE archive_id=?"
            args.append(archive_id)
        q += " GROUP BY status"
        return {r["status"]: r["c"] for r in self.conn.execute(q, args).fetchall()}

    def counts(self, table: str, col: str) -> dict:
        rows = self.conn.execute(
            f"SELECT {col} k, COUNT(*) c FROM {table} GROUP BY {col}").fetchall()
        return {r["k"]: r["c"] for r in rows}

    # ── streaming sync queue (low-memory mode) ──────────────────
    def build_sync_queue(self, views: list[dict]) -> tuple[int, int]:
        """Persist the plan's views as a work queue (id = plan order).

        Run offline (`main.py plan-queue`) in a roomy environment; the
        container then only ever holds ONE view at a time.
        """
        with self.transaction():
            self.conn.execute("DELETE FROM sync_queue_members")
            self.conn.execute("DELETE FROM sync_queue")
            try:
                self.conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN "
                    "('sync_queue', 'sync_queue_members')")
            except sqlite3.OperationalError:
                pass
            n_m = 0
            for i, v in enumerate(views, start=1):
                cur = self.conn.execute(
                    "INSERT INTO sync_queue(id, sort_key, gid, name, confidence,"
                    " topic_title, kind, summary, to_saved, review_reason, marker)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (i, int(v["sort_key"]), int(v["gid"]), v.get("name"),
                     v.get("confidence"), v.get("topic_title"), v.get("kind"),
                     v.get("summary"), int(bool(v.get("to_saved"))),
                     v.get("review_reason"), v.get("marker")))
                qid = cur.lastrowid
                for j, m in enumerate(v["members"]):
                    self.conn.execute(
                        "INSERT INTO sync_queue_members"
                        "(queue_id, seq, msg_id, file_id) VALUES (?,?,?,?)",
                        (qid, j, int(m["msg_id"]), m.get("file_id")))
                    n_m += 1
        return len(views), n_m

    def queue_count(self) -> int:
        try:
            return int(self.conn.execute(
                "SELECT COUNT(*) c FROM sync_queue").fetchone()["c"])
        except sqlite3.OperationalError:
            return 0

    def iter_sync_views(self):
        """Stream plan views one at a time — O(1) memory, plan order."""
        for r in self.conn.execute("SELECT * FROM sync_queue ORDER BY id"):
            members = [
                {"msg_id": mr["msg_id"], "file_id": mr["file_id"]}
                for mr in self.conn.execute(
                    "SELECT msg_id, file_id FROM sync_queue_members"
                    " WHERE queue_id=? ORDER BY seq", (r["id"],))
            ]
            yield {
                "id": r["id"], "sort_key": r["sort_key"], "gid": r["gid"],
                "name": r["name"], "confidence": r["confidence"],
                "topic_title": r["topic_title"], "kind": r["kind"],
                "summary": r["summary"], "to_saved": bool(r["to_saved"]),
                "review_reason": r["review_reason"], "marker": r["marker"],
                "members": members,
            }

    def queue_msg_ids(self) -> list[int]:
        """Numeric msg_ids for batch prefetch — a few MB of ints, no views."""
        rows = self.conn.execute(
            "SELECT DISTINCT msg_id FROM sync_queue_members"
            " WHERE file_id GLOB '[0-9]*' ORDER BY 1")
        return [r["msg_id"] for r in rows]
