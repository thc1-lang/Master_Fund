"""HTTP original-PDF downloads with browser-cookie continuity and HTML fallback."""
import logging
import hashlib
import asyncio
from pathlib import Path
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .browser import Browser, links, BLOCK_TEXT
from .extraction import is_pdf, metadata
from .filesystem import document_path
from .models import CollectionError, Document, Link
from .pdf_utils import render_release, validate_pdf
from .urls import canonicalize_url, public_url
from .network import ResourceClient
from .structured import wire_link, WIRE_HOSTS
from .archival_pdf import render_archival
from .document_targets import DocumentTarget, resolve_document_target

log = logging.getLogger(__name__)

class Downloader:
    def __init__(self, browser: Browser, context, folder: Path, ticker: str, company_name: str = "", issuer_context=None):
        self.browser, self.context, self.folder, self.ticker = browser, context, folder, ticker
        self.company_name = company_name or ticker
        self.issuer_context = issuer_context
        self.hashes: dict[str, str] = {}
        self.urls: dict[str, Document] = {}
        self.existing: dict[str, dict] = {}
        self.resolved_existing: dict[str, dict] = {}
        self.cache_hits = 0
        self.network_requests_avoided = 0
        self.last_pdf_response: dict[str, object] = {}

    def register_existing(self, records: list[dict]) -> None:
        """Register manifest records eligible for identity-validated reuse."""
        for record in records or []:
            key = canonicalize_url(str(record.get("source_url", "")))
            if key:
                self.existing[key] = record
            resolved = canonicalize_url(str(record.get("resolved_document_url") or record.get("pdf_source_url") or ""))
            if resolved:
                self.resolved_existing[resolved] = record

    def _existing_valid(self, document: Document, record: dict, resolved_url: str | None = None) -> bool:
        if resolved_url is None and canonicalize_url(document.source_url) != canonicalize_url(str(record.get("source_url", ""))):
            return False
        if resolved_url is not None:
            previous_resolved = canonicalize_url(str(record.get("resolved_document_url") or record.get("pdf_source_url") or record.get("source_url") or ""))
            if previous_resolved != resolved_url and canonicalize_url(document.source_url) != canonicalize_url(str(record.get("source_url", ""))):
                return False
        if record.get("category") and record.get("category") != document.category:
            return False
        if record.get("date") and document.date and str(record.get("date")) != str(document.date):
            return False
        local = record.get("local_filename")
        if not local:
            return False
        path = self.folder / str(local)
        try:
            if not path.resolve().is_relative_to(self.folder.resolve()):
                return False
        except (OSError, ValueError):
            return False
        if not path.is_file() or path.stat().st_size == 0:
            return False
        expected = record.get("sha256")
        if expected:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                return False
        return True

    def _apply_target(self, document: Document, target: DocumentTarget) -> None:
        """Copy wrapper-resolution evidence onto the manifest document."""
        document.original_source_url = document.original_source_url or document.source_url
        document.wrapper_detected = bool(target.wrapper_detected)
        document.wrapper_type = target.wrapper_type
        document.wrapper_resolution_method = target.resolution_method
        document.resolved_document_url = target.canonical_document_url or document.source_url
        document.resolution_confidence = target.confidence
        document.document_type_hint = target.document_type_hint
        if target.diagnostics:
            marker = {
                "wrapper_url": document.source_url,
                "resolved_document_url": target.canonical_document_url,
                "resolution_method": target.resolution_method,
                "wrapper_type": target.wrapper_type or "",
                "resolution_confidence": target.confidence,
                "diagnostics": ";".join(target.diagnostics),
                "relation": "document_wrapper_resolution",
            }
            if not any(item.get("relation") == marker["relation"] and item.get("resolved_document_url") == marker["resolved_document_url"] for item in document.provenance):
                document.provenance.append(marker)

    async def fetch_pdf(self, url: str, referer: str) -> tuple[bytes, str]:
        from .network import ResourceClient
        client = ResourceClient(self.browser, self.context)
        initial = resolve_document_target(url, issuer_context=self.issuer_context)
        current = initial.canonical_document_url or url
        seen = set()
        self.last_pdf_response = {
            "original_url": url,
            "resolved_document_url": current,
            "resolution_method": initial.resolution_method,
            "wrapper_detected": initial.wrapper_detected,
        }
        for _ in range(int(getattr(self.browser.config, "max_redirects", 8))):
            if not public_url(current) or current in seen:
                raise CollectionError("PDF_DOWNLOAD_FAILED", f"Invalid viewer URL or cycle: {current}")
            seen.add(current)
            response = await client.get(current, referer)
            data = response.body
            content_type = str((response.headers or {}).get("content-type", ""))
            self.last_pdf_response.update({
                "resolved_document_url": response.url,
                "resolved_content_type": content_type,
                "resolved_http_status": response.status,
            })
            if data.lstrip().startswith(b"%PDF-"):
                validate_pdf(data)
                return data, response.url
            target = resolve_document_target(response.url, response, self.issuer_context)
            if target.canonical_document_url and target.canonical_document_url != response.url and target.identity_eligible:
                self.last_pdf_response.update({
                    "resolved_document_url": target.canonical_document_url,
                    "resolution_method": target.resolution_method,
                    "wrapper_detected": target.wrapper_detected,
                    "wrapper_type": target.wrapper_type,
                })
                current = target.canonical_document_url
                continue
            raise CollectionError("PDF_DOWNLOAD_FAILED", f"Expected PDF, received {content_type}: {response.url}")
        raise CollectionError("PDF_DOWNLOAD_FAILED", "Too many document viewers")

    async def save(self, document: Document) -> Document:
        document.official_ir_url = document.official_ir_url or document.source_url
        document.artifact_url = document.artifact_url or document.pdf_source_url or document.source_url
        document.retrieved_at = document.retrieved_at or datetime.now(timezone.utc).isoformat()
        try:
            saved = await self._save(document)
            saved.status = "CACHED" if saved.cache_hit else "DOWNLOADED"
            saved.download_url = saved.pdf_source_url or saved.wire_source_url or saved.source_url
            return saved
        except Exception as exc:
            document.status = "BLOCKED" if getattr(exc, "code", "") == "SITE_BLOCKED" else "FAILED"
            if document.status == "BLOCKED":
                document.http_status = next((e.status for e in reversed(self.browser.access_events) if e.blocked and e.status), 403)
                log.warning("DISCOVERED_BUT_BLOCKED: %s", document.source_url)
            raise

    async def _save(self, document: Document) -> Document:
        key = canonicalize_url(document.source_url)
        target = resolve_document_target(document.pdf_source_url or document.source_url, issuer_context=self.issuer_context)
        self._apply_target(document, target)
        resolved_key = canonicalize_url(target.canonical_document_url or document.source_url)
        existing = self.existing.get(key)
        resolved_existing = self.resolved_existing.get(resolved_key)
        if resolved_existing and self._existing_valid(document, resolved_existing, resolved_key):
            existing = resolved_existing
        if existing and self._existing_valid(document, existing, resolved_key if existing is resolved_existing else None):
            document.local_filename = existing.get("local_filename")
            document.sha256 = existing.get("sha256")
            document.method = existing.get("method") or document.method
            document.pdf_source_url = existing.get("pdf_source_url") or document.pdf_source_url
            document.download_url = existing.get("download_url") or document.download_url
            document.resolved_document_url = existing.get("resolved_document_url") or document.resolved_document_url
            document.artifact_url = document.resolved_document_url or existing.get("artifact_url") or document.artifact_url
            document.resolved_content_type = existing.get("resolved_content_type") or document.resolved_content_type
            document.resolved_http_status = existing.get("resolved_http_status") or document.resolved_http_status
            document.cache_hit = True
            self.cache_hits += 1
            self.network_requests_avoided += 1
            self.urls[key] = document
            self.urls[resolved_key] = document
            log.info("Reused cached artifact: %s", document.local_filename)
            return document
        if resolved_key in self.urls or key in self.urls:
            previous = self.urls.get(resolved_key) or self.urls[key]
            document.local_filename, document.sha256 = previous.local_filename, previous.sha256
            document.method, document.pdf_source_url = previous.method, previous.pdf_source_url
            document.duplicate_of = previous.local_filename
            document.resolved_document_url = previous.resolved_document_url or document.resolved_document_url
            return document
        data, last_error = None, None
        resolved_assets = [document.resolved_document_url] if document.pdf_source_url or target.wrapper_detected or target.resolution_method != "UNKNOWN" else []
        assets = list(dict.fromkeys([asset for asset in (resolved_assets + ([document.pdf_source_url] if document.pdf_source_url else []) + document.alternate_pdf_urls) if asset]))
        for asset in assets:
            try:
                data, actual = await self.fetch_pdf(asset, document.linked_from_url or document.source_url)
                document.pdf_source_url, document.method = actual, "official_pdf"
                document.resolved_document_url = actual
                document.artifact_url = actual
                document.resolved_content_type = self.last_pdf_response.get("resolved_content_type")
                document.resolved_http_status = self.last_pdf_response.get("resolved_http_status")
                break
            except Exception as exc:
                last_error = exc
                log.warning("Original linked PDF unavailable %s: %s", asset, exc)
        if data is None and document.category == "reports" and document.source_family == "SEC_EDGAR":
            data = await self.retrieve_sec(document)
        if data is None and document.category == "reports":
            raise last_error or CollectionError("PDF_DOWNLOAD_FAILED", "No report PDF link")
        if data is None:
            data = await self.retrieve_news(document)
        digest = validate_pdf(data)
        document.sha256 = digest
        if digest in self.hashes:
            document.local_filename = self.hashes[digest]
            document.duplicate_of = document.local_filename
            log.info("Duplicate content retained once: %s", document.source_url)
        else:
            sub = self.folder / ("News Releases" if document.category == "news" else "Reports")
            target = document_path(sub, self.ticker, document.title, document.date)
            temporary = target.with_suffix(".pdf.part")
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            document.local_filename = target.relative_to(self.folder).as_posix()
            self.hashes[digest] = document.local_filename
            log.info("%s: %s", "Downloaded original PDF" if document.method == "official_pdf" else "HTML release converted to PDF", document.local_filename)
        self.urls[key] = document
        self.urls[canonicalize_url(document.resolved_document_url or document.pdf_source_url or document.source_url)] = document
        return document

    async def retrieve_sec(self, document: Document) -> bytes:
        """Fetch an official SEC HTML/text filing without bypassing blocks."""
        from urllib.request import Request, urlopen
        from .official_sources import SEC_USER_AGENT

        def fetch() -> bytes:
            request = Request(
                document.source_url,
                headers={"User-Agent": SEC_USER_AGENT, "Accept": "text/html,text/plain,*/*"},
            )
            with urlopen(request, timeout=float(getattr(self.browser.config, "http_total_timeout", 30.0))) as response:
                return response.read()

        try:
            body = await asyncio.to_thread(fetch)
        except Exception as exc:
            raise CollectionError("PDF_DOWNLOAD_FAILED", f"SEC filing unavailable: {exc}") from exc
        if not body:
            raise CollectionError("PDF_DOWNLOAD_FAILED", "SEC filing body is empty")
        document.content_html = body.decode("utf-8", "replace")
        document.content_complete = True
        document.method = "official_sec_html"
        return await render_archival(self.context, document, self.ticker, self.company_name, document.content_html)

    async def retrieve_news(self, document: Document) -> bytes:
        page = None
        html, browser_loaded, last_error = None, False, None
        try:
            try:
                if urlsplit(document.source_url).hostname in self.browser.blocked_hosts:
                    raise CollectionError("SITE_BLOCKED", "Browser frontend previously blocked; testing public release HTTP")
                page = await self.context.new_page()
                await self.browser.goto(page, document.source_url)
                html, browser_loaded = await page.content(), True
            except Exception as exc:
                last_error = exc
                try:
                    response = await ResourceClient(self.browser, self.context).get(document.source_url, document.linked_from_url or document.source_url)
                    html = response.body.decode("utf-8", "replace")
                except Exception as http_error:
                    last_error = http_error
            if html:
                check = BeautifulSoup(html, "html.parser")
                if BLOCK_TEXT.search(check.title.get_text() if check.title else ""):
                    last_error = CollectionError("SITE_BLOCKED", "Release HTTP returned an access-challenge page")
                    html = None
            if html:
                soup = BeautifulSoup(html, "html.parser")
                title, date = metadata(html)
                document.title, document.date = title or document.title, date or document.date
                for anchor in soup.select("a[href]"):
                    label = anchor.get_text(" ", strip=True)
                    if not re.fullmatch(r"(?:download |view |printable )?PDF(?: Version)?(?:\s*\([^)]*\))?", label, re.I):
                        continue
                    asset = canonicalize_url(anchor["href"], document.source_url)
                    try:
                        data, actual = await self.fetch_pdf(asset, document.source_url)
                        document.pdf_source_url, document.method = actual, "official_pdf"
                        return data
                    except Exception as exc:
                        last_error = exc
                        log.warning("Official PDF link unavailable %s: %s", asset, exc)
                wire = wire_link(soup, document.source_url)
                if wire:
                    document.wire_source_url, document.wire_provenance_url = wire, document.source_url
                    document.provenance.append({"source_url":document.source_url,"wire_source_url":wire,"relation":"Wire source explicitly linked by official IR"})
                document.method = "official_html_to_pdf"
                if browser_loaded:
                    return await render_release(page, self.browser)
                article = soup.select_one(".node--type-nir-news .node__content, article.node--type-nir-news, article, main")
                if article and len(article.get_text(" ",strip=True)) >= 150:
                    return await render_archival(self.context, document, self.ticker, self.company_name, str(article))
            if document.content_complete and document.content_html and document.feed_url:
                document.method = "official_rss_to_pdf"
                return await render_archival(self.context, document, self.ticker, self.company_name, document.content_html)
            if document.wire_source_url and document.wire_provenance_url and document.wire_provenance_url in (document.source_url, document.feed_url):
                host = urlsplit(document.wire_source_url).hostname or ""
                if any(host == h or host == "www." + h for h in WIRE_HOSTS):
                    if page is None:
                        page = await self.context.new_page()
                    await self.browser.goto(page, document.wire_source_url)
                    document.method = "verified_wire_source_html_to_pdf"
                    return await render_release(page, self.browser)
            raise last_error or CollectionError("PDF_DOWNLOAD_FAILED", "No accessible full official release content")
        finally:
            if page is not None:
                await page.close()
