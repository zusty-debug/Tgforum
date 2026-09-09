"""Filename normalization — comparison bases, tokens, display names.

The original filename is NEVER mutated; these produce parallel
representations used for grouping/dedup/display.
"""
from __future__ import annotations

import re
import unicodedata

from .models import PartInfo

# Technical noise words: dropped from the *comparison* representation and
# from the rare-token index (too generic to identify a dataset).
STOP_TOKENS = {
    "part", "parts", "vol", "volume", "set", "the", "and", "for", "with",
    "data", "database", "db", "dataset", "datasets", "dump", "dumps",
    "files", "file", "update", "updated", "backup", "copy", "copies",
    "final", "full", "complete", "completa", "all", "new", "latest",
    "version", "ver",
    "users", "user", "accounts", "account", "records", "record",
    "list", "lists", "customers", "customer", "orders", "order",
    "master", "export", "exports", "info", "stuff", "batch", "items",
    "notes", "misc",
}

_PAREN_NUM = re.compile(r"[\s_]*\(\d+\)\s*$")
_COPYWORD = re.compile(
    r"[\s_]*(?:copy|backup|original|final|old)[\s_]*\d*\s*$", re.I)
_SEP_RUN = re.compile(r"[\s_\-./()]+")


def fold(s: str) -> str:
    """Unicode-normalize + casefold for comparisons."""
    return unicodedata.normalize("NFKC", s or "").casefold()


def compare_base(part: PartInfo) -> str:
    """Normalized comparison base: separators unified, re-upload noise gone.

    'Facebook_2024_827Million.z01' -> 'facebook 2024 827million'
    'Database Copy.zip'            -> 'database'
    """
    s = fold(part.base)
    for _ in range(3):  # peel repeated noise BEFORE unifying separators,
        s2 = _PAREN_NUM.sub(" ", s)  # so " (1)" / "_copy" are still visible
        s2 = _COPYWORD.sub(" ", s2)
        s2 = re.sub(r"\s+", " ", s2).strip()
        if s2 == s:
            break
        s = s2
    s = _SEP_RUN.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def significant_tokens(compare_str: str) -> list[str]:
    """Tokens worth indexing for candidate discovery (rare, informative)."""
    out = []
    for t in (compare_str or "").split():
        if len(t) < 3:
            continue
        if t in STOP_TOKENS:
            continue
        if t.isdigit():  # bare years/numbers are weak identity signals
            continue
        out.append(t)
    return out


def display_name(filename: str, part: PartInfo) -> str:
    """Human-readable name derived from ONE filename (first-file rule).

    Strips part suffixes + extensions, unifies separators. Never destroys
    meaningful words.
    """
    s = part.base or re.sub(r"\.[A-Za-z0-9]{1,8}$", "", filename or "")
    s = re.sub(r"[\s_\-]+", " ", s).strip()
    for _ in range(3):
        s = _PAREN_NUM.sub(" ", s)
        s = _COPYWORD.sub(" ", s)
        s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = re.sub(r"[\s_\-]+", " ", re.sub(r"\.[A-Za-z0-9]{1,8}$", "", filename or "")).strip()
    return s[:120]


def is_archive(container: str | None) -> bool:
    from .partparser import ARCHIVE_CONTAINERS
    return container in ARCHIVE_CONTAINERS


def prepare_file(f) -> "SourceFile":
    """Populate derived comparison fields from the parsed part (idempotent).

    Every code path that uses a SourceFile's base_compare/tokens/display_name
    must call this first (load_files, load_groups_full, the engine, tests).
    """
    f.base_compare = compare_base(f.part)
    f.tokens = significant_tokens(f.base_compare)
    f.display_name = display_name(f.filename, f.part)
    return f
