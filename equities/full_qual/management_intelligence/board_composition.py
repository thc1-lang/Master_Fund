"""Current board composition and leadership structure."""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from .board_governance_models import GovernanceBoardMembership
from .governance_sources import CachedGovernanceSource
from .models import BoardMembership, ManagementPerson
from .store import ManagementStore


def _as_of_current(row: BoardMembership, as_of_date: str) -> bool:
    if not row.is_current:
        return False
    if row.start_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.start_date) and row.start_date > as_of_date:
        return False
    if row.end_date and row.end_date <= as_of_date:
        return False
    return True


def _window(text: str, name: str, limit: int = 420) -> str:
    if not text or not name:
        return ""
    tokens = [token for token in re.split(r"\W+", name) if len(token) > 2]
    patterns = [re.escape(name), r"\b" + r"\s+".join(re.escape(token) for token in tokens) + r"\b"]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return text[max(0, match.start() - limit): match.end() + limit]
    return ""


def infer_independence(row: BoardMembership, person: ManagementPerson | None, proxy_text: str = "") -> tuple[str, str, str]:
    row_evidence = (row.evidence_text or "").lower()
    # Flattened proxy nominee tables retain an explicit Yes/No independence
    # column.  Prefer that fact over broad title heuristics.
    if re.search(r"\|\s*yes\s*\|", row_evidence):
        return "INDEPENDENT", "proxy nominee independence column", "proxy"
    if re.search(r"\|\s*no\s*\|", row_evidence):
        return "NOT_INDEPENDENT", "proxy nominee independence column", "proxy"
    issuer_name = (person.company_name if person else "") or ""
    issuer_context = bool(issuer_name and re.search(r"\b" + r"\s+".join(re.escape(token) for token in re.findall(r"[a-z0-9]+", issuer_name.lower()) if len(token) > 2) + r"\b", row_evidence))
    issuer_executive = issuer_context and bool(re.search(r"\b(?:chief executive officer|ceo|president|chief financial officer|cfo)\b", row_evidence))
    unqualified_ceo_title = bool(re.search(r"\b(?:chief executive officer|ceo)\s+and\s+director\b", row_evidence))
    if issuer_executive or unqualified_ceo_title or ("co-founder" in row_evidence and issuer_context) or ("executive chair" in row_evidence and issuer_context):
        return "NOT_INDEPENDENT", "executive/founder relationship disclosed", "proxy"
    if "not independent" in row_evidence:
        return "NOT_INDEPENDENT", "issuer explicitly states not independent", "proxy"
    if re.search(r"\bindependent\s+(?:chair|director)\b", row_evidence) or re.search(r"\bindependent\b", row_evidence):
        return "INDEPENDENT", "issuer explicitly states independent", "proxy"
    if proxy_text and person:
        # Explicit issuer exceptions must take precedence over broad
        # independence summaries.  A flattened proxy often lists the
        # independent nominees first and later states that the CEO/founder is
        # not deemed independent; searching only for the first "independent"
        # sentence would misclassify that person.
        last_name = re.findall(r"[A-Za-z]{3,}", person.full_name)[-1] if person.full_name else ""
        for match in re.finditer(r"(?:not\s+deemed\s+independent|not\s+independent)", proxy_text, re.IGNORECASE):
            section = proxy_text[max(0, match.start() - 900):match.end() + 900]
            if last_name and re.search(r"\b" + re.escape(last_name) + r"\b", section, re.IGNORECASE):
                return "NOT_INDEPENDENT", "issuer explicitly states not independent", "proxy"
        # Do not apply a broad "most directors are independent" sentence to
        # every nominee: flattened proxies often place the full roster in the
        # same text window, which would misclassify founders and non-executive
        # controlling shareholders.  Exact row evidence above and a bounded
        # name-specific statement are the only safe positive conclusions.
        for match in re.finditer(r"(?:board of directors has determined|board has determined)", proxy_text, re.IGNORECASE):
            section = proxy_text[match.start():match.start() + 2600]
            name_match = re.search(
                rf"\b{re.escape(last_name)}\b[^.\n]{{0,180}}\b(?:is|are)\s+independent\b|"
                rf"\bindependent\b[^.\n]{{0,180}}\b{re.escape(last_name)}\b",
                section,
                re.IGNORECASE,
            )
            if name_match:
                return "INDEPENDENT", "issuer independence determination", "proxy"
    return "UNKNOWN", "unknown", "unknown"


def _age(evidence: str) -> int | None:
    match = re.search(r"\|\s*(\d{2})\s*\|", evidence or "")
    return int(match.group(1)) if match else None


def build_board_memberships(store: ManagementStore, ticker: str, *, as_of_date: str, proxy: CachedGovernanceSource | None = None) -> list[GovernanceBoardMembership]:
    ticker = ticker.strip().upper()
    people = {person.person_id: person for person in store.list_people()}
    rows = [row for row in store.list_boards() if (row.board_ticker or row.ticker).upper() == ticker and _as_of_current(row, as_of_date)]
    result: list[GovernanceBoardMembership] = []
    for row in rows:
        person = people.get(row.person_id)
        independence, basis, independence_source = infer_independence(row, person, proxy.text if proxy else "")
        evidence = row.evidence_text or (person.biography if person else "")
        role = (row.board_role or "director").lower()
        chair_status = None
        if "chair" in role:
            chair_status = "chair"
        elif person and any("chair" in item.lower() for item in person.current_roles):
            chair_status = "chair"
        result.append(GovernanceBoardMembership(
            board_membership_id=f"governance-board:{ticker}:{row.person_id}",
            person_id=row.person_id,
            ticker=ticker,
            issuer_cik=row.issuer_cik,
            board_role=row.board_role,
            is_current=True,
            start_date=row.start_date,
            end_date=row.end_date,
            date_precision=row.date_precision,
            is_independent=independence,
            independence_basis=basis,
            independence_source=independence_source,
            chair_status=chair_status,
            lead_independent_status="lead independent" in role.lower() or (person is not None and any("lead independent" in item.lower() for item in person.current_roles)),
            age=_age(row.evidence_text),
            ownership_link={"insider_person_ids": list(person.insider_person_ids), "reporting_owner_ciks": list(person.reporting_owner_ciks)} if person else {},
            source_url=row.source_url,
            source_accession=row.accession_number,
            source_form=row.form_type,
            source_date=(proxy.filing_date if proxy and proxy.accession_number == row.accession_number else None),
            local_source_path=row.local_source_path,
            evidence_text=evidence,
        ))
    return sorted({item.board_membership_id: item for item in result}.values(), key=lambda item: item.person_id)


def leadership_structure(memberships: Iterable[GovernanceBoardMembership], roles: Iterable, people: Iterable[ManagementPerson] = ()) -> dict[str, object]:
    members = list(memberships)
    role_rows = list(roles)
    by_id = {person.person_id: person for person in people}
    chairs = [item for item in members if item.chair_status == "chair" or "chair" in item.board_role.lower()]
    chair = chairs[0] if chairs else None
    ceo_ids = {item.person_id for item in role_rows if getattr(item, "is_current", False) and "chief executive" in (getattr(item, "role", "") or "").lower()}
    ceo_is_chair = bool(chair and chair.person_id in ceo_ids)
    lead = next((item for item in members if item.lead_independent_status), None)
    chair_independent = chair.is_independent if chair else "UNKNOWN"
    if ceo_is_chair and lead:
        structure = "CEO_CHAIR_WITH_LEAD_INDEPENDENT_DIRECTOR"
    elif ceo_is_chair:
        structure = "CEO_AND_CHAIR_COMBINED"
    elif chair and chair_independent == "INDEPENDENT":
        structure = "SEPARATE_INDEPENDENT_CHAIR"
    elif chair:
        structure = "SEPARATE_NONINDEPENDENT_CHAIR"
    else:
        structure = "UNKNOWN"
    return {
        "chair_person_id": chair.person_id if chair else None,
        "chair_is_independent": chair_independent,
        "ceo_is_chair": ceo_is_chair if chair else None,
        "lead_independent_director_person_id": lead.person_id if lead else None,
        "chair_structure": structure,
    }


__all__ = ["build_board_memberships", "infer_independence", "leadership_structure"]
