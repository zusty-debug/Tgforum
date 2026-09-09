"""Safety net: quarantine filter.

Files whose filename/caption strongly suggests breach/credential/PII
material are NEVER auto-redistributed. The operator's data is synthetic
and authorized, so this is a safety net, not an assumption of guilt —
but it is on by default and configurable.
"""
from __future__ import annotations

import re

from .normalize import fold

_CACHE: dict[tuple, re.Pattern] = {}


def _pattern_re(p: str) -> re.Pattern:
    key = (p, "re")
    if key in _CACHE:
        return _CACHE[key]
    if " " in p:
        rx = re.compile(re.escape(p), re.I)
    else:
        # left-anchored, suffix-tolerant: "breach" also catches "breached"
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(p) + r"[a-z0-9]*", re.I)
    _CACHE[key] = rx
    return rx


def check(filename: str, caption: str | None, patterns: list[str]) -> str | None:
    """Return a quarantine reason, or None if the file is clean."""
    text = fold(filename) + " " + fold(caption or "")
    for p in patterns:
        if _pattern_re(p).search(text):
            return f"matched quarantine pattern '{p}'"
    return None
