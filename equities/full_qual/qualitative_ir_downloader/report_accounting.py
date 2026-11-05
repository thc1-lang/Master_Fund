"""Report candidate validation and failure accounting.

The archive crawler sees links, while the acquisition layer needs to count
genuine report/document candidates.  This module keeps that distinction
explicit and gives every failed report a stable, reviewable classification.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from .models import Document

REPORT_FAILURE_CLASSIFICATIONS = {
    "REAL_DOWNLOAD_FAILURE",
    "ACCESS_BLOCKED",
    "INVALID_DOCUMENT",
    "DUPLICATE_ALREADY_REPRESENTED",
    "NON_DOCUMENT_LINK",
    "SUPERSEDED_DUPLICATE",
    "HTML_PAGE_NOT_REPORT",
    "UNSUPPORTED_CONTENT_TYPE",
    "STALE_URL",
    "TRANSIENT_HTTP_ERROR",
    "EXPECTED_EXTERNAL_LIMITATION",
}

_REPORT_WORDS = re.compile(
    r"annual|quarterly|financial|report|presentation|results|earnings|sustainability|esg",
    re.I,
)


def is_report_candidate(document: Document | Mapping) -> bool:
    """Return whether a discovered item has a document asset to validate.

    Report archives may contain event pages, webcast links, and HTML landing
    pages.  A report candidate must have an explicit PDF/static-file asset (or
    a PDF URL); those links are validated later by the downloader.
    """
    category = document.category if isinstance(document, Document) else document.get("category")
    if category != "reports":
        return False
    source_family = document.source_family if isinstance(document, Document) else document.get("source_family")
    # SEC EDGAR filings are authoritative HTML/text documents as well as
    # PDFs.  They are validated by the SEC acquisition route and therefore do
    # not need an issuer-hosted ``.pdf`` suffix to be a report candidate.
    if source_family == "SEC_EDGAR":
        return True
    if source_family == "OFFICIAL_IR_FINANCIAL_RESULTS":
        provenance = document.provenance if isinstance(document, Document) else document.get("provenance") or []
        return any(str(item.get("artifact_type")) == "financial_result_artifact" for item in provenance if isinstance(item, Mapping))
    source = str(document.source_url if isinstance(document, Document) else document.get("source_url") or "")
    pdf_source = document.pdf_source_url if isinstance(document, Document) else document.get("pdf_source_url")
    title = document.title if isinstance(document, Document) else document.get("title") or ""
    path = urlsplit(source).path.lower()
    return bool(pdf_source or path.endswith(".pdf") or "/static-files/" in path)


def report_document_type(document: Document | Mapping) -> str:
    title = str(document.title if isinstance(document, Document) else document.get("title") or "")
    source = str(document.source_url if isinstance(document, Document) else document.get("source_url") or "")
    resolved = str(document.resolved_document_url if isinstance(document, Document) else document.get("resolved_document_url") or "")
    hint = str(document.document_type_hint if isinstance(document, Document) else document.get("document_type_hint") or "")
    if isinstance(document, Document):
        provenance = document.provenance or []
    else:
        provenance = document.get("provenance") or []
    forms = " ".join(
        str(item.get("form_type") or item.get("sec_form") or "")
        for item in provenance
        if isinstance(item, Mapping)
    )
    text = f"{title} {source} {resolved} {hint} {forms}".lower()
    # Foreign private issuers use 20-F for the annual filing and 6-K for
    # current reports.  Keep the form evidence in classification without
    # pretending that every 6-K is an interim financial statement.
    if re.search(r"\b20[- ]?f\b", text):
        return "annual_report"
    if re.search(r"\b6[- ]?k\b", text):
        if re.search(r"interim|half[- ]year|halfyear|quarter|results", text):
            return "interim_report"
        if re.search(r"trading", text):
            return "trading_update"
    for label, pattern in (
        ("annual_report", r"annual"),
        ("interim_report", r"interim|half[- ]year|halfyear"),
        ("quarterly_report", r"quarterly|q[1-4](?:\b|[-_])"),
        ("trading_update", r"trading update|trading"),
        ("investor_presentation", r"presentation|investor[- ]?deck|business update|results deck"),
        ("earnings_transcript", r"transcript"),
        ("governance_report", r"governance|proxy|agm|board"),
        ("ESG_report", r"sustainability|esg"),
    ):
        if re.search(pattern, text, re.I):
            return label
    return "other_report"


def report_coverage_diagnostics(records: Iterable[Mapping], *, discovered: int | None = None) -> dict:
    """Separate raw report volume from usable and critical-source coverage."""
    rows = list(records or [])
    metrics = report_metrics(rows, discovered=discovered)
    usable = sum(
        record.get("status") in {"DOWNLOADED", "CACHED", "REUSED"}
        and not (record.get("status") == "DOWNLOADED" and record.get("duplicate_of"))
        for record in rows
    )
    raw = metrics["reports_discovered"]
    rate = usable / raw if raw else 0.0
    types = {report_document_type(record) for record in rows if record.get("status") in {"DOWNLOADED", "CACHED", "REUSED"}}
    critical = {
        "latest_annual": "annual_report" in types,
        "latest_interim": bool(types & {"interim_report", "quarterly_report"}),
        "trading_update": "trading_update" in types,
        "results_presentation": "investor_presentation" in types,
        "governance_source": "governance_report" in types,
    }
    severe = bool(raw >= 10 and (rate < 0.5 or metrics["reports_failed"] + metrics["reports_blocked"] >= raw * 0.5))
    limitations = ["REPORT_DOWNLOAD_COVERAGE_CRITICAL"] if severe else []
    return {
        "raw_report_candidates": raw,
        "resolved_document_targets": sum(bool(record.get("resolved_document_url")) for record in rows),
        "downloaded_reports": usable,
        "failed_reports": metrics["reports_failed"],
        "success_rate": rate,
        "critical_financial_report_coverage": critical,
        "critical_financial_report_coverage_complete": all(critical.values()),
        "limitations": limitations,
    }


def failure_stage(code: str | None, message: str = "", document: Document | Mapping | None = None) -> str:
    code = (code or "").upper()
    message = message.lower()
    if "invalid pdf structure" in message or "encrypted or empty pdf" in message:
        return "pdf_validation"
    if "not a pdf" in message or "missing pdf signature" in message:
        return "content_validation"
    if code in {"SITE_BLOCKED", "ACCESS_BLOCKED", "CAPTCHA_REQUIRED"}:
        return "access_check"
    if code in {"HTTP_FAILED", "TRANSIENT_HTTP_ERROR", "TIMEOUT", "NAVIGATION_FAILED"}:
        return "pdf_fetch"
    if document is not None and not is_report_candidate(document):
        return "candidate_validation"
    return "pdf_fetch"


def classify_failure(
    code: str | None,
    message: str = "",
    *,
    http_status: int | None = None,
    status: str | None = None,
    duplicate_of: str | None = None,
    existing_artifact: bool = False,
    document: Document | Mapping | None = None,
) -> str:
    """Map acquisition evidence to the controlled report classification set."""
    code_upper = (code or "").upper()
    text = message.lower()
    if duplicate_of:
        return "SUPERSEDED_DUPLICATE"
    if existing_artifact:
        return "DUPLICATE_ALREADY_REPRESENTED"
    if document is not None and not is_report_candidate(document):
        if "html" in text or "landing" in text or "report" not in text:
            return "HTML_PAGE_NOT_REPORT"
        return "NON_DOCUMENT_LINK"
    if status == "BLOCKED" or code_upper in {"SITE_BLOCKED", "ACCESS_BLOCKED", "CAPTCHA_REQUIRED"}:
        return "ACCESS_BLOCKED"
    if code_upper in {"UNSUPPORTED_CONTENT_TYPE"} or "unsupported content" in text:
        return "UNSUPPORTED_CONTENT_TYPE"
    if code_upper in {"STALE_URL", "NOT_FOUND", "HTTP_404"} or "404" in text:
        return "STALE_URL"
    if code_upper in {"HTTP_FAILED", "TRANSIENT_HTTP_ERROR", "TIMEOUT", "NAVIGATION_FAILED"} or http_status in {408, 425, 429} or (http_status is not None and http_status >= 500):
        return "TRANSIENT_HTTP_ERROR"
    if "invalid pdf" in text or "encrypted or empty pdf" in text or "missing pdf signature" in text:
        return "INVALID_DOCUMENT"
    if "expected pdf, received" in text and "text/html" in text:
        return "HTML_PAGE_NOT_REPORT"
    if code_upper == "PDF_DOWNLOAD_FAILED":
        return "REAL_DOWNLOAD_FAILURE"
    return "EXPECTED_EXTERNAL_LIMITATION" if code_upper else "REAL_DOWNLOAD_FAILURE"


def report_metrics(records: Iterable[Mapping], *, discovered: int | None = None, raw_candidates: int | None = None) -> dict[str, int]:
    """Return reconciled report counts for manifests and run summaries."""
    records = list(records or [])
    saved = sum(r.get("status") == "DOWNLOADED" and not r.get("cache_hit") and not r.get("duplicate_of") for r in records)
    reused = sum(bool(bool(r.get("cache_hit")) or r.get("status") in {"CACHED", "REUSED"} or (r.get("status") == "DOWNLOADED" and r.get("duplicate_of"))) for r in records)
    failed = sum(r.get("status") == "FAILED" for r in records)
    blocked = sum(r.get("status") == "BLOCKED" for r in records)
    validated = saved + reused + failed
    return {
        "raw_report_candidates": int(raw_candidates if raw_candidates is not None else discovered if discovered is not None else len(records)),
        "reports_discovered": int(discovered if discovered is not None else len(records)),
        "reports_validated": validated,
        "reports_saved": saved,
        "reports_reused": reused,
        "reports_failed": failed,
        "reports_blocked": blocked,
    }


def reconcile_report_metrics(metrics: Mapping) -> bool:
    """Check the production invariant for fully validated report sets."""
    return int(metrics.get("reports_validated", -1)) == int(metrics.get("reports_saved", 0)) + int(metrics.get("reports_reused", 0)) + int(metrics.get("reports_failed", 0))
