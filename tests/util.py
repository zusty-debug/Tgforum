"""Shared test helpers."""
from __future__ import annotations

import copy

from organizer.models import SourceFile
from organizer.normalize import compare_base, display_name, significant_tokens
from organizer.partparser import parse_filename

T0 = 1_700_000_000


def mk(mid, name, size=1_000_000_000, date=None, caption=None,
       group_id=None, fid=None) -> SourceFile:
    f = SourceFile(
        message_id=mid, chat_id=-1001, file_id=fid or f"fid-{mid}",
        filename=name, size=size, date=date if date is not None else T0,
        mime="application/octet-stream", caption=caption,
        media_group_id=group_id)
    f.part = parse_filename(name)
    f.base_compare = compare_base(f.part)
    f.tokens = significant_tokens(f.base_compare)
    f.display_name = display_name(name, f.part)
    return f


BASE_CFG = {
    "classification": {"auto_group_threshold": 0.90, "review_threshold": 0.65},
    "country": {"topic_confidence": 0.75},
    "organization": {
        "review_destination": "saved",
        "known_services": ["yandex", "udemy", "facebook"],
        "max_topics_per_forum": 6000,
        "overflow_topic_capacity": 50,
    },
    "quarantine": {
        "mode": "quarantine_topic",
        "patterns": ["credential", "password", "breach", "leak", "pii", "dox"],
    },
}


def cfg() -> dict:
    return copy.deepcopy(BASE_CFG)
