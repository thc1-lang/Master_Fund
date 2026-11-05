"""Deterministic source-priority and research-coverage helpers.

The downloader may discover thousands of links, but a bounded run must spend
its first budget on the documents that establish the issuer's financial
period, trajectory, management, and governance.  This module is transport
agnostic so it can be unit-tested without a browser and reused by every
source provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from .models import Document
from .report_accounting import report_document_type


TIER_0_IDENTITY = 0
TIER_1_CRITICAL_FINANCIAL = 1
TIER_2_GOVERNANCE_MANAGEMENT = 2
TIER_3_RESULTS_PRESENTATIONS = 3
TIER_4_REGULATORY_NEWS = 4
TIER_5_GENERAL_NEWS = 5
TIER_6_DEEP_ARCHIVE = 6


@dataclass(frozen=True)
class PriorityKey:
    tier: int
    pit_rank: int
    recency_rank: int
    class_rank: int
    canonical_url: str

    def as_tuple(self) -> tuple:
        return (self.tier, self.pit_rank, self.recency_rank, self.class_rank, self.canonical_url)


def _value(document: Document | Mapping, name: str, default=""):
    if isinstance(document, Document):
        return getattr(document, name, default)
    return document.get(name, default)


def _text(document: Document | Mapping) -> str:
    provenance = _value(document, "provenance", []) or []
    forms = " ".join(
        str(row.get("form_type") or row.get("sec_form") or "")
        for row in provenance
        if isinstance(row, Mapping)
    )
    return " ".join(
        str(_value(document, key, "") or "")
        for key in ("title", "description", "content_html", "source_url", "resolved_document_url", "document_type_hint", "source_family", "discovery_method")
    ) + " " + forms


def source_roles(document: Document | Mapping) -> frozenset[str]:
    """Return jurisdiction-neutral roles supported by a candidate."""
    text = _text(document).casefold()
    classified_type = report_document_type(document) if str(_value(document, "category", "") or "").casefold() == "reports" else ""
    roles: set[str] = set()
    if classified_type == "annual_report" or any(token in text for token in ("annual", "10-k", "10k", "20-f", "20f")):
        roles.add("ANNUAL_FINANCIAL_REPORT")
    if classified_type in {"interim_report", "quarterly_report"} or any(token in text for token in ("interim", "half-year", "half year", "quarterly", "10-q", "10q")):
        roles.add("INTERIM_FINANCIAL_REPORT")
    if classified_type == "trading_update" or any(token in text for token in ("trading update", "results release", "earnings release", "financial results", "preliminary results")):
        roles.add("CURRENT_RESULTS_UPDATE")
    if any(token in text for token in ("governance", "proxy", "agm", "board", "director", "remuneration", "shareholder meeting")):
        roles.add("GOVERNANCE_SOURCE")
    if any(token in text for token in ("management", "leadership", "executive", "officer", "director biography")):
        roles.add("MANAGEMENT_SOURCE")
    if any(token in text for token in ("presentation", "investor deck", "results deck", "slides")):
        roles.add("RESULTS_PRESENTATION")
    if any(token in text for token in ("regulatory", "8-k", "8k", "6-k", "6k")):
        roles.add("REGULATORY_UPDATE")
    return frozenset(roles)


def _document_date(document: Document | Mapping) -> date | None:
    raw = str(_value(document, "date", "") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def priority_tier(document: Document | Mapping) -> int:
    roles = source_roles(document)
    category = str(_value(document, "category", "") or "").casefold()
    if roles & {"ANNUAL_FINANCIAL_REPORT", "INTERIM_FINANCIAL_REPORT", "CURRENT_RESULTS_UPDATE"}:
        return TIER_1_CRITICAL_FINANCIAL
    if roles & {"GOVERNANCE_SOURCE", "MANAGEMENT_SOURCE"}:
        return TIER_2_GOVERNANCE_MANAGEMENT
    if "RESULTS_PRESENTATION" in roles:
        return TIER_3_RESULTS_PRESENTATIONS
    if "REGULATORY_UPDATE" in roles:
        return TIER_4_REGULATORY_NEWS
    if category == "news":
        return TIER_5_GENERAL_NEWS
    return TIER_6_DEEP_ARCHIVE


def priority_key(document: Document | Mapping, *, as_of_date: str | None = None) -> PriorityKey:
    cutoff = None
    if as_of_date:
        try:
            cutoff = date.fromisoformat(str(as_of_date)[:10])
        except ValueError:
            cutoff = None
    published = _document_date(document)
    if published is None:
        pit_rank = 1
        recency_rank = 0
    elif cutoff and published > cutoff:
        pit_rank = 2
        recency_rank = -published.toordinal()
    else:
        pit_rank = 0
        # Latest eligible document first.  Negating the ordinal keeps the
        # ordering deterministic while preferring the nearest PIT date.
        recency_rank = -published.toordinal()
    class_rank = 0 if source_roles(document) & {"ANNUAL_FINANCIAL_REPORT", "INTERIM_FINANCIAL_REPORT"} else 1
    url = str(_value(document, "resolved_document_url", "") or _value(document, "source_url", "") or "")
    return PriorityKey(priority_tier(document), pit_rank, recency_rank, class_rank, url.casefold())


def sort_documents(documents: Iterable[Document | Mapping], *, as_of_date: str | None = None) -> list:
    """Stable deterministic priority ordering for discovered candidates."""
    return sorted(list(documents or []), key=lambda row: priority_key(row, as_of_date=as_of_date).as_tuple())


def critical_source_coverage(documents: Iterable[Document | Mapping], *, as_of_date: str | None = None) -> dict:
    rows = list(documents or [])
    cutoff = None
    if as_of_date:
        try:
            cutoff = date.fromisoformat(str(as_of_date)[:10])
        except ValueError:
            cutoff = None
    usable = []
    for row in rows:
        if str(_value(row, "status", "DISCOVERED") or "DISCOVERED") not in {"DISCOVERED", "DOWNLOADED", "CACHED", "REUSED"}:
            continue
        published = _document_date(row)
        # Future-dated records must never establish current critical-source
        # coverage.  Unknown dates remain visible for acquisition diagnostics,
        # but a dated record is only eligible through the requested cutoff.
        if cutoff and published is not None and published > cutoff:
            continue
        usable.append(row)
    roles = [source_roles(row) for row in usable]
    annual = any("ANNUAL_FINANCIAL_REPORT" in value for value in roles)
    interim = any("INTERIM_FINANCIAL_REPORT" in value for value in roles)
    current = any("CURRENT_RESULTS_UPDATE" in value for value in roles)
    governance = any("GOVERNANCE_SOURCE" in value for value in roles)
    management = any("MANAGEMENT_SOURCE" in value for value in roles)
    financial = annual and (interim or current)
    return {
        "annual": annual,
        "interim": interim,
        "current_results": current,
        "governance": governance,
        "management": management,
        "financial": financial,
        "period": annual and interim,
        # A critical corpus is not research-ready until both governance and
        # management identity evidence are represented.  The role model is
        # jurisdiction-neutral, so this can be satisfied by a proxy, annual
        # report leadership section, board page, or equivalent source.
        "minimum_research_corpus_acquired": bool(financial and governance and management),
        "usable_documents": len(usable),
        "pit_ordered": [str(_value(row, "source_url", "")) for row in sort_documents(rows, as_of_date=as_of_date)],
    }


__all__ = [
    "PriorityKey",
    "TIER_0_IDENTITY",
    "TIER_1_CRITICAL_FINANCIAL",
    "TIER_2_GOVERNANCE_MANAGEMENT",
    "TIER_3_RESULTS_PRESENTATIONS",
    "TIER_4_REGULATORY_NEWS",
    "TIER_5_GENERAL_NEWS",
    "TIER_6_DEEP_ARCHIVE",
    "critical_source_coverage",
    "priority_key",
    "priority_tier",
    "sort_documents",
    "source_roles",
]
