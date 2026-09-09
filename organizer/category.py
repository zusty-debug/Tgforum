"""Category classification (ULP / Books / Courses / Software / Research /
Databases / Miscellaneous) + domain/service detection for single files.

Metadata-based only; never claims facts the metadata doesn't support.
"""
from __future__ import annotations

import re

from .models import DomainResult
from .normalize import fold

ULP_DEFAULT_KEYWORDS = ["ulp"]

_COURSE_WORDS = {
    "udemy", "coursera", "edx", "course", "courses", "lecture", "lectures",
    "training", "bootcamp", "certification", "tutorial", "study", "academy",
}
_BOOK_WORDS = {
    "book", "books", "ebook", "epub", "guide", "manual", "handbook",
    "novel", "ebook",
}
_SOFTWARE_WORDS = {
    "setup", "installer", "portable", "crack", "cracked", "patch", "patches",
    "apk", "driver", "mod", "repack",
}
_RESEARCH_WORDS = {
    "research", "academic", "journal", "thesis", "dissertation", "scientific",
    "paper",
}
_DB_WORDS = {
    "database", "databases", "db", "dataset", "datasets", "dump", "dumps",
    "sql", "sqlite", "csv", "excel", "xlsx", "sqlite3", "parquet",
}

_DOMAIN_LIKE = re.compile(r"[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+")
_WORD = re.compile(r"[a-z0-9]+")


def is_ulp(filename: str, keywords: list[str]) -> bool:
    toks = set(_WORD.findall(fold(filename)))
    keys = {fold(k) for k in keywords if len(fold(k)) >= 3}
    # prefix match so "ULPS"/"ulp-update" count
    return any(t == k or t.startswith(k) for t in toks for k in keys)


def classify(filename: str, is_multipart: bool) -> tuple[str, list[str]]:
    """Return (category, reasons). ULP is checked separately by callers."""
    f = fold(filename)
    toks = set(_WORD.findall(f))

    def has(words: set[str]) -> list[str]:
        return sorted(toks & words)

    for words, label in (
        (_COURSE_WORDS, "Courses / Education"),
        (_BOOK_WORDS, "Books"),
        (_SOFTWARE_WORDS, "Software"),
        (_RESEARCH_WORDS, "Research / Academic"),
        (_DB_WORDS, "Databases / Datasets"),
    ):
        matched = has(words)
        if matched:
            return label, [f"keywords: {', '.join(matched)}"]
    if is_multipart:
        return "Databases / Datasets", ["multi-part archive set"]
    return "Miscellaneous", ["no category keywords"]


def detect_domain(filename: str, known_services: list[str]) -> DomainResult:
    """Detect a domain/service a single file is named after.

    Priority: dotted domain token (brand if known, else full token) >
    first significant token in the known-services list.
    """
    f = fold(filename)
    services = {fold(s) for s in known_services}

    # 1) dotted domain-like tokens: "eda.yandex.ru", "api.google.com"
    for d in _DOMAIN_LIKE.findall(f):
        labels = d.split(".")
        if any(len(l) == 1 for l in labels):
            continue
        if labels[0] in services:
            return DomainResult(labels[0], 0.85, f"service '{labels[0]}' in domain '{d}'")
        if len(labels) >= 2 and len(labels[-1]) in (2, 3):
            return DomainResult(d, 0.70, f"domain token '{d}'")

    # 2) known service as a word in the filename
    toks = set(_WORD.findall(f))
    hit = toks & services
    if hit:
        return DomainResult(sorted(hit)[0], 0.80, "known service name in filename")

    return DomainResult(None, 0.0, "")
