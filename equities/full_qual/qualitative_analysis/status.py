"""Shared evidence and coverage statuses.

The qualitative pipeline has several layers that can each be incomplete.  A
single boolean cannot distinguish an unavailable source from a parser failure
or an explicit company non-disclosure, so all new records use this vocabulary.
The values are strings on purpose: they remain stable in JSONL artifacts and
can be consumed by older clients without importing this module.
"""

from __future__ import annotations

from enum import Enum


class SourceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"
    INFERRED = "INFERRED"
    NOT_DISCLOSED = "NOT_DISCLOSED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    STALE = "STALE"
    PENDING = "PENDING"
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


STATUS_VALUES = frozenset(item.value for item in SourceStatus)

# These statuses describe coverage, rather than a business result.  They must
# never be mapped to a negative score by downstream components.
COVERAGE_NEUTRAL_STATUSES = frozenset(
    {
        SourceStatus.NOT_DISCLOSED.value,
        SourceStatus.NOT_APPLICABLE.value,
        SourceStatus.SOURCE_UNAVAILABLE.value,
        SourceStatus.EXTRACTION_FAILED.value,
        SourceStatus.PENDING.value,
        SourceStatus.STALE.value,
    }
)


def normalize_status(value: str | SourceStatus | None, default: str = SourceStatus.PENDING.value) -> str:
    """Return a canonical status or ``default`` for missing/unknown values."""

    text = str(value or "").strip().upper()
    return text if text in STATUS_VALUES else default


def is_coverage_neutral(value: str | SourceStatus | None) -> bool:
    return normalize_status(value) in COVERAGE_NEUTRAL_STATUSES


def is_releasable(value: str | SourceStatus | None) -> bool:
    return normalize_status(value) == SourceStatus.VERIFIED.value


__all__ = [
    "SourceStatus",
    "STATUS_VALUES",
    "COVERAGE_NEUTRAL_STATUSES",
    "normalize_status",
    "is_coverage_neutral",
    "is_releasable",
]
