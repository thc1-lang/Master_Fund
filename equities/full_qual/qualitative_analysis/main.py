"""Command-line entry point for offline normalization and extraction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .document_store import DocumentStore
from .extraction_store import ExtractionStore
from .ingestion import ingest
from .qualitative_extraction import DeterministicExtractionProvider, extract_document
from .temporal_comparison import DeterministicTemporalComparisonProvider, claims_available_as_of, compare_latest_vs_previous, eligible_observed_claims
from .temporal_store import TemporalStore
from .state_store import StateStore
from .state_synthesis import DeterministicStateSynthesisProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize acquired IR documents into the qualitative-analysis store")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--ticker", help="ticker to normalize")
    scope.add_argument("--all", action="store_true", dest="all_companies", help="normalize the latest local artifact set for every ticker")
    parser.add_argument("--rebuild", action="store_true", help="replace the existing normalized store")
    parser.add_argument("--replace-ticker", action="store_true", help="replace only the selected ticker's normalized view")
    parser.add_argument("--document-type", help="only ingest one normalized document type")
    parser.add_argument("--extract", action="store_true", help="extract evidence-preserving qualitative claims from normalized documents")
    parser.add_argument("--dimension", help="only extract one controlled qualitative dimension")
    parser.add_argument("--force-extraction", action="store_true", help="re-extract even when document content and extraction version are unchanged")
    parser.add_argument("--compare", action="store_true", help="compare evidence-backed claims across fiscal periods")
    parser.add_argument("--latest-vs-previous", action="store_true", help="compare the latest fiscal period with the immediately previous period")
    parser.add_argument("--force-comparison", action="store_true", help="recompute a stored temporal comparison")
    parser.add_argument("--state", action="store_true", help="synthesize current qualitative states")
    parser.add_argument("--force-state", action="store_true", help="recompute stored qualitative states")
    parser.add_argument("--dry-run", action="store_true", help="normalize and report without writing the store")
    parser.add_argument("--as-of-date", help="historical cutoff for evidence admission")
    parser.add_argument("--issuer-cik", help=argparse.SUPPRESS)
    parser.add_argument("--download-root", type=Path, default=Path.home() / "Downloads", help=argparse.SUPPRESS)
    parser.add_argument("--store-root", type=Path, default=Path("artifacts/qualitative_analysis"), help=argparse.SUPPRESS)
    return parser


def _print_result(result) -> None:
    # Windows PowerShell sessions may expose a legacy cp1252 stream while IR
    # titles can legitimately contain Unicode punctuation.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {result.ticker or 'ALL'}")
    print()
    print(f"Documents discovered: {result.documents_discovered}")
    print(f"Documents normalized: {result.documents_normalized}")
    print(f"Added: {result.documents_added}")
    print(f"Updated: {result.documents_updated}")
    print(f"Unchanged: {result.documents_unchanged}")
    print(f"Failed: {result.documents_failed}")
    print()
    print("By type:")
    for kind in ("news_release", "annual_report", "quarterly_report", "investor_presentation", "earnings_transcript", "event_transcript", "other_report"):
        print(f"{kind}: {result.by_type.get(kind, 0)}")
    print()
    print(f"Fiscal periods detected: {result.fiscal_periods_detected}")
    print(f"Unknown fiscal periods: {result.unknown_fiscal_periods}")
    print()
    print(f"Characters ingested: {result.characters_ingested}")
    print("Sample:")
    for document in result.sample:
        print(f"- {document.document_id} {document.title}")
    if result.failures:
        print("Failures:")
        for failure in result.failures[:10]:
            print(f"- {failure.failure_type}: {failure.title or failure.message}")


def _print_extraction(summary: dict) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {summary['ticker']}")
    print()
    print(f"Documents eligible: {summary['documents_eligible']}")
    print(f"Documents extracted: {summary['documents_extracted']}")
    print(f"Documents reused: {summary['documents_reused']}")
    print(f"Claims created: {summary['claims_created']}")
    print(f"Claims reused: {summary['claims_reused']}")
    print(f"Claims rejected: {summary['claims_rejected']}")
    print("Claims by dimension:")
    for dimension, count in sorted(summary["claims_by_dimension"].items()):
        print(f"{dimension}: {count}")
    print(f"Evidence validation failures: {summary['evidence_validation_failures']}")
    print(f"Characters/chunks processed: {summary['characters_processed']}/{summary['chunks_processed']}")
    if summary["samples"]:
        print("Samples:")
        for claim in summary["samples"]:
            print(f"- {claim.claim_id} [{claim.dimension}] {claim.evidence_text[:180]}")


def _run_extraction(args, document_store: DocumentStore) -> int:
    # A first extraction command is allowed to hydrate the normalized store for
    # one explicitly requested ticker.  This remains offline and does not alter
    # acquisition artifacts or downloader behavior.
    documents = document_store.get_by_ticker(args.ticker) if args.ticker else document_store.list_all()
    if args.document_type:
        documents = [d for d in documents if d.document_type == args.document_type]
    if not documents and args.ticker and not args.dry_run:
        ingest(ticker=args.ticker, download_root=args.download_root, store=document_store, rebuild=False, document_type=args.document_type, dry_run=False, issuer_cik=args.issuer_cik)
        documents = document_store.get_by_ticker(args.ticker)
        if args.document_type:
            documents = [d for d in documents if d.document_type == args.document_type]

    claim_store = ExtractionStore(args.store_root)
    # Read the claim cache once.  Re-reading and rewriting the complete JSONL
    # store for every document made a forced issuer rebuild quadratic in the
    # number of documents and could leave a production run looking failed
    # after the claims themselves had already been extracted.
    cached_claims = claim_store.list_all() if not args.dry_run else []
    claims_by_document: dict[str, list[QualitativeClaim]] = {}
    for claim in cached_claims:
        claims_by_document.setdefault(claim.document_id, []).append(claim)
    if args.ticker and not args.dry_run:
        claim_store.remove_ticker_except_documents(args.ticker, {document.document_id for document in documents})
        if args.force_extraction:
            # A forced issuer rebuild replaces every current claim, including
            # documents that now produce zero claims.  Source documents remain
            # untouched; only the derived claim rows are refreshed.
            claim_store.remove_ticker_except_documents(args.ticker, set())
            cached_claims = [claim for claim in cached_claims if claim.ticker.upper() != args.ticker.upper()]
            claims_by_document = {}
            for claim in cached_claims:
                claims_by_document.setdefault(claim.document_id, []).append(claim)
    provider = DeterministicExtractionProvider()
    summary = {
        "ticker": args.ticker or "ALL",
        "documents_eligible": len(documents), "documents_extracted": 0, "documents_reused": 0,
        "claims_created": 0, "claims_reused": 0, "claims_rejected": 0,
        "claims_by_dimension": {}, "evidence_validation_failures": 0,
        "characters_processed": 0, "chunks_processed": 0, "samples": [],
    }
    pending_claims: list[QualitativeClaim] = []
    documents_to_remove: set[str] = set()
    for document in documents:
        if args.as_of_date and document.available_date and str(document.available_date)[:10] > args.as_of_date:
            # Known-future evidence cannot remain in the historical claim
            # store. Unknown availability is retained with an explicit status.
            if not args.dry_run:
                documents_to_remove.add(document.document_id)
            continue
        existing_for_document = claims_by_document.get(document.document_id, [])
        state = None
        if existing_for_document:
            first = existing_for_document[0]
            state = {
                "source_content_hash": first.source_content_hash,
                "extraction_version": first.extraction_version,
                "provider_version": first.provider_version,
                "rules_version": first.rules_version,
            }
        period_current = (not existing_for_document or all(
            claim.period_label == document.period_label
            and claim.period_type == document.period_type
            and claim.available_date == document.available_date
            for claim in existing_for_document
        ))
        reusable = (not args.force_extraction and state is not None and state.get("source_content_hash") == document.content_hash and state.get("extraction_version") == "qualitative-extraction-v8" and state.get("provider_version") == provider.provider_version and state.get("rules_version") == provider.rules_version and period_current)
        if reusable:
            existing = existing_for_document
            if args.dimension:
                existing = [c for c in existing if c.dimension == args.dimension]
            summary["documents_reused"] += 1
            summary["claims_reused"] += len(existing)
            for claim in existing:
                summary["claims_by_dimension"][claim.dimension] = summary["claims_by_dimension"].get(claim.dimension, 0) + 1
                if len(summary["samples"]) < 5:
                    summary["samples"].append(claim)
            continue
        if state is not None and not args.dry_run:
            documents_to_remove.add(document.document_id)
        # Extract the complete document even when the CLI is displaying one
        # dimension.  This prevents a targeted run from deleting other valid
        # dimensions for the same document; filtering is applied only to the
        # reported counters and samples below.
        result = extract_document(document, provider)
        reported_claims = [claim for claim in result.claims if not args.dimension or claim.dimension == args.dimension]
        summary["documents_extracted"] += 1
        summary["claims_rejected"] += result.rejected
        summary["evidence_validation_failures"] += len(result.validation_failures)
        summary["characters_processed"] += result.characters_processed
        summary["chunks_processed"] += result.chunks_processed
        if not args.dry_run:
            pending_claims.extend(result.claims)
            summary["claims_created"] += len(reported_claims)
        else:
            summary["claims_created"] += len(reported_claims)
        for claim in reported_claims:
            summary["claims_by_dimension"][claim.dimension] = summary["claims_by_dimension"].get(claim.dimension, 0) + 1
            if len(summary["samples"]) < 5:
                summary["samples"].append(claim)
    if not args.dry_run:
        claim_store.remove_for_documents(documents_to_remove)
        if pending_claims:
            claim_store.upsert_many(pending_claims)
    _print_extraction(summary)
    return 0


def _print_comparison(summary: dict) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {summary['ticker']}")
    print()
    print(f"Periods available: {', '.join(summary['periods_available']) or 'none'}")
    print(f"Period compared: {summary['period_compared']}")
    print()
    print(f"Claims previous: {summary['claims_previous']}")
    print(f"Claims current: {summary['claims_current']}")
    print(f"Topics matched: {summary['topics_matched']}")
    print(f"Temporal changes created: {summary['changes_created']}")
    print(f"Temporal changes reused: {summary['changes_reused']}")
    print(f"Rejected comparisons: {summary['rejected']}")
    print()
    print("Changes by dimension:")
    for dimension, count in sorted(summary["by_dimension"].items()):
        print(f"{dimension}: {count}")
    print("Changes by type:")
    for change_type, count in sorted(summary["by_type"].items()):
        print(f"{change_type}: {count}")
    print()
    print(f"Evidence validation failures: {summary['evidence_failures']}")
    if summary["samples"]:
        print("Samples:")
        for change in summary["samples"]:
            print(f"- {change.change_id} [{change.change_type}] {change.summary}")


def _run_comparison(args, claim_store: ExtractionStore) -> int:
    from collections import Counter

    ticker = args.ticker
    if not ticker:
        raise SystemExit("--compare requires --ticker")
    claims = claim_store.get_by_ticker(ticker)
    if args.as_of_date:
        claims = claims_available_as_of(claims, args.as_of_date)
    eligible_claims, eligibility = eligible_observed_claims(claims, as_of_date=args.as_of_date)
    temporal_store = TemporalStore(args.store_root)
    valid_periods = {claim.period_label for claim in eligible_claims if claim.period_label}
    if not args.dry_run:
        if args.force_comparison:
            # A forced analytical rebuild replaces the issuer's complete
            # derived comparison view.  Keeping rows produced by an earlier
            # period-selection implementation would let stale Q1->Q3 changes
            # survive beside the corrected adjacent-quarter result.
            temporal_store.remove_ticker(ticker)
        temporal_store.remove_invalid_periods(ticker, valid_periods)
    provider = DeterministicTemporalComparisonProvider()
    # Maintain separate like-for-like horizons.  The quarterly comparison is
    # the historical default; an annual comparison is materialized alongside
    # it when two completed annual periods exist.  No annual/quarterly mixing
    # or synthetic Q4 is permitted.
    quarterly = compare_latest_vs_previous(ticker, claims, dimension=args.dimension, provider=provider, horizon="quarterly", as_of_date=args.as_of_date)
    annual = compare_latest_vs_previous(ticker, claims, dimension=args.dimension, provider=provider, horizon="annual", as_of_date=args.as_of_date)
    result = quarterly if quarterly.status != "INSUFFICIENT_PERIODS" else annual
    summary = {
        "ticker": ticker.upper(),
        "periods_available": result.available_periods,
        "period_compared": f"{result.from_period} -> {result.to_period}" if result.from_period and result.to_period else "INSUFFICIENT_PERIODS",
        "claims_previous": result.previous_claims,
        "claims_current": result.current_claims,
        "topics_matched": result.topics_matched,
        "changes_created": 0,
        "changes_reused": 0,
        "rejected": len(result.rejected),
        "by_dimension": Counter(change.dimension for change in result.changes),
        "by_type": Counter(change.change_type for change in result.changes),
        "evidence_failures": len(result.evidence_validation_failures),
        "samples": result.changes[:5],
    }
    comparisons: dict[str, object] = {}
    for horizon, candidate in (("quarterly", quarterly), ("annual", annual)):
        comparisons[horizon] = {
            "status": candidate.status, "from_period": candidate.from_period, "to_period": candidate.to_period,
            "period_basis": candidate.period_basis,
            "sequence_relation": candidate.sequence_relation,
            "gap_status": candidate.gap_status,
            "available_periods": candidate.available_periods, "changes": len(candidate.changes),
            "as_of_date": candidate.as_of_date,
            "eligible_periods": candidate.eligible_periods,
            "excluded_periods": candidate.excluded_periods,
            "period_diagnostics": candidate.period_diagnostics,
        }
    summary["comparison_horizons"] = comparisons
    summary["latest_quarterly_comparison"] = {"from_period": quarterly.from_period, "to_period": quarterly.to_period, "status": quarterly.status, "period_basis": quarterly.period_basis, "sequence_relation": quarterly.sequence_relation, "gap_status": quarterly.gap_status}
    summary["latest_annual_comparison"] = {"from_period": annual.from_period, "to_period": annual.to_period, "status": annual.status, "period_basis": annual.period_basis, "sequence_relation": annual.sequence_relation, "gap_status": annual.gap_status}
    if result.status == "INSUFFICIENT_PERIODS":
        _print_comparison(summary)
        print("INSUFFICIENT_PERIODS")
        return 0

    if not args.dry_run:
        created = reused = 0
        for candidate in (quarterly, annual):
            if candidate.status == "INSUFFICIENT_PERIODS" or not candidate.from_period or not candidate.to_period:
                continue
            existing = temporal_store.get_by_periods(candidate.from_period, candidate.to_period)
            stale = any(item.ticker.upper() == ticker.upper() and item.comparison_version != provider.comparison_version for item in existing)
            if args.force_comparison:
                temporal_store.remove_scope(ticker, candidate.from_period, candidate.to_period, dimension=args.dimension)
            elif stale:
                temporal_store.invalidate_scope(ticker, candidate.from_period, candidate.to_period, provider.comparison_version, dimension=args.dimension)
            counts = temporal_store.upsert_many(candidate.changes)
            created += counts["added"] + counts["updated"]
            reused += counts["unchanged"]
        summary["changes_created"] = created
        summary["changes_reused"] = reused
    else:
        summary["changes_created"] = len(quarterly.changes) + (len(annual.changes) if annual.status != "INSUFFICIENT_PERIODS" else 0)
    if not args.dry_run:
        metadata_path = args.store_root / "temporal_comparison_metadata.json"
        metadata_path.write_text(json.dumps({"ticker": ticker.upper(), "as_of_date": args.as_of_date, "eligible_periods": quarterly.eligible_periods or annual.eligible_periods, "excluded_periods": quarterly.excluded_periods + [item for item in annual.excluded_periods if item not in quarterly.excluded_periods], "period_diagnostics": quarterly.period_diagnostics, "latest_quarterly_comparison": comparisons["quarterly"], "latest_annual_comparison": comparisons["annual"]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_comparison(summary)
    return 0


def _print_state(summary: dict) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {summary['ticker']}")
    print(f"As-of period: {summary['period_label'] or 'UNKNOWN'}")
    if summary.get("latest_annual_period") or summary.get("latest_quarterly_period"):
        print(f"Latest annual: {summary.get('latest_annual_period') or 'UNKNOWN'}")
        print(f"Latest quarterly: {summary.get('latest_quarterly_period') or 'UNKNOWN'}")
        print(f"State freshness: {summary.get('state_freshness') or 'UNKNOWN'}")
    print()
    print(f"Claims considered: {summary['claims_considered']}")
    print(f"Temporal changes considered: {summary['temporal_changes_considered']}")
    print(f"States created: {summary['states_created']}")
    print(f"States reused: {summary['states_reused']}")
    print(f"States updated: {summary['states_updated']}")
    print()
    print("By dimension:")
    for dimension, count in sorted(summary["by_dimension"].items()):
        print(f"{dimension}: {count}")
    print(f"Mixed states: {summary['mixed_states']}")
    print(f"Unknown states: {summary['unknown_states']}")
    print(f"Average confidence: {summary['average_confidence']:.3f}")
    print(f"Evidence validation failures: {summary['evidence_failures']}")
    if summary["samples"]:
        print("Samples:")
        for state in summary["samples"]:
            topic = f"/{state.topic}" if state.topic else ""
            print(f"- {state.dimension}{topic}: {state.current_state} (trend={state.trend}, confidence={state.confidence:.3f}) - {state.summary}")


def _run_state(args, claim_store: ExtractionStore) -> int:
    from collections import Counter

    tickers = [args.ticker] if args.ticker else sorted({claim.ticker for claim in claim_store.list_all()})
    if not tickers:
        raise SystemExit("--state requires stored claims")
    provider = DeterministicStateSynthesisProvider()
    temporal_store = TemporalStore(args.store_root)
    state_store = StateStore(args.store_root)
    for ticker in tickers:
        claims = claim_store.get_by_ticker(ticker)
        if args.as_of_date:
            claims = claims_available_as_of(claims, args.as_of_date)
            # State synthesis must use the same observed-period PIT boundary
            # as temporal comparison.  Keep non-observed guidance/reference
            # claims in the input, but remove actual-period claims that cannot
            # be proven eligible at the requested cutoff.
            eligible_observed, _period_eligibility = eligible_observed_claims(claims, as_of_date=args.as_of_date)
            eligible_ids = {claim.claim_id for claim in eligible_observed}
            claims = [
                claim for claim in claims
                if claim.claim_id in eligible_ids or getattr(claim, "period_type", "UNKNOWN") not in {"QUARTER_ACTUAL", "ANNUAL_ACTUAL", "YTD_ACTUAL"}
            ]
        if args.dimension:
            claims = [claim for claim in claims if claim.dimension == args.dimension]
        changes = temporal_store.get_by_ticker(ticker)
        if args.dimension:
            changes = [change for change in changes if change.dimension == args.dimension]
        result = provider.synthesize(claims, changes)
        valid_periods = {claim.period_label for claim in claims if claim.period_label}
        if not args.dry_run:
            state_store.remove_invalid_periods(ticker, valid_periods)
        if not result.states:
            _print_state({
                "ticker": ticker.upper(), "period_label": result.period_label,
                "claims_considered": result.claims_considered, "temporal_changes_considered": result.temporal_changes_considered,
                "states_created": 0, "states_reused": 0, "states_updated": 0, "by_dimension": {},
                "mixed_states": 0, "unknown_states": 0, "average_confidence": 0.0,
                "evidence_failures": len(result.validation_failures), "samples": [],
                "latest_annual_period": result.latest_annual_period,
                "latest_quarterly_period": result.latest_quarterly_period,
                "state_freshness": result.state_freshness,
            })
            continue
        summary = {
            "ticker": ticker.upper(),
            "period_label": result.period_label,
            "claims_considered": result.claims_considered,
            "temporal_changes_considered": result.temporal_changes_considered,
            "states_created": 0,
            "states_reused": 0,
            "states_updated": 0,
            "by_dimension": Counter(state.dimension for state in result.states),
            "mixed_states": sum(state.current_state == "mixed" for state in result.states),
            "unknown_states": sum(state.current_state == "unknown" for state in result.states),
            "average_confidence": sum(state.confidence for state in result.states) / len(result.states),
            "evidence_failures": len(result.validation_failures),
            "samples": [state for state in result.states if state.topic is None][:8],
            "latest_annual_period": result.latest_annual_period,
            "latest_quarterly_period": result.latest_quarterly_period,
            "state_freshness": result.state_freshness,
        }
        if not args.dry_run:
            existing = state_store.get_by_period(result.period_label or "")
            # State synthesis represents the current issuer snapshot.  Drop
            # superseded source-label aliases (for example Q2 FY2025 and Q2
            # FY2026 for the same calendar quarter) before materializing it.
            state_store.retain_period(ticker, result.period_label or "")
            stale = any(
                state.ticker.upper() == ticker.upper()
                and (state.state_version != provider.state_version or state.rules_version != provider.rules_version or state.provider != provider.provider_version)
                for state in existing
            )
            if args.force_state:
                state_store.remove_scope(ticker, result.period_label or "", dimension=args.dimension)
            elif stale:
                state_store.invalidate_scope(ticker, result.period_label or "", provider.state_version, dimension=args.dimension)
            counts = state_store.upsert_many(result.states)
            summary["states_created"] = counts["added"]
            summary["states_reused"] = counts["unchanged"]
            summary["states_updated"] = counts["updated"]
        else:
            summary["states_created"] = len(result.states)
        _print_state(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = DocumentStore(args.store_root)
    if args.state:
        return _run_state(args, ExtractionStore(args.store_root))
    if args.compare:
        return _run_comparison(args, ExtractionStore(args.store_root))
    if args.extract:
        return _run_extraction(args, store)
    result = ingest(ticker=args.ticker, all_companies=args.all_companies, download_root=args.download_root, store=store, rebuild=args.rebuild, replace_ticker=args.replace_ticker, document_type=args.document_type, dry_run=args.dry_run, issuer_cik=args.issuer_cik)
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
