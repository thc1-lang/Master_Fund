"""Reusable point-in-time gates for every evidence-bearing component."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Any, Iterable, Mapping


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class InformationDates:
    event_date: str | None = None
    effective_date: str | None = None
    period_end_date: str | None = None
    publication_date: str | None = None
    filing_date: str | None = None
    public_availability_date: str | None = None
    acquisition_date: str | None = None
    analysis_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def public_availability_date(record: Mapping[str, Any] | InformationDates) -> date | None:
    """Resolve the date on which information became public.

    Period end and event dates describe the subject of a document; they do not
    make the document public and are deliberately excluded from this function.
    """

    if isinstance(record, InformationDates):
        values = record.to_dict()
    else:
        values = record
    for key in ("public_availability_date", "available_date", "acceptance_datetime", "filing_date", "publication_date", "source_date", "issued_date", "effective_public_date"):
        value = parse_date(values.get(key))
        if value:
            return value
    # Derived records commonly retain their source envelope rather than
    # copying every SEC/IR date to the top level.  Resolve only explicit
    # source dates; never use acquisition or event dates as a proxy for public
    # availability.
    for key in ("source", "source_record", "source_metadata", "provenance", "metadata"):
        nested = values.get(key)
        if isinstance(nested, Mapping):
            resolved = public_availability_date(nested)
            if resolved:
                return resolved
    return None


def point_in_time_status(record: Mapping[str, Any] | InformationDates, cutoff: str | date) -> str:
    cutoff_date = cutoff if isinstance(cutoff, date) else parse_date(cutoff)
    available = public_availability_date(record)
    if cutoff_date is None or available is None:
        return "UNKNOWN"
    return "PASS" if available <= cutoff_date else "FAIL"


def filter_as_of(records: Iterable[Mapping[str, Any]], cutoff: str | date, *, include_unknown: bool = False) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    passed: list[Mapping[str, Any]] = []
    future: list[Mapping[str, Any]] = []
    unknown: list[Mapping[str, Any]] = []
    for record in records:
        status = point_in_time_status(record, cutoff)
        if status == "PASS" or include_unknown and status == "UNKNOWN":
            passed.append(record)
        elif status == "FAIL":
            future.append(record)
        else:
            unknown.append(record)
    return passed, future, unknown


def assert_point_in_time(records: Iterable[Mapping[str, Any]], cutoff: str | date, *, allow_unknown: bool = False) -> None:
    values = list(records)
    future = [record for record in values if point_in_time_status(record, cutoff) == "FAIL"]
    unknown = [record for record in values if point_in_time_status(record, cutoff) == "UNKNOWN"]
    if future:
        raise ValueError(f"future evidence is not eligible at cutoff: {len(future)} record(s)")
    if unknown and not allow_unknown:
        raise ValueError(f"evidence availability is unknown at cutoff: {len(unknown)} record(s)")


__all__ = [
    "InformationDates", "parse_date", "public_availability_date",
    "point_in_time_status", "filter_as_of", "assert_point_in_time",
]
