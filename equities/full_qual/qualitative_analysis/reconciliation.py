"""Authority-aware reconciliation with explicit conflict retention."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from .status import SourceStatus


AUTHORITY_RANK = {
    "sec_structured": 100,
    "sec_amended": 110,
    "sec_official_filing": 90,
    "official_earnings_release": 80,
    "official_proxy": 78,
    "official_investor_presentation": 75,
    "official_transcript": 70,
    "issuer_official": 65,
    "verified_official_webpage": 60,
    "third_party": 10,
}


@dataclass
class ReconciliationConflict:
    identity: str
    field: str
    values: list[Any]
    source_ids: list[str]
    status: str = SourceStatus.CONFLICTED.value


@dataclass
class ReconciliationResult:
    selected: dict[str, Any] | None
    retained: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[ReconciliationConflict] = field(default_factory=list)
    status: str = SourceStatus.PENDING.value
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "retained": self.retained,
            "conflicts": [conflict.__dict__ for conflict in self.conflicts],
            "status": self.status,
            "rationale": self.rationale,
        }


def source_rank(record: Mapping[str, Any]) -> int:
    authority = str(record.get("source_authority") or record.get("source_type") or "").lower()
    if record.get("amendment") or str(record.get("form_type") or "").upper().endswith("/A"):
        return AUTHORITY_RANK.get("sec_amended", 110)
    if "structured" in authority and "sec" in authority:
        authority = "sec_structured"
    elif "sec" in authority or str(record.get("form_type") or "").upper() in {"3", "4", "5", "10-K", "10-Q", "8-K", "DEF 14A", "13D", "13G"}:
        authority = "sec_official_filing"
    elif "proxy" in authority:
        authority = "official_proxy"
    elif "presentation" in authority:
        authority = "official_investor_presentation"
    elif "transcript" in authority:
        authority = "official_transcript"
    return AUTHORITY_RANK.get(authority, AUTHORITY_RANK.get(str(record.get("source_family") or "").lower(), 0))


def _date_key(record: Mapping[str, Any]) -> str:
    return str(record.get("filing_date") or record.get("publication_date") or record.get("available_date") or "")


class SourceReconciler:
    def __init__(self, *, authority_rank: Mapping[str, int] | None = None):
        self.authority_rank = dict(AUTHORITY_RANK)
        if authority_rank:
            self.authority_rank.update(authority_rank)

    def reconcile(self, records: Iterable[Mapping[str, Any]], *, identity_fields: tuple[str, ...] = ("ticker", "metric", "period"), compare_fields: Iterable[str] | None = None) -> ReconciliationResult:
        values = [dict(record) for record in records]
        if not values:
            return ReconciliationResult(None, status=SourceStatus.SOURCE_UNAVAILABLE.value, rationale="no source records")
        identity = "|".join(str(values[0].get(field) or "") for field in identity_fields)
        selected = max(values, key=lambda record: (source_rank(record), _date_key(record), str(record.get("source_id") or "")))
        fields = list(compare_fields or ("value", "title", "content", "text"))
        conflicts: list[ReconciliationConflict] = []
        for field in fields:
            field_values = [(record.get(field), str(record.get("source_id") or "")) for record in values if record.get(field) is not None]
            distinct = {repr(value) for value, _source in field_values}
            if len(distinct) > 1:
                conflicts.append(ReconciliationConflict(identity, field, [value for value, _source in field_values], [source for _value, source in field_values]))
        if conflicts and len({source_rank(record) for record in values}) == 1:
            status = SourceStatus.CONFLICTED.value
            rationale = "equal-authority source values conflict; all records retained"
        else:
            status = SourceStatus.VERIFIED.value
            rationale = "highest-authority source selected; lower-authority provenance retained"
        return ReconciliationResult(selected, values, conflicts, status, rationale)

    def reconcile_many(self, records: Iterable[Mapping[str, Any]], *, identity_fields: tuple[str, ...] = ("ticker", "metric", "period"), compare_fields: Iterable[str] | None = None) -> list[ReconciliationResult]:
        groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
        for record in records:
            key = tuple(str(record.get(field) or "") for field in identity_fields)
            groups.setdefault(key, []).append(record)
        return [self.reconcile(group, identity_fields=identity_fields, compare_fields=compare_fields) for group in groups.values()]


def reconcile_sources(records: Iterable[Mapping[str, Any]], **kwargs: Any) -> ReconciliationResult:
    return SourceReconciler().reconcile(records, **kwargs)


__all__ = ["AUTHORITY_RANK", "ReconciliationConflict", "ReconciliationResult", "SourceReconciler", "source_rank", "reconcile_sources"]
