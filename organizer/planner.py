"""Destination planning — maps logical groups to forum topics.

Rule priority:
  0. IGNORED (admin)   → nowhere
  1. QUARANTINED       → quarantine topic (or skipped, per config)
  2. REVIEW            → admin review flow (Saved Messages per config)
  3. ULP material      → single "ULPs" topic (files only, no summary)
  4. multi-file set    → dedicated topic named from the FIRST filename
  5. single file       → country topic (if confident) → domain/service → Miscellaneous

Also applies: duplicate skipping, the dataset-topic cap with numbered
"Extra Datasets N" overflow, and the summary text posted (and pinned)
after each dataset completes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .category import classify, detect_domain, is_ulp
from .country import detect, flag
from .models import LogicalGroup
from .quarantine import check as quarantine_check

ICON_COLORS = [0xFF681D, 0xA56EFF, 0xFF8A3D, 0x68B7FF, 0x34C759,
               0xFFB800, 0xFF375F, 0x00C7BE, 0x72D5FF]
_UNRESOLVABLE = re.compile(r"part[\s_-]*\d*")


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def topic_icon(title: str) -> int:
    return ICON_COLORS[sum(map(ord, title or "")) % len(ICON_COLORS)]


@dataclass
class TopicPlan:
    title: str
    kind: str                      # dataset|country|domain|ulp|misc|quarantine|review|overflow
    icon_color: int
    file_count: int = 0
    total_size: int = 0
    group_ids: list = field(default_factory=list)


@dataclass
class GroupPlan:
    group_id: int
    topic_title: str | None = None
    topic_kind: str | None = None
    summary: str | None = None     # final message text (posted + pinned)
    copy_ids: list = field(default_factory=list)
    skipped_ids: list = field(default_factory=list)
    to_saved: bool = False
    skipped_reason: str | None = None


class Planner:
    def __init__(self, cfg: dict, archives: list[dict] | None = None):
        self.cfg = cfg or {}
        self.archives = archives or []
        org = self.cfg.get("organization", {})
        self.ulp_name = org.get("ulp_topic_name", "ULPs")
        self.ulp_kw = org.get("ulp_keywords", ["ulp"])
        self.misc = org.get("miscellaneous_topic", "Miscellaneous")
        self.quar_name = org.get("quarantine_topic_name", "Quarantine")
        self.review_name = org.get("review_topic_name", "Review")
        self.review_dest = org.get("review_destination", "saved")
        self.max_title = int(org.get("topic_title_max_chars", 120))
        self.max_topics = int(org.get("max_topics_per_forum", 0) or 0)
        self.overflow_capacity = int(org.get("overflow_topic_capacity", 50))
        self.summary_in_ulp = bool(org.get("summary_in_ulp_topic", False))
        self.country_conf = float(self.cfg.get("country", {}).get("topic_confidence", 0.75))
        self.services = org.get("known_services", [])
        self.quar_patterns = self.cfg.get("quarantine", {}).get("patterns", [])
        self.quar_mode = self.cfg.get("quarantine", {}).get("mode", "quarantine_topic")

    # ── enrichment (runs once at classify time, stored in DB) ──
    def enrich(self, groups: list[LogicalGroup], dup_of: dict):
        for g in groups:
            text = " ".join(m.filename for m in g.members)
            cap = next((m.caption for m in g.members if m.caption), None)
            g.country = detect(text + (" " + (cap or "")))
            cat, cat_reasons = classify(g.members[0].filename, g.is_multipart)
            g.category = cat
            g.reasons.extend(cat_reasons)
            for m in g.members:
                reason = quarantine_check(m.filename, m.caption, self.quar_patterns)
                if reason:
                    g.status = "QUARANTINED"
                    g.quarantine_reason = f"{m.filename}: {reason}"
                    break
            if self._is_ulp(g):
                g.category = "ULP"
            if g.status != "QUARANTINED" and g.norm:
                g.domain = detect_domain(g.norm, self.services)

    # ── planning ────────────────────────────────────────────────
    def plan(self, groups: list[LogicalGroup], group_ids: dict, dup_of: dict) -> dict:
        topics: dict[str, TopicPlan] = {}
        plans: dict[int, GroupPlan] = {}
        saved: list[int] = []
        by_gid: dict[int, LogicalGroup] = {}

        ok_datasets = [g for g in groups if g.status == "OK" and g.is_multipart]
        overflow_ids = set()
        if self.max_topics and len(ok_datasets) > self.max_topics:
            ranked = sorted(ok_datasets,
                            key=lambda g: (-g.total_size, g.members[0].message_id))
            overflow_ids = {id(g) for g in ranked[self.max_topics:]}
        overflow_buckets: list[list[LogicalGroup]] = []
        for g in ok_datasets:
            if id(g) not in overflow_ids:
                continue
            bucket = next((b for b in overflow_buckets
                           if len(b) < self.overflow_capacity), None)
            if bucket is None:
                bucket = []
                overflow_buckets.append(bucket)
            bucket.append(g)

        for g in groups:
            gid = group_ids[id(g)]
            by_gid[gid] = g
            plan = GroupPlan(group_id=gid)
            plan.copy_ids = [m.message_id for m in g.members if m.message_id not in dup_of]
            plan.skipped_ids = [m.message_id for m in g.members if m.message_id in dup_of]

            if g.status == "IGNORED":
                plan.skipped_reason = "ignored by admin"
            elif g.status == "QUARANTINED":
                if self.quar_mode == "quarantine_topic":
                    self._add_topic(topics, self.quar_name, "quarantine", gid)
                    plan.topic_title, plan.topic_kind = self.quar_name, "quarantine"
                else:
                    plan.skipped_reason = "quarantined (skip mode)"
            elif g.status == "REVIEW" or self._unresolvable(g):
                if self._unresolvable(g) and not g.review_reason:
                    plan.skipped_reason = None
                if self.review_dest == "topic":
                    self._add_topic(topics, self.review_name, "review", gid)
                    plan.topic_title, plan.topic_kind = self.review_name, "review"
                elif self.review_dest == "saved":
                    plan.to_saved = True
                    saved.extend(plan.copy_ids)
            elif self._is_ulp(g):
                self._add_topic(topics, self.ulp_name, "ulp", gid)
                plan.topic_title, plan.topic_kind = self.ulp_name, "ulp"
                plan.summary = self._summary_text(g) if self.summary_in_ulp else None
            elif g.is_multipart:
                if id(g) in overflow_ids:
                    plan.topic_kind = "overflow"  # title assigned after bucketing
                else:
                    title = self._collision_free(topics, self._safe_title(g.name), "dataset")
                    self._add_topic(topics, title, "dataset", gid)
                    plan.topic_title, plan.topic_kind = title, "dataset"
                    plan.summary = self._summary_text(g)
            else:
                title, kind = self._single_file_title(g)
                self._add_topic(topics, title, kind, gid)
                plan.topic_title, plan.topic_kind = title, kind

            plans[gid] = plan

        for n, bucket in enumerate(overflow_buckets, 1):
            title = f"Extra Datasets {n}"
            self._add_topic(topics, title, "overflow", None)
            for g in bucket:
                plans[group_ids[id(g)]].topic_title = title
                plans[group_ids[id(g)]].summary = self._summary_text(g)

        for gid, p in plans.items():
            t = topics.get(p.topic_title or "")
            if t is None:
                continue
            t.file_count += len(p.copy_ids)
            t.total_size += sum(m.size or 0 for m in by_gid[gid].members
                                if m.message_id not in dup_of)
        return {"topics": topics, "groups": plans, "saved": saved}

    # ── helpers ─────────────────────────────────────────────────
    def _is_ulp(self, g: LogicalGroup) -> bool:
        return any(is_ulp(m.filename, self.ulp_kw) for m in g.members)

    @staticmethod
    def _unresolvable(g: LogicalGroup) -> bool:
        if g.is_multipart or g.status != "OK":
            return False
        first = g.members[0]
        base = (first.base_compare or "").strip()
        if not base:
            return True
        return bool(_UNRESOLVABLE.fullmatch(base))

    def _single_file_title(self, g):
        c = g.country
        if c and c.confidence >= self.country_conf:
            return c.name, "country"
        # domain is re-derivable from the normalized name (it is not stored
        # in the DB), so recompute when the in-memory value is missing
        g.domain = g.domain or detect_domain(g.norm, self.services)
        if g.domain and g.domain.name and g.domain.confidence >= 0.6:
            dname = g.domain.name
            if "." not in dname:  # title-case plain service words, keep TLDs
                dname = dname.title()
            return self._safe_title(dname, 60), "domain"
        return self.misc, "misc"

    def _safe_title(self, title: str, maxlen: int | None = None) -> str:
        t = re.sub(r"[\x00-\x1f\x7f]", " ", title or "").strip()
        t = re.sub(r"\s+", " ", t)
        t = t[: maxlen or self.max_title].strip()
        return t or "Untitled"

    @staticmethod
    def _collision_free(topics, title, kind):
        if title not in topics:
            return title
        if topics[title].kind == kind:
            return title
        base, i = title, 2
        while f"{base} ({i})" in topics:
            i += 1
        return f"{base} ({i})"

    @staticmethod
    def _add_topic(topics, title, kind, gid):
        t = topics.setdefault(title, TopicPlan(title, kind, topic_icon(title)))
        if gid is not None and gid not in t.group_ids:
            t.group_ids.append(gid)

    def _summary_text(self, g: LogicalGroup) -> str:
        c = g.country
        if c and c.confidence >= self.country_conf:
            head = f"{flag(c.code)} {c.name.upper()}"
        else:
            head = "🌐 COUNTRY: UNKNOWN"
        parts = [head, f"📁 Database: {g.name}"]
        if g.part_numbers:
            lo, hi = g.part_numbers[0], g.part_numbers[-1]
            rng = f"{lo}–{hi}"
            if g.partial_start:
                rng += f" (parts 1–{lo - 1} not in source)"
            elif g.missing_parts:
                rng += f" (missing: {', '.join(map(str, g.missing_parts))})"
            parts.append(f"📦 Parts: {rng}")
        parts.append(f"📄 Total files: {len(g.members)}")
        parts.append(f"💾 Total size: {human_size(g.total_size)}")
        return "\n".join(parts)
