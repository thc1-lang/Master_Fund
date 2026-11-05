"""Jurisdiction-neutral official-source roles for management and governance.

The role vocabulary is deliberately independent of a filing name.  A 20-F,
annual report, issuer governance page, AGM result or relevant 6-K can all be
the primary evidence for a foreign issuer; a DEF 14A remains one eligible
domestic source rather than a prerequisite.
"""

from __future__ import annotations

import re


MANAGEMENT_PRIMARY = "MANAGEMENT_PRIMARY"
BOARD_PRIMARY = "BOARD_PRIMARY"
GOVERNANCE_PRIMARY = "GOVERNANCE_PRIMARY"
GOVERNANCE_SUPPLEMENTAL = "GOVERNANCE_SUPPLEMENTAL"
REMUNERATION_PRIMARY = "REMUNERATION_PRIMARY"
AGM_PRIMARY = "AGM_PRIMARY"
ANNUAL_REPORT_PRIMARY = "ANNUAL_REPORT_PRIMARY"
REGULATORY_FILING_PRIMARY = "REGULATORY_FILING_PRIMARY"


def source_roles(*, form_type: str | None = None, title: str = "", text: str = "", document_type: str | None = None) -> frozenset[str]:
    form = str(form_type or "").upper().strip()
    value = " ".join((title or "", text[:12000] or "", document_type or "")).lower()
    roles: set[str] = set()
    annual_source = form in {"DEF 14A", "DEFA14A", "10-K", "20-F"} or "annual report" in value
    if annual_source:
        roles.update({ANNUAL_REPORT_PRIMARY, REGULATORY_FILING_PRIMARY})
    if form in {"DEF 14A", "DEFA14A"}:
        roles.update({MANAGEMENT_PRIMARY, BOARD_PRIMARY, GOVERNANCE_PRIMARY})
    if form in {"10-K", "20-F"} or (annual_source and not form):
        # Annual regulatory filings are authoritative roster and governance
        # sources even when flattened text omits the relevant headings from
        # its first page.  Do not make primary status depend on a keyword
        # surviving a lossy PDF/HTML extraction.
        roles.update({MANAGEMENT_PRIMARY, BOARD_PRIMARY, GOVERNANCE_PRIMARY})
    if form in {"10-Q", "6-K", "8-K", "8-K/A"}:
        roles.add(REGULATORY_FILING_PRIMARY)
        if re.search(r"annual general meeting|result of agm|board of directors|committee|governance", value, re.I):
            roles.update({BOARD_PRIMARY, GOVERNANCE_PRIMARY, AGM_PRIMARY})
        # A generic 6-K often contains boilerplate about "management" or
        # PDMR shareholdings.  Those words alone do not make it a primary
        # management roster.  Require an explicit appointment/departure or
        # executive-office statement instead.
        if re.search(r"appointed(?:\s+as)?|stepped down|resigned|retired|chief executive|chief financial|chief operating|chief technology|chief legal|executive committee", value, re.I):
            roles.add(MANAGEMENT_PRIMARY)
        if re.search(r"remuneration|compensation", value, re.I):
            roles.add(REMUNERATION_PRIMARY)
        if not (roles & {MANAGEMENT_PRIMARY, BOARD_PRIMARY, GOVERNANCE_PRIMARY, AGM_PRIMARY, REMUNERATION_PRIMARY}):
            roles.add(GOVERNANCE_SUPPLEMENTAL)
    return frozenset(roles)


def source_role(*, form_type: str | None = None, title: str = "", text: str = "", document_type: str | None = None) -> str:
    roles = source_roles(form_type=form_type, title=title, text=text, document_type=document_type)
    for preferred in (AGM_PRIMARY, BOARD_PRIMARY, MANAGEMENT_PRIMARY, GOVERNANCE_PRIMARY, ANNUAL_REPORT_PRIMARY, REGULATORY_FILING_PRIMARY, GOVERNANCE_SUPPLEMENTAL):
        if preferred in roles:
            return preferred
    return GOVERNANCE_SUPPLEMENTAL


__all__ = [
    "MANAGEMENT_PRIMARY", "BOARD_PRIMARY", "GOVERNANCE_PRIMARY", "GOVERNANCE_SUPPLEMENTAL",
    "REMUNERATION_PRIMARY", "AGM_PRIMARY", "ANNUAL_REPORT_PRIMARY", "REGULATORY_FILING_PRIMARY",
    "source_roles", "source_role",
]
