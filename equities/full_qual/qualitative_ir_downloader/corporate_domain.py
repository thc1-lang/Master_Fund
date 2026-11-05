"""Deterministic corporate-domain candidate and verification primitives.

The acquisition layer must keep a legal SEC registrant, a public brand, and a
verified web identity separate.  This module is intentionally transport
agnostic: :mod:`ir_discovery` supplies DNS/HTTP/page evidence, while these
records preserve the reason a candidate was accepted or rejected.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

import tldextract

from .company_identity import domain_candidates, identify
from .urls import canonicalize_url, rejected_domain

_DOMAIN = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
IMPLEMENTATION_VERSION = "corporate-domain-resolution-v2"


@dataclass
class DomainCandidate:
    url: str
    domain: str
    source_type: str
    source_reference: str = ""
    confidence: str = "LOW"
    resolution_status: str = "UNTESTED"
    http_status: int | None = None
    redirect_target: str | None = None
    identity_match: bool = False
    ir_link_found: bool = False
    verification_status: str = "UNVERIFIED_GUESS"

    def record(self) -> dict[str, Any]:
        return asdict(self)


def _domain(url: str) -> str:
    return _DOMAIN(url).top_domain_under_public_suffix or ""


def candidate_from_url(url: str, *, source_type: str, source_reference: str = "", confidence: str = "LOW") -> DomainCandidate | None:
    canonical = canonicalize_url(url)
    if not canonical or rejected_domain(canonical) or not _domain(canonical):
        return None
    return DomainCandidate(canonical, _domain(canonical), source_type, source_reference, confidence)


def generated_candidates(company_name: str, *, corporate_brand: str | None = None) -> list[DomainCandidate]:
    """Generate brand-first candidates with legal-name guesses explicitly weak."""
    identity = identify(company_name)
    candidates: list[DomainCandidate] = []
    seen: set[str] = set()
    aliases = []
    if corporate_brand:
        aliases.append((corporate_brand, "corporate_brand"))
    aliases.extend((alias, "normalized_brand") for alias in identity.brand_candidates)
    for alias, source in aliases:
        for suffix in (".com", ".co.uk"):
            text = "".join(ch.lower() if ch.isalnum() else "-" for ch in alias).strip("-")
            if not text:
                continue
            candidate = candidate_from_url(f"https://{text}{suffix}/", source_type=source, source_reference=alias, confidence="MEDIUM" if source == "corporate_brand" else "LOW")
            if candidate and candidate.domain not in seen:
                seen.add(candidate.domain); candidates.append(candidate)
    # Preserve the legacy legal-name guess as an auditable last resort, but it
    # can never be promoted without independent page/domain verification.
    legal = "".join(ch.lower() for ch in company_name if ch.isalnum())
    candidate = candidate_from_url(f"https://{legal}.com/", source_type="legal_name_guess", source_reference=company_name, confidence="LOW") if legal else None
    if candidate and candidate.domain not in seen:
        candidates.append(candidate)
    return candidates


def classify_verified(candidate: DomainCandidate, *, identity_match: bool, ir_link_found: bool = False, redirect_target: str | None = None) -> DomainCandidate:
    candidate.identity_match = bool(identity_match)
    candidate.ir_link_found = bool(ir_link_found)
    candidate.redirect_target = redirect_target
    candidate.verification_status = "VERIFIED_CORPORATE" if identity_match else "REJECTED"
    if ir_link_found and identity_match:
        candidate.verification_status = "VERIFIED_IR"
    candidate.resolution_status = "VERIFIED" if identity_match else "REJECTED"
    return candidate


def deterministic_domain_order(candidates: Iterable[DomainCandidate]) -> list[DomainCandidate]:
    rank = {"VERIFIED_IR": 0, "VERIFIED_CORPORATE": 1, "PROBABLE_CORPORATE": 2, "UNVERIFIED_GUESS": 3, "REJECTED": 4}
    return sorted(candidates, key=lambda item: (rank.get(item.verification_status, 9), -int(item.identity_match), -int(item.ir_link_found), item.domain, item.url))


__all__ = ["DomainCandidate", "IMPLEMENTATION_VERSION", "candidate_from_url", "classify_verified", "deterministic_domain_order", "generated_candidates"]
