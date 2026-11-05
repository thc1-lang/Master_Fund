"""Generic listed-security to SEC-registrant lineage primitives.

This module is intentionally independent of any issuer name or CIK.  It is
used by resolution and corpus gates to keep name changes, affiliates, and
corporate-action parties distinct from the security's canonical registrant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable


@dataclass(frozen=True)
class LineageEdge:
    source_cik: str
    target_cik: str
    relation: str
    effective_date: str | None = None
    security_transfer: bool = False
    evidence: str | None = None

    def active_as_of(self, as_of: str | None) -> bool:
        if not as_of or not self.effective_date:
            return True
        return self.effective_date <= as_of


@dataclass(frozen=True)
class RegistrantCandidate:
    cik: str
    legal_name: str
    ticker: str | None = None
    exchange: str | None = None
    security_title: str | None = None
    security_evidence: bool = False
    source: str = "unknown"


@dataclass(frozen=True)
class CanonicalSecurityIdentity:
    requested_ticker: str
    exchange: str | None
    security_title: str | None
    sec_registrant_cik: str
    sec_registrant_name: str
    historical_registrant_names: tuple[str, ...] = ()
    successor_predecessor_links: tuple[LineageEdge, ...] = ()
    corporate_action_status: str = "UNCHANGED"
    effective_date: str | None = None
    source: str = "unknown"
    confidence: str = "MEDIUM"
    lineage_version: str = "issuer-lineage-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_ticker": self.requested_ticker,
            "exchange": self.exchange,
            "security_title": self.security_title,
            "sec_registrant_cik": self.sec_registrant_cik,
            "sec_registrant_name": self.sec_registrant_name,
            "legal_name": self.sec_registrant_name,
            "historical_registrant_names": list(self.historical_registrant_names),
            "successor_predecessor_links": [edge.__dict__ for edge in self.successor_predecessor_links],
            "corporate_action_status": self.corporate_action_status,
            "effective_date": self.effective_date,
            "source": self.source,
            "confidence": self.confidence,
            "lineage_version": self.lineage_version,
        }


def relation_for_source(source_cik: str | None, canonical_cik: str, *, edges: Iterable[LineageEdge] = (), as_of: str | None = None) -> tuple[str, bool]:
    """Return a conservative relation/eligibility pair for corpus evidence."""
    source = str(source_cik or "").zfill(10)
    canonical = str(canonical_cik or "").zfill(10)
    if not source or not canonical:
        return "UNKNOWN", True
    if source == canonical:
        return "CANONICAL_REGISTRANT", True
    for edge in edges:
        if not edge.active_as_of(as_of):
            continue
        if edge.source_cik.zfill(10) == source and edge.target_cik.zfill(10) == canonical:
            if edge.relation.upper() in {"HISTORICAL_REGISTRANT", "PREDECESSOR"} and edge.security_transfer:
                return edge.relation.upper(), True
            return edge.relation.upper(), False
        if edge.target_cik.zfill(10) == source and edge.source_cik.zfill(10) == canonical:
            reverse = "PREDECESSOR" if edge.relation.upper() == "SUCCESSOR" else edge.relation.upper()
            return reverse, bool(edge.security_transfer and reverse in {"PREDECESSOR", "HISTORICAL_REGISTRANT"})
    return "AFFILIATE", False


def choose_security_registrant(candidates: Iterable[RegistrantCandidate], ticker: str, *, exchange: str | None = None, as_of: str | None = None) -> RegistrantCandidate:
    """Choose only a candidate with security-level ticker evidence when available."""
    values = list(candidates)
    symbol = ticker.upper()
    exact = [item for item in values if item.security_evidence and (not item.ticker or item.ticker.upper() == symbol) and (not exchange or not item.exchange or item.exchange.upper() == exchange.upper())]
    if exact:
        return exact[0]
    ticker_matches = [item for item in values if item.ticker and item.ticker.upper() == symbol]
    if ticker_matches:
        return ticker_matches[0]
    if not values:
        raise LookupError(f"No registrant candidate for ticker {ticker}")
    raise LookupError(f"No security-level registrant evidence for ticker {ticker}")


__all__ = ["LineageEdge", "RegistrantCandidate", "CanonicalSecurityIdentity", "relation_for_source", "choose_security_registrant"]
