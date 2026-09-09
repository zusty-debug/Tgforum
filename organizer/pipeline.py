"""High-level pipeline steps shared by the CLI and the tests.

Everything here works offline against the SQLite database — no Telegram
calls — so scan→classify→dry-run is fully testable before any
credentials exist.
"""
from __future__ import annotations

import logging
import os

from .db import DB
from .dedup import DuplicateDetector
from .grouping import GroupingEngine
from .planner import Planner
from .report import build_report

log = logging.getLogger("organizer.grouping")


def run_classify(db: DB, cfg: dict, archives: list[dict]) -> dict:
    """Scan results → logical groups + dedup + review items (idempotent)."""
    import time
    t_phase = time.time()
    log.info("classify: loading files…")
    files = db.load_files()
    if not files:
        return {"error": "no source files — run `python main.py scan` first"}
    log.info("classify: loaded %d files in %.1fs", len(files), time.time() - t_phase)

    t_phase = time.time()
    groups = GroupingEngine(cfg).group(files)
    log.info("classify: %d groups in %.1fs", len(groups), time.time() - t_phase)

    t_phase = time.time()
    dup_of, l2 = DuplicateDetector().detect(files)
    log.info("classify: dedup done in %.1fs (%d exact dups, %d review candidates)",
             time.time() - t_phase, len(dup_of), len(l2))

    t_phase = time.time()
    planner = Planner(cfg, archives)
    planner.enrich(groups, dup_of)
    log.info("classify: enrich done in %.1fs", time.time() - t_phase)

    t_phase = time.time()
    db.reset_classification()
    id_map = {}
    with db.transaction():  # one commit for all 90k+ writes, not one per row
        for g in groups:
            gid = db.insert_group(g)
            id_map[id(g)] = gid
        db.mark_duplicates(dup_of)
        for g in groups:
            gid = id_map[id(g)]
            if g.status == "REVIEW":
                db.insert_review_item("grouping", {
                    "group": g.name, "confidence": g.confidence,
                    "reason": g.review_reason, "parts": g.part_numbers,
                    "filenames": [m.filename for m in g.members[:10]],
                    "files": len(g.members),
                }, "accept | ignore | rename", group_id=gid)
        for a, b, sim, note in l2:
            db.insert_review_item("duplicate",
                                  {"a": a, "b": b, "files": note,
                                   "name_similarity": sim},
                                  "mark duplicate | keep both", msg_id=a)
        for g in groups:
            c = g.country
            if g.status == "OK" and c and 0.4 <= c.confidence < 0.75:
                db.insert_review_item("country",
                                      {"group": g.name, "country": c.name,
                                       "confidence": c.confidence, "reasons": c.reasons},
                                      "verify country", group_id=id_map[id(g)])
        db.insert_dedup(dup_of)
    log.info("classify: DB writes done in %.1fs", time.time() - t_phase)

    summary = {
        "files": len(files),
        "groups": len(groups),
        "multipart": sum(1 for g in groups if g.is_multipart),
        "ok": sum(1 for g in groups if g.status == "OK"),
        "review": sum(1 for g in groups if g.status == "REVIEW"),
        "quarantined": sum(1 for g in groups if g.status == "QUARANTINED"),
        "duplicates": len(dup_of),
        "dup_candidates": len(l2),
    }
    log.info("classify: %s", summary)
    return summary


def run_dry_run(db: DB, cfg: dict, archives: list[dict]) -> dict:
    """Full proposed plan from stored classification. No Telegram calls."""
    groups = db.load_groups_full()
    if not groups:
        return {"error": "nothing classified — run `python main.py classify` first"}
    dup_of = db.load_dedup()
    l2 = [(r["source_message_id"], None, None, p.get("files", ""))
          for r in db.list_review_items(kind="duplicate", limit=1000)
          if (p := __import__("json").loads(r["payload"] or "{}"))]
    planner = Planner(cfg, archives)
    id_map = {id(g): g._db_id for g in groups}  # type: ignore[attr-defined]
    plan = planner.plan(groups, id_map, dup_of)
    report = build_report(os.environ.get("SOURCE_CHAT_ID", "?"),
                          db.count_source(), groups, dup_of, l2, plan, archives)
    report["_plan"] = plan
    report["_groups"] = groups
    return report


def build_group_views(groups: list, plan: dict) -> list[dict]:
    """Per-group work orders for the sync worker (dups excluded, parts ordered)."""
    by_gid = {getattr(g, "_db_id", None): g for g in groups}
    views = []
    for gid, p in plan["groups"].items():
        g = by_gid.get(gid)
        if g is None:
            continue
        if p.topic_title is None and not p.to_saved:
            continue
        copy_set = set(p.copy_ids)
        members = sorted((m for m in g.members if m.message_id in copy_set),
                         key=lambda m: (m.part.part_number if m.part.has_part() else 10 ** 9,
                                        m.message_id))
        v = {
            "gid": gid,
            "name": g.name,
            "confidence": g.confidence,
            "topic_title": p.topic_title,
            "kind": p.topic_kind,
            "summary": p.summary,
            "to_saved": p.to_saved,
            "review_reason": g.review_reason,
            "members": [{"msg_id": m.message_id, "file_id": m.file_id} for m in members],
            "sort_key": g.members[0].message_id,
            "marker": None,
        }
        if v["kind"] == "review" and v["topic_title"]:
            v["marker"] = (
                f"🔎 REVIEW — {v['name']} (confidence {v['confidence']:.2f})\n"
                f"Reason: {v['review_reason'] or 'needs review'}\n"
                f"Approve: python main.py approve {v['gid']} --action accept")
        views.append(v)
    views.sort(key=lambda v: v["sort_key"])
    return views
