"""Dry-run report — the full picture BEFORE anything touches Telegram."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def build_report(source_chat, n_files, groups, dup_of, l2_candidates, plan, archives) -> dict:
    topics = plan["topics"]
    by_kind = {}
    for t in topics.values():
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1

    cats: dict[str, int] = {}
    for g in groups:
        cats[g.category] = cats.get(g.category, 0) + 1
    countries: dict[str, int] = {}
    for g in groups:
        if g.country and g.country.confidence >= 0.75:
            countries[g.country.name] = countries.get(g.country.name, 0) + 1

    ops = {}
    for a in archives:
        copies = sum(len(p.copy_ids) for p in plan["groups"].values())
        summaries = sum(1 for p in plan["groups"].values() if p.summary)
        ops[a["id"]] = {"topics": len(topics), "copies": copies, "summaries": summaries}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_chat_id": source_chat,
        "files_scanned": n_files,
        "logical_groups": len(groups),
        "multipart_groups": sum(1 for g in groups if g.is_multipart),
        "single_file_groups": sum(1 for g in groups if not g.is_multipart),
        "status": {
            "OK": sum(1 for g in groups if g.status == "OK"),
            "REVIEW": sum(1 for g in groups if g.status == "REVIEW"),
            "QUARANTINED": sum(1 for g in groups if g.status == "QUARANTINED"),
            "IGNORED": sum(1 for g in groups if g.status == "IGNORED"),
        },
        "duplicates": {
            "exact_skipped": len(dup_of),
            "candidates_for_review": len(l2_candidates),
        },
        "categories": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        "countries_detected": dict(sorted(countries.items(), key=lambda kv: -kv[1])),
        "ulp_files": sum(len(g.members) for g in groups if g.category == "ULP"),
        "topics": {
            "total": len(topics),
            "by_kind": by_kind,
            "dataset_topics": by_kind.get("dataset", 0),
            "overflow_topics": by_kind.get("overflow", 0),
        },
        "saved_for_review": len(plan.get("saved", [])),
        "estimated_operations_per_archive": ops,
        "top_topics_by_size": sorted(
            ({"title": t.title, "kind": t.kind, "files": t.file_count, "size": t.total_size}
             for t in topics.values()),
            key=lambda x: -x["size"])[:25],
        "largest_groups": sorted(
            ({"name": g.name, "status": g.status, "confidence": g.confidence,
              "parts": g.part_numbers, "missing": g.missing_parts,
              "size": g.total_size, "category": g.category,
              "country": g.country.name if g.country else None}
             for g in groups),
            key=lambda x: -x["size"])[:15],
        "quarantined": [{"name": g.name, "reason": g.quarantine_reason}
                        for g in groups if g.status == "QUARANTINED"][:200],
        "review_queue": [{"name": g.name, "confidence": g.confidence,
                          "reason": g.review_reason, "parts": g.part_numbers,
                          "files": len(g.members)}
                         for g in groups if g.status == "REVIEW"][:500],
        "duplicate_candidates": l2_candidates[:500],
    }


def _fmt(n):
    return f"{n:,}"


def render_text(r: dict) -> str:
    L = []
    L.append("═" * 62)
    L.append(" DRY RUN REPORT — nothing will be posted from this")
    L.append("═" * 62)
    L.append(f"Generated: {r['generated_at']}")
    L.append(f"Source:    chat {r['source_chat_id']}  ·  {_fmt(r['files_scanned'])} files scanned")
    L.append("")
    L.append(f"Logical groups: {_fmt(r['logical_groups'])}   "
             f"(multi-part {_fmt(r['multipart_groups'])} · single-file {_fmt(r['single_file_groups'])})")
    s = r["status"]
    L.append(f"Status:       OK {_fmt(s['OK'])} · REVIEW {_fmt(s['REVIEW'])} · "
             f"QUARANTINED {_fmt(s['QUARANTINED'])} · IGNORED {_fmt(s['IGNORED'])}")
    d = r["duplicates"]
    L.append(f"Duplicates:   {_fmt(d['exact_skipped'])} exact (will be skipped) · "
             f"{_fmt(d['candidates_for_review'])} candidates for review")
    L.append(f"ULP files:    {_fmt(r['ulp_files'])} → one 'ULPs' topic")
    L.append(f"Saved for review (admin approval): {_fmt(r['saved_for_review'])} files")
    L.append("")
    L.append("Categories:")
    for k, v in r["categories"].items():
        L.append(f"  {k:<24} {_fmt(v)}")
    L.append("")
    c = r["countries_detected"]
    L.append(f"Confidently detected countries ({len(c)}):")
    for k, v in list(c.items())[:15]:
        L.append(f"  {k:<24} {_fmt(v)}")
    if len(c) > 15:
        L.append(f"  … and {len(c) - 15} more")
    L.append("")
    t = r["topics"]
    L.append(f"Topics to create per archive: {t['total']}  "
             f"(dataset {t['dataset_topics']} · overflow {t['overflow_topics']} · "
             f"other {t['total'] - t['dataset_topics'] - t['overflow_topics']})")
    for aid, op in r["estimated_operations_per_archive"].items():
        L.append(f"  {aid}: topics {_fmt(op['topics'])} · copies {_fmt(op['copies'])} · "
                 f"summaries {_fmt(op['summaries'])}")
    L.append("")
    L.append("Top topics by size:")
    for i, x in enumerate(r["top_topics_by_size"], 1):
        L.append(f"  {i:>2}. {x['title']}  [{x['kind']}]  {x['files']} files, {_human(x['size'])}")
    L.append("")
    if r["quarantined"]:
        L.append(f"QUARANTINED ({len(r['quarantined'])} shown):")
        for q in r["quarantined"][:20]:
            L.append(f"  ✋ {q['name']} — {q['reason']}")
    if r["review_queue"]:
        L.append(f"REVIEW QUEUE ({len(r['review_queue'])} shown):")
        for q in r["review_queue"][:20]:
            L.append(f"  🔎 {q['name']}  conf={q['confidence']:.2f}  files={q['files']}  — {q['reason']}")
    L.append("═" * 62)
    return "\n".join(L)


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def save_report(report: dict, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
