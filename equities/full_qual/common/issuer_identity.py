"""Pure canonical issuer-identity helpers.

This module intentionally has no imports from orchestration, acquisition,
Sheets, or research-output packages.  It is safe for either side of the
pipeline dependency graph to import.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


# SEC CIKs are numeric identifiers represented canonically as ten digits.  A
# zero-filled value is a common placeholder emitted by an incomplete lookup,
# not a registrant identity.  Keep this validation in the dependency-light
# common module so orchestration, acquisition, and SEC ingestion share the
# same fail-closed rule without importing one another.
CIK_INVALID_SENTINELS = frozenset({"", "0", "0000000000", "none", "null", "missing"})


def normalize_cik(value: object) -> str | None:
    """Return a canonical ten-digit CIK, or ``None`` for invalid input.

    Only plain decimal identifiers are accepted.  In particular, accession
    numbers, names, floating-point text, and the all-zero placeholder are not
    silently coerced into an issuer identity.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.casefold() in CIK_INVALID_SENTINELS:
        return None
    if not re.fullmatch(r"\d{1,10}", text):
        return None
    normalized = text.zfill(10)
    if normalized == "0000000000":
        return None
    return normalized


def is_valid_cik(value: object) -> bool:
    return normalize_cik(value) is not None


def canonical_identity_hash(
    *,
    ticker: str,
    exchange: str | None,
    cik: str | None,
    legal_name: str | None,
    lineage_version: str = "issuer-lineage-v1",
) -> str:
    """Return the deterministic identity fingerprint for a listed issuer."""
    normalized = unicodedata.normalize("NFKC", str(legal_name or "")).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    payload = "|".join((str(ticker).upper(), str(exchange or "").upper(), str(cik or ""), normalized, lineage_version))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["CIK_INVALID_SENTINELS", "canonical_identity_hash", "is_valid_cik", "normalize_cik"]
