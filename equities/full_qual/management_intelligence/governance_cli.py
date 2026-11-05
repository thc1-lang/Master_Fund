"""CLI and orchestration for Component 6C Board & Governance Intelligence."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Iterable

from .board_committees import extract_committees
from .board_composition import build_board_memberships, leadership_structure
from .board_expertise import build_expertise, expertise_counts
from .board_governance_models import (
    GovernanceCoverage, GovernanceEvent, GovernanceRun, GovernanceSnapshot,
)
from .board_tenure import calculate_tenure, refreshment, summarize_tenure
from .governance_sources import GovernanceSourceCatalog
from .governance_store import GovernanceStore
from .main import ManagementIntelligenceProvider
from .sec_sources import SECManagementSourceProvider
from insider_intelligence.sec_ingestion import SECSourceProvider
from .overboarding import build_overboarding
from .people import normalize_name
from .related_parties import extract_related_parties
from .shareholder_rights import extract_shareholder_rights
from .store import ManagementStore
from .voting_control import extract_voting_structure


def _status(available: int, known: int, *, complete_when_all: bool = True) -> str:
    if available == 0:
        return "INSUFFICIENT"
    if complete_when_all and known >= available:
        return "COMPLETE"
    return "PARTIAL"


def _related_diagnostics(related: list, statuses: dict[str, str]) -> dict[str, str]:
    """Separate an explicit negative disclosure from an empty parse result."""
    diagnostics: dict[str, str] = {}
    for key, value in statuses.items():
        if value == "NO":
            diagnostics[key] = "EXPLICIT_NONE"
        elif value == "UNKNOWN":
            diagnostics[key] = "UNKNOWN"
        elif any(
            (key == "family" and item.relationship_type == "family_relationship")
            or (key == "interlocks" and item.relationship_type == "interlocking_relationship")
            or (key == "related_party" and item.relationship_type in {"related_party_transaction", "consulting_relationship"})
            for item in related
        ):
            diagnostics[key] = "POSITIVE"
        else:
            diagnostics[key] = "NO_PARSEABLE_RECORDS"
    return diagnostics


def _governance_events(store: ManagementStore, ticker: str, catalog: GovernanceSourceCatalog) -> list[GovernanceEvent]:
    result: list[GovernanceEvent] = []
    cached = {source.accession_number: source for source in catalog.sources(ticker)}
    for event in store.list_role_changes(ticker):
        source = cached.get(event.accession_number or "")
        governance_event = GovernanceEvent(
            event_id=f"governance-event:{ticker.upper()}:{event.event_id}", ticker=ticker.upper(), event_type="director_appointment" if event.event_type == "appointment" else "director_departure" if event.event_type in {"departure", "resignation", "retirement"} else "governance_policy_change",
            effective_date=event.effective_date, person_id=event.person_id, description=event.person_name or event.role or "",
            source_url=event.source_url or (source.source_url if source else None), source_accession=event.accession_number,
            source_form=event.form_type, source_date=event.filing_date, local_source_path=event.local_source_path,
            evidence_text=event.evidence_text,
        )
        result.append(governance_event)
        committee_match = re.search(r"((?:audit|compensation|nominating|governance|executive|finance|risk|cybersecurity|technology|science)[^.]{0,80}committee)", event.evidence_text or "", re.IGNORECASE)
        if committee_match:
            committee_name = re.sub(r"\s+", " ", committee_match.group(1)).strip()
            change = "appointed to" if event.event_type == "appointment" else "departed from" if event.event_type in {"departure", "resignation", "retirement"} else "referenced for"
            result.append(GovernanceEvent(
                event_id=f"governance-event:{ticker.upper()}:{event.event_id}:committee:{re.sub(r'[^a-z0-9]+', '-', committee_name.lower()).strip('-')}",
                ticker=ticker.upper(), event_type="committee_change", effective_date=event.effective_date,
                person_id=event.person_id,
                description=f"{event.person_name or event.person_id} {change} {committee_name}; historical and current membership are retained as source evidence",
                source_url=event.source_url or (source.source_url if source else None), source_accession=event.accession_number,
                source_form=event.form_type, source_date=event.filing_date, local_source_path=event.local_source_path,
                evidence_text=event.evidence_text,
            ))
    return sorted({item.event_id: item for item in result}.values(), key=lambda item: item.event_id)


def _coverage(ticker: str, as_of_date: str, *, board, committees, expertise, overboarding, related_status, classes, rights, warnings) -> GovernanceCoverage:
    known_independence = sum(item.is_independent != "UNKNOWN" for item in board)
    related_diagnostics = _related_diagnostics(related_status.get("_records", []), {key: value for key, value in related_status.items() if key != "_records"})
    expertise_diagnostics = {"regulatory": "REGULATORY_EXPERTISE_IDENTIFIED" if any("regulatory" in item.categories for item in expertise) else "REGULATORY_EXPERTISE_NOT_IDENTIFIED"}
    related_known = sum(value in {"EXPLICIT_NONE", "POSITIVE"} for value in related_diagnostics.values())
    coverage = GovernanceCoverage(
        coverage_id=f"governance-coverage:{ticker.upper()}:{as_of_date}", ticker=ticker.upper(), as_of_date=as_of_date,
        board_composition="COMPLETE" if board else "INSUFFICIENT",
        independence=_status(len(board), known_independence),
        tenure="COMPLETE" if board and all(item.start_date for item in board) else "PARTIAL" if board else "INSUFFICIENT",
        committees="COMPLETE" if committees and all(item.members for item in committees) else "PARTIAL" if committees else "INSUFFICIENT",
        expertise="COMPLETE" if expertise and all(item.categories for item in expertise) else "PARTIAL" if expertise else "INSUFFICIENT",
        outside_boards="COMPLETE" if overboarding and all(item.current_public_company_board_count is not None for item in overboarding) else "PARTIAL" if overboarding else "INSUFFICIENT",
        related_parties="COMPLETE" if related_diagnostics and related_known == len(related_diagnostics) else "PARTIAL" if related_known else "UNKNOWN",
        voting_structure="COMPLETE" if classes and all(item.votes_per_share is not None for item in classes) else "PARTIAL" if classes else "INSUFFICIENT",
        shareholder_rights=("COMPLETE" if rights and all(getattr(rights, field_name) != "UNKNOWN" for field_name in ("classified_board_status", "director_election_standard", "special_meeting_right", "written_consent_right", "proxy_access", "advance_notice_requirements", "cumulative_voting", "supermajority_requirements")) else "PARTIAL" if rights else "INSUFFICIENT"),
        related_party_diagnostics=related_diagnostics,
        expertise_diagnostics=expertise_diagnostics,
        rights_source_coverage=rights.source_coverage if rights else "UNKNOWN",
        limitations=list(warnings),
    )
    return coverage


def build_governance(
    ticker: str,
    *,
    as_of_date: str = "2026-09-24",
    store_root: str | Path = "artifacts/management_intelligence",
    sec_cache_root: str | Path = "artifacts/insider_intelligence/sec",
    normalized_root: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    del force  # 6C is cache-only by design; callers can refresh 6A separately.
    ticker = ticker.strip().upper()
    store = ManagementStore(store_root)
    governance_store = GovernanceStore(store_root)
    catalog = GovernanceSourceCatalog(store_root=store_root, sec_cache_root=sec_cache_root, normalized_root=normalized_root, as_of_date=as_of_date)
    # ``proxy`` is retained as a compatibility variable for the existing
    # governance parsers.  For foreign issuers the primary source may be a
    # 20-F, annual report or AGM-result 6-K.
    proxy = catalog.latest_primary(ticker) or catalog.latest_proxy(ticker)
    warnings: list[str] = []
    if proxy is None:
        warnings.append("NO_ELIGIBLE_SOURCE")
    if not any(re.search(r"charter|bylaw|exhibit", source.local_path or "", re.IGNORECASE) for source in catalog.sources(ticker)):
        warnings.append("cached charter/bylaw exhibits unavailable; rights remain proxy-limited")
    # A normalized SEC proxy is an authoritative source handoff.  If 6A has
    # not run yet, hydrate the shared management store through the same parser
    # rather than requiring a coincidental second SEC cache.
    if proxy is not None and not store.list_people(ticker) and normalized_root:
        provider = ManagementIntelligenceProvider(
            store=store,
            sec=SECManagementSourceProvider(
                SECSourceProvider(cache_root=sec_cache_root),
                normalized_root=normalized_root,
            ),
        )
        parsed = provider.acquire(ticker, as_of_date=as_of_date)
        if not parsed.people:
            warnings.append("PROXY_PARSE_FAILED")
        store = ManagementStore(store_root)
    board = build_board_memberships(store, ticker, as_of_date=as_of_date, proxy=proxy)
    if proxy is not None and not board:
        warnings.append("PROXY_PARSE_FAILED")
        warnings.append("GOVERNANCE_EXTRACTION_FAILED")
    cached_sources = {source.accession_number: source for source in catalog.sources(ticker)}
    for member in board:
        source = cached_sources.get(member.source_accession or "")
        if source is not None:
            member.source_date = member.source_date or source.filing_date
        member.public_availability_date = member.public_availability_date or member.source_date or (proxy.available_date if proxy else None) or (proxy.filing_date if proxy else None)
    # A current annual/20-F roster is a point-in-time snapshot.  Later AGM
    # results and other official issuer filings can change that roster before
    # the requested cutoff.  Reconcile those generic role-change events before
    # committees and leadership are derived; retain the historical source rows
    # in the management store rather than deleting them.
    people_by_id = {person.person_id: person for person in store.list_people()}
    role_changes = store.list_role_changes(ticker)
    for member in board:
        member_name = normalize_name(people_by_id.get(member.person_id).full_name) if people_by_id.get(member.person_id) else ""
        member_tokens = set(member_name.split())
        for change in role_changes:
            if change.effective_date and change.effective_date > as_of_date:
                continue
            change_name = normalize_name(change.person_name or "")
            change_tokens = set(change_name.split())
            name_match = bool(change_name and member_name and (change_name == member_name or (len(change_tokens) >= 2 and change_tokens <= member_tokens)))
            matches = bool(change.person_id and change.person_id == member.person_id) or name_match
            if not matches:
                continue
            if change.event_type in {"departure", "resignation", "retirement"}:
                member.is_current = False
                member.board_status = "HISTORICAL"
                member.end_date = member.end_date or change.effective_date
            elif change.event_type == "appointment":
                member.is_current = True
                member.board_status = "CURRENT"
    board = [member for member in board if member.is_current]
    people = store.list_people()
    roles = store.list_roles(ticker)
    committees = extract_committees(ticker, proxy, people, board)
    # Every governance record retains the normalized SEC document identity in
    # addition to accession/source URL.  This keeps the routing chain
    # auditable even when multiple cached representations share an accession.
    if proxy is not None:
        for committee in committees:
            committee.normalized_document_id = proxy.normalized_document_id
            committee.source_filed_at = proxy.filing_date
            committee.source_period = proxy.report_date
            committee.as_of_eligibility = "ELIGIBLE" if not proxy.filing_date or proxy.filing_date <= as_of_date else "INELIGIBLE_FUTURE"
    if proxy is not None:
        proxy_text = proxy.text or ""
        has_independence_language = bool(re.search(r"independent\s+(?:director|nominee)|independence", proxy_text, re.I))
        # A committee heading alone is not committee extraction.  Flattened
        # proxy tables may produce named committee shells while losing the
        # member grid; retain the release blocker until membership evidence is
        # actually captured.
        if not committees or any(item.membership_status == "EXTRACTION_FAILED" or (not item.members and item.source_status != "SOURCE_COVERAGE_INCOMPLETE") for item in committees):
            warnings.append("CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE")
        elif any(item.source_status == "SOURCE_COVERAGE_INCOMPLETE" for item in committees):
            warnings.append("SOURCE_COVERAGE_INCOMPLETE")
        if has_independence_language and board and all(item.is_independent == "UNKNOWN" for item in board):
            warnings.append("CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE")
        if committees and not board:
            warnings.append("GOVERNANCE_INTERNAL_INCONSISTENCY")
    current_ids = {item.person_id for item in board}
    # Proxy committee tables are historical to the proxy date.  Apply later
    # 8-K departures and appointments without deleting the historical source.
    for committee in committees:
        committee.public_availability_date = committee.public_availability_date or committee.source_date or (proxy.available_date if proxy else None) or (proxy.filing_date if proxy else None)
        committee.members = [person_id for person_id in committee.members if person_id in current_ids]
        committee.member_independence = {person_id: value for person_id, value in committee.member_independence.items() if person_id in current_ids}
        committee.financial_expert_person_ids = [person_id for person_id in committee.financial_expert_person_ids if person_id in current_ids]
        committee.financial_background = {person_id: value for person_id, value in committee.financial_background.items() if person_id in current_ids}
    for role_change in role_changes:
        if role_change.event_type == "appointment" and role_change.person_id and role_change.person_id in current_ids and re.search(r"audit committee", role_change.evidence_text or "", re.IGNORECASE):
            audit = next((item for item in committees if item.committee_type == "audit"), None)
            if audit and role_change.person_id not in audit.members:
                audit.members.append(role_change.person_id)
                audit.members.sort()
                audit.member_independence[role_change.person_id] = next((item.is_independent for item in board if item.person_id == role_change.person_id), "UNKNOWN")
    committee_by_person: dict[str, list[str]] = {}
    for committee in committees:
        for person_id in committee.members:
            committee_by_person.setdefault(person_id, []).append(committee.committee_name)
    for member in board:
        member.committee_memberships = sorted(set(committee_by_person.get(member.person_id, [])))
    events = _governance_events(store, ticker, catalog)
    tenure = calculate_tenure(board, as_of_date=as_of_date)
    refresh = refreshment(ticker, board, store.list_role_changes(ticker), as_of_date=as_of_date)
    expertise = build_expertise(board, store.list_career(ticker))
    overboarding = build_overboarding(ticker, board, store.list_boards(), people, roles, as_of_date=as_of_date)
    related, related_status = extract_related_parties(ticker, proxy, people)
    classes, voting = extract_voting_structure(ticker, proxy)
    rights = extract_shareholder_rights(ticker, proxy)
    leadership = leadership_structure(board, roles, people)
    tenure_summary = summarize_tenure(tenure)
    related_status_with_records = dict(related_status)
    related_status_with_records["_records"] = related
    coverage = _coverage(ticker, as_of_date, board=board, committees=committees, expertise=expertise, overboarding=overboarding, related_status=related_status_with_records, classes=classes, rights=rights, warnings=warnings)
    eligible_proxies = catalog.primary_sources(ticker)
    candidate_rows = [
        row for row in store.list_boards(ticker)
        if (row.board_ticker or row.ticker).upper() == ticker
    ]
    committee_failures = sum(
        item.membership_status == "EXTRACTION_FAILED" or item.source_status == "EXTRACTION_FAILED"
        for item in committees
    )
    linked_members = len({person_id for item in committees for person_id in item.members})
    known_independence = sum(item.is_independent != "UNKNOWN" for item in board)
    # This is deliberately a coverage score, not an assessment of governance
    # quality.  Missing optional fields reduce completeness without becoming a
    # critical extraction failure once the board roster is validated.
    completeness_score = (
        (40 if board else 0)
        + (20 if committees else 0)
        + (15 if linked_members else 0)
        + (15 if known_independence else 0)
        + (10 if board and all(item.start_date for item in board) else 0)
    )
    coverage.proxy_sources_eligible = len(eligible_proxies)
    coverage.board_member_candidates = len(candidate_rows)
    coverage.board_members_validated = len(board)
    coverage.board_members_rejected = max(0, len(candidate_rows) - len(board))
    coverage.committees_found = len(committees)
    coverage.committee_members_linked = linked_members
    coverage.committee_member_extraction_failures = committee_failures
    coverage.current_directors = len(board)
    coverage.historical_directors = sum(not row.is_current for row in candidate_rows)
    coverage.nominees = 0
    coverage.independence_status_known = known_independence
    coverage.governance_completeness_score = completeness_score
    governance_diagnostics = {
        "GOVERNANCE_PRIMARY_SOURCES": len(eligible_proxies),
        "PROXY_SOURCES_ELIGIBLE": len(eligible_proxies),
        "BOARD_MEMBER_CANDIDATES": len(candidate_rows),
        "BOARD_MEMBERS_VALIDATED": len(board),
        "BOARD_MEMBERS_REJECTED": max(0, len(candidate_rows) - len(board)),
        "INDEPENDENCE_KNOWN": known_independence,
        "INDEPENDENCE_UNKNOWN": sum(item.is_independent == "UNKNOWN" for item in board),
        "COMMITTEES_FOUND": len(committees),
        "COMMITTEE_MEMBERS_LINKED": linked_members,
        "COMMITTEE_MEMBER_EXTRACTION_FAILURES": committee_failures,
        "CURRENT_DIRECTORS": len(board),
        "HISTORICAL_DIRECTORS": sum(not row.is_current for row in candidate_rows),
        "NOMINEES": 0,
        "INDEPENDENCE_STATUS_KNOWN": known_independence,
        "GOVERNANCE_COMPLETENESS_SCORE": completeness_score,
        "CHAIR_STRUCTURE_STATUS": leadership["chair_structure"],
    }
    independent_count = sum(item.is_independent == "INDEPENDENT" for item in board)
    known_status_count = sum(item.is_independent != "UNKNOWN" for item in board)
    independence_failed = bool(board) and known_status_count == 0
    tenure_statistics_precision = str(tenure_summary.get("statistics_precision", "UNKNOWN"))
    snapshot = GovernanceSnapshot(
        snapshot_id=f"governance-snapshot:{ticker}:{as_of_date}", ticker=ticker, as_of_date=as_of_date,
        board_size=len(board) if board else None, independent_count=None if (independence_failed or not board) else independent_count,
        independent_denominator=None if independence_failed else (len(board) if board else None),
        known_independence_count=None if independence_failed else known_status_count,
        known_independence_denominator=None if independence_failed else known_status_count,
        known_status_independent_percentage=None if independence_failed else (round(100 * independent_count / known_status_count, 2) if known_status_count else None),
        independent_percentage=None if independence_failed else (round(100 * independent_count / len(board), 2) if board else None),
        unknown_independence=sum(item.is_independent == "UNKNOWN" for item in board), chair_structure=str(leadership["chair_structure"]),
        chair_person_id=leadership["chair_person_id"], chair_is_independent=str(leadership["chair_is_independent"]), ceo_is_chair=leadership["ceo_is_chair"],
        lead_independent_director=leadership["lead_independent_director_person_id"], average_tenure=tenure_summary["average"], median_tenure=tenure_summary["median"], tenure_statistics_precision=tenure_statistics_precision,
        tenure_buckets=tenure_summary["buckets"], committee_structure=[item.committee_name for item in committees],
        board_membership_ids=[item.board_membership_id for item in board],
        committee_ids=[item.committee_id for item in committees], tenure_ids=[item.tenure_id for item in tenure],
        expertise_ids=[item.expertise_id for item in expertise], overboarding_ids=[item.overboarding_id for item in overboarding],
        related_party_ids=[item.relationship_id for item in related], voting_class_ids=[item.voting_class_id for item in classes],
        event_ids=[item.event_id for item in events],
        public_board_counts={item.person_id: item.current_public_company_board_count for item in overboarding}, related_party_relationship_count=len(related),
        share_class_structure=voting.share_class_structure, founder_voting_control=voting.founder_control,
        classified_board_status=rights.classified_board_status,
        shareholder_rights={
            "director_election_standard": rights.director_election_standard,
            "contested_election_standard": rights.contested_election_standard,
            "uncontested_election_standard": rights.uncontested_election_standard,
            "resignation_policy": rights.resignation_policy,
            "special_meeting": rights.special_meeting_right,
            "written_consent": rights.written_consent_right,
            "proxy_access": rights.proxy_access,
            "advance_notice": rights.advance_notice_requirements,
            "cumulative_voting": rights.cumulative_voting,
            "supermajority": rights.supermajority_requirements,
        },
        shareholder_rights_id=rights.rights_id,
        coverage_id=coverage.coverage_id,
        created_at=as_of_date,
        independence_status="EXTRACTION_FAILED" if independence_failed else "AVAILABLE" if board else "UNKNOWN",
        public_availability_date=(proxy.available_date if proxy else None) or (proxy.filing_date if proxy else None),
    )
    records: dict[str, list] = {
        "governance_board_memberships": board, "governance_committees": committees,
        "governance_tenure": tenure, "governance_refreshment": [refresh], "governance_expertise": expertise,
        "governance_overboarding": overboarding, "governance_related_parties": related,
        "governance_voting_classes": classes, "governance_voting_control": [voting],
        "governance_shareholder_rights": [rights], "governance_events": events,
        "governance_coverage": [coverage], "governance_snapshots": [snapshot],
    }
    counts = {} if dry_run else governance_store.replace_ticker_transaction(ticker, records, run_id=f"governance-run:{ticker}:{as_of_date}")
    run = GovernanceRun(run_id=f"governance-run:{ticker}:{as_of_date}", tickers=[ticker], as_of_date=as_of_date, records={name: len(values) for name, values in records.items()}, warnings=warnings, diagnostics=governance_diagnostics, network_requests=catalog.network_requests, created_at=as_of_date)
    if not dry_run:
        governance_store.record_run(run)
    status = "GOVERNANCE_INSUFFICIENT"
    if proxy is not None and any(code in warnings for code in {"PROXY_PARSE_FAILED", "GOVERNANCE_EXTRACTION_FAILED", "CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE", "GOVERNANCE_INTERNAL_INCONSISTENCY"}):
        status = "GOVERNANCE_EXTRACTION_FAILED"
    elif board and completeness_score >= 70:
        status = "GOVERNANCE_COMPLETE"
    elif board:
        status = "GOVERNANCE_PARTIAL"
    elif proxy is None:
        status = "GOVERNANCE_SOURCE_UNAVAILABLE"
    return {"ticker": ticker, "proxy": proxy, "board": board, "committees": committees, "tenure": tenure, "refreshment": refresh, "expertise": expertise, "overboarding": overboarding, "related": related, "related_status": related_status, "classes": classes, "voting": voting, "rights": rights, "events": events, "coverage": coverage, "snapshot": snapshot, "counts": counts, "run": run, "warnings": warnings, "diagnostics": governance_diagnostics, "status": status}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build factual SEC-backed board and governance intelligence")
    parser.add_argument("ticker", nargs="*", help="issuer ticker(s)")
    parser.add_argument("--ticker", dest="ticker_flags", action="append", default=[], help="issuer ticker (repeatable)")
    parser.add_argument("--as-of-date", default="2026-09-24")
    parser.add_argument("--store-root", default="artifacts/management_intelligence")
    parser.add_argument("--sec-cache-root", default="artifacts/insider_intelligence/sec")
    parser.add_argument("--normalized-root", default=None, help="normalized SEC document store used as the authoritative source handoff")
    parser.add_argument("--force", action="store_true", help="reserved for an explicit upstream cache refresh")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tickers = [*args.ticker, *args.ticker_flags]
    if not tickers:
        build_parser().error("at least one ticker is required")
    for ticker in tickers:
        result = build_governance(ticker, as_of_date=args.as_of_date, store_root=args.store_root, sec_cache_root=args.sec_cache_root, normalized_root=args.normalized_root, force=args.force, dry_run=args.dry_run)
        snapshot = result["snapshot"]
        coverage = result["coverage"]
        print(json.dumps({"ticker": ticker.upper(), "status": result["status"], "board_size": snapshot.board_size, "independent_directors": snapshot.independent_count, "independent_percentage": snapshot.independent_percentage, "chair_structure": snapshot.chair_structure, "committees": len(result["committees"]), "average_tenure": snapshot.average_tenure, "median_tenure": snapshot.median_tenure, "share_class_structure": snapshot.share_class_structure, "coverage": coverage.to_dict(), "diagnostics": result.get("diagnostics", {}), "warnings": result["warnings"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_governance", "build_parser", "main"]
