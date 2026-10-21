"""Typed records used by the qualitative-analysis ingestion component."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class IssuerFiscalCalendar:
    """Issuer fiscal-calendar facts used to interpret reporting periods.

    The model deliberately keeps the calendar as data rather than embedding
    issuer-specific rules in temporal code.  A month-end calendar is enough
    for ordinary issuers; ``week_based`` and ``weeks_per_year`` preserve the
    evidence needed for 52/53-week issuers whose exact Sunday boundaries move.
    """

    fiscal_year_end_month: int = 12
    fiscal_year_end_day: int | None = None
    fiscal_year_label_convention: str = "ending_year"
    quarter_boundaries: dict[str, Any] = field(default_factory=dict)
    week_based: bool = False
    weeks_per_year: int | None = None
    calendar_type: str = "calendar"
    source: str = "default"
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FiscalPeriod:
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    period_label: str | None = None
    detection_method: str | None = None
    confidence: float | None = None
    warning: str | None = None
    # Explicit semantics prevent annual guidance/counterparty references from
    # entering the issuer's reporting-period sequence.
    period_type: str = "UNKNOWN"
    detection_evidence: str | None = None
    detection_sources: list[str] = field(default_factory=list)
    period_end_date: str | None = None
    period_start_date: str | None = None
    issuer_fiscal_calendar: dict[str, Any] = field(default_factory=dict)
    canonical_period_identity: str | None = None
    source_id: str | None = None
    source_status: str = "VERIFIED"
    extraction_status: str = "VERIFIED"
    document_hash: str | None = None


@dataclass
class IngestionFailure:
    failure_type: str
    message: str
    ticker: str | None = None
    title: str | None = None
    source_url: str | None = None
    local_path: str | None = None
    document_type: str | None = None
    warning: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedDocument:
    """A complete, provenance-preserving document ready for later analysis."""

    document_id: str
    ticker: str
    company_name: str
    document_type: str
    document_subtype: str | None
    title: str
    publication_date: str | None
    event_date: str | None
    fiscal_year: int | None
    fiscal_quarter: int | None
    period_label: str | None
    source_url: str | None
    local_path: str | None
    source_method: str | None
    text: str
    text_length: int
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    period_detection_method: str | None = None
    period_detection_confidence: float | None = None
    sections: list[dict[str, Any]] = field(default_factory=list)
    period_type: str = "UNKNOWN"
    period_detection_evidence: str | None = None
    period_detection_sources: list[str] = field(default_factory=list)
    filing_date: str | None = None
    source_date: str | None = None
    download_date: str | None = None
    available_date: str | None = None
    # Explicit alias used by downstream evidence indexes.  ``available_date``
    # remains the compatibility field for frozen v1/v2 artifacts.
    public_availability_date: str | None = None
    availability_status: str = "UNKNOWN"
    period_end_date: str | None = None
    period_start_date: str | None = None
    issuer_fiscal_calendar: dict[str, Any] = field(default_factory=dict)
    canonical_period_identity: str | None = None
    # Stable provenance and extraction state used by v2 validators.  Defaults
    # keep every frozen v1 JSONL artifact readable.
    source_id: str = ""
    source_status: str = "VERIFIED"
    extraction_status: str = "VERIFIED"
    document_hash: str | None = None
    normalization_version: str = "qualitative-normalization-v2"
    schema_version: str = "normalized-document-schema-v2"

    def validate(self) -> list[str]:
        from .extraction_safety import validate_extracted_text
        from .status import STATUS_VALUES
        errors: list[str] = []
        try:
            validate_extracted_text(self.text)
        except ValueError as exc:
            errors.append(str(exc))
        for field_name in ("source_status", "extraction_status"):
            if str(getattr(self, field_name) or "").upper() not in STATUS_VALUES:
                errors.append(f"{field_name} is unsupported")
        if self.document_hash and self.document_hash != self.content_hash:
            errors.append("document_hash does not match content_hash")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedDocument":
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{key: value[key] for key in fields if key in value})
