"""Core data structures shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PartInfo:
    """Result of parsing a filename's archive-part structure."""
    base: str                      # filename minus part token(s) and extension(s)
    family: str | None             # part | z | r | nn | nn2 | vol | None
    part_number: int | None
    part_width: int | None
    container: str | None          # zip, rar, 7z, ... (may be embedded: zip.007)
    is_final_container: bool       # ends with a plain container (e.g. "x.zip")
    part_label: str | None         # raw token as seen ("z01", "007", "part1")
    confidence: float              # 0..1 confidence of the part parse

    def has_part(self) -> bool:
        return self.part_number is not None


@dataclass
class SourceFile:
    """One file-bearing message from the source channel, enriched."""
    message_id: int
    chat_id: int
    file_id: str | None
    filename: str
    size: int | None
    date: int | None
    mime: str | None
    caption: str | None
    media_group_id: str | None
    part: PartInfo = field(default_factory=lambda: PartInfo(
        "", None, None, None, None, False, None, 0.0))
    base_compare: str = ""         # normalized comparison base
    tokens: list[str] = field(default_factory=list)
    display_name: str = ""         # human-readable name (first-filename rule)


@dataclass
class CountryResult:
    code: str | None
    name: str | None
    confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class DomainResult:
    name: str | None
    confidence: float
    reason: str = ""


@dataclass
class LogicalGroup:
    """A logical dataset: one or more physical files."""
    name: str
    norm: str
    members: list[SourceFile]
    is_multipart: bool
    part_numbers: list[int] = field(default_factory=list)
    missing_parts: list[int] = field(default_factory=list)
    partial_start: bool = False        # parts present but set starts mid-range
    total_size: int = 0
    confidence: float = 1.0
    reasons: list[str] = field(default_factory=list)
    status: str = "OK"                 # OK | REVIEW | QUARANTINED
    review_reason: str | None = None
    quarantine_reason: str | None = None
    country: CountryResult | None = None
    domain: DomainResult | None = None
    category: str = "Miscellaneous"

    @property
    def member_ids(self) -> list[int]:
        return [m.message_id for m in self.members]
