"""Logical grouping engine — the heart of the system.

Clusters source files into logical datasets from multiple independent
signals with a weighted, explainable score:

  * filename / normalized-base similarity
  * part numbering + archive-family relationships
  * file size · timestamps · message neighborhood · media groups
  * captions

No single magic threshold: score >= auto merges automatically;
review..auto merges provisionally and flags the group for admin review;
below review the files stay separate. Every merge keeps its reasons so
any of the 90k decisions can be audited (logs/grouping.log).
"""
from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass

import rapidfuzz.fuzz as fuzz

from .models import LogicalGroup, SourceFile
from .normalize import is_archive, prepare_file

log = logging.getLogger(__name__)

MIN_NAME_SIM = 0.45   # below this the names contradict -> never merge
SHORT_BASE_LEN = 2    # 1-char bases get reduced name weight
NB_MSGID_BONUS = 100  # message-id distance that still counts as "adjacent"
_BARE_PART = re.compile(r"part[\s_-]*\d*")


class UnionFind:
    __slots__ = ("p", "rank")

    def __init__(self, n: int):
        self.p = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True


@dataclass
class _Link:
    i: int
    j: int
    score: float
    reasons: list
    duplicate_slot: bool = False


class GroupingEngine:
    def __init__(self, cfg: dict | None = None):
        c = (cfg or {}).get("classification", {})
        self.auto = float(c.get("auto_group_threshold", 0.90))
        self.review = float(c.get("review_threshold", 0.65))
        self.max_df = int(c.get("max_token_doc_frequency", 300))
        self.nb_window = int(c.get("neighborhood_window_messages", 15))

    # ─────────────────────────── public ───────────────────────────
    def group(self, files: list[SourceFile]) -> list[LogicalGroup]:
        n = len(files)
        for f in files:
            prepare_file(f)

        uf = UnionFind(n)
        links: list[_Link] = []

        # 1) exact-base buckets — the strong majority of real splits
        buckets: dict[str, list[int]] = {}
        for idx, f in enumerate(files):
            if f.base_compare:
                buckets.setdefault(f.base_compare, []).append(idx)
        for idxs in buckets.values():
            self._link_bucket(files, idxs, uf, links)

        # 2) cross-bucket candidates via rare shared tokens
        tindex: dict[str, list[int]] = {}
        for idx, f in enumerate(files):
            for t in f.tokens:
                tindex.setdefault(t, []).append(idx)
        for t in [t for t, v in tindex.items() if len(v) > self.max_df]:
            tindex.pop(t, None)

        seen: set[tuple[int, int]] = set()
        scored = 0
        for idx, f in enumerate(files):
            if not f.tokens:
                continue
            cand: set[int] = set()
            for t in f.tokens:
                cand.update(tindex.get(t, ()))  # high-df tokens were pruned
            cand.discard(idx)
            for k in cand:
                if k <= idx:
                    continue
                other = files[k]
                if other.base_compare == f.base_compare:
                    continue
                # one shared token alone ("toyota" in 5,000 files) is too
                # generic — require corroboration before paying full scoring
                corroborated = (
                    len(set(f.tokens) & set(other.tokens)) >= 2
                    or (f.size and other.size
                        and max(f.size, other.size) <= min(f.size, other.size) * 1.05)
                    or (f.date and other.date and abs(f.date - other.date) <= 60)
                    or abs(f.message_id - other.message_id) <= NB_MSGID_BONUS
                    or (f.media_group_id and f.media_group_id == other.media_group_id)
                )
                if not corroborated:
                    continue
                seen.add((idx, k))
                self._maybe_link(files, idx, k, uf, links)
                scored += 1
                if scored > 2_000_000:  # hard backstop on pathological channels
                    log.warning("token-path scoring backstop reached (2M pairs) — stopping token path")
                    break
            if scored > 2_000_000:
                break
            if idx and idx % 20000 == 0:
                log.info("grouping: token path %d/%d files (pairs scored so far: %d)",
                         idx, n, scored)

        # 3) neighborhood rescue — files posted in the same batch
        order = sorted(range(n), key=lambda i: files[i].message_id)
        nb_scored = 0
        for pos, idx in enumerate(order):
            f = files[idx]
            lo, hi = max(0, pos - self.nb_window), min(n, pos + self.nb_window + 1)
            for pos2 in range(lo, hi):
                if pos2 == pos:
                    continue
                k = order[pos2]
                key = (idx, k) if idx < k else (k, idx)
                if key in seen:
                    continue
                seen.add(key)
                other = files[k]
                # identical NON-EMPTY bases were handled by exact buckets;
                # two unnamed (bare part) files still need scoring here
                if other.base_compare and other.base_compare == f.base_compare:
                    continue
                if not self._cheap_prefilter(f, other):
                    continue
                self._maybe_link(files, idx, k, uf, links)
                nb_scored += 1
                if nb_scored > 2_000_000:
                    log.warning("neighborhood scoring cap reached (2M) — skipping rest")
                    break
            if nb_scored > 2_000_000:
                break
            if pos and pos % 20000 == 0:
                log.info("grouping: neighborhood %d/%d (full scores: %d)",
                         pos, n, nb_scored)

        # 4) finalize components
        comps: dict[int, list[int]] = {}
        for idx in range(n):
            comps.setdefault(uf.find(idx), []).append(idx)
        # index links by their (final) component ONCE — doing this filter
        # inside _finalize would be O(groups × links) ≈ 10^11 ops at 90k scale
        links_by_comp: dict[int, list] = {}
        for l in links:
            if l.duplicate_slot:
                continue
            links_by_comp.setdefault(uf.find(l.i), []).append(l)
        groups = [self._finalize(files, idxs,
                                 links_by_comp.get(uf.find(idxs[0]), []))
                  for idxs in comps.values()]
        groups.sort(key=lambda g: g.members[0].message_id)
        return groups

    # ───────────────────────── internals ─────────────────────────
    def _maybe_link(self, files, i, j, uf, links):
        score, reasons, dup_slot = self._pair_score(files, i, j)
        if dup_slot or score >= self.review:
            links.append(_Link(i, j, score, reasons, dup_slot))
            uf.union(i, j)

    def _link_bucket(self, files, idxs, uf, links):
        if len(idxs) > 200:
            reduced, seen_slot, base_count = [], set(), 0
            for i in idxs:
                slot = files[i].part.part_number if files[i].part.has_part() else None
                if slot is None:
                    if base_count < 5:
                        reduced.append(i)
                        base_count += 1
                elif slot not in seen_slot:
                    seen_slot.add(slot)
                    reduced.append(i)
            idxs = reduced[:400]
        for ai in range(len(idxs)):
            a, fa = idxs[ai], files[idxs[ai]]
            for b in idxs[ai + 1:]:
                fb = files[b]
                dup_slot = (fa.part.has_part() and fb.part.has_part()
                            and fa.part.part_number == fb.part.part_number)
                if dup_slot:
                    links.append(_Link(a, b, 1.0,
                                       [f"same part slot ({fa.part.part_label} appears twice) — duplicate upload"],
                                       True))
                    uf.union(a, b)
                    continue
                score, reasons, _ = self._pair_score(files, a, b)
                if score >= self.review:
                    links.append(_Link(a, b, score, reasons, False))
                    uf.union(a, b)

    @staticmethod
    def _cheap_prefilter(a: SourceFile, b: SourceFile) -> bool:
        if a.media_group_id and a.media_group_id == b.media_group_id:
            return True
        if a.size and b.size and max(a.size, b.size) <= min(a.size, b.size) * 1.05:
            return True
        if set(a.tokens) & set(b.tokens):
            return True
        ft_a = a.tokens[0] if a.tokens else a.base_compare
        ft_b = b.tokens[0] if b.tokens else b.base_compare
        if ft_a and ft_b and len(ft_a) >= 4 and len(ft_b) >= 4:
            if fuzz.ratio(ft_a, ft_b) >= 60:
                return True
        return False

    def _pair_score(self, files, i, j):
        a, b = files[i], files[j]
        reasons: list[str] = []

        if a.base_compare and a.base_compare == b.base_compare:
            name = 1.0
        elif not a.base_compare and not b.base_compare:
            name = 1.0  # both unnamed — the part sequence decides
        else:
            ab, bb = a.base_compare, b.base_compare
            ts = fuzz.token_set_ratio(ab, bb) / 100.0
            wr = fuzz.WRatio(ab, bb) / 100.0
            pr = fuzz.partial_ratio(ab, bb) / 100.0
            # mixed conventions ("YAeda" vs "Yandex EDA SQL") need the
            # partial component to surface; the floor keeps garbage apart
            name = max(wr, 0.75 * ts, 0.85 * pr)
            if name < MIN_NAME_SIM:
                return 0.0, [f"contradictory names (similarity {name:.2f})"], False
        base_len = min(len(a.base_compare), len(b.base_compare))
        name_w = 0.45 if base_len >= SHORT_BASE_LEN else 0.35
        s = name * name_w
        # names at the contradiction floor must not be rescued by structure
        if name < 0.60:
            s -= (0.60 - name) * 0.6
            reasons.append(f"weak name similarity ({name:.2f}) — discount applied")

        pa, pb = a.part, b.part
        # same part slot = duplicate upload ONLY within the same dataset
        # (part 1 of A and part 1 of B are unrelated!)
        dup_slot = (pa.has_part() and pb.has_part()
                    and pa.part_number == pb.part_number
                    and a.base_compare == b.base_compare
                    and a.base_compare != "")
        if dup_slot:
            reasons.append(f"identical part slot ({pa.part_label} twice) — duplicate upload")
            return max(0.5, s + 0.1), reasons, True

        if pa.has_part() and pb.has_part():
            if self._families_compatible(pa, pb):
                gap = abs(pa.part_number - pb.part_number)
                if gap == 0:
                    # same number across different structures is a weak
                    # signal (part 1 of A vs part 1 of B) — not a sequence
                    s += 0.10
                    reasons.append(f"same part number ({pa.part_label} / {pb.part_label}) — weak signal")
                else:
                    s += 0.30 if pa.family == pb.family else 0.28
                    if gap == 1:
                        s += 0.02
                        reasons.append(f"consecutive parts ({pa.part_label} → {pb.part_label})")
                    else:
                        if gap <= 4:
                            s += 0.01
                        reasons.append(f"part sequence compatible ({pa.part_label} ↔ {pb.part_label}, gap {gap})")
            else:
                s -= 0.05
                reasons.append("incompatible archive families")
        elif pa.has_part() != pb.has_part():
            part, other = (pa, pb) if pa.has_part() else (pb, pa)
            if other.is_final_container and is_archive(other.container):
                if part.container and other.container and part.container == other.container:
                    s += 0.30
                    reasons.append("part + final container of same archive family")
                elif part.container is None and other.container in ("zip", "rar", "7z", "tar"):
                    s += 0.34
                    reasons.append(f"part + final .{other.container} (classic split pair)")
                elif part.container and other.container:
                    s += 0.15
                    reasons.append(f"part (.{part.container}) + final (.{other.container}) — possible re-pack")
                else:
                    s += 0.10
            elif part.container and other.container and part.container == other.container:
                s += 0.15
                reasons.append("matching container, one side numbered")
            else:
                s += 0.02

        if a.date and b.date:
            d = abs(a.date - b.date)
            if d <= 60:
                s += 0.08
                reasons.append("posted in the same minute")
            elif d <= 600:
                s += 0.05
            elif d <= 3600:
                s += 0.02
            elif d > 7 * 86400:
                s -= 0.03
                reasons.append("posted far apart in time")

        if a.media_group_id and a.media_group_id == b.media_group_id:
            s += 0.10
            reasons.append("same media group")
        elif abs(a.message_id - b.message_id) <= NB_MSGID_BONUS:
            s += 0.04
            reasons.append("adjacent source messages")

        if a.size and b.size and (pa.has_part() or pb.has_part()):
            if max(a.size, b.size) <= min(a.size, b.size) * 1.5:
                s += 0.03
                reasons.append("compatible part sizes")

        if a.caption and b.caption:
            cs = fuzz.token_set_ratio(a.caption, b.caption) / 100.0
            if cs > 0.5:
                s += 0.05 * cs
                reasons.append(f"similar captions ({cs:.0%})")

        return min(0.99, max(0.0, s)), reasons, False

    @staticmethod
    def _families_compatible(pa, pb) -> bool:
        if pa.family == pb.family:
            return True
        if pa.container and pb.container and pa.container == pb.container:
            return True
        part_families = {"part", "z", "r", "nn", "nn2", "vol"}
        if pa.family in part_families and pb.family in part_families:
            return pa.container == pb.container or pa.container is None or pb.container is None
        return False

    def _finalize(self, files, idxs, comp_links) -> LogicalGroup:
        members = sorted((files[i] for i in idxs), key=lambda m: m.message_id)
        # comp_links is pre-indexed by component (see group())
        real_links = comp_links
        min_score = min((l.score for l in real_links), default=1.0)

        nums = sorted({m.part.part_number for m in members if m.part.has_part()})
        is_multipart = len(members) > 1
        missing: list[int] = []
        partial_start = False
        if nums:
            lo, hi = nums[0], nums[-1]
            if lo not in (0, 1):
                partial_start = True
            else:
                missing = sorted(set(range(lo, hi + 1)) - set(nums))

        seq_bonus = 0.0
        reasons: list[str] = []
        if is_multipart and len(nums) >= 3:
            if partial_start:
                seq_bonus = 0.05
                reasons.append(f"{len(nums)} parts in a sequence starting mid-range ({nums[0]}–{nums[-1]})")
            elif len(missing) <= 2:
                seq_bonus = 0.08
                reasons.append(f"consistent {len(nums)}-part sequence (missing: {missing if missing else 'none'})")
        if real_links:
            weakest = min(real_links, key=lambda l: l.score)
            reasons.append(f"weakest link {weakest.score:.2f} — " + "; ".join(weakest.reasons[:3]))
        if not is_multipart:
            reasons.append("single file — no grouping needed")

        conf = round(min(0.99, max(0.0, min_score) + seq_bonus), 3)
        status, review_reason = "OK", None
        # judge by FINAL group confidence (weakest link + sequence bonus):
        # a consistent 12-part sequence with one 0.87 link is still automatic
        if real_links and conf < self.auto:
            status, review_reason = "REVIEW", (
                f"group confidence {conf:.2f} below auto threshold {self.auto:.2f}")
        first = members[0]
        if not is_multipart and (not first.base_compare.strip() or
                                 _BARE_PART.fullmatch((first.display_name or "").lower())):
            if status == "OK":
                status, review_reason = "REVIEW", "no identifiable name (bare part filename) — needs admin"
        if is_multipart and not any(m.base_compare.strip() for m in members):
            status = "REVIEW"
            review_reason = ((review_reason + " · ") if review_reason else "") + \
                "no identifiable name (bare part filenames) — needs admin"

        total = sum(m.size or 0 for m in members)
        return LogicalGroup(
            name=first.display_name or first.filename,
            norm=first.base_compare,
            members=members,
            is_multipart=is_multipart,
            part_numbers=nums,
            missing_parts=missing,
            partial_start=partial_start,
            total_size=total,
            confidence=conf,
            reasons=reasons,
            status=status,
            review_reason=review_reason,
        )
