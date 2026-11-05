"""Safe, deterministic resolution of document wrappers to downloadable assets.

Archive pages frequently expose an HTML viewer or a download endpoint rather
than the underlying PDF.  This module is deliberately transport agnostic: it
only inspects URLs/HTML and applies the issuer scope supplied by the caller.
Network access and PDF validation remain the responsibility of ``Downloader``.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import tldextract

from .urls import canonicalize_url, public_url, rejected_domain

_DOMAIN = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
_DOCUMENT_WORDS = re.compile(
    r"annual|interim|quarterly|financial|report|presentation|results|earnings|"
    r"trading|governance|sustainability|esg|transcript|investor",
    re.I,
)
_TARGET_PARAMETERS = ("src", "url", "file", "document", "doc", "download", "pdf", "path", "target", "resource")
_WRAPPER_PATH = re.compile(r"(?:viewer|download|document|render|asset|file)\b", re.I)
_PRIVATE_HOST = re.compile(r"^(?:0|10|127|169\.254|192\.168|172\.(?:1[6-9]|2\d|3[0-1]))(?:\.|$)")


@dataclass
class DocumentTarget:
    original_url: str
    canonical_document_url: str
    resolution_method: str = "DIRECT_DOCUMENT"
    document_type_hint: str = "UNKNOWN"
    wrapper_detected: bool = False
    wrapper_type: str | None = None
    confidence: str = "MEDIUM"
    source_domain: str = ""
    final_domain: str = ""
    identity_eligible: bool = False
    diagnostics: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return asdict(self)


def _registrable(url: str) -> str:
    return _DOMAIN(url).top_domain_under_public_suffix or ""


def _decode(value: str) -> str:
    current = html_lib.unescape(str(value or "")).strip()
    for _ in range(2):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded.strip()
    return current


def _looks_document(url: str, text: str = "") -> bool:
    path = urlsplit(url).path.casefold()
    return path.endswith(_DOCUMENT_EXTENSIONS) or bool(_DOCUMENT_WORDS.search(path + " " + text))


def _allowed_domains(candidate_url: str, issuer_context) -> set[str]:
    values: list[str] = []
    if isinstance(issuer_context, dict):
        for key in ("official_corporate_domain", "corporate_domain", "source_domain", "allowed_document_domains"):
            value = issuer_context.get(key)
            values.extend(value if isinstance(value, (list, tuple, set)) else [value] if value else [])
        values.extend(issuer_context.get("verified_hosted_ir_domains", []) or [])
        values.extend(issuer_context.get("verified_related_domains", []) or [])
    else:
        for key in ("official_corporate_domain", "corporate_domain", "allowed_document_domains", "verified_hosted_ir_domains"):
            value = getattr(issuer_context, key, None) if issuer_context is not None else None
            values.extend(value if isinstance(value, (list, tuple, set)) else [value] if value else [])
    source = _registrable(candidate_url)
    allowed = {_registrable(str(value)) for value in values if value}
    if source:
        allowed.add(source)
    return {item for item in allowed if item}


def _safe_target(url: str, *, base_url: str, issuer_context, text: str = "") -> tuple[str, bool, list[str]]:
    candidate = canonicalize_url(_decode(url), base_url)
    diagnostics: list[str] = []
    if not candidate:
        return "", False, ["INVALID_TARGET_URL"]
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return "", False, ["UNSAFE_TARGET_SCHEME_OR_CREDENTIALS"]
    if not public_url(candidate) or _PRIVATE_HOST.match(host) or rejected_domain(candidate):
        return "", False, ["UNSAFE_TARGET_HOST"]
    allowed = _allowed_domains(base_url, issuer_context)
    target_domain = _registrable(candidate)
    if target_domain not in allowed:
        return "", False, ["TARGET_DOMAIN_NOT_VERIFIED"]
    if not _looks_document(candidate, text):
        return "", False, ["TARGET_NOT_DOCUMENT_LIKE"]
    diagnostics.append("TARGET_DOMAIN_ALLOWED")
    return candidate, True, diagnostics


def _query_target(url: str, issuer_context) -> tuple[str, str, list[str]]:
    parsed = urlsplit(url)
    rejected: list[str] = []
    for key, raw in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() not in _TARGET_PARAMETERS:
            continue
        decoded = _decode(raw)
        target, safe, diagnostics = _safe_target(decoded, base_url=url, issuer_context=issuer_context, text=decoded)
        if safe:
            return target, f"QUERY_PARAM_{key.upper()}", diagnostics
        rejected.extend(f"{key.upper()}_{item}" for item in diagnostics)
    return "", "", rejected


def _html_targets(body: bytes | str) -> list[tuple[str, str]]:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    if not text:
        return []
    soup = BeautifulSoup(text, "html.parser")
    values: list[tuple[str, str]] = []
    for selector, attribute, method in (
        ("a[href]", "href", "DOWNLOAD_LINK"),
        ("iframe[src]", "src", "IFRAME_SRC"),
        ("embed[src]", "src", "EMBED_SRC"),
        ("object[data]", "data", "OBJECT_DATA"),
        ('link[rel="canonical"][href]', "href", "CANONICAL_METADATA"),
    ):
        for node in soup.select(selector):
            value = node.get(attribute)
            if value:
                values.append((_decode(value), method))
    # Some viewers keep a document URL in a JSON data attribute or inline
    # configuration rather than a visible DOM element.
    for node in soup.select("[data-url], [data-src], [data-file], [data-document], [data-download]"):
        for attribute in ("data-url", "data-src", "data-file", "data-document", "data-download"):
            if node.get(attribute):
                values.append((_decode(node.get(attribute)), "EMBEDDED_METADATA"))
    for match in re.finditer(r"(?:src|url|file|document|download|resource)\s*[:=]\s*[\"']([^\"']+)[\"']", text, re.I):
        values.append((_decode(match.group(1)), "SCRIPT_DOCUMENT_URL"))
    return values


def _hint(url: str, context: str = "") -> str:
    text = f"{url} {context}".casefold()
    if "annual" in text:
        return "annual_report"
    if re.search(r"interim|half[- ]year|halfyear", text):
        return "interim_report"
    if re.search(r"trading update|trading", text):
        return "trading_update"
    if re.search(r"presentation|investor deck|results deck", text):
        return "investor_presentation"
    if "transcript" in text:
        return "earnings_transcript"
    if re.search(r"governance|proxy|agm|board", text):
        return "governance_report"
    if re.search(r"sustainability|esg", text):
        return "ESG_report"
    if re.search(r"quarterly|results", text):
        return "quarterly_report"
    return "other_report" if _looks_document(url, context) else "UNKNOWN"


def resolve_document_target(candidate_url: str, page_context=None, issuer_context=None) -> DocumentTarget:
    """Resolve a document candidate without performing network access.

    ``page_context`` may be HTML bytes/text, a response-like object, or a
    mapping containing ``body``/``headers``.  The resolver never permits an
    embedded URL to leave the issuer/CDN scope supplied by ``issuer_context``.
    """
    original = canonicalize_url(candidate_url)
    if not original:
        return DocumentTarget(str(candidate_url), "", "UNKNOWN", "UNKNOWN", diagnostics=["INVALID_ORIGINAL_URL"])
    target, method, diagnostics = _query_target(original, issuer_context)
    if target:
        return DocumentTarget(
            original, target, method, _hint(target), True, "QUERY_PARAMETER", "HIGH",
            _registrable(original), _registrable(target), True, diagnostics,
        )
    body = None
    headers: dict = {}
    response_url = original
    if page_context is not None:
        if isinstance(page_context, (bytes, str)):
            body = page_context
        elif isinstance(page_context, dict):
            body = page_context.get("body")
            headers = page_context.get("headers") or {}
            response_url = canonicalize_url(page_context.get("url") or original) or original
        else:
            body = getattr(page_context, "body", None)
            headers = getattr(page_context, "headers", {}) or {}
            response_url = canonicalize_url(getattr(page_context, "url", original)) or original
    if body is not None:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        if str(headers.get("content-type", "")).lower().startswith("application/pdf") or text.lstrip().startswith("%PDF-"):
            return DocumentTarget(original, response_url, "DIRECT_DOCUMENT", _hint(response_url), False, None, "HIGH", _registrable(original), _registrable(response_url), True, ["PDF_PAYLOAD"])
        for raw, html_method in _html_targets(body):
            resolved, safe, extra = _safe_target(raw, base_url=response_url, issuer_context=issuer_context, text=text)
            if safe:
                wrapper_type = "HTML_VIEWER" if html_method in {"IFRAME_SRC", "EMBED_SRC", "OBJECT_DATA", "SCRIPT_DOCUMENT_URL", "EMBEDDED_METADATA"} else "HTML_REPORT"
                return DocumentTarget(original, resolved, html_method, _hint(resolved, text), True, wrapper_type, "HIGH", _registrable(original), _registrable(resolved), True, extra)
        return DocumentTarget(original, response_url, "HTML_REPORT", _hint(response_url, text), True, "HTML_REPORT", "LOW", _registrable(original), _registrable(response_url), True, ["NO_SAFE_EMBEDDED_DOCUMENT"])
    path = urlsplit(original).path.casefold()
    direct = path.endswith(_DOCUMENT_EXTENSIONS)
    query_wrapper = bool(parse_qsl(urlsplit(original).query, keep_blank_values=True)) and any(key.casefold() in _TARGET_PARAMETERS for key, _ in parse_qsl(urlsplit(original).query, keep_blank_values=True))
    return DocumentTarget(original, original, "DIRECT_DOCUMENT" if direct else "UNKNOWN", _hint(original), query_wrapper, "DOCUMENT_WRAPPER" if query_wrapper else None, "HIGH" if direct else "LOW", _registrable(original), _registrable(original), bool(_allowed_domains(original, issuer_context)) and not query_wrapper, diagnostics or (["DIRECT_EXTENSION"] if direct else ["NO_DOCUMENT_HINT"]))


__all__ = ["DocumentTarget", "resolve_document_target"]
