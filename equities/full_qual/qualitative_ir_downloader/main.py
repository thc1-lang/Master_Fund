"""CLI and sequential company orchestration."""
import argparse
import asyncio
import csv
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from .browser import Browser
from .config import Config, WORKSHEETS, DEFAULT_SERVICE_ACCOUNT_PATH, DOWNLOAD_ROOT
from .crawler import ArchiveCrawler
from .downloader import Downloader
from .filesystem import atomic_json, company_folder
from .google_sheets import read_stocks
from .ir_discovery import IRDiscovery
from .logging_utils import company_handler, setup_logging
from .manifest import Manifest
from .models import Issue, Stock, ArchiveResult, IRCandidate
from .rss import collect_feeds
from .providers import ProviderContext, select_provider
from .structured import merge_documents
from urllib.parse import urlsplit
from .news import find_news_section
from .reports import find_reports_section
from .urls import Scope
from .events_config import EventConfig
from .events import collect_events, matching_folder
from .privacy import safe_record
from .urls import public_url
from . import ir_state
from .company_context import CompanyContext, MediaBudget, prepare_company_context
from .scoring import company_key
from .event_outcomes import company_outcome
from .company_identity import identify
from .corporate_domain import IMPLEMENTATION_VERSION as DOMAIN_IMPLEMENTATION_VERSION
from .production_status import (
    aggregate_status,
    cache_metrics,
    category_status,
    completeness,
    document_metrics,
    event_status,
    ir_status,
)
from .report_accounting import (
    classify_failure,
    failure_stage,
    is_report_candidate,
    report_metrics,
    report_coverage_diagnostics,
)
from .acquisition_priority import critical_source_coverage, sort_documents
from .urls import canonicalize_url
from .official_sources import OfficialSourceStrategy, SOURCE_NEWSROOM
from .reliability import (
    AcquisitionDeadlineExceeded,
    AcquisitionProgressTimeout,
    ProgressTracker,
)

_SOURCE_ARCHIVE_CIK_RE = re.compile(r"/Archives/edgar/data/(\d{1,10})/", re.I)
_SOURCE_ACCESSION_RE = re.compile(r"(?<!\d)(\d{10})-\d{2}-\d{6}(?!\d)")


def annotate_source_identity(document, *, canonical_cik: str | None, canonical_name: str | None = None):
    """Attach and validate source registrant identity before materialization."""
    canonical = str(canonical_cik or "").zfill(10) if canonical_cik else None
    document.canonical_registrant_cik = canonical
    document.canonical_registrant_name = canonical_name
    archive_ciks: list[str] = []
    accession_ciks: list[str] = []
    provenance_ciks: list[str] = []
    for provenance in document.provenance or []:
        accession = str(provenance.get("accession_number") or "")
        if provenance.get("issuer_cik"):
            provenance_ciks.append(str(provenance["issuer_cik"]).zfill(10))
        for value in (accession, str(provenance.get("source_url") or "")):
            archive = _SOURCE_ARCHIVE_CIK_RE.search(value)
            if archive:
                archive_ciks.append(archive.group(1).zfill(10))
            match = _SOURCE_ACCESSION_RE.search(value)
            if match:
                accession_ciks.append(match.group(1))
    source_url = str(document.source_url or "")
    archive = _SOURCE_ARCHIVE_CIK_RE.search(source_url)
    if archive:
        archive_ciks.append(archive.group(1).zfill(10))
    match = _SOURCE_ACCESSION_RE.search(source_url)
    if match:
        accession_ciks.append(match.group(1))
    # Filing agents often own the accession prefix (for example 0001193125)
    # while the archive path and issuer metadata identify the registrant. Use
    # the canonical CIK whenever any filing-level evidence matches it.
    evidence_ciks = archive_ciks + accession_ciks + provenance_ciks
    source_cik = next((cik for cik in evidence_ciks if canonical and cik == canonical), None)
    if not source_cik:
        source_cik = document.source_registrant_cik or (archive_ciks[0] if archive_ciks else (accession_ciks[0] if accession_ciks else (provenance_ciks[0] if provenance_ciks else None)))
    document.source_registrant_cik = str(source_cik).zfill(10) if source_cik else None
    if document.source_registrant_cik and canonical:
        compatible = document.source_registrant_cik == canonical
        document.source_lineage_relation = "CANONICAL_REGISTRANT" if compatible else "AFFILIATE"
        document.source_identity_eligible = compatible
    else:
        document.source_lineage_relation = document.source_lineage_relation or "UNKNOWN"
        document.source_identity_eligible = True
    if document.provenance:
        document.provenance[0].update({
            "canonical_registrant_cik": canonical or "",
            "canonical_registrant_name": canonical_name or "",
            "source_registrant_cik": document.source_registrant_cik or "",
            "source_lineage_relation": document.source_lineage_relation,
            "source_identity_eligible": "true" if document.source_identity_eligible else "false",
        })
        # Accession identity is authoritative when an older cached
        # provenance row carried the serving/affiliate CIK. Preserve the raw
        # value for auditability, but expose the validated filing CIK to the
        # normalized SEC catalog and downstream source routers.
        if document.source_registrant_cik and document.provenance[0].get("issuer_cik") and str(document.provenance[0].get("issuer_cik")).zfill(10) != document.source_registrant_cik:
            document.provenance[0]["raw_issuer_cik"] = str(document.provenance[0].get("issuer_cik"))
            document.provenance[0]["issuer_cik"] = document.source_registrant_cik
    return document


def _newsroom_coverage_diagnostic(data: dict) -> str:
    """Explain an empty newsroom category without turning it into a failure."""
    if data.get("news") or data.get("discovered_news"):
        return "NEWSROOM_ITEMS_AVAILABLE"
    status = str((data.get("official_source_status") or {}).get(SOURCE_NEWSROOM) or "")
    if status in {"ACCESS_BLOCKED", "UNUSABLE"}:
        return "NEWSROOM_SOURCE_UNAVAILABLE"
    if status in {"SUCCESS", "ACCESSIBLE_EMPTY"}:
        return "NEWSROOM_NO_ELIGIBLE_ITEMS"
    if status:
        return "NEWSROOM_SOURCE_UNAVAILABLE"
    return "NEWSROOM_NOT_DISCOVERED"

def load_previously_verified_ir(root: Path, stock: Stock) -> IRCandidate | None:
    return ir_state.recover(root, stock, Path(__file__).resolve().parent.parent)

log = logging.getLogger(__name__)

def select_stocks(stocks: list[Stock], ticker=None, start_ticker=None, limit=None) -> list[Stock]:
    if ticker:
        stocks = [s for s in stocks if s.ticker.casefold() == ticker.casefold()]
        if not stocks:
            raise ValueError(f"Ticker {ticker!r} not found in selected worksheets")
    if start_ticker:
        index = next((i for i, s in enumerate(stocks) if s.ticker.casefold() == start_ticker.casefold()), None)
        if index is None:
            raise ValueError(f"Start ticker {start_ticker!r} not found")
        stocks = stocks[index:]
    return stocks[:limit] if limit else stocks


def _finalize_production_status(
    manifest: Manifest,
    winner: IRCandidate | None,
    event_metrics: dict,
    browser: Browser,
    runtime,
    *,
    scan_only: bool = False,
    downloader: Downloader | None = None,
) -> dict:
    """Derive module and company statuses once all collection state is known."""
    data = manifest.data
    applicable = not browser.config.events.only
    news_metrics = document_metrics(data, "news")
    reports_metrics = document_metrics(data, "reports")
    report_coverage = report_coverage_diagnostics(data.get("reports", []), discovered=reports_metrics.get("discovered"))
    data["report_coverage"] = report_coverage
    # A report category can be technically downloaded while still lacking the
    # minimum issuer corpus needed for period/state analysis.  Keep this
    # distinction explicit: report discovery success must not masquerade as
    # critical-source coverage success.
    critical_coverage = critical_source_coverage(
        data.get("reports", []),
        as_of_date=data.get("as_of_date"),
    )
    data["critical_source_coverage"] = critical_coverage
    critical_limitations: list[str] = []
    if reports_metrics.get("discovered"):
        for key, code in (
            ("annual", "CRITICAL_ANNUAL_SOURCE_MISSING"),
            ("interim", "CRITICAL_INTERIM_SOURCE_MISSING"),
            ("governance", "CRITICAL_GOVERNANCE_SOURCE_MISSING"),
        ):
            if not critical_coverage.get(key):
                critical_limitations.append(code)
    news_state = category_status(data, "news", applicable=applicable, scanned=("news" in data if applicable else None))
    reports_state = category_status(data, "reports", applicable=applicable, scanned=("reports" in data if applicable else None))
    ir_state_value = ir_status(data, winner=winner)
    if report_coverage.get("limitations") and reports_state == "SUCCESS":
        reports_state = "SUCCESS_WITH_LIMITATIONS"
    if critical_limitations and reports_state == "SUCCESS":
        reports_state = "SUCCESS_WITH_LIMITATIONS"
    data["critical_source_coverage_status"] = (
        "COMPLETE" if critical_coverage.get("minimum_research_corpus_acquired") else
        "INCOMPLETE" if reports_metrics.get("discovered") else "NOT_AVAILABLE"
    )
    data["critical_source_limitations"] = critical_limitations
    events_state = event_status(event_metrics, data) if browser.config.events.enabled else "NOT_APPLICABLE"
    overall, limitations = aggregate_status(
        ir=ir_state_value,
        news=news_state,
        reports=reports_state,
        events=events_state,
        news_metrics=news_metrics,
        reports_metrics=reports_metrics,
        event_metrics=event_metrics,
        official_evidence=bool(data.get("official_source_evidence")),
        source_limitations=(
            list(data.get("official_source_limitations", []) or [])
            + list(report_coverage.get("limitations", []) or [])
            + critical_limitations
        ),
    )
    data.update(
        ir_status=ir_state_value,
        news_status=news_state,
        reports_status=reports_state,
        events_status=events_state,
        overall_status=overall,
        completeness=completeness(
            ir=ir_state_value,
            news=news_state,
            reports=reports_state,
            events=events_state,
            event_metrics=event_metrics,
            limitations=limitations,
        ),
    )
    data["news_coverage_diagnostic"] = _newsroom_coverage_diagnostic(data)
    data["primary_ir_status"] = data.get("primary_ir_status") or (
        "BLOCKED" if ir_state_value == "ACCESS_BLOCKED" else "SUCCESS" if ir_state_value == "SUCCESS" else ir_state_value
    )
    cache = cache_metrics(
        document_cache_hits=getattr(downloader, "cache_hits", 0),
        event_metrics=event_metrics,
        ir_cache_hit=bool(getattr(runtime, "verified_cache_hit", False)),
    )
    data["cache_metrics"] = cache
    # Keep the old field as a compatibility alias.  Event-only integration
    # searches historically used it for their terminal event outcome; the
    # production-facing overall_status is always authoritative.
    if data.get("status") == "INTERRUPTED":
        # A user interruption is a durable run outcome; analytical coverage
        # may still be summarized, but it must never be relabeled SUCCESS.
        data["status"] = "INTERRUPTED"
    elif browser.config.events.only and runtime.selected:
        data["status"] = events_state
    elif not winner and data.get("status") == "IR_DISCOVERY_FAILED":
        data["status"] = "IR_DISCOVERY_FAILED"
    else:
        data["status"] = overall
    return {
        "news_metrics": news_metrics,
        "reports_metrics": reports_metrics,
        "ir_status": ir_state_value,
        "news_status": news_state,
        "reports_status": reports_state,
        "events_status": events_state,
        "overall_status": overall,
        "limitations": limitations,
        **cache,
    }


async def _save_official_documents(
    *,
    browser: Browser,
    context,
    folder: Path,
    stock: Stock,
    manifest: Manifest,
    documents_by_category: dict[str, list],
    max_documents: int | None = None,
    as_of_date: str | None = None,
    stop_after_critical_financial: bool = False,
) -> tuple[Downloader, int]:
    """Materialize issuer-verified fallback documents without an IR winner."""
    downloader = Downloader(browser, context, folder, stock.ticker, stock.company_name, issuer_context=manifest.data.get("ecosystem") or None)
    downloader.register_existing(list(manifest.data.get("news", [])) + list(manifest.data.get("reports", [])))
    failed = 0
    for category in ("reports", "news"):
        discovered = list(documents_by_category.get(category, []) or [])
        discovered = sort_documents(discovered, as_of_date=as_of_date)
        raw_count = len(discovered)
        if category == "reports":
            discovered = [document for document in discovered if is_report_candidate(document)]
            manifest.data["raw_report_candidates"] = raw_count
        manifest.data[f"discovered_{category}"] = [asdict(document) for document in discovered]
        manifest.data.setdefault("archive_pages", {})[category] = 0
        materialized = 0
        for document in discovered[:max_documents] if max_documents else discovered:
            annotate_source_identity(document, canonical_cik=manifest.data.get("issuer_cik"), canonical_name=stock.company_name)
            if not document.source_identity_eligible:
                failed += 1
                document.status = "REJECTED"
                document.failure_stage = "SOURCE_IDENTITY"
                document.failure_code = "SOURCE_IDENTITY_INCOMPATIBLE"
                document.failure_classification = "SOURCE_IDENTITY_INCOMPATIBLE"
                document.failure_reason = "Source registrant is not compatible with the canonical ticker registrant"
                manifest.document(document)
                manifest.error(Issue(document.failure_code, document.failure_reason, document.source_url))
                continue
            try:
                saved = await downloader.save(document)
                saved.status = "CACHED" if saved.cache_hit else "DOWNLOADED"
                saved.existing_artifact = bool(saved.cache_hit)
                manifest.document(saved)
                materialized += 1
                if stop_after_critical_financial and category == "reports":
                    coverage = critical_source_coverage(
                        manifest.data.get("reports", []),
                        as_of_date=as_of_date,
                    )
                    if coverage.get("financial"):
                        # Tier 1 has established the annual plus current
                        # interim/results corpus.  Leave the reserved
                        # governance/management stage and lower-priority
                        # stages their own budgets instead of backfilling
                        # every historical filing in this loop.
                        manifest.data["critical_financial_documents_processed"] = materialized
                        break
            except Exception as exc:
                failed += 1
                code = getattr(exc, "code", "PDF_DOWNLOAD_FAILED" if document.category == "reports" else "HTML_TO_PDF_FAILED")
                document.status = "BLOCKED" if code == "SITE_BLOCKED" else "FAILED"
                document.failure_code = code
                document.failure_stage = failure_stage(code, str(exc), document)
                document.failure_classification = classify_failure(code, str(exc), document=document)
                document.failure_reason = str(exc)
                manifest.document(document)
                manifest.error(Issue(code, str(exc), document.source_url))
    return downloader, failed

async def process_company(browser: Browser, stock: Stock, timestamp: str, max_documents: int | None = None, *, scan_only=False, issuer_cik: str | None = None, issuer_resolution_source: str | None = None, as_of_date: str | None = None) -> dict:
    company_started=time.perf_counter()
    key = (stock.ticker.casefold(), company_key(stock.company_name))
    prepared = getattr(browser, "prepared_companies", {})
    runtime = prepare_company_context(browser,stock,Path(__file__).resolve().parent.parent)
    runtime.scan_only = scan_only
    browser.current_company = runtime
    # Incremental production runs reuse the latest verified company folder in
    # every mode.  A fresh folder is created only for a company with no prior
    # manifest, preserving the persistent document/event registry on reruns.
    folder = matching_folder(browser.config.download_root, stock)
    resume = folder is not None
    folder = folder or company_folder(browser.config.download_root, stock.ticker, stock.company_name, timestamp)
    handler = company_handler(folder)
    cached_identity = runtime.verified_ir
    runtime.verified_ir = cached_identity
    manifest = Manifest(folder, stock, timestamp, resume=resume)
    if as_of_date:
        manifest.data["as_of_date"] = as_of_date
    manifest.data["acquisition_stage_order"] = [
        "IDENTITY",
        "CRITICAL_FINANCIAL_DISCOVERY",
        "CRITICAL_FINANCIAL_DOWNLOAD",
        "GOVERNANCE_MANAGEMENT_DISCOVERY",
        "RESULTS_PRESENTATIONS",
        "REGULATORY_NEWS",
        "GENERAL_NEWS",
        "DEEP_ARCHIVE",
    ]
    manifest.data["acquisition_stage_budgets"] = {
        "identity": float(getattr(browser.config, "identity_budget", 60.0)),
        "critical_financial": float(getattr(browser.config, "critical_financial_budget", 300.0)),
        "governance_management": float(getattr(browser.config, "governance_budget", 180.0)),
        "news": float(getattr(browser.config, "news_budget", 180.0)),
        "deep_archive": float(getattr(browser.config, "deep_archive_budget", 60.0)),
    }
    tracker = ProgressTracker(
        folder,
        stock.ticker,
        no_progress_timeout=float(getattr(browser.config, "no_progress_timeout", 900.0)),
        deadline=float(getattr(browser.config, "acquisition_deadline", 7200.0)),
        manifest_checkpoint=manifest.progress_checkpoint,
    )
    browser.acquisition_tracker = tracker
    manifest.data["progress_file"] = str(tracker.progress_path)
    manifest.data["acquisition_started_at"] = tracker._last_progress_at
    manifest.write()
    if issuer_cik:
        manifest.data["issuer_cik"] = str(issuer_cik)
        manifest.data["canonical_registrant_cik"] = str(issuer_cik).zfill(10)
        manifest.data["canonical_registrant_name"] = stock.company_name
    if issuer_resolution_source:
        manifest.data["issuer_resolution_source"] = str(issuer_resolution_source)
    event_metrics = {}
    failed_downloads = 0
    raw_report_candidates = 0
    discovery = None
    winner = None
    downloader = None
    official_source_result = {"documents": [], "documents_by_category": {"news": [], "reports": []}, "routes": [], "statuses": {}, "limitations": [], "coverage_status": "ALL_OFFICIAL_SOURCES_UNUSABLE", "usable": False}
    manifest.data.update(ir_official=bool(cached_identity), ir_accessible=False, rss_feeds=[], discovery_errors=[])
    def stage_budget(stage: str) -> float:
        """Return a bounded stage budget without exceeding the issuer deadline."""
        configured = {
            "identity": float(getattr(browser.config, "identity_budget", 60.0)),
            "reports": float(getattr(browser.config, "critical_financial_budget", 300.0)),
            "governance": float(getattr(browser.config, "governance_budget", 180.0)),
            "news": float(getattr(browser.config, "news_budget", 180.0)),
            "archive": float(getattr(browser.config, "deep_archive_budget", 60.0)),
        }.get(stage, float(getattr(browser.config, "stage_timeout", 900.0)))
        remaining = float(getattr(browser.config, "acquisition_deadline", 0.0)) - tracker.elapsed
        if remaining <= 0:
            return 0.1
        return max(0.1, min(configured, float(getattr(browser.config, "stage_timeout", configured)), remaining))
    if cached_identity:
        log.info("[%s] verified IR cache hit: %s; no rediscovery",stock.ticker,cached_identity.url)
        manifest.data["investor_relations_url"] = cached_identity.url
        manifest.data["ir_provenance_source"] = "verified_ir_cache"
    try:
        log.info("Starting %s - %s (%s row %s)", stock.ticker, stock.company_name, stock.worksheet, stock.spreadsheet_row)
        async with browser.session() as context:
            discovery = IRDiscovery(browser)
            discovery.identity=identify(stock.company_name);discovery.ticker=stock.ticker
            manifest.data.update({
                "requested_ticker": stock.ticker,
                "sec_registrant_name": stock.company_name,
                "company_display_name": stock.company_name,
                "corporate_brand_name": discovery.identity.normalized_company_name,
                "domain_resolution_implementation": DOMAIN_IMPLEMENTATION_VERSION,
            })
            tracker.start("ISSUER_RESOLUTION", item=stock.ticker, priority_tier=0, budget_seconds=stage_budget("identity"), budget_remaining_seconds=stage_budget("identity"))
            winner = cached_identity
            fresh = None
            if not winner or (browser.config.revalidate_ir and not runtime.scanned):
                tracker.start("IR_DISCOVERY", item=stock.ticker, priority_tier=0, budget_seconds=stage_budget("identity"), budget_remaining_seconds=stage_budget("identity"))
                try:
                    discovery_timeout = stage_budget("identity")
                    # Preserve the historical three-argument call when no
                    # issuer CIK is available; test/extension implementations
                    # may intentionally expose that stable interface.  The
                    # optional CIK is supplied only for authoritative identity
                    # aware discovery.
                    resolved_cik = issuer_cik or manifest.data.get("issuer_cik")
                    discovery_call = (
                        discovery.discover_investor_relations_site(
                            stock.ticker, stock.company_name, context, issuer_cik=resolved_cik
                        )
                        if resolved_cik
                        else discovery.discover_investor_relations_site(
                            stock.ticker, stock.company_name, context
                        )
                    )
                    fresh = await asyncio.wait_for(discovery_call, timeout=discovery_timeout)
                except Exception as exc:
                    # A bounded transport/search timeout cannot erase ownership already proven.
                    proven=[c for c in discovery.candidates if c.official]
                    fresh=max(proven,key=lambda c:c.officiality_score) if proven else None
                    discovery.errors.append(Issue("IR_DISCOVERY_BUDGET_EXHAUSTED" if isinstance(exc,TimeoutError) else "IR_DISCOVERY_FAILED", str(exc)))
                if fresh and fresh.official:
                    # A verified IR URL on the issuer's exact branded root
                    # is stronger than a regional/search-discovered corporate
                    # candidate.  Promote that registrable domain as the
                    # canonical corporate identity while retaining hosted IR
                    # domains (ir./investor.) as separate relationships.
                    from .ir_discovery import DOMAIN
                    from .ir_evidence import brand_domain
                    winner_host = DOMAIN(fresh.url)
                    if winner_host.subdomain in ("", "www") and brand_domain(fresh.url, stock.company_name) == 2:
                        fresh.corporate_url = f"https://{winner_host.top_domain_under_public_suffix}/"
                    try:
                        ir_state.upsert(browser.config.download_root, stock, fresh, confirmed=fresh.content_validated)
                    except (OSError, ValueError) as exc:
                        log.warning("Verified IR cache write failed; identity retained: %s", exc)
                    winner = ir_state.load(browser.config.download_root, stock) or fresh
                    winner.access = fresh.access
                    winner.content_validated = fresh.content_validated
                tracker.complete("IR_DISCOVERY", processed=1, saved=int(bool(fresh and fresh.official)), network_requests=len(browser.access_events))
            elif winner:
                tracker.skipped("IR_DISCOVERY", item=stock.ticker, reason="VERIFIED_CACHE_REUSED")
                tracker.complete("ISSUER_RESOLUTION", processed=1, reused=1)
            if winner:
                from .ir_discovery import DOMAIN
                from .ir_evidence import brand_domain
                winner_host = DOMAIN(winner.url)
                if winner_host.subdomain in ("", "www") and brand_domain(winner.url, stock.company_name) == 2:
                    winner.corporate_url = f"https://{winner_host.top_domain_under_public_suffix}/"
                    # Upgrade a legacy verified-IR cache entry whose
                    # corporate relationship was learned from a regional
                    # search result.  The selected branded-root IR URL is
                    # stronger evidence, so persist the corrected relationship
                    # without reacquiring any source documents.
                    if cached_identity:
                        try:
                            ir_state.upsert(
                                browser.config.download_root,
                                stock,
                                winner,
                                source="domain_relationship_revalidation",
                                confirmed=True,
                            )
                        except (OSError, ValueError) as exc:
                            log.warning("Verified IR cache domain upgrade failed; identity retained: %s", exc)
                if runtime.scanned:
                    discovery.ecosystem = runtime.ecosystem
                if winner.url not in discovery.ecosystem.verified_ir_urls:
                    discovery.ecosystem.verified_ir_urls.append(winner.url)
                discovery.ecosystem.official_ir_domain = urlsplit(winner.url).hostname
                discovery.ecosystem.official_corporate_url = winner.corporate_url
                if winner.corporate_url:
                    from .ir_discovery import DOMAIN
                    discovery.ecosystem.official_corporate_domain = DOMAIN(winner.corporate_url).top_domain_under_public_suffix
                if winner.verified_hosted_ir_domain:
                    discovery.ecosystem.verified_hosted_ir_domains.append(winner.verified_hosted_ir_domain)
                if cached_identity and not fresh and not runtime.scanned and not browser.config.events.only and runtime.access_strategy.get("browser_root_required",True):
                    log.info("Verified IR cache: %s (score=%s); no rediscovery", winner.url, winner.officiality_score)
                    probe = await context.new_page()
                    try:
                        winner.access = await browser.assess(probe, winner.url)
                    except Exception as exc:
                        winner.access.error = str(exc)
                        log.warning("Root probe failed; retaining verified identity: %s", exc)
                    finally:
                        await probe.close()
                discovery.candidates = [winner] + [c for c in discovery.candidates if c.url != winner.url]
                runtime.verified_ir = winner
                runtime.ecosystem = discovery.ecosystem
                manifest.data.update(officiality_score=winner.officiality_score, ir_evidence=winner.evidence)
                tracker.complete("ISSUER_RESOLUTION", processed=1, reused=int(bool(cached_identity)), saved=int(not bool(cached_identity)))
            if hasattr(discovery,'diagnostics'):
                diagnostics=discovery.diagnostics()
                if isinstance(diagnostics,dict):manifest.data['ir_discovery_diagnostics']=diagnostics
            manifest.data["discovery"] = [asdict(c) for c in discovery.candidates]
            manifest.data["discovery_errors"] = [asdict(e) for e in discovery.errors]
            manifest.data["ecosystem"] = asdict(discovery.ecosystem)
            manifest.data["corporate_domain"] = discovery.ecosystem.official_corporate_domain
            manifest.data["corporate_domain_verification_status"] = "VERIFIED_CORPORATE" if discovery.ecosystem.official_corporate_domain else "UNVERIFIED"
            manifest.data["rss_feeds"] = discovery.rss_feeds
            if not winner:
                for issue in discovery.errors:
                    manifest.error(issue)
                manifest.error(Issue("IR_DISCOVERY_FAILED", "No official IR candidate met the confidence threshold"))
                manifest.data["status"] = "IR_DISCOVERY_FAILED"
                manifest.data["primary_ir_status"] = "DISCOVERY_FAILED"
                # SEC is an independent official-source branch.  A failed IR
                # search must not prevent a verified issuer CIK from yielding
                # filings that can be normalized downstream.
                try:
                    tracker.start("SEC_DISCOVERY", item=stock.ticker, priority_tier=1, budget_seconds=stage_budget("reports"), budget_remaining_seconds=stage_budget("reports"))
                    source_strategy = OfficialSourceStrategy(
                        browser,
                        context,
                        stock,
                        discovery.ecosystem,
                        None,
                        cache_root=browser.config.download_root,
                        issuer_cik=issuer_cik or manifest.data.get("issuer_cik"),
                    )
                    official_source_result = await asyncio.wait_for(
                        source_strategy.discover(critical_only=True), timeout=stage_budget("reports")
                    )
                    tracker.complete("SEC_DISCOVERY", discovered=len(official_source_result.get("documents", [])), processed=1, network_requests=len(browser.access_events))
                    manifest.data["official_source_routes"] = official_source_result.get("routes", [])
                    manifest.data["official_source_status"] = official_source_result.get("statuses", {})
                    manifest.data["official_source_limitations"] = official_source_result.get("limitations", [])
                    manifest.data["official_source_coverage_status"] = official_source_result.get("coverage_status", "ALL_OFFICIAL_SOURCES_UNUSABLE")
                    manifest.data["official_source_evidence_families"] = official_source_result.get("evidence_families", [])
                    manifest.data["official_source_evidence"] = bool(official_source_result.get("usable"))
                    if official_source_result.get("documents"):
                        downloader, fallback_failures = await _save_official_documents(
                            browser=browser,
                            context=context,
                            folder=folder,
                            stock=stock,
                            manifest=manifest,
                            documents_by_category=official_source_result.get("documents_by_category", {}),
                            max_documents=max_documents,
                            as_of_date=as_of_date,
                            stop_after_critical_financial=True,
                        )
                        failed_downloads += fallback_failures
                except Exception as exc:
                    if isinstance(exc, (AcquisitionProgressTimeout, AcquisitionDeadlineExceeded)):
                        raise
                    log.warning("Independent official-source fallback discovery failed: %s", exc)
                    manifest.error(Issue("OFFICIAL_SOURCE_FALLBACK_FAILED", str(exc), None))
            else:
                manifest.data["investor_relations_url"] = winner.url
                manifest.data["ir_official"] = winner.official
                manifest.data["ir_accessible"] = winner.access.browser_accessible
                manifest.data["ir_access"] = asdict(winner.access)
                manifest.data["ir_content_validated"] = winner.content_validated
                # A verified primary IR identity is retained even when its
                # current landing route is blocked.  Probe bounded alternate
                # official routes before treating the company as empty.
                try:
                    tracker.start("IR_VERIFICATION", item=winner.url, priority_tier=0, budget_seconds=stage_budget("reports"), budget_remaining_seconds=stage_budget("reports"))
                    source_strategy = OfficialSourceStrategy(
                        browser,
                        context,
                        stock,
                        discovery.ecosystem,
                        winner,
                        cache_root=browser.config.download_root,
                        issuer_cik=issuer_cik or manifest.data.get("issuer_cik"),
                    )
                    official_source_result = await asyncio.wait_for(
                        source_strategy.discover(critical_only=True), timeout=stage_budget("reports")
                    )
                    tracker.complete("IR_VERIFICATION", discovered=len(official_source_result.get("documents", [])), processed=1, network_requests=len(browser.access_events))
                except Exception as exc:
                    if isinstance(exc, (AcquisitionProgressTimeout, AcquisitionDeadlineExceeded)):
                        raise
                    log.warning("Official-source fallback discovery failed: %s", exc)
                    manifest.error(Issue("OFFICIAL_SOURCE_FALLBACK_FAILED", str(exc), winner.url))
                manifest.data["official_source_routes"] = official_source_result.get("routes", [])
                manifest.data["official_source_status"] = official_source_result.get("statuses", {})
                manifest.data["official_source_limitations"] = official_source_result.get("limitations", [])
                manifest.data["official_source_coverage_status"] = official_source_result.get("coverage_status", "ALL_OFFICIAL_SOURCES_UNUSABLE")
                manifest.data["official_source_evidence_families"] = official_source_result.get("evidence_families", [])
                manifest.data["official_source_evidence"] = bool(official_source_result.get("usable"))
                manifest.data["primary_ir_status"] = "BLOCKED" if winner.access.blocked and not (winner.access.browser_accessible or winner.access.http_accessible) else "SUCCESS"
                manifest.write()
                scope = Scope(discovery.ecosystem.verified_ir_urls or [winner.url])
                # Materialize verified financial/governance fallback documents
                # before touching broad archive/news routes.  This is the
                # critical-source-first boundary: a slow newsroom cannot
                # starve annual/interim/results/governance acquisition.
                critical_documents = official_source_result.get("documents_by_category", {}).get("reports", [])
                if critical_documents:
                    tracker.start("CRITICAL_FINANCIAL_DOWNLOAD", item=stock.ticker, priority_tier=1)
                    downloader, fallback_failures = await _save_official_documents(
                        browser=browser,
                        context=context,
                        folder=folder,
                        stock=stock,
                        manifest=manifest,
                        documents_by_category={"reports": critical_documents},
                        max_documents=max_documents,
                        as_of_date=as_of_date,
                        stop_after_critical_financial=True,
                    )
                    failed_downloads += fallback_failures
                    coverage = critical_source_coverage(manifest.data.get("reports", []), as_of_date=as_of_date)
                    manifest.data["critical_source_coverage"] = coverage
                    manifest.data["critical_source_coverage_status"] = (
                        "COMPLETE" if coverage.get("minimum_research_corpus_acquired") else "INCOMPLETE"
                    )
                    tracker.complete("CRITICAL_FINANCIAL_DOWNLOAD", processed=len(critical_documents), failed=fallback_failures, priority_tier=1, **{key: value for key, value in coverage.items() if isinstance(value, bool)})
                else:
                    downloader = Downloader(browser, context, folder, stock.ticker, stock.company_name, issuer_context=discovery.ecosystem)
                # Existing records are reused only after URL, category, date,
                # local-file, and (when available) content-hash validation.
                downloader.register_existing(
                    list(manifest.data.get("news", [])) + list(manifest.data.get("reports", []))
                )
                provider = None
                categories = () if browser.config.events.only else (("reports", find_reports_section, "REPORT_SECTION_NOT_FOUND"), ("news", find_news_section, "NEWS_SECTION_NOT_FOUND"))
                for category, finder, missing in categories:
                    discovery_stage = "NEWS_DISCOVERY" if category == "news" else "REPORT_DISCOVERY"
                    download_stage = "NEWS_DOWNLOAD" if category == "news" else "REPORT_DOWNLOAD"
                    category_budget = stage_budget("news" if category == "news" else "reports")
                    category_deadline = time.monotonic() + category_budget
                    def category_timeout() -> float:
                        return max(0.1, min(
                            float(getattr(browser.config, "stage_timeout", category_budget)),
                            category_deadline - time.monotonic(),
                        ))
                    tracker.start(
                        discovery_stage,
                        item=category,
                        priority_tier=1 if category == "reports" else 5,
                        budget_seconds=category_budget,
                        budget_remaining_seconds=max(0.0, category_deadline - time.monotonic()),
                    )
                    category_errors = []
                    # A browser landing-page validation failure must not hide
                    # independently verified official-source artifacts (for
                    # example a public newsroom or presentations route).  The
                    # source strategy has already established issuer identity
                    # for those documents, so only skip the primary archive
                    # path when no verified fallback evidence exists.
                    if (winner.access.browser_accessible and not winner.content_validated
                            and not cached_identity and not official_source_result.get("documents")):
                        manifest.error(Issue("IR_VALIDATION_FAILED", "Retrieved page did not validate as company IR content", winner.url))
                        continue
                    seeds = []
                    section_failed = False
                    page = await context.new_page()
                    try:
                        seeds = await asyncio.wait_for(
                            finder(page, winner.url, browser, scope, stock.company_name),
                            timeout=category_timeout(),
                        )
                    except Exception as exc:
                        if isinstance(exc, (AcquisitionProgressTimeout, AcquisitionDeadlineExceeded)):
                            raise
                        section_failed = True
                        log.warning("%s section discovery failed: %s", category, exc)
                        issue = Issue(getattr(exc, "code", missing), str(exc), winner.url)
                        category_errors.append(issue)
                        manifest.error(issue)
                    finally:
                        await page.close()
                    if not seeds:
                        if not section_failed:
                            issue = Issue(missing, f"No confident {category} archive", winner.url)
                            category_errors.append(issue)
                            manifest.error(issue)
                        section_failed = True
                    manifest.data[f"{category}_sections"] = seeds
                    manifest.data["allowed_html_hosts"] = sorted(scope.hosts)
                    try:
                        remaining = browser.config.max_links_per_company - (len(manifest.data.get("discovered_news", [])) if category == "reports" else 0)
                        archive = await asyncio.wait_for(
                            ArchiveCrawler(browser, scope).collect(context, seeds, category, remaining),
                            timeout=category_timeout(),
                        ) if seeds else ArchiveResult()
                    except Exception as exc:
                        if isinstance(exc, (AcquisitionProgressTimeout, AcquisitionDeadlineExceeded)):
                            raise
                        log.exception("Archive traversal failed")
                        issue = Issue(getattr(exc, "code", "ARCHIVE_FAILED"), str(exc), seeds[0] if seeds else winner.url)
                        category_errors.append(issue)
                        archive = ArchiveResult(errors=[issue])
                    if category == "news":
                        manifest.data["rss_feeds"] = list(dict.fromkeys(manifest.data["rss_feeds"] + archive.rss_feeds))
                        feed_docs, feed_errors = await asyncio.wait_for(
                            collect_feeds(browser, context, manifest.data["rss_feeds"], scope, winner.url),
                            timeout=category_timeout(),
                        )
                        existing = {d.source_url for d in archive.documents}
                        for document in feed_docs:
                            if document.source_url not in existing:
                                if len(archive.documents) >= remaining:
                                    archive.errors.append(Issue("SAFETY_LIMIT_REACHED", "Combined archive/RSS document budget"))
                                    break
                                archive.documents.append(document)
                                existing.add(document.source_url)
                        archive.errors.extend(feed_errors)
                    if section_failed or not archive.documents or archive.errors:
                        if provider is None:
                            provider = await runtime.resolve_provider(browser, context)
                            log.info("IR provider detected: %s; evidence=%s", provider.name, provider.evidence)
                        fallback = await (provider.discover_news() if category == "news" else provider.discover_reports())
                        archive.documents = merge_documents(archive.documents + fallback.documents)
                        archive.errors.extend(fallback.errors)
                        archive.pages += fallback.pages
                        archive.coverage = fallback.coverage
                        manifest.data["provider"] = {"name":provider.name,"evidence":provider.evidence}
                        manifest.data.setdefault("provider_diagnostics", {})[category] = fallback.diagnostics
                        manifest.data["rss_feeds"] = list(dict.fromkeys(manifest.data["rss_feeds"] + fallback.rss_feeds))
                    # Merge verified alternate-source documents after both
                    # primary archive and provider fallback discovery.  The
                    # shared identity merge preserves title/date duplicates
                    # while retaining every provenance sighting.
                    alternate = official_source_result.get("documents_by_category", {}).get(category, [])
                    archive.documents = merge_documents(archive.documents + alternate)
                    manifest.data.setdefault("coverage", {})[category] = archive.coverage
                    manifest.data["archive_pages"][category] = archive.pages
                    if category == "reports":
                        # Keep raw link sightings separate from genuine report
                        # candidates.  HTML landing pages and event/webcast
                        # links must never become failed PDF records.
                        raw_report_candidates = len(archive.documents)
                        archive.documents = [d for d in archive.documents if is_report_candidate(d)]
                        manifest.data["raw_report_candidates"] = raw_report_candidates
                    manifest.data[f"discovered_{category}"] = [asdict(d) for d in archive.documents]
                    tracker.progress(discovery_stage, discovered=len(archive.documents), processed=archive.pages,
                                     network_requests=len(browser.access_events))
                    for error in archive.errors:
                        category_errors.append(error)
                        manifest.error(error)
                    manifest.data.setdefault("archive_errors", {})[category] = [asdict(error) for error in category_errors]
                    documents = archive.documents
                    if max_documents and len(documents) > max_documents:
                        manifest.error(Issue("SAFETY_LIMIT_REACHED", f"Explicit test limit: only {max_documents} {category} documents"))
                        documents = documents[:max_documents]
                    documents = sort_documents(documents, as_of_date=as_of_date)
                    manifest.data[category] = [asdict(d) for d in archive.documents]
                    manifest.write()
                    for index, document in enumerate(documents, 1):
                        annotate_source_identity(document, canonical_cik=manifest.data.get("issuer_cik"), canonical_name=stock.company_name)
                        if not document.source_identity_eligible:
                            failed_downloads += 1
                            document.status = "REJECTED"
                            document.failure_stage = "SOURCE_IDENTITY"
                            document.failure_code = "SOURCE_IDENTITY_INCOMPATIBLE"
                            document.failure_classification = "SOURCE_IDENTITY_INCOMPATIBLE"
                            document.failure_reason = "Source registrant is not compatible with the canonical ticker registrant"
                            manifest.document(document)
                            manifest.error(Issue(document.failure_code, document.failure_reason, document.source_url))
                            continue
                        tracker.check()
                        tracker.start(download_stage, item=document.source_url)
                        event_start = len(browser.access_events)
                        existing_record = downloader.existing.get(canonicalize_url(document.source_url)) if downloader else None
                        existing_artifact = bool(existing_record and downloader._existing_valid(document, existing_record)) if downloader else False
                        try:
                            log.info("Downloading %s %s/%s: %s", category, index, len(documents), document.source_url)
                            saved = await downloader.save(document)
                            saved.status = "CACHED" if saved.cache_hit else "DOWNLOADED"
                            saved.existing_artifact = bool(saved.cache_hit)
                            saved.attempt_count = 0 if saved.cache_hit else max(1, len(browser.access_events) - event_start)
                            saved.retry_count = max(0, saved.attempt_count - 1)
                            for target in (saved.pdf_source_url,):
                                if target:
                                    domain = urlsplit(target).hostname
                                    if domain not in discovery.ecosystem.allowed_document_domains:
                                        discovery.ecosystem.allowed_document_domains.append(domain)
                                    discovery.ecosystem.relationships.append({"source_url": saved.linked_from_url or saved.source_url, "target_url": target, "relation": "Linked document asset"})
                            manifest.data["ecosystem"] = asdict(discovery.ecosystem)
                            manifest.document(saved)
                            tracker.progress(download_stage, processed=index,
                                             saved=sum(1 for row in manifest.data.get(category, []) if row.get("status") in {"DOWNLOADED", "CACHED", "REUSED"}),
                                             reused=sum(1 for row in manifest.data.get(category, []) if row.get("status") == "CACHED"),
                                             failed=failed_downloads,
                                             network_requests=len(browser.access_events))
                        except Exception as exc:
                            failed_downloads += 1
                            log.exception("Document failed: %s", document.source_url)
                            code = getattr(exc, "code", "PDF_DOWNLOAD_FAILED" if document.pdf_source_url else "HTML_TO_PDF_FAILED")
                            document.status = "BLOCKED" if code == "SITE_BLOCKED" else "FAILED"
                            relevant_events = browser.access_events[event_start:]
                            targets = {canonicalize_url(document.source_url)}
                            if document.pdf_source_url:
                                targets.add(canonicalize_url(document.pdf_source_url))
                            targets.update(canonicalize_url(url) for url in document.alternate_pdf_urls)
                            relevant_events = [event for event in relevant_events if canonicalize_url(event.url) in targets]
                            document.attempt_count = max(1, len(relevant_events))
                            document.retry_count = max(0, document.attempt_count - 1)
                            document.http_status = next((event.status for event in reversed(relevant_events) if event.status is not None), document.http_status)
                            document.existing_artifact = existing_artifact
                            document.failure_code = code
                            document.failure_stage = failure_stage(code, str(exc), document)
                            document.failure_classification = classify_failure(code, str(exc), http_status=document.http_status, status=document.status, existing_artifact=existing_artifact, document=document)
                            document.failure_reason = str(exc)
                            manifest.document(document)
                            manifest.error(Issue(code, str(exc), document.source_url))
                            tracker.progress(download_stage, processed=index, failed=failed_downloads,
                                             network_requests=len(browser.access_events))
                    tracker.complete(download_stage, processed=len(documents), failed=failed_downloads,
                                     network_requests=len(browser.access_events))
                    coverage = critical_source_coverage(
                        manifest.data.get("reports", []),
                        as_of_date=as_of_date,
                    )
                    manifest.data["critical_source_coverage"] = coverage
                    manifest.data["critical_source_coverage_status"] = (
                        "COMPLETE" if coverage.get("minimum_research_corpus_acquired") else "INCOMPLETE"
                    )
                    if coverage.get("minimum_research_corpus_acquired"):
                        manifest.data["minimum_research_corpus_acquired"] = True
                        tracker.progress(
                            "CRITICAL_COVERAGE_CHECKPOINT",
                            item=stock.ticker,
                            priority_tier=1,
                            **{key: value for key, value in coverage.items() if isinstance(value, bool)},
                        )
                    if category == "reports":
                        tracker.start(
                            "GOVERNANCE_MANAGEMENT_DISCOVERY",
                            item=stock.ticker,
                            priority_tier=2,
                            budget_seconds=stage_budget("governance"),
                            budget_remaining_seconds=stage_budget("governance"),
                        )
                        tracker.complete(
                            "GOVERNANCE_MANAGEMENT_DISCOVERY",
                            processed=len(manifest.data.get("reports", [])),
                            priority_tier=2,
                            governance_source=bool(coverage.get("governance")),
                            management_source=bool(coverage.get("management")),
                        )
                if browser.config.events.enabled:
                    tracker.start("EVENT_DISCOVERY", item=stock.ticker)
                    ir_setup_seconds=time.perf_counter()-company_started
                    try:
                        event_metrics = await asyncio.wait_for(
                            collect_events(browser, context, stock, discovery.ecosystem, folder, manifest),
                            timeout=float(getattr(browser.config, "stage_timeout", 900.0)),
                        )
                        tracker.complete("EVENT_DETAIL", discovered=event_metrics.get("events_discovered", 0),
                                         processed=event_metrics.get("events_processed", 0),
                                         network_requests=len(browser.access_events))
                        tracker.complete("EVENT_DISCOVERY", discovered=event_metrics.get("events_discovered", 0),
                                         processed=event_metrics.get("events_processed", 0),
                                         network_requests=len(browser.access_events))
                        event_metrics.setdefault('timings',{})['ir_setup']=ir_setup_seconds
                        if event_metrics["event_failures"] or manifest.data.get("events_discovery_errors") or not event_metrics["events_discovered"]:
                            manifest.error(Issue("EVENTS_PARTIAL", "Events collection has incomplete discovery or stopped events"))
                    except Exception as exc:
                        if isinstance(exc, (AcquisitionProgressTimeout, AcquisitionDeadlineExceeded)):
                            raise
                        manifest.error(Issue(getattr(exc,"code","EVENTS_FAILED"),str(exc)))
                any_saved = any(e.get("status") == "TRANSCRIBED" for e in manifest.data.get("events",[])) or any(d.get("status") in {"DOWNLOADED", "CACHED"} for category in ("news", "reports") for d in manifest.data[category])
                if not winner.access.browser_accessible and not any_saved:
                    manifest.data["status"] = "SITE_BLOCKED" if winner.access.blocked else "IR_ACCESS_FAILED"
                elif winner.access.browser_accessible and not winner.content_validated and not cached_identity:
                    manifest.data["status"] = "IR_VALIDATION_FAILED"
                else:
                    manifest.data["status"] = "PARTIAL" if manifest.data["errors"] else "SUCCESS"
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        tracker.interrupted(type(exc).__name__)
        manifest.data["status"] = "INTERRUPTED"
        manifest.data["acquisition_status"] = "INTERRUPTED"
        manifest.write()
        raise
    except (AcquisitionProgressTimeout, AcquisitionDeadlineExceeded) as exc:
        tracker.finalize("TIMED_OUT", reason=exc.code, diagnostics=exc.diagnostics)
        manifest.data["status"] = "PARTIAL"
        manifest.data["acquisition_status"] = exc.code
        manifest.error(Issue(exc.code, str(exc)))
        manifest.write()
    except Exception as exc:
        log.exception("Company processing failed: %s", stock.ticker)
        manifest.error(Issue(getattr(exc, "code", "COMPANY_FAILED"), str(exc)))
        manifest.data["status"] = company_outcome(runtime.selected, "FAILED")
    finally:
        manifest.data.update(runtime.access_record(browser.access_events))
        if runtime.discovery and hasattr(runtime.discovery,'diagnostics'):
            diagnostics=runtime.discovery.diagnostics()
            if isinstance(diagnostics,dict):manifest.data['event_discovery_diagnostics']=diagnostics
        if manifest.data.get('ir_official') and event_metrics and not event_metrics.get('events_discovered'):
            event_diag = manifest.data.get('event_discovery_diagnostics', {})
            if bool(event_diag.get('access_blocked')) or any(e.blocked for e in browser.access_events):
                event_metrics['event_outcome'] = 'IR_VERIFIED_ACCESS_BLOCKED'
            elif not event_metrics.get('event_outcome'):
                event_metrics['event_outcome'] = 'IR_VERIFIED_EVENT_DISCOVERY_FAILED'
        elif scan_only and event_metrics and event_metrics.get('event_outcome') == 'CANDIDATE_SCANNED':
            event_metrics['event_outcome'] = 'EVENTS_FOUND'

        # Finalize only after discovery diagnostics and access records have been
        # attached.  Module outcomes remain available independently.
        # Discovery alone is not acquisition evidence.  A fallback family is
        # meaningful only after at least one issuer-verified artifact is saved
        # or reused from a validated cache.
        acquired_official = []
        for category in ("news", "reports"):
            acquired_official.extend(
                record for record in manifest.data.get(category, [])
                if record.get("issuer_verified") and record.get("source_family")
                and record.get("status") in {"DOWNLOADED", "CACHED", "REUSED"}
            )
        manifest.data["official_source_evidence"] = bool(acquired_official)
        if not acquired_official and manifest.data.get("primary_ir_status") == "BLOCKED":
            manifest.data["official_source_coverage_status"] = "ALL_OFFICIAL_SOURCES_UNUSABLE"
            limits = list(manifest.data.get("official_source_limitations", []))
            if "ALL_OFFICIAL_SOURCES_UNUSABLE" not in limits:
                limits.append("ALL_OFFICIAL_SOURCES_UNUSABLE")
            manifest.data["official_source_limitations"] = limits
        _finalize_production_status(
            manifest,
            winner,
            event_metrics,
            browser,
            runtime,
            scan_only=scan_only,
            downloader=downloader,
        )

        if scan_only:
            prepared[key] = runtime
            browser.prepared_companies = prepared
        manifest.data["access_events"] = safe_record([asdict(e) for e in browser.access_events])
        manifest.write()
        logging.getLogger().removeHandler(handler)
        handler.close()
    news_doc_metrics = document_metrics(manifest.data, "news")
    reports_doc_metrics = document_metrics(manifest.data, "reports")
    reports_accounting = report_metrics(
        manifest.data.get("reports", []),
        discovered=len(manifest.data.get("discovered_reports", [])),
        raw_candidates=manifest.data.get("raw_report_candidates", len(manifest.data.get("discovered_reports", []))),
    )
    news = news_doc_metrics["newly_saved"]
    reports = reports_doc_metrics["newly_saved"]
    summary = {"verified_ir_cache_hit":runtime.verified_cache_hit,"ir_discovery_required":not runtime.verified_cache_hit or browser.config.revalidate_ir,**asdict(stock), "ir_found": manifest.data["ir_official"], "ir_official": manifest.data["ir_official"], "ir_accessible": manifest.data["ir_accessible"], "news_discovered": news_doc_metrics["discovered"], "news": news, "news_saved": news, "news_reused": news_doc_metrics["cached"], "reports_discovered": reports_accounting["reports_discovered"], "reports": reports, "reports_saved": reports_accounting["reports_saved"], "reports_reused": reports_accounting["reports_reused"], "reports_validated": reports_accounting["reports_validated"], "reports_failed": reports_accounting["reports_failed"], "raw_report_candidates": reports_accounting["raw_report_candidates"], "access_blocks": sum(e.blocked for e in browser.access_events), "discovery_errors": sum(e.code != "SITE_BLOCKED" for e in discovery.errors) if discovery else 1, "document_failures": failed_downloads, "failed_downloads": failed_downloads, "errors": len(manifest.data["errors"]), "status": manifest.data["status"], "overall_status": manifest.data.get("overall_status", manifest.data["status"]), "folder": str(folder)}
    summary.update(identify(stock.company_name).record())
    summary['ir_discovery_diagnostics']=manifest.data.get('ir_discovery_diagnostics',{})
    summary['event_discovery_diagnostics']=manifest.data.get('event_discovery_diagnostics',{})
    summary['ir_status'] = manifest.data.get('ir_status')
    summary['primary_ir_status'] = manifest.data.get('primary_ir_status')
    summary['official_source_status'] = manifest.data.get('official_source_status', {})
    summary['official_source_evidence_families'] = manifest.data.get('official_source_evidence_families', [])
    summary['official_source_limitations'] = manifest.data.get('official_source_limitations', [])
    summary['official_source_coverage_status'] = manifest.data.get('official_source_coverage_status', 'ALL_OFFICIAL_SOURCES_UNUSABLE')
    summary['news_status'] = manifest.data.get('news_status')
    summary['news_coverage_diagnostic'] = manifest.data.get('news_coverage_diagnostic')
    summary['reports_status'] = manifest.data.get('reports_status')
    summary['report_coverage'] = manifest.data.get('report_coverage', {})
    summary['critical_source_coverage'] = manifest.data.get('critical_source_coverage', {})
    summary['critical_source_coverage_status'] = manifest.data.get('critical_source_coverage_status', 'NOT_AVAILABLE')
    summary['critical_source_limitations'] = manifest.data.get('critical_source_limitations', [])
    summary['events_status'] = manifest.data.get('events_status')
    summary['overall_status'] = manifest.data.get('overall_status', manifest.data.get('status'))
    summary['completeness'] = manifest.data.get('completeness', {})
    summary['limitations'] = manifest.data.get('completeness', {}).get('limitations', [])
    for category in ("news", "reports"):
        summary[category + "_blocked"] = sum(d.get("status") == "BLOCKED" for d in manifest.data[category])
        if category == "reports":
            summary[category + "_failed"] = reports_accounting["reports_failed"]
        else:
            summary[category + "_failed"] = sum(d.get("status") == "FAILED" for d in manifest.data[category])
    summary["news_rss_generated"] = sum(d.get("status") in {"DOWNLOADED", "CACHED"} and d.get("method") == "official_rss_to_pdf" for d in manifest.data["news"])
    summary["news_wire_generated"] = sum(d.get("status") in {"DOWNLOADED", "CACHED"} and d.get("method") == "verified_wire_source_html_to_pdf" for d in manifest.data["news"])
    summary.update(event_metrics)
    summary.update({key: manifest.data.get("cache_metrics", {}).get(key, 0) for key in ("artifact_cache_hits", "documents_skipped_existing", "event_cache_hits", "network_requests_avoided")})
    summary.setdefault('timings',{})['total_company']=time.perf_counter()-company_started
    manifest.data['timings']=summary['timings']
    summary.update(runtime.access_record(browser.access_events))
    summary["network_requests"] = len(manifest.data.get("access_events", []))
    summary["official_ir_url"] = runtime.verified_ir.url if runtime.verified_ir else None
    summary["officiality_score"] = runtime.verified_ir.officiality_score if runtime.verified_ir else None
    manifest.data["reports_validated"] = reports_accounting["reports_validated"]
    manifest.data["metrics"] = {k: summary[k] for k in ("news_discovered", "news", "news_saved", "news_reused", "reports_discovered", "reports", "reports_saved", "reports_reused", "reports_validated", "raw_report_candidates", "access_blocks", "discovery_errors", "document_failures", "news_blocked", "news_failed", "reports_blocked", "reports_failed", "news_rss_generated", "news_wire_generated", "artifact_cache_hits", "documents_skipped_existing", "event_cache_hits", "network_requests_avoided")}
    tracker.start("MANIFEST_FINALIZATION", item=stock.ticker)
    acquisition_status = str(manifest.data.get("acquisition_status") or "")
    if acquisition_status in {"ACQUISITION_DEADLINE_EXCEEDED", "ACQUISITION_PROGRESS_TIMEOUT"}:
        progress_final_status = "TIMED_OUT"
    elif acquisition_status == "INTERRUPTED":
        progress_final_status = "INTERRUPTED"
    else:
        progress_final_status = "COMPLETE"
    tracker.finalize(progress_final_status)
    summary["acquisition_progress"] = manifest.data.get("acquisition_progress", {})
    summary["acquisition_status"] = manifest.data.get("acquisition_status", manifest.data.get("status"))
    manifest.write()
    log.info("Finished %s: official=%s accessible=%s news=%s(saved=%s cached=%s) reports=%s(saved=%s cached=%s) discovery_errors=%s access_blocks=%s document_failures=%s overall=%s events=%s", stock.ticker, summary["ir_official"], summary["ir_accessible"], news_doc_metrics["discovered"], news, news_doc_metrics["cached"], reports_doc_metrics["discovered"], reports, reports_doc_metrics["cached"], summary["discovery_errors"], summary["access_blocks"], failed_downloads, summary["overall_status"], summary.get("events_status"))
    return summary

def save_summary(root: Path, timestamp: str, rows: list[dict]) -> None:
    stem = root / f"qualitative_analysis_run_summary_{timestamp}"
    atomic_json(stem.with_suffix(".json"), rows)
    if rows:
        with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
            writer.writeheader()
            writer.writerows(rows)

async def run(args) -> int:
    run_started=time.perf_counter()
    integration_search = args.find_transcribable_event or args.find_generatable_event
    event_search = integration_search or args.discover_registration_destinations
    if args.skip_events and event_search:
        raise ValueError("Event search cannot be combined with --skip-events")
    config = Config(as_of_date=getattr(args, "as_of_date", None), service_account_path=args.service_account, download_root=args.download_root,
                    request_delay=args.request_delay, max_archive_pages=args.max_archive_pages,
                    max_links_per_company=args.max_links_per_company, max_interactions=args.max_interactions, revalidate_ir=args.rediscover_ir,
                    no_progress_timeout=args.no_progress_timeout,
                    acquisition_deadline=args.acquisition_deadline,
                    stage_timeout=args.stage_timeout,
                    critical_financial_budget=args.critical_financial_budget,
                    governance_budget=args.governance_budget,
                    news_budget=args.news_budget,
                    deep_archive_budget=args.deep_archive_budget,
                    archive_no_new_document_threshold=args.archive_no_new_document_threshold,
                    http_total_timeout=args.http_timeout,
                    events=EventConfig(enabled=not args.skip_events,approved_registration_domains=tuple(args.approved_registration_domain),max_media_attempts=args.max_media_attempts,discover_registration_destinations=args.discover_registration_destinations,skip_registration=args.skip_event_registration or args.discover_registration_destinations,generated_only=args.find_generatable_event,require_new_generated_event=args.require_new_generated_event,only=args.events_only or event_search,force=args.force_events,limit=args.event_limit or 1,model=args.whisper_model,language=None if args.whisper_language=="auto" else args.whisper_language,device=args.whisper_device,keep_media=args.keep_event_media,ffmpeg=args.ffmpeg))
    from dataclasses import replace
    config=replace(config,events=replace(config.events,transcription_profile=args.transcription_profile,cpu_threads=args.cpu_threads,beam_size=args.beam_size,batch_size=args.batch_size))
    config.download_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    setup_logging(config.download_root / f"qualitative_analysis_run_{timestamp}.log", args.verbose)
    if args.benchmark_transcription:
        if not args.benchmark_audio:raise ValueError('--benchmark-audio must point to retained local audio')
        from .benchmark import benchmark
        await benchmark(config.events,args.benchmark_audio,args.benchmark_seconds,args.benchmark_output,args.benchmark_model)
        return 0
    rows = []
    try:
        manual_company_name = str(getattr(args, "company_name", "") or "").strip()
        manual_issuer_cik = str(getattr(args, "issuer_cik", "") or "").strip() or None
        if manual_company_name:
            if not args.ticker:
                raise ValueError("--company-name requires --ticker")
            # The runner supplies an authoritative issuer identity for an
            # explicit ticker. This path intentionally never reads Sheets.
            stocks = [Stock(args.ticker.upper(), manual_company_name, "manual", 0)]
            sheets_time = 0.0
        else:
            sheets_started=time.perf_counter()
            stocks = await asyncio.to_thread(read_stocks, config, args.worksheet, args.ticker)
            sheets_time=time.perf_counter()-sheets_started
            stocks = select_stocks(stocks, args.ticker, args.start_ticker, args.limit)
        if event_search:
            unique_stocks={}
            for stock in stocks:
                unique_stocks.setdefault((stock.ticker.casefold(),company_key(stock.company_name)),stock)
            stocks=list(unique_stocks.values())
            stocks = stocks[:args.max_companies or (20 if args.find_generatable_event else 10)]
        if not stocks:
            log.warning("No valid qualifying stocks")
            return 1
        if args.list_only:
            for stock in stocks:
                log.info("Selected %s %s %s row=%s", stock.ticker, stock.company_name, stock.worksheet, stock.spreadsheet_row)
            return 0
        async with Browser(config) as browser:
            browser.media_budget = MediaBudget(args.max_media_attempts)
            if args.find_generatable_event and not args.discover_registration_destinations:
                scans = []
                for stock in stocks:
                    scan_started=time.perf_counter()
                    result = await process_company(browser, stock, timestamp, args.max_documents, scan_only=True,
                                                   issuer_cik=manual_issuer_cik,
                                                   issuer_resolution_source="pipeline_runner" if manual_company_name else None,
                                                   as_of_date=config.as_of_date)
                    result["scan_seconds"]=time.perf_counter()-scan_started
                    scans.append((stock, result))
                    rows.append(result)
                    save_summary(config.download_root,timestamp,rows)
                scans.sort(key=lambda pair: pair[1].get("candidate_priority", -10000), reverse=True)
                stocks = [stock for stock, result in scans if result.get("ir_official")]
            for stock in stocks:
                try:
                    result = await process_company(browser, stock, timestamp, args.max_documents,
                                                   issuer_cik=manual_issuer_cik,
                                                   issuer_resolution_source="pipeline_runner" if manual_company_name else None,
                                                   as_of_date=config.as_of_date)
                    result.setdefault('timings',{}).update(google_sheets=sheets_time,total_run=time.perf_counter()-run_started)
                    log.info('Timing: %s',result['timings'])
                    row_index=next((i for i,r in enumerate(rows) if r.get("ticker")==stock.ticker and company_key(r.get("company_name",stock.company_name))==company_key(stock.company_name)),None)
                    if row_index is None: rows.append(result)
                    else:
                        result["scan_seconds"]=rows[row_index].get("scan_seconds",0)
                        result["ir_discovery_diagnostics"]=rows[row_index].get("ir_discovery_diagnostics",{})
                        result["event_discovery_diagnostics"]=rows[row_index].get("event_discovery_diagnostics",{})
                        rows[row_index]=result
                    success_count = result.get("generated_transcript_created" if args.require_new_generated_event else "generated_transcript_available", 0) if args.find_generatable_event else result.get("generated_transcript_available", 0) + result.get("official_transcripts", 0) + result.get("official_captions", 0)
                    if integration_search and not args.discover_registration_destinations and success_count > 0:
                        log.info("%s integration event found; stopping search", "Generated transcript" if args.find_generatable_event else "Transcribable")
                        break
                except Exception as exc:
                    # Includes folder creation failures; the remaining universe still proceeds.
                    log.exception("Company setup failed")
                    cached = ir_state.load(config.download_root,stock) or ir_state.load(Path(__file__).resolve().parent.parent,stock)
                    rows.append({**asdict(stock), "ir_found":bool(cached), "ir_official":bool(cached), "ir_accessible":False, "ir_status":"DISCOVERY_FAILED" if not cached else "ACCESS_BLOCKED", "news_status":"NOT_SCANNED", "reports_status":"NOT_SCANNED", "events_status":"NOT_SCANNED", "overall_status":"FAILED", "completeness":{"ir":bool(cached),"news":None,"reports":None,"events":None,"transcript":False,"limitations":[]}, "news_discovered":0, "news":0, "news_saved":0, "news_reused":0, "reports_discovered":0, "reports":0, "raw_report_candidates":0, "reports_validated":0, "reports_saved":0, "reports_reused":0, "access_blocks":0, "discovery_errors":1, "document_failures":0, "failed_downloads":0, "errors":1, "status":"FAILED", "folder":"", "news_blocked":0, "news_failed":0, "reports_blocked":0, "reports_failed":0, "news_rss_generated":0, "news_wire_generated":0, "artifact_cache_hits":0, "documents_skipped_existing":0, "event_cache_hits":0, "network_requests_avoided":0})
                save_summary(config.download_root, timestamp, rows)
    except Exception:
        log.exception("RUN_FAILED")
        return 1
    finally:
        if args.discover_registration_destinations:
            destinations=[]
            for row in rows:
                try:
                    data=__import__('json').loads((Path(row['folder'])/'manifest.json').read_text(encoding='utf-8'))
                    for event in data.get('events',[]):
                        if event.get('registration_destination_domain') and event.get("status")=="REGISTRATION_DESTINATION_DISCOVERED":
                            destinations.append({'ticker':row['ticker'],'company_name':row.get('company_name'),'event':event.get('title'),'event_date':event.get('date'),'event_url':event.get('event_url'),'webcast_url':event.get('webcast_url'),'provider':event.get('provider'),'registration_page_url':event.get('registration_page_url'),'form_action':event.get('registration_form_action'),'domain':event.get('registration_destination_domain'),'fields':event.get('registration_fields',[]),'submission_count':0})
                except (OSError,ValueError,TypeError): pass
            atomic_json(config.download_root/f'registration_destinations_{timestamp}.json',destinations)
            log.info('REGISTRATION DESTINATIONS requiring approval: %s; submissions=0',len(destinations))
        save_summary(config.download_root, timestamp, rows)
    if event_search or args.events_only:
        log.info("Ticker | IR | Public usable | Events | Selected event | Provider | Official Transcript Found | Official Captions Found | Media | Protected | Generated Created/Reused/Available | Outcome")
        for row in rows:
            log.info("%s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | overall=%s event=%s", row["ticker"], row["ir_official"], row["ir_accessible"], row.get("events_discovered",0), row.get("selected_event",""), row.get("webcast_provider",""), row.get("official_transcript_discovered",0), row.get("official_captions_discovered",0), row.get("selected_media") or "n/a", row.get("media_blocked",0), f"{row.get('generated_transcript_created',0)}/{row.get('generated_transcript_reused',0)}/{row.get('generated_transcript_available',0)}", row.get("overall_status", row["status"]), row.get("events_status"))
    else:
        log.info("Ticker | IR Official | IR Usable | News | Reports | Status")
        for row in rows:
            log.info("%s | %s | %s | %s(saved=%s cached=%s) | %s(saved=%s cached=%s) | overall=%s",row["ticker"],row["ir_official"],row["ir_accessible"],row["news_discovered"],row["news_saved"],row["news_reused"],row["reports_discovered"],row["reports_saved"],row["reports_reused"],row.get("overall_status",row["status"]))
    for row in rows:
        if "events_discovered" in row:
            log.info('Event coverage %s: listing_unique=%s final_unique=%s historical=%s future=%s undated=%s detail_pages=%s webcast_count_complete=%s',row['ticker'],row.get('events_listing_unique'),row['events_discovered'],row.get('events_historical'),row.get('events_future'),row.get('events_undated'),row.get('event_details_inspected'),row.get('webcast_count_complete'))
            if row.get('transcript_qa'):log.info('Transcript QA %s: model=%s %s',row['ticker'],row.get('transcription_model'),row['transcript_qa'])
            if row.get('transcription_performance'):log.info('Transcription performance %s: %s',row['ticker'],row['transcription_performance'])
            log.info('Resume summary %s: generated_created=%s generated_reused=%s generated_available=%s qa_status=%s media_attempts_used=%s media_attempts_skipped_cached=%s',row['ticker'],row.get('generated_transcript_created',0),row.get('generated_transcript_reused',0),row.get('generated_transcript_available',0),row.get('transcript_qa',{}).get('status'),row.get('media_attempts_used',0),row.get('media_attempts_skipped_cached',0))
            log.info("Events summary %s: events=%s webcasts=%s processed=%s reused=%s official_transcript_found=%s official_transcript_saved=%s official_captions_found=%s official_captions_saved=%s generated_created=%s generated_reused=%s protected=%s unavailable=%s failed=%s overall=%s event=%s", row["ticker"],row["events_discovered"],row["events_with_webcasts"],row["events_processed"],row.get("events_resumed",0),row.get("official_transcript_discovered",0),row.get("official_transcript_saved",0),row.get("official_captions_discovered",0),row.get("official_captions_saved",0),row.get("generated_transcript_created",0),row.get("generated_transcript_reused",0),row.get("media_blocked",0),row.get("media_not_found",0),row["event_failures"],row.get("overall_status",row["status"]),row.get("events_status"))
    if integration_search:
        totals={key:sum(row.get(key,0) for row in rows) for key in ('verified_ir_cache_hit','ir_discovery_required','events_processed','generated_transcript_created','generated_transcript_reused','generated_transcript_available','integration_skipped_official','media_blocked','media_attempts_used','media_attempts_skipped_cached','whisper_runs','media_downloads','news_discovered','news_saved','news_reused','reports_discovered','reports_saved','reports_reused','artifact_cache_hits','documents_skipped_existing','event_cache_hits','network_requests_avoided','network_requests')}
        totals.update(companies_scanned=len(rows),total_scan_time=sum(row.get('scan_seconds',0) for row in rows),total_transcription_time=sum(row.get('timings',{}).get('transcription',0) if row.get('whisper_runs') else 0 for row in rows))
        totals.update(verified_ir=sum(bool(r.get('ir_official')) for r in rows),ir_discovery_failures=sum(r.get('ir_status')=='DISCOVERY_FAILED' for r in rows),event_discovery_failures=sum(r.get('events_status') in {'IR_VERIFIED_EVENT_DISCOVERY_FAILED','IR_VERIFIED_ACCESS_BLOCKED'} for r in rows),status_counts={status:sum(r.get('overall_status',r.get('status'))==status for r in rows) for status in ('SUCCESS','SUCCESS_WITH_LIMITATIONS','PARTIAL','FAILED')})
        for key in ('search_queries','search_seconds','http_probes','browser_probes'):
            totals[key]=sum(r.get('ir_discovery_diagnostics',{}).get(key,0) for r in rows)
        totals['average_discovery_seconds_per_company']=sum(r.get('ir_discovery_diagnostics',{}).get('discovery_seconds',0) for r in rows)/max(1,len(rows))
        atomic_json(config.download_root/f'generated_search_summary_{timestamp}.json',totals)
        log.info('Generated search summary: %s',totals)
        keys=("generated_transcript_created" if args.require_new_generated_event else "generated_transcript_available",) if args.find_generatable_event else ("generated_transcript_available","official_transcripts","official_captions")
        return 0 if any(sum(row.get(key,0) for key in keys)>0 for row in rows) else 2
    production_totals = {
        "companies_processed": len(rows),
        "elapsed_seconds": time.perf_counter() - run_started,
        "status_counts": {status: sum(r.get("overall_status", r.get("status")) == status for r in rows) for status in ("SUCCESS", "SUCCESS_WITH_LIMITATIONS", "PARTIAL", "FAILED")},
        "ir": {
            "verified": sum(r.get("ir_status") == "SUCCESS" for r in rows),
            "blocked": sum(r.get("ir_status") == "ACCESS_BLOCKED" for r in rows),
            "failed": sum(r.get("ir_status") == "DISCOVERY_FAILED" for r in rows),
        },
        "news": {key: sum(r.get(key, 0) for r in rows) for key in ("news_discovered", "news_saved", "news_reused", "news_failed")},
        "reports": {key: sum(r.get(key, 0) for r in rows) for key in ("raw_report_candidates", "reports_discovered", "reports_validated", "reports_saved", "reports_reused", "reports_failed", "reports_blocked")},
        "events": {key: sum(r.get(key, 0) for r in rows) for key in ("events_discovered", "events_processed", "events_resumed", "media_blocked", "media_not_found", "event_failures")},
        "transcripts": {key: sum(r.get(key, 0) for r in rows) for key in ("official_transcript_saved_new", "official_transcript_reused", "generated_transcript_created", "generated_transcript_reused", "whisper_runs")},
        "run": {key: sum(r.get(key, 0) for r in rows) for key in ("network_requests", "media_downloads", "media_attempts_used", "artifact_cache_hits", "documents_skipped_existing", "event_cache_hits", "network_requests_avoided")},
    }
    atomic_json(config.download_root / f"production_run_summary_{timestamp}.json", production_totals)
    log.info("Production run summary: %s", production_totals)
    return 0 if all(r.get("overall_status", r["status"]) in {"SUCCESS", "SUCCESS_WITH_LIMITATIONS", "PARTIAL"} for r in rows) else 2

def positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed

def nonnegative(value: str) -> float:
    parsed = float(value)
    if parsed < 0 or not __import__("math").isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return parsed

def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Collect official IR news and report PDFs sequentially.")
    cli.add_argument("--service-account", type=Path, default=DEFAULT_SERVICE_ACCOUNT_PATH)
    cli.add_argument("--download-root", type=Path, default=DOWNLOAD_ROOT)
    cli.add_argument("--worksheet", choices=WORKSHEETS)
    group = cli.add_mutually_exclusive_group()
    group.add_argument("--ticker")
    group.add_argument("--start-ticker")
    cli.add_argument("--company-name", help="Resolved issuer name for an explicit manual ticker; bypasses worksheet selection")
    cli.add_argument("--issuer-cik", help="Resolved SEC issuer CIK for an explicit manual ticker")
    cli.add_argument("--as-of-date", help="PIT cutoff used to prioritize current documents")
    cli.add_argument("--limit", type=positive)
    cli.add_argument("--request-delay", type=nonnegative, default=1.0)
    cli.add_argument("--max-archive-pages", type=positive, default=250)
    cli.add_argument("--max-links-per-company", type=positive, default=10000)
    cli.add_argument("--max-interactions", type=positive, default=500)
    cli.add_argument("--no-progress-timeout", type=nonnegative, default=900.0,
                     help="Seconds without durable progress before acquisition stops")
    cli.add_argument("--acquisition-deadline", type=nonnegative, default=7200.0,
                     help="Maximum issuer acquisition wall-clock budget")
    cli.add_argument("--stage-timeout", type=nonnegative, default=900.0,
                     help="Maximum wall-clock seconds for one discovery/download stage")
    cli.add_argument("--critical-financial-budget", type=nonnegative, default=300.0,
                     help="Reserved wall-clock budget for annual/interim/results sources")
    cli.add_argument("--governance-budget", type=nonnegative, default=180.0,
                     help="Reserved wall-clock budget for governance/management sources")
    cli.add_argument("--news-budget", type=nonnegative, default=180.0,
                     help="Best-effort wall-clock budget for general news")
    cli.add_argument("--deep-archive-budget", type=nonnegative, default=60.0,
                     help="Best-effort wall-clock budget for deep archive traversal")
    cli.add_argument("--archive-no-new-document-threshold", type=positive, default=3,
                     help="Stop an archive route after this many low-yield pages")
    cli.add_argument("--http-timeout", type=nonnegative, default=45.0,
                     help="Overall timeout for one HTTP operation")
    cli.add_argument("--max-documents", type=positive, help="Testing only: cap downloads per category; marks truncated runs PARTIAL")
    cli.add_argument("--list-only", action="store_true", help="Verify read-only sheet access and selection without crawling")
    event_mode=cli.add_mutually_exclusive_group()
    event_mode.add_argument("--events-only",action="store_true",help="Only collect events; reuse the latest matching company folder")
    event_mode.add_argument("--skip-events",action="store_true",help="Run news/reports without transcription")
    cli.add_argument("--require-new-generated-event",action="store_true",help="Skip completed generated events and find a new one; never retranscribe cached artifacts")
    cli.add_argument("--force-events","--force-transcription",dest="force_events",action="store_true",help="Reprocess completed transcripts")
    cli.add_argument("--rediscover-ir",action="store_true",help="Force fresh IR discovery while retaining verified cache on failure")
    cli.add_argument("--event-limit",type=positive,help="Process at most N replay candidates, newest historical first")
    search_mode=cli.add_mutually_exclusive_group()
    search_mode.add_argument("--find-transcribable-event",action="store_true",help="Try at most one historical event per company until a legitimate transcript is created")
    search_mode.add_argument("--find-generatable-event",action="store_true",help="Find one event requiring local Whisper transcription")
    cli.add_argument("--skip-event-registration",action="store_true",help="Never submit registration details; use only ungated replays")
    cli.add_argument("--discover-registration-destinations",action="store_true",help="Inspect registration forms without filling or submitting them")
    cli.add_argument("--approved-registration-domain",action="append",default=[],help="Explicitly approved registrable domain for profile submission (repeatable)")
    cli.add_argument("--max-media-attempts",type=positive,help="Maximum full webcast registration/player/media attempts across this run")
    cli.add_argument("--max-companies",type=positive,help="Integration company limit: default 20 for generated search, otherwise 10")
    cli.add_argument("--whisper-model",help="Explicit model name/directory overrides the transcription profile")
    cli.add_argument('--transcription-profile',choices=('fast','balanced','maximum_accuracy'),default='balanced')
    cli.add_argument('--cpu-threads',type=positive)
    cli.add_argument('--beam-size',type=positive)
    cli.add_argument('--batch-size',type=positive,help='Override profile batch size (balanced=2; other profiles=1)')
    cli.add_argument('--benchmark-transcription',action='store_true')
    cli.add_argument('--benchmark-audio',type=Path)
    cli.add_argument('--benchmark-seconds',type=positive,default=300)
    cli.add_argument('--benchmark-output',type=Path,default=Path('artifacts/transcription_benchmark'))
    cli.add_argument('--benchmark-model',action='append',help='Repeat to select supported benchmark model names or local directories')
    cli.add_argument("--whisper-language",default="en",help="Language code, or auto")
    cli.add_argument("--whisper-device",choices=("auto","cpu","cuda"),default="auto")
    cli.add_argument("--keep-event-media",action="store_true")
    cli.add_argument("--ffmpeg",help="Path to ffmpeg.exe; PATH/bundled fallback otherwise")
    cli.add_argument("--verbose", action="store_true")
    return cli

def main() -> None:
    try:
        code = asyncio.run(run(parser().parse_args()))
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("Interrupted; completed document manifests are preserved")
        code = 130
    raise SystemExit(code)

if __name__ == "__main__":
    main()
