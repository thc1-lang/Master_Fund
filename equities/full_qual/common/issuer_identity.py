"""Pure canonical issuer-identity helpers.

This module intentionally has no imports from orchestration, acquisition,
Sheets, or research-output packages.  It is safe for either side of the
pipeline dependency graph to import.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


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


__all__ = ["canonical_identity_hash"]
