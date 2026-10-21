"""Company-generic business intelligence primitives.

These engines deliberately produce structured assessments and evidence links;
they do not turn sparse disclosure into a positive or negative investment view.
Industry-specific logic is supplied through adapters rather than embedded in
the generic pipeline.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Protocol

from .status import SourceStatus


@dataclass
class BusinessAssessment:
    dimension: str
    assessment: str
    mechanism: str
    evidence_ids: list[str] = field(default_factory=list)
    status: str = SourceStatus.PENDING.value
    materiality: str = "MEDIUM"
    uncertainty: str = "UNKNOWN"
    confidence: str = "UNCALIBRATED"
    # Kept separate deliberately: reliable evidence can still be incomplete,
    # and neither is the same thing as the business conclusion.
    evidence_reliability: str = "UNKNOWN"
    evidence_completeness: str = "UNKNOWN"
    analytical_confidence: str = "UNCALIBRATED"
    business_assessment: str = "UNRESOLVED"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KPIDefinition:
    name: str
    description: str
    unit: str | None = None
    materiality: str = "MEDIUM"
    applicable_when: str | None = None


class IndustryAdapter(Protocol):
    name: str

    def kpis(self) -> list[KPIDefinition]: ...


class GenericKPIAdapter:
    name = "generic"

    def kpis(self) -> list[KPIDefinition]:
        return [
            KPIDefinition("revenue", "reported revenue", "currency", "HIGH"),
            KPIDefinition("volume", "reported unit or activity volume", "units"),
            KPIDefinition("pricing", "reported price or mix movement", "percent"),
            KPIDefinition("gross_margin", "reported gross margin", "percent", "HIGH"),
            KPIDefinition("operating_margin", "reported operating margin", "percent"),
            KPIDefinition("orders", "orders or bookings", "currency or units"),
            KPIDefinition("backlog", "backlog or remaining performance obligation", "currency or units"),
            KPIDefinition("customer_growth", "customer additions or growth", "count or percent"),
            KPIDefinition("capex", "capital expenditure", "currency"),
            KPIDefinition("cash_generation", "cash generation or free cash flow", "currency"),
        ]


class IndustryAdapterRegistry:
    def __init__(self):
        self._adapters: dict[str, IndustryAdapter] = {"generic": GenericKPIAdapter()}

    def register(self, adapter: IndustryAdapter) -> None:
        key = str(adapter.name).strip().lower()
        if not key:
            raise ValueError("adapter name is required")
        self._adapters[key] = adapter

    def get(self, name: str | None) -> IndustryAdapter:
        return self._adapters.get(str(name or "generic").lower(), self._adapters["generic"])

    def names(self) -> list[str]:
        return sorted(self._adapters)


class BusinessModelEngine:
    """Extract high-level economic mechanisms from verified claims."""

    _mechanism_patterns = {
        "recurring_revenue": r"\b(subscription|recurring|annual contract|renewal|remaining performance obligation)\b",
        "transactional_revenue": r"\b(transaction|usage[- ]based|per[- ]unit|volume[- ]based)\b",
        "customer_concentration": r"\b(customer|client).{0,80}\b(?:10|one|single|concentrat)",
        "supplier_concentration": r"\b(supplier|foundry|single[- ]source|sole[- ]source)\b",
        "capital_intensity": r"\b(capex|capital expenditure|capital intensive|manufacturing capacity)\b",
        "pricing": r"\b(price|pricing|asp|average selling price|mix)\b",
    }

    def analyze(self, claims: Iterable[Mapping[str, Any]], *, documents: Iterable[Mapping[str, Any]] = ()) -> list[BusinessAssessment]:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for claim in claims:
            text = " ".join(str(claim.get(key) or "") for key in ("claim_text", "evidence_text", "summary"))
            for mechanism, pattern in self._mechanism_patterns.items():
                if re.search(pattern, text, re.IGNORECASE):
                    grouped.setdefault(mechanism, []).append(claim)
        assessments: list[BusinessAssessment] = []
        for mechanism, rows in sorted(grouped.items()):
            ids = [str(row.get("claim_id") or row.get("document_id") or "") for row in rows if row.get("claim_id") or row.get("document_id")]
            assessments.append(BusinessAssessment(
                dimension="business_model",
                assessment=mechanism.upper(),
                mechanism=f"Disclosure contains evidence relevant to {mechanism.replace('_', ' ')}.",
                evidence_ids=sorted(set(ids)),
                status=SourceStatus.VERIFIED.value if ids else SourceStatus.PENDING.value,
                materiality="HIGH" if mechanism in {"customer_concentration", "supplier_concentration", "capital_intensity"} else "MEDIUM",
                uncertainty="LOW" if len(ids) >= 2 else "MEDIUM",
                evidence_reliability="HIGH" if ids else "UNKNOWN",
                evidence_completeness="PARTIAL",
                analytical_confidence="UNCALIBRATED",
                business_assessment="UNRESOLVED",
                notes=["This is a mechanism assessment; it is not a valuation or trade recommendation."],
            ))
        return assessments


@dataclass
class TrajectoryAssessment:
    metric: str
    absolute_level: str = "UNKNOWN"
    rate_of_change: str = "UNKNOWN"
    change_in_rate: str = "UNKNOWN"
    qualitative_transition: str = "UNRESOLVED"
    evidence_ids: list[str] = field(default_factory=list)
    status: str = SourceStatus.PENDING.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_trajectory(metric: str, *, level: str | None = None, rate: str | None = None, acceleration: str | None = None, evidence_ids: Iterable[str] = ()) -> TrajectoryAssessment:
    return TrajectoryAssessment(metric=metric, absolute_level=level or "UNKNOWN", rate_of_change=rate or "UNKNOWN", change_in_rate=acceleration or "UNKNOWN", qualitative_transition="UNRESOLVED" if not any((level, rate, acceleration)) else "SUPPORTED", evidence_ids=sorted(set(str(item) for item in evidence_ids)), status=SourceStatus.VERIFIED.value if evidence_ids else SourceStatus.PENDING.value)


def analyze_cause(metric: str, evidence_text: str, *, evidence_id: str | None = None) -> dict[str, Any]:
    text = str(evidence_text or "")
    causes = []
    for label, pattern in (("price", r"\bprice|pricing|asp\b"), ("volume", r"\bvolume|units|shipments\b"), ("mix", r"\bmix|product mix\b"), ("cost", r"\bcost|expense|utili[sz]ation\b"), ("fx", r"\bforeign exchange|currency|fx\b"), ("acquisition", r"\bacqui(?:sition|red)\b")):
        if re.search(pattern, text, re.IGNORECASE):
            causes.append(label)
    return {"metric": metric, "causes": sorted(set(causes)), "status": "CAUSE_RESOLVED" if causes else "CAUSE_UNRESOLVED", "evidence_ids": [evidence_id] if evidence_id else [], "evidence_text": text}


__all__ = [
    "BusinessAssessment", "KPIDefinition", "IndustryAdapter", "GenericKPIAdapter",
    "IndustryAdapterRegistry", "BusinessModelEngine", "TrajectoryAssessment",
    "build_trajectory", "analyze_cause",
]
