"""Evidence-backed risk and catalyst records with explicit availability."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from .extraction_rules import semantic_dimension_exclusion, temporal_status as classify_temporal_status


MATERIALITY = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


@dataclass
class RiskRecord:
    risk_id: str
    category: str
    description: str
    status: str
    materiality: str
    evidence_ids: list[str] = field(default_factory=list)
    mitigants: list[str] = field(default_factory=list)
    monitoring_indicators: list[str] = field(default_factory=list)
    available_date: str | None = None
    public_availability_date: str | None = None
    temporal_status: str = "UNKNOWN_TEMPORAL_STATUS"
    risk_type: str = "RISK_DISCLOSURE"

    def __post_init__(self) -> None:
        self.materiality = str(self.materiality).upper()
        if self.materiality not in MATERIALITY:
            raise ValueError(f"unsupported risk materiality: {self.materiality}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CatalystRecord:
    catalyst_id: str
    description: str
    status: str
    materiality: str
    event_date: str | None
    mechanism: str
    evidence_ids: list[str] = field(default_factory=list)
    existing_or_new: str = "UNRESOLVED"
    public_availability_date: str | None = None
    temporal_status: str = "UNKNOWN_TEMPORAL_STATUS"

    def __post_init__(self) -> None:
        self.materiality = str(self.materiality).upper()
        if self.materiality not in MATERIALITY:
            raise ValueError(f"unsupported catalyst materiality: {self.materiality}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stable(prefix: str, *parts: Any) -> str:
    return prefix + "-" + hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:24]


def classify_risk_semantics(text: str) -> str:
    """Classify risk evidence without treating model methodology as an active risk."""
    lower = str(text or "").lower()
    if re.search(r"\b(?:var|value at risk|cva|xva|model)\b", lower) and re.search(r"\b(?:calculated|calculation|methodology|methodologies|non[- ]additive|validation|assumption|definition|measure(?:s|d)?)\b", lower) and not re.search(r"\b(?:breach|loss|deteriorat|increased|elevated|material exposure|limit exceeded|stress)\b", lower):
        return "RISK_MEASUREMENT_METHOD"
    if re.search(r"\b(?:breach|default|incident|failure|stress test result|charge[- ]off|write[- ]off|loss event)\b", lower):
        return "RISK_EVENT"
    if re.search(r"\b(?:mitigat|hedg|controls?|reduced|reduction|limit(?:s|ed)?|remediat)\b", lower):
        return "RISK_MITIGATION"
    if re.search(r"\b(?:increased|increasing|elevated|worsen|deteriorat|declin|improv|eased|reduced)\b", lower):
        return "RISK_TREND"
    if re.search(r"\b(?:loss|impact|charge[- ]off|write[- ]off|outcome|result)\b", lower):
        return "RISK_OUTCOME"
    if re.search(r"\b(?:var|value at risk|exposure|ratio|sensitivity|volatil)\b", lower):
        return "RISK_METRIC"
    if re.search(r"\b(?:concentration|cyber|credit|liquidity|funding|market|operational|regulatory|geopolitical|counterparty)\b", lower):
        return "RISK_FACTOR"
    return "RISK_DISCLOSURE"


def extract_risks(claims: Iterable[Mapping[str, Any]]) -> list[RiskRecord]:
    values = []
    for claim in claims:
        text = " ".join(str(claim.get(key) or "") for key in ("claim_text", "evidence_text"))
        status = str(claim.get("temporal_status") or classify_temporal_status(text))
        if not re.search(r"\brisk|headwind|uncertain|could adversely|volatil", text, re.I):
            continue
        risk_type = classify_risk_semantics(text)
        if risk_type == "RISK_MEASUREMENT_METHOD":
            # Preserve methodology in provenance only when it also contains
            # an economic deterioration signal; definitions alone should not
            # enter current risk state or materiality scoring.
            continue
        materiality = "CRITICAL" if re.search(r"material|significant|重大|substantial", text, re.I) else "MEDIUM"
        available = claim.get("public_availability_date") or claim.get("available_date") or claim.get("filing_date") or claim.get("publication_date")
        values.append(RiskRecord(_stable("risk", claim.get("claim_id") or claim.get("document_id")), str(claim.get("topic") or "general"), text, "OPEN", materiality, [str(claim.get("claim_id") or claim.get("document_id") or "")], available_date=available, public_availability_date=available, temporal_status=status, risk_type=risk_type))
    return list({value.risk_id: value for value in values}.values())


def extract_catalysts(claims: Iterable[Mapping[str, Any]]) -> list[CatalystRecord]:
    values = []
    for claim in claims:
        text = " ".join(str(claim.get(key) or "") for key in ("claim_text", "evidence_text"))
        status = str(claim.get("temporal_status") or classify_temporal_status(text))
        if semantic_dimension_exclusion("catalysts", text) or status in {"HISTORICAL", "RESOLVED"}:
            continue
        if not re.search(r"\b(plan|planned|launch|approval|milestone|expected|target|catalyst|complet)\b", text, re.I):
            continue
        date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        available = claim.get("public_availability_date") or claim.get("available_date") or claim.get("filing_date") or claim.get("publication_date")
        values.append(CatalystRecord(_stable("catalyst", claim.get("claim_id") or claim.get("document_id")), text, "OPEN", "HIGH" if re.search(r"material|major", text, re.I) else "MEDIUM", date_match.group(1) if date_match else None, "The disclosed event could change a future operating or strategic outcome; the economic effect remains evidence-dependent.", [str(claim.get("claim_id") or claim.get("document_id") or "")], "NEW", available, status))
    return list({value.catalyst_id: value for value in values}.values())


def risk_coverage_diagnostics(claims: Iterable[Mapping[str, Any]], extracted: Iterable[RiskRecord] = (), canonical: Iterable[Mapping[str, Any]] = ()) -> dict[str, int]:
    """Report risk coverage without allowing methodology to become a risk.

    The counts are deliberately diagnostic only.  They expose what was
    considered, excluded, and merged so an analyst can distinguish source
    coverage from parser or canonicalisation behaviour.
    """
    rows = list(claims)
    candidates = [row for row in rows if (re.search(r"\brisk|headwind|uncertain|could adversely|volatil", " ".join(str(row.get(key) or "") for key in ("claim_text", "evidence_text")), re.I) or classify_risk_semantics(" ".join(str(row.get(key) or "") for key in ("claim_text", "evidence_text"))) == "RISK_MEASUREMENT_METHOD")]
    methodology = [row for row in candidates if classify_risk_semantics(" ".join(str(row.get(key) or "") for key in ("claim_text", "evidence_text"))) == "RISK_MEASUREMENT_METHOD"]
    historical = [row for row in candidates if str(row.get("temporal_status") or classify_temporal_status(" ".join(str(row.get(key) or "") for key in ("claim_text", "evidence_text")))).upper() in {"HISTORICAL", "RESOLVED"}]
    extracted_rows = list(extracted)
    descriptions: dict[str, int] = {}
    for record in extracted_rows:
        key = re.sub(r"\W+", " ", record.description.lower()).strip()
        descriptions[key] = descriptions.get(key, 0) + 1
    duplicate_rows = sum(max(0, count - 1) for count in descriptions.values())
    canonical_rows = list(canonical)
    return {
        "raw_candidate_count": len(candidates),
        "methodology_excluded": len(methodology),
        "historical_excluded": len(historical),
        "boilerplate_deduplicated": duplicate_rows,
        "active_risk_groups": sum(1 for row in canonical_rows if str(row.get("current_status") or "").upper() in {"OPEN", "ESCALATING"}),
    }


__all__ = ["MATERIALITY", "RiskRecord", "CatalystRecord", "classify_risk_semantics", "extract_risks", "extract_catalysts", "risk_coverage_diagnostics"]
