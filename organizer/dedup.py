"""Duplicate detection — two levels, metadata only (no downloads).

Level 1 (exact):   same Telegram file_id, or same (base, part-slot, size).
                   → marked duplicate; later copies are skipped in archives.
Level 2 (logical): nearly identical names + identical sizes.
                   → NEVER auto-merged; flagged in the review queue.

Source files are never deleted — we only avoid copying duplicates into
the destination archives.
"""
from __future__ import annotations

import rapidfuzz.fuzz as fuzz

from .models import SourceFile


class DuplicateDetector:
    def __init__(self, name_similarity: float = 0.90):
        self.name_similarity = name_similarity

    def detect(self, files: list[SourceFile]):
        """Returns (dup_of, level2_candidates).

        dup_of:  message_id -> (canonical_message_id, confidence, type)
        level2_candidates: list of (a_id, b_id, name_sim, note)
        """
        dup_of: dict[int, tuple[int, float, str]] = {}

        # Level 1a — same Telegram file id (authoritative)
        by_fid: dict[str, list[SourceFile]] = {}
        for f in files:
            if f.file_id:
                by_fid.setdefault(f.file_id, []).append(f)
        for lst in by_fid.values():
            if len(lst) > 1:
                lst.sort(key=lambda f: f.message_id)
                canon = lst[0]
                for f in lst[1:]:
                    dup_of[f.message_id] = (canon.message_id, 1.0, "file_id")

        # Level 1b — same normalized base + same part slot + same size
        by_nsp: dict[tuple, list[SourceFile]] = {}
        for f in files:
            if len(f.base_compare) >= 2 and f.size:
                slot = f.part.part_label if f.part.has_part() else "base"
                by_nsp.setdefault((f.base_compare, slot, f.size), []).append(f)
        for lst in by_nsp.values():
            if len(lst) > 1:
                lst.sort(key=lambda f: f.message_id)
                canon = lst[0]
                for f in lst[1:]:
                    prev = dup_of.get(f.message_id)
                    if prev is None or prev[1] < 0.95:
                        dup_of[f.message_id] = (canon.message_id, 0.95, "name+part+size")

        # Level 2 — identical size + very similar name → review candidates only
        candidates: list[tuple[int, int, float, str]] = []
        by_size: dict[int, list[SourceFile]] = {}
        for f in files:
            if f.size and f.size > 0:
                by_size.setdefault(f.size, []).append(f)
        import logging
        _log = logging.getLogger("organizer.grouping")
        for lst in by_size.values():
            if len(lst) < 2 or len(lst) > 400:
                continue
            pairs = len(lst) * (len(lst) - 1) // 2
            if pairs > 30_000:
                _log.info("dedup: size bucket %d has %d files (%d pairs) — capping",
                          lst[0].size, len(lst), pairs)
                lst = lst[:250]
            for ai in range(len(lst)):
                a = lst[ai]
                for b in lst[ai + 1:]:
                    if not a.base_compare or a.base_compare == b.base_compare:
                        continue  # empty or identical base handled by level 1
                    sim = fuzz.WRatio(a.base_compare, b.base_compare) / 100.0
                    if sim >= self.name_similarity:
                        candidates.append((
                            min(a.message_id, b.message_id),
                            max(a.message_id, b.message_id),
                            round(sim, 3),
                            f"'{a.filename}' vs '{b.filename}'",
                        ))
        return dup_of, candidates
