"""Strict, small schemas for canonical research records.

Dataclasses in the legacy components intentionally remain permissive for
backward compatibility.  New ingestion and release code calls these validators
before records enter a canonical store.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Mapping

from .extraction_safety import validate_extracted_text
from .status import STATUS_VALUES, SourceStatus, normalize_status


class SchemaValidationError(ValueError):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class EvidenceAnchor:
    source_id: str
    normalized_document_id: str
    source_url: str | None = None
    document_type: str | None = None
    publication_date: str | None = None
    filing_date: str | None = None
    page_number: int | None = None
    table_identifier: str | None = None
    paragraph_id: str | None = None
    chunk_id: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    exact_text: str = ""
    document_hash: str = ""
    status: str = SourceStatus.VERIFIED.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PersonRole:
    person_id: str
    normalized_name: str
    title: str
    role_type: str
    company: str
    source_id: str
    evidence_anchor: EvidenceAnchor | dict[str, Any]
    start_date: str | None = None
    end_date: str | None = None
    current_status: str = "CURRENT"
    confidence: str = "reported"
    aliases: list[str] = field(default_factory=list)
    issuer_cik: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if isinstance(self.evidence_anchor, EvidenceAnchor):
            value["evidence_anchor"] = self.evidence_anchor.to_dict()
        return value

    def validate(self) -> list[str]:
        return validate_person_role(self)


@dataclass
class MetricRecord:
    metric: str
    value: float | None
    unit: str
    currency: str | None = None
    scale: str = "units"
    period: str | None = None
    period_type: str = "UNKNOWN"
    reported: bool = True
    accounting_basis: str | None = None
    source_id: str = ""
    evidence_anchor: EvidenceAnchor | dict[str, Any] | None = None
    status: str = SourceStatus.VERIFIED.value
    derived_from: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        return validate_metric(self)


def _iso(value: str | None, field_name: str, errors: list[str]) -> None:
    if value is None or value == "":
        return
    try:
        date.fromisoformat(str(value)[:10])
    except ValueError:
        errors.append(f"{field_name} must be an ISO date")


def _anchor(value: EvidenceAnchor | Mapping[str, Any] | None, errors: list[str], *, required: bool = True) -> None:
    if value is None:
        if required:
            errors.append("evidence_anchor is required")
        return
    data = value.to_dict() if isinstance(value, EvidenceAnchor) else dict(value)
    for field_name in ("source_id", "normalized_document_id"):
        if not str(data.get(field_name) or "").strip():
            errors.append(f"evidence_anchor.{field_name} is required")
    for field_name in ("page_number", "start_char", "end_char"):
        if data.get(field_name) is not None and (not isinstance(data[field_name], int) or data[field_name] < 0):
            errors.append(f"evidence_anchor.{field_name} must be a non-negative integer")
    if data.get("page_number") == 0:
        errors.append("evidence_anchor.page_number must be positive")
    if data.get("start_char") is not None and data.get("end_char") is not None and data["end_char"] < data["start_char"]:
        errors.append("evidence_anchor.end_char precedes start_char")
    if data.get("exact_text"):
        try:
            validate_extracted_text(str(data["exact_text"]))
        except ValueError as exc:
            errors.append(str(exc))
    if data.get("status") and str(data["status"]).upper() not in STATUS_VALUES:
        errors.append("evidence_anchor.status is unsupported")


def validate_person_role(value: PersonRole | Mapping[str, Any]) -> list[str]:
    data = value.to_dict() if isinstance(value, PersonRole) else dict(value)
    errors: list[str] = []
    for field_name in ("person_id", "normalized_name", "title", "role_type", "company", "source_id"):
        if not str(data.get(field_name) or "").strip():
            errors.append(f"{field_name} is required")
    title = str(data.get("title") or "")
    if title and (title.isdigit() or re.fullmatch(r"\d+(?:\.\d+)?%?", title) or title.upper().startswith("%PDF-")):
        errors.append("title is not a valid human role")
    if title and any(ord(char) < 32 and char not in "\n\r\t" for char in title):
        errors.append("title contains binary-like characters")
    name = str(data.get("normalized_name") or "")
    if name and (name.isdigit() or len(re.findall(r"[A-Za-z]", name)) < 2):
        errors.append("normalized_name is not a valid person name")
    _iso(data.get("start_date"), "start_date", errors)
    _iso(data.get("end_date"), "end_date", errors)
    if data.get("start_date") and data.get("end_date") and str(data["end_date"]) < str(data["start_date"]):
        errors.append("end_date precedes start_date")
    status = str(data.get("current_status") or "").upper()
    if status not in {"CURRENT", "FORMER", "UNKNOWN", "PENDING"}:
        errors.append("current_status is unsupported")
    _anchor(data.get("evidence_anchor"), errors)
    return errors


def validate_metric(record: MetricRecord | Mapping[str, Any]) -> list[str]:
    data = asdict(record) if isinstance(record, MetricRecord) else dict(record)
    errors: list[str] = []
    if not str(data.get("metric") or "").strip():
        errors.append("metric is required")
    value = data.get("value")
    if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value))):
        errors.append("value must be a finite number or null")
    unit = str(data.get("unit") or "").lower()
    if unit in {"percent", "%"} and value is not None and not -100.0 <= float(value) <= 100.0:
        errors.append("percentage is outside the valid range")
    if str(data.get("status") or "").upper() not in STATUS_VALUES:
        errors.append("status is unsupported")
    if not str(data.get("source_id") or "").strip() and data.get("status") == SourceStatus.VERIFIED.value:
        errors.append("verified metrics require source_id")
    _anchor(data.get("evidence_anchor"), errors, required=data.get("status") == SourceStatus.VERIFIED.value)
    return errors


def validate_evidence_record(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    text = record.get("evidence_text") or record.get("exact_text")
    if text is not None:
        try:
            validate_extracted_text(str(text))
        except ValueError as exc:
            errors.append(str(exc))
    if str(record.get("status") or "").upper() == SourceStatus.VERIFIED.value:
        _anchor(record.get("evidence_anchor") or record.get("source_location"), errors)
    return errors


def validate_record(record: Any) -> list[str]:
    if isinstance(record, PersonRole) or (isinstance(record, Mapping) and "role_type" in record and "normalized_name" in record):
        return validate_person_role(record)
    if isinstance(record, MetricRecord) or (isinstance(record, Mapping) and "metric" in record and "unit" in record):
        return validate_metric(record)
    if isinstance(record, Mapping):
        return validate_evidence_record(record)
    return ["unsupported record type"]


def assert_valid(record: Any) -> None:
    errors = validate_record(record)
    if errors:
        raise SchemaValidationError(errors)


__all__ = [
    "SchemaValidationError", "EvidenceAnchor", "PersonRole", "MetricRecord",
    "validate_person_role", "validate_metric", "validate_evidence_record",
    "validate_record", "assert_valid",
]
