"""Generic capital-allocation event extraction with auditable evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


EVENT_TYPES = frozenset({"BUYBACK", "DIVIDEND", "M_AND_A", "DIVESTITURE", "CAPEX", "R_AND_D", "DEBT", "EQUITY_ISSUANCE", "OTHER"})


@dataclass
class CapitalAllocationAssessment:
    event_id: str
    ticker: str
    event_type: str
    event_date: str | None
    value: float | None
    currency: str | None
    status: str
    rationale: str | None
    outcome: str = "UNRESOLVED"
    evidence_ids: list[str] = field(default_factory=list)
    source_url: str | None = None
    source_document_id: str | None = None
    materiality: str = "MEDIUM"

    def __post_init__(self) -> None:
        self.event_type = str(self.event_type).upper()
        self.materiality = str(self.materiality).upper()
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported capital event type: {self.event_type}")
        if self.materiality not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise ValueError(f"unsupported materiality: {self.materiality}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _id(*parts: Any) -> str:
    return "capital-" + hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:24]


def classify_capital_event(text: str) -> str:
    lower = str(text or "").lower()
    # An acquisition mentioned only as the driver of an operating metric is
    # NII/revenue evidence, not a capital-deployment event.  Require
    # transaction mechanics before assigning M&A.
    acquisition_reference = re.search(r"acqui(?:re|red|sition)", lower)
    transaction_mechanics = re.search(r"purchase price|consideration|closing|closed|transaction value|deal value|merger agreement|divest|dispose|paid|funded|capital deployment", lower)
    if acquisition_reference and re.search(r"\b(?:nii|net interest income|run[- ]rate|revenue|earnings|margin)\b", lower) and not transaction_mechanics:
        return "OTHER"
    patterns = (
        ("BUYBACK", r"repurchas|buyback|share repurchase"),
        ("DIVIDEND", r"dividend|distribution"),
        ("M_AND_A", r"acqui(?:re|red|sition)|merger|purchase of .*business"),
        ("DIVESTITURE", r"divest|disposition|sale of .*business"),
        ("CAPEX", r"capital expenditure|\bcapex\b|capacity investment"),
        ("R_AND_D", r"research and development|\br&d\b"),
        ("DEBT", r"debt issuance|borrow|term loan|notes offering|repay.*debt"),
        ("EQUITY_ISSUANCE", r"share issuance|equity offering|dilution"),
    )
    for event_type, pattern in patterns:
        if re.search(pattern, lower):
            return event_type
    return "OTHER"


def extract_capital_allocation(claims: Iterable[Mapping[str, Any]], ticker: str) -> list[CapitalAllocationAssessment]:
    values: list[CapitalAllocationAssessment] = []
    for claim in claims:
        text = " ".join(str(claim.get(key) or "") for key in ("claim_text", "evidence_text"))
        event_type = classify_capital_event(text)
        if event_type == "OTHER":
            continue
        amount = re.search(r"(?:\$|USD\s*)([0-9][0-9,]*(?:\.\d+)?)\s*(million|billion|thousand|m|bn|mm)?", text, re.I)
        number = float(amount.group(1).replace(",", "")) if amount else None
        if amount and amount.group(2):
            number *= {"million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "thousand": 1e3, "mm": 1e6}[amount.group(2).lower()]
        values.append(CapitalAllocationAssessment(
            event_id=_id(ticker.upper(), claim.get("claim_id") or claim.get("document_id"), event_type),
            ticker=ticker.upper(), event_type=event_type, event_date=claim.get("available_date") or claim.get("publication_date"),
            value=number, currency="USD" if "$" in text or "USD" in text.upper() else None,
            status="PLANNED" if re.search(r"plan|intend|expect|authorize", text, re.I) else "COMPLETED",
            rationale=text, evidence_ids=[str(claim.get("claim_id") or claim.get("document_id") or "")],
            source_url=claim.get("source_url"), source_document_id=claim.get("document_id"), materiality="HIGH" if number and number >= 100_000_000 else "MEDIUM",
        ))
    return list({item.event_id: item for item in values}.values())


__all__ = ["EVENT_TYPES", "CapitalAllocationAssessment", "classify_capital_event", "extract_capital_allocation"]
