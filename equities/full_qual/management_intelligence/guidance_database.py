"""Persistent point-in-time guidance and forecast reliability records."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from qualitative_analysis.numeric import percentage_change, range_midpoint
from qualitative_analysis.point_in_time import parse_date, point_in_time_status
from qualitative_analysis.status import SourceStatus


GUIDANCE_STATUSES = frozenset({"OPEN", "REVISED", "WITHDRAWN", "RESOLVED"})
GUIDANCE_COVERAGE_STATUSES = frozenset(
    {
        "GUIDANCE_AVAILABLE_AND_CAPTURED",
        "GUIDANCE_EVIDENCE_NOT_FOUND",
        "GUIDANCE_NOT_IN_CURRENT_DOCUMENT_STORE",
        "GUIDANCE_DOCUMENTS_PRESENT_BUT_NOT_EXTRACTED",
        "GUIDANCE_PRESENT_BUT_NOT_NUMERIC",
        "GUIDANCE_PRESENT_BUT_NOT_COMPARABLE",
        "NO_GUIDANCE_GIVEN_CONFIRMED",
    }
)


@dataclass
class GuidanceRecord:
    guidance_id: str
    company: str
    ticker: str
    issue_date: str
    publication_date: str | None
    metric: str
    target_period: str
    low: float | None = None
    high: float | None = None
    midpoint: float | None = None
    unit: str | None = None
    currency: str | None = None
    tolerance: float | None = None
    qualitative: bool = False
    original_or_revised: str = "ORIGINAL"
    status: str = "OPEN"
    source_id: str = ""
    normalized_document_id: str = ""
    source_url: str | None = None
    local_source_path: str | None = None
    evidence_anchor: dict[str, Any] = field(default_factory=dict)
    available_date: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper().strip()
        self.status = self.status.upper()
        if self.status not in GUIDANCE_STATUSES:
            raise ValueError(f"unsupported guidance status: {self.status}")
        for field_name in ("low", "high", "midpoint", "tolerance"):
            value = getattr(self, field_name)
            if value is not None:
                try:
                    numeric = float(value)
                    if not math.isfinite(numeric):
                        raise ValueError(f"{field_name} must be finite")
                    setattr(self, field_name, numeric)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field_name} must be numeric") from exc
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("guidance low exceeds high")
        if self.midpoint is None:
            self.midpoint = range_midpoint(self.low, self.high)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GuidanceRecord":
        fields = {field.name for field in cls.__dataclass_fields__.values()}
        defaults = {"publication_date": None, "low": None, "high": None, "midpoint": None, "unit": None, "currency": None, "tolerance": None, "qualitative": False, "original_or_revised": "ORIGINAL", "status": "OPEN", "source_id": "", "normalized_document_id": "", "source_url": None, "local_source_path": None, "evidence_anchor": {}, "available_date": None, "created_at": ""}
        defaults.update({key: value[key] for key in fields if key in value})
        return cls(**defaults)


@dataclass
class GuidanceActual:
    outcome_id: str
    guidance_id: str
    actual: float | None
    actual_period: str
    actual_available_date: str
    variance: float | None
    variance_pct: float | None
    result: str
    source_id: str
    evidence_anchor: dict[str, Any] = field(default_factory=dict)
    status: str = "RESOLVED"

    def __post_init__(self) -> None:
        self.result = str(self.result).upper()
        self.status = str(self.status).upper()
        if not self.guidance_id or not self.outcome_id or not self.actual_available_date:
            raise ValueError("guidance outcome identity and public availability are required")
        if self.status not in GUIDANCE_STATUSES:
            # Actuals use RESOLVED in normal operation; retain the shared
            # vocabulary while accepting the lifecycle's terminal value.
            if self.status != "RESOLVED":
                raise ValueError(f"unsupported guidance outcome status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GuidanceActual":
        fields = {item.name for item in cls.__dataclass_fields__.values()}
        data = {key: value[key] for key in fields if key in value}
        data.setdefault("evidence_anchor", {})
        data.setdefault("status", "RESOLVED")
        return cls(**data)


def _safe_id(*parts: Any) -> str:
    import hashlib
    return "guidance-" + hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:24]


def evaluate_guidance(record: GuidanceRecord, actual: float | None, *, actual_period: str, actual_available_date: str, tolerance: float | None = None, source_id: str = "", evidence_anchor: dict[str, Any] | None = None) -> GuidanceActual:
    """Resolve a record only when the actual was public by the outcome date."""

    if parse_date(actual_available_date) is None:
        raise ValueError("actual_available_date is required to resolve guidance")
    guidance_available = record.available_date or record.publication_date or record.issue_date
    if guidance_available and parse_date(actual_available_date) < parse_date(guidance_available):
        raise ValueError("actual cannot predate guidance availability")
    if actual is None or record.midpoint is None:
        result = "NOT_COMPARABLE"
        variance = variance_pct = None
    else:
        variance = float(actual) - float(record.midpoint)
        variance_pct = percentage_change(float(record.midpoint), float(actual))
        band = float(tolerance if tolerance is not None else record.tolerance or 0.0)
        if abs(variance) <= band:
            result = "MET"
        elif variance > 0:
            result = "BEAT"
        else:
            result = "MISS"
    return GuidanceActual(_safe_id(record.guidance_id, actual_period, actual_available_date), record.guidance_id, actual, actual_period, actual_available_date, variance, variance_pct, result, source_id, evidence_anchor or {})


class GuidanceDatabase:
    """Small deterministic JSONL database with explicit lifecycle transitions."""

    def __init__(self, root: str | Path = "artifacts/management_intelligence/guidance_v2"):
        path = Path(root)
        self.root = path.parent if path.suffix else path
        self.records_path = path if path.suffix else self.root / "guidance_records.jsonl"
        self.outcomes_path = self.root / "guidance_actuals.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read(path: Path, cls):
        if not path.exists():
            return []
        values = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    values.append(cls.from_dict(json.loads(line)))
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
        return values

    @staticmethod
    def _write(path: Path, values: Iterable[Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
        os.close(fd)
        temp = Path(temporary)
        try:
            temp.write_text("".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in sorted(values, key=lambda item: getattr(item, "guidance_id", getattr(item, "outcome_id", "")))), encoding="utf-8")
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def records(self, ticker: str | None = None, *, as_of_date: str | None = None) -> list[GuidanceRecord]:
        values = self._read(self.records_path, GuidanceRecord)
        return [item for item in values if (ticker is None or item.ticker == ticker.upper()) and (as_of_date is None or point_in_time_status({"public_availability_date": item.available_date or item.issue_date}, as_of_date) != "FAIL")]

    def outcomes(self, ticker: str | None = None, *, as_of_date: str | None = None) -> list[GuidanceActual]:
        values = self._read(self.outcomes_path, GuidanceActual)
        ids = {item.guidance_id for item in self.records(ticker, as_of_date=as_of_date)}
        return [item for item in values if item.guidance_id in ids and (as_of_date is None or point_in_time_status({"public_availability_date": item.actual_available_date}, as_of_date) != "FAIL")]

    def upsert(self, record: GuidanceRecord) -> str:
        values = {item.guidance_id: item for item in self._read(self.records_path, GuidanceRecord)}
        action = "added" if record.guidance_id not in values else "updated" if values[record.guidance_id].to_dict() != record.to_dict() else "unchanged"
        if action != "unchanged":
            values[record.guidance_id] = record
            self._write(self.records_path, values.values())
        return action

    def resolve(self, record: GuidanceRecord, actual: GuidanceActual) -> str:
        if actual.guidance_id != record.guidance_id:
            raise ValueError("actual belongs to a different guidance record")
        if parse_date(actual.actual_available_date) is None:
            raise ValueError("actual availability is required")
        record.status = "RESOLVED"
        self.upsert(record)
        values = {item.outcome_id: item for item in self._read(self.outcomes_path, GuidanceActual)}
        values[actual.outcome_id] = actual
        self._write(self.outcomes_path, values.values())
        return actual.result

    def revise(self, record: GuidanceRecord, *, issue_date: str, **changes: Any) -> GuidanceRecord:
        data = record.to_dict()
        data.update(changes)
        data["guidance_id"] = _safe_id(record.guidance_id, issue_date)
        data["issue_date"] = issue_date
        data["original_or_revised"] = "REVISED"
        data["status"] = "REVISED"
        revised = GuidanceRecord.from_dict(data)
        record.status = "REVISED"
        self.upsert(record)
        self.upsert(revised)
        return revised

    def withdraw(self, record: GuidanceRecord) -> GuidanceRecord:
        record.status = "WITHDRAWN"
        self.upsert(record)
        return record


def guidance_coverage_status(*, document_count: int, claim_count: int, guidance_claim_count: int, captured_count: int, explicit_no_guidance: bool = False, numeric_count: int | None = None, comparable_count: int | None = None) -> str:
    if explicit_no_guidance and guidance_claim_count == 0:
        return "NO_GUIDANCE_GIVEN_CONFIRMED"
    if document_count == 0:
        return "GUIDANCE_NOT_IN_CURRENT_DOCUMENT_STORE"
    if claim_count == 0:
        return "GUIDANCE_DOCUMENTS_PRESENT_BUT_NOT_EXTRACTED"
    if guidance_claim_count == 0:
        return "GUIDANCE_EVIDENCE_NOT_FOUND"
    if captured_count == 0 and (numeric_count is None or numeric_count == 0):
        return "GUIDANCE_PRESENT_BUT_NOT_NUMERIC"
    if comparable_count is not None and comparable_count == 0:
        return "GUIDANCE_PRESENT_BUT_NOT_COMPARABLE"
    return "GUIDANCE_AVAILABLE_AND_CAPTURED"


__all__ = [
    "GUIDANCE_STATUSES", "GUIDANCE_COVERAGE_STATUSES", "GuidanceRecord",
    "GuidanceActual", "GuidanceDatabase", "evaluate_guidance",
    "guidance_coverage_status",
]
