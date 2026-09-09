"""Archive part-family parser.

Turns raw filenames into (base, family, part_number, container, ...) so the
grouping engine can reason about split archives in MANY conventions:

    Database.part01.rar      Database.part2.rar
    Database.zip.001         Database.zip.007
    Database.z01  Database.z02  ...  Database.zip
    Database.7z.019          Database.7z.020
    Dataset.001  Dataset.002  Dataset.004  Dataset.005
    Movie.part-01.rar        movie vol2.iso
"""
from __future__ import annotations

import re

from .models import PartInfo

# Anything that can end a filename as its "container" extension.
CONTAINERS = {
    "rar", "zip", "7z", "tar", "gz", "bz2", "xz", "zst", "lz4", "iso", "cab",
    "arj", "lzh", "ace", "cbz", "uue", "sfx", "tgz", "tbz2",
    "pdf", "epub", "csv", "xlsx", "xls", "doc", "docx", "txt", "md",
    "sqlite", "db", "sql", "bak", "dat", "bin",
    "mov", "mp4", "mkv", "avi", "webm", "mp3", "flac", "wav",
    "apk", "jar", "war", "whl", "jpg", "jpeg", "png", "gif", "webp",
    "nzb", "torrent",
}

ARCHIVE_CONTAINERS = {
    "rar", "zip", "7z", "tar", "gz", "bz2", "xz", "zst", "lz4", "iso",
    "cab", "arj", "lzh", "ace", "cbz", "uue", "sfx", "tgz", "tbz2",
}

# Trailing dot-token part patterns, checked from the tail inward.
_DOT_PART_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("part", re.compile(r"^part[-_ ]?(\d{1,4})$", re.I), 0.95),
    ("z",    re.compile(r"^z(\d{2,3})$", re.I), 0.98),     # WinRar .z01..z99
    ("r",    re.compile(r"^r(\d{2,3})$", re.I), 0.98),     # WinRar .r00..r99
    ("nn",   re.compile(r"^(\d{3})$"), 0.90),              # .001 .007 .019
    ("nn2",  re.compile(r"^(\d{2})$"), 0.85),              # .01  .19
    ("vol",  re.compile(r"^vol[-_ ]?(\d{1,4})$", re.I), 0.85),
]

# "part 01" / "part1" / "part-01" embedded in the (hyphen/space) stem.
_INLINE_PART = re.compile(
    r"(?:(?<=^)|(?<=[\s_\-./]))part[-_ ]?(\d{1,4})(?=[\s_\-./]|$)", re.I)


def _is_numeric_token(tok: str) -> bool:
    return tok.isdigit()


def parse_filename(filename: str) -> PartInfo:
    """Parse a filename into base + part structure. Never raises."""
    name = (filename or "").strip()
    if not name:
        return PartInfo("", None, None, None, None, False, None, 0.0)

    tokens = [t for t in name.split(".") if t != ""]
    container: str | None = None
    if tokens and tokens[-1].lower() in CONTAINERS:
        container = tokens[-1].lower()
        tokens = tokens[:-1]

    if not tokens:
        return PartInfo("", None, None, None, container,
                        container in ARCHIVE_CONTAINERS, None, 0.0)

    # 1) dot-token part at the tail:  base[.container].007  /  base.z01
    for i in (len(tokens) - 1, len(tokens) - 2):
        if i < 0:
            break
        tok = tokens[i]
        low = tok.lower()
        for fam, rx, conf in _DOT_PART_PATTERNS:
            m = rx.match(low)
            if not m:
                continue
            num = int(m.group(1))
            base_tokens = tokens[:i]
            # guard: date-like chains ("2024.01.02", "file.1985.zip")
            if fam in ("nn", "nn2") and i > 0 and _is_numeric_token(tokens[i - 1]):
                break
            base_str = " ".join(tokens[:i])
            if fam in ("nn", "nn2") and re.search(r"\d$", base_str):
                break
            if fam == "nn2" and base_tokens and all(_is_numeric_token(t) for t in base_tokens):
                break
            embedded: str | None = None
            if base_tokens and base_tokens[-1].lower() in CONTAINERS:
                embedded = base_tokens[-1].lower()
                base_tokens = base_tokens[:-1]
            base = ".".join(base_tokens)
            return PartInfo(
                base=base,
                family=fam,
                part_number=num,
                part_width=len(m.group(1)),
                container=container or embedded,
                is_final_container=False,  # has a part number → not the final file
                part_label=low,
                confidence=conf,
            )
        # fall through: this token was not a part

    # 2) inline part inside the stem:  "Yandex-EDA-Part-01-SQL.zip"
    base = ".".join(tokens)
    m = _INLINE_PART.search(base)
    if m:
        num = int(m.group(1))
        new_base = (base[: m.start()] + " " + base[m.end():]).strip(" ._-\t")
        new_base = re.sub(r"[\s_\-]{2,}", " ", new_base).strip()
        return PartInfo(
            base=new_base,
            family="part",
            part_number=num,
            part_width=len(m.group(1)),
            container=container,
            is_final_container=False,  # has a part number → not the final file
            part_label=f"part{num}",
            confidence=0.80,
        )

    return PartInfo(
        base=base,
        family=None,
        part_number=None,
        part_width=None,
        container=container,
        is_final_container=(container in ARCHIVE_CONTAINERS),
        part_label=None,
        confidence=0.0,
    )
