"""Typed records and controlled vocabularies for current qualitative state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


STATE_VERSION = "qualitative-state-v2"
RULES_VERSION = "state-rules-v2"
PROVIDER_VERSION = "deterministic-state-v2"

GENERAL_STATE_VALUES = (
    "strong", "healthy", "neutral", "weak", "stressed", "mixed", "unknown",
)
TREND_VALUES = (
    "improving", "deteriorating", "stable", "accelerating",
    "decelerating", "mixed", "unknown",
)
DOMAIN_STATE_VALUES = {
    "margins": ("expanding", "stable", "compressing", "mixed", "unknown"),
    "guidance": ("raised", "maintained", "lowered", "mixed", "unknown"),
    "risks": ("low", "moderate", "elevated", "increasing", "mixed", "unknown"),
    "product": ("progressing", "stable", "delayed", "mixed", "unknown"),
    "capital_allocation": ("expansionary", "neutral", "conservative", "mixed", "unknown"),
    "comparable_sales": ("strong", "weak", "mixed", "unknown"),
    "same_store_sales": ("strong", "weak", "mixed", "unknown"),
    "traffic_transactions": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "average_ticket": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "gross_margin": ("expanding", "stable", "compressing", "mixed", "unknown"),
    "operating_margin": ("expanding", "stable", "compressing", "mixed", "unknown"),
    "inventory_growth": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "inventory_efficiency": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "ecommerce_growth": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "membership_growth": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "advertising_revenue": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "retail_media": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "shrink": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "labor_costs": ("improving", "deteriorating", "stable", "mixed", "unknown"),
    "wage_pressure": ("improving", "deteriorating", "stable", "mixed", "unknown"),
}
ALL_STATE_VALUES = tuple(sorted(set(GENERAL_STATE_VALUES).union(*DOMAIN_STATE_VALUES.values())))


@dataclass
class QualitativeState:
    """A current state with explicit evidence and confidence provenance."""

    state_id: str
    ticker: str
    company_name: str
    dimension: str
    topic: str | None
    subtopic: str | None
    as_of_fiscal_year: int | None
    as_of_fiscal_quarter: int | None
    period_label: str | None
    current_state: str
    trend: str
    summary: str
    supporting_claim_ids: list[str]
    supporting_change_ids: list[str]
    contradicting_claim_ids: list[str]
    contradicting_change_ids: list[str]
    confidence: float
    evidence_count: int
    contradiction_count: int
    state_method: str
    state_version: str
    rules_version: str
    created_at: str
    evidence_groups_considered: int = 0
    evidence_groups_retained: int = 0
    evidence_groups_rejected: int = 0
    duplicate_groups_removed: int = 0
    confidence_factors: dict[str, Any] = field(default_factory=dict)
    supporting_document_ids: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_local_paths: list[str] = field(default_factory=list)
    child_state_ids: list[str] = field(default_factory=list)
    unknown_reason: str | None = None
    provider: str = PROVIDER_VERSION
    period_type: str = "UNKNOWN"
    period_end_date: str | None = None
    period_start_date: str | None = None
    issuer_fiscal_calendar: dict[str, Any] = field(default_factory=dict)
    public_availability_date: str | None = None
    availability_status: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QualitativeState":
        fields = {item.name for item in cls.__dataclass_fields__.values()}
        data = {key: value[key] for key in fields if key in value}
        data.setdefault("topic", None)
        data.setdefault("subtopic", None)
        data.setdefault("supporting_claim_ids", [])
        data.setdefault("supporting_change_ids", [])
        data.setdefault("contradicting_claim_ids", [])
        data.setdefault("contradicting_change_ids", [])
        data.setdefault("evidence_groups_considered", 0)
        data.setdefault("evidence_groups_retained", 0)
        data.setdefault("evidence_groups_rejected", 0)
        data.setdefault("duplicate_groups_removed", 0)
        data.setdefault("confidence_factors", {})
        data.setdefault("supporting_document_ids", [])
        data.setdefault("source_urls", [])
        data.setdefault("source_local_paths", [])
        data.setdefault("child_state_ids", [])
        data.setdefault("unknown_reason", None)
        data.setdefault("provider", PROVIDER_VERSION)
        data.setdefault("period_type", "UNKNOWN")
        data.setdefault("period_end_date", None)
        data.setdefault("period_start_date", None)
        data.setdefault("issuer_fiscal_calendar", {})
        data.setdefault("public_availability_date", None)
        data.setdefault("availability_status", "UNKNOWN")
        data.setdefault("state_version", STATE_VERSION)
        data.setdefault("rules_version", RULES_VERSION)
        return cls(**data)


@dataclass
class StateSynthesisResult:
    """Provider output plus diagnostics used by the CLI and tests."""

    ticker: str
    period_label: str | None
    claims_considered: int = 0
    temporal_changes_considered: int = 0
    states: list[QualitativeState] = field(default_factory=list)
    rejected_groups: list[str] = field(default_factory=list)
    validation_failures: list[str] = field(default_factory=list)
    latest_annual_period: str | None = None
    latest_quarterly_period: str | None = None
    latest_reporting_period: str | None = None
    state_freshness: str = "UNKNOWN"
    period_integrity_failures: list[str] = field(default_factory=list)
    temporal_period_mismatch: bool = False
