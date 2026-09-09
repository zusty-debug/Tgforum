#!/usr/bin/env python3
"""Telegram Archive Organizer — CLI.

    python main.py scan            Scan source channel metadata (resumable)
    python main.py classify        Build logical groups (dedup/country/ULP/…)
    python main.py review          Show the review queue
    python main.py approve ID      accept | ignore | quarantine | rename --name X | mark-dup
    python main.py dry-run         Full proposed plan — NO Telegram changes
    python main.py sync            Sync one archive (use --archive archive_1)
    python main.py sync-all        Sync every configured archive (parallel)
    python main.py resume          Alias of sync-all (crash recovery)
    python main.py rebuild-index   Regenerate index.html + data.json
    python main.py status          Show progress
    python main.py auth            One-time login → saves session to .env
    python main.py discover-chats  List your chats and their IDs
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from organizer.db import DB
from organizer.indexgen import generate
from organizer.pipeline import build_group_views, run_classify, run_dry_run
from organizer.planner import Planner
from organizer.report import render_text, save_report


# ─────────────────────────── helpers ───────────────────────────
def load_config(path: str) -> dict:
    import yaml
    for p in (Path(path), Path("config.yaml"),
              Path(__file__).parent / "config.example.yaml"):
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    return {}


def setup_logging(cfg: dict, verbose: bool = False):
    lcfg = cfg.get("logging", {})
    logdir = Path(lcfg.get("dir", "./logs"))
    logdir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else getattr(
        logging, str(lcfg.get("level", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    def add_file(name: str, lvl: int):
        h = logging.FileHandler(logdir / name, encoding="utf-8")
        h.setLevel(lvl)
        h.setFormatter(fmt)
        root.addHandler(h)

    add_file("app.log", level)
    add_file("errors.log", logging.ERROR)
    glog = logging.getLogger("organizer.grouping")
    gh = logging.FileHandler(logdir / "grouping.log", encoding="utf-8")
    gh.setLevel(logging.INFO)
    gh.setFormatter(fmt)
    glog.addHandler(gh)
    tlog = logging.getLogger("organizer.telegram")
    tlog.propagate = False
    th = logging.FileHandler(logdir / "telegram.log", encoding="utf-8")
    th.setLevel(logging.INFO)
    th.setFormatter(fmt)
    tlog.addHandler(th)
    tc = logging.StreamHandler()
    tc.setLevel(level)
    tc.setFormatter(fmt)
    tlog.addHandler(tc)


def archives_from_env() -> list[dict]:
    out = []
    for i in (1, 2, 3):
        raw = os.environ.get(f"ARCHIVE_{i}_CHAT_ID", "").strip()
        if raw:
            try:
                out.append({"id": f"archive_{i}", "name": f"Archive {i}",
                            "chat_id": int(raw)})
            except ValueError:
                print(f"warning: ARCHIVE_{i}_CHAT_ID is not an integer: {raw!r}")
    return out


# ─────────────────────── Telegram commands ─────────────────────
async def cmd_scan(cfg: dict, db: DB) -> int:
    from organizer.telegram.client import build_client
    from organizer.telegram.scanner import scan_source
    source = os.environ.get("SOURCE_CHAT_ID", "").strip()
    if not source:
        print("Set SOURCE_CHAT_ID in .env first (find IDs: python main.py discover-chats)")
        return 1
    client = build_client()
    await client.start()
    try:
        n = await scan_source(client, int(source), db,
                              commit_every=int(cfg.get("processing", {}).get("scan_commit_every", 200)))
    finally:
        await client.disconnect()
    print(f"✔ Scanned {n} new files (total in DB: {db.count_source()})")
    print("Next: python main.py classify")
    return 0


def _prepare_views(cfg: dict, db: DB, archives: list[dict]):
    groups = db.load_groups_full()
    if not groups:
        print("Nothing classified — run `python main.py classify` first")
        return None
    dup_of = db.load_dedup()
    planner = Planner(cfg, archives)
    id_map = {id(g): g._db_id for g in groups}  # type: ignore[attr-defined]
    plan = planner.plan(groups, id_map, dup_of)
    return build_group_views(groups, plan)


async def _sync_archives(cfg: dict, db: DB, archives: list[dict],
                         only: str | None = None, limit: int | None = None,
                         sequential: bool = False) -> int:
    from organizer.telegram.client import build_client, verify_forum_permissions
    from organizer.telegram.sync import GlobalPacer, run_archive
    views = _prepare_views(cfg, db, archives)
    if views is None:
        return 1
    targets = [a for a in archives if only is None or a["id"] == only]
    if not targets:
        print(f"archive '{only}' not configured — set its ARCHIVE_N_CHAT_ID in .env")
        return 1
    client = build_client()
    await client.start()
    parallel = (not sequential and
                int(cfg.get("processing", {}).get("max_parallel_archives", 3)) > 1)

    # One shared pacing slot across ALL archives: Telegram rate-limits the
    # account, so parallel bursts cause 5-9 min FLOOD_WAIT cooldowns.
    # SYNC_GLOBAL_GAP env overrides the config (used by sequential runs).
    gap_cfg = cfg.get("processing", {}).get("global_copy_gap_seconds", 1.5)
    gap = float(os.environ.get("SYNC_GLOBAL_GAP") or gap_cfg)
    pacer = GlobalPacer(gap=gap)
    print(f"  global pacing: 1 Telegram op per {gap:g}s (account-wide), "
          f"auto-raises on long flood waits")

    async def one(a):
        info = await verify_forum_permissions(client, a["chat_id"])
        print(f"✔ {info['title']} — forum OK, manage-topics OK")
        local_views = [dict(v, members=[dict(m) for m in v["members"]]) for v in views]
        return await run_archive(client, db, cfg, local_views, a, limit,
                                 pacer=pacer)

    try:
        if len(targets) == 1 or not parallel:
            results = [await one(a) for a in targets]
        else:
            results = await asyncio.gather(*(one(a) for a in targets))
    finally:
        await client.disconnect()
    for a, (done, failed) in zip(targets, results):
        print(f"  {a['id']}: {done} ops done, {failed} failed "
              f"(rerun `python main.py resume` to continue)")
    return 0


async def cmd_auth() -> int:
    from organizer.telegram.client import interactive_auth
    sess, who = await interactive_auth()
    print(f"\n✔ Logged in as: {who}")
    print("String session (kept in .env — never commit it):")
    print(sess)
    env_path = Path(".env")
    if env_path.exists():
        lines, replaced = [], False
        for ln in env_path.read_text().splitlines():
            if ln.startswith("TELEGRAM_STRING_SESSION="):
                lines.append(f"TELEGRAM_STRING_SESSION={sess}")
                replaced = True
            else:
                lines.append(ln)
        if not replaced:
            lines.append(f"TELEGRAM_STRING_SESSION={sess}")
        env_path.write_text("\n".join(lines) + "\n")
        print("\nSaved to .env (TELEGRAM_STRING_SESSION).")
    else:
        print("\nCreate .env (copy .env.example) and add TELEGRAM_STRING_SESSION=<value>")
    return 0


async def cmd_discover_chats() -> int:
    from organizer.telegram.client import build_client
    client = build_client()
    await client.start()
    try:
        print(f"{'ID':>20}  {'TYPE':<12}  TITLE")
        async for d in client.iter_dialogs():
            ent = d.entity
            if getattr(ent, "broadcast", False):
                kind = "channel"
            elif getattr(ent, "megagroup", False):
                kind = "supergroup"
            elif getattr(ent, "is_group", False):
                kind = "group"
            else:
                continue
            print(f"{d.id:>20}  {kind:<12}  {d.title}")
    finally:
        await client.disconnect()
    return 0


# ─────────────────────── offline commands ──────────────────────
def cmd_classify(cfg: dict, db: DB, archives: list[dict]) -> int:
    summary = run_classify(db, cfg, archives)
    if "error" in summary:
        print(summary["error"])
        return 1
    print(f"✔ Classified {summary['files']:,} files → {summary['groups']:,} logical groups")
    print(f"  multi-part: {summary['multipart']:,} · OK: {summary['ok']:,} · "
          f"review: {summary['review']:,} · quarantined: {summary['quarantined']:,}")
    print(f"  duplicates: {summary['duplicates']:,} exact (will skip) · "
          f"{summary['dup_candidates']:,} candidates for review")
    print("Next: python main.py dry-run")
    return 0


def cmd_dry_run(cfg: dict, db: DB, archives: list[dict]) -> int:
    if not archives:
        print("note: no archives configured yet (ARCHIVE_N_CHAT_ID in .env) — "
              "topic plan will still be shown")
    report = run_dry_run(db, cfg, archives)
    if "error" in report:
        print(report["error"])
        return 1
    print(render_text(report))
    out_dir = Path(cfg.get("index", {}).get("output_dir", "./out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    public_report = {k: v for k, v in report.items() if not k.startswith("_")}
    save_report(public_report, out_dir / "dryrun_report.json")
    print(f"\nJSON report: {out_dir / 'dryrun_report.json'}")
    print("No Telegram changes were made.")
    return 0


def cmd_review(cfg: dict, db: DB, limit: int) -> int:
    items = db.list_review_items(limit=limit)
    if not items:
        print("Review queue is empty. 🎉")
        return 0
    for it in items:
        p = json.loads(it["payload"] or "{}")
        print(f"\n#{it['id']} [{it['kind']}] {p.get('group') or p.get('files') or ''}")
        if p.get("confidence") is not None:
            print(f"   confidence: {p['confidence']}")
        if p.get("reason"):
            print(f"   reason:     {p['reason']}")
        for fn in (p.get("filenames") or [])[:8]:
            print(f"   - {fn}")
        if it["logical_group_id"]:
            print(f"   group id:   {it['logical_group_id']}")
        print(f"   → {it['recommended_action']}")
    print(f"\n({len(items)} items shown — `--limit` to show more)")
    return 0


def cmd_approve(cfg: dict, db: DB, rid: int, action: str, name: str | None) -> int:
    it = db.get_review_item(rid)
    if not it:
        print(f"No review item #{rid}")
        return 1
    gid = it["logical_group_id"]
    if action in ("accept", "rename"):
        if gid:
            db.set_group_status(gid, "OK", name=name)
            print(f"✔ group approved" + (f" as '{name}'" if name else ""))
        db.set_review_status(rid, "APPROVED", action)
    elif action == "ignore":
        if gid:
            db.set_group_status(gid, "IGNORED")
            print("✔ group ignored (files will not be copied)")
        db.set_review_status(rid, "IGNORED", "ignored")
    elif action == "quarantine":
        if gid:
            db.set_group_status(gid, "QUARANTINED", quarantine_reason="quarantined by admin")
            print("✔ group quarantined")
        db.set_review_status(rid, "APPROVED", "quarantine")
    elif action == "mark-dup":
        p = json.loads(it["payload"] or "{}")
        a, b = p.get("a"), p.get("b")
        if a and b:
            db.insert_dedup({b: (a, 0.9, "admin")})
            print(f"✔ marked file msg {b} as duplicate of msg {a} (later copies skipped)")
        db.set_review_status(rid, "APPROVED", "mark-dup")
    else:
        print(f"unknown action: {action}")
        return 1
    print("The next sync will apply this decision.")
    return 0


def cmd_rebuild_index(cfg: dict, db: DB, archives: list[dict]) -> int:
    groups = db.load_groups_full()
    if not groups:
        print("Nothing to index — classify first")
        return 1
    dup_of = db.load_dedup()
    planner = Planner(cfg, archives)
    id_map = {id(g): g._db_id for g in groups}  # type: ignore[attr-defined]
    plan = planner.plan(groups, id_map, dup_of)
    ico = cfg.get("index", {})
    html_path, json_path = generate(
        ico.get("output_dir", "./out"), groups, plan["groups"], archives, db,
        single_file_html=bool(ico.get("single_file_html", False)))
    print(f"✔ {html_path}  (+ {json_path.name})")
    return 0


def cmd_status(cfg: dict, db: DB, archives: list[dict]) -> int:
    print(f"Source files in DB: {db.count_source():,}")
    print(f"Logical groups:     {dict(db.counts('logical_groups', 'status')) or '—'}")
    for a in archives:
        print(f"  {a['id']}: topics {db.topic_counts(a['id'])} · "
              f"jobs {dict(db.job_counts(a['id'])) or '—'}")
    print(f"Open review items:  {len(db.list_review_items(limit=100000))}")
    return 0


# ───────────────────────────── main ────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="main.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("classify")
    sub.add_parser("dry-run")
    p = sub.add_parser("review")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("approve")
    p.add_argument("id", type=int)
    p.add_argument("--action", required=True,
                   choices=["accept", "ignore", "quarantine", "rename", "mark-dup"])
    p.add_argument("--name", default=None)
    p = sub.add_parser("sync")
    p.add_argument("--archive", required=True)
    p.add_argument("--limit", type=int, default=None,
                   help="pilot: only the first N groups")
    p = sub.add_parser("sync-all")
    p.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("resume")
    p.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("sync-seq",
                       help="sync dumps ONE AT A TIME (1→2→3) in a single process; "
                            "shared pacing + resolve cache; fewer Telegram rate limits")
    p.add_argument("--limit", type=int, default=None)
    sub.add_parser("rebuild-index")
    sub.add_parser("status")
    sub.add_parser("auth")
    sub.add_parser("discover-chats")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)
    db = DB(os.environ.get("DB_PATH", "./data/archive.db"))
    archives = archives_from_env()

    try:
        if args.cmd == "scan":
            return asyncio.run(cmd_scan(cfg, db))
        if args.cmd == "classify":
            return cmd_classify(cfg, db, archives)
        if args.cmd == "dry-run":
            return cmd_dry_run(cfg, db, archives)
        if args.cmd == "review":
            return cmd_review(cfg, db, args.limit)
        if args.cmd == "approve":
            return cmd_approve(cfg, db, args.id, args.action, args.name)
        if args.cmd == "sync":
            return asyncio.run(_sync_archives(cfg, db, archives,
                                              only=args.archive, limit=args.limit))
        if args.cmd in ("sync-all", "resume"):
            return asyncio.run(_sync_archives(cfg, db, archives,
                                              only=None, limit=args.limit))
        if args.cmd == "sync-seq":
            return asyncio.run(_sync_archives(cfg, db, archives,
                                              only=None, limit=args.limit,
                                              sequential=True))
        if args.cmd == "rebuild-index":
            return cmd_rebuild_index(cfg, db, archives)
        if args.cmd == "status":
            return cmd_status(cfg, db, archives)
        if args.cmd == "auth":
            return asyncio.run(cmd_auth())
        if args.cmd == "discover-chats":
            return asyncio.run(cmd_discover_chats())
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
