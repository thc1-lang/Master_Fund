"""Resolve an explicitly requested issuer without making the worksheet a gate.

The runner needs a small amount of identity metadata before it can invoke the
downloader in manual-ticker mode.  Resolution is deliberately kept separate
from the analytical components: it only identifies the issuer and preserves
the source used for that identity.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.issuer_identity import canonical_identity_hash


_ARCHIVE_CIK_RE = re.compile(r"/Archives/edgar/data/(\d{1,10})/", re.I)
_ACCESSION_RE = re.compile(r"(?<!\d)(\d{10})-\d{2}-\d{6}(?!\d)")
_ENTITY_CONTEXT_RE = re.compile(
    r'<xbrli:context\b[^>]*\bid=["\']([^"\']+)["\'][^>]*>(.*?)</xbrli:context>', re.I | re.S
)
_ENTITY_NONNUMERIC_RE = re.compile(r'<(?:ix:)?nonNumeric\b([^>]*)>(.*?)</(?:ix:)?nonNumeric>', re.I | re.S)
_SECURITY_FACT_RE = re.compile(
    r'<(?:ix:)?nonNumeric\b([^>]*)name=["\'](?:dei:)?(TradingSymbol|SecurityExchangeName|SecurityTitle)["\'][^>]*>(.*?)</(?:ix:)?nonNumeric>',
    re.I | re.S,
)


class IssuerResolutionError(LookupError):
    """Raised when an explicit ticker cannot be mapped to an issuer."""


@dataclass(frozen=True)
class IssuerMetadata:
    ticker: str
    company_name: str
    cik: str | None = None
    source: str = "unknown"
    source_url: str | None = None
    worksheet: str | None = None
    worksheet_row: int | None = None
    exchange: str | None = None
    security_title: str | None = None
    sec_registrant_cik: str | None = None
    sec_registrant_name: str | None = None
    historical_registrant_names: tuple[str, ...] = ()
    successor_predecessor_links: tuple[dict[str, Any], ...] = ()
    corporate_action_status: str = "UNCHANGED"
    effective_date: str | None = None
    identity_confidence: str | None = None
    lineage_version: str = "issuer-lineage-v1"
    # Web identity is deliberately separate from the SEC registrant/legal
    # identity.  Acquisition fills these when it verifies an IR domain; SEC
    # resolution alone leaves them unknown rather than guessing.
    company_display_name: str | None = None
    corporate_brand_name: str | None = None
    corporate_domain: str | None = None
    investor_relations_url: str | None = None

    @property
    def legal_name(self) -> str:
        return self.sec_registrant_name or self.company_name

    @property
    def normalized_legal_name(self) -> str:
        value = unicodedata.normalize("NFKC", self.legal_name or "").casefold()
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    @property
    def identity_version(self) -> str:
        return self.lineage_version

    @property
    def identity_hash(self) -> str:
        return canonical_identity_hash(ticker=self.ticker, exchange=self.exchange,
                                       cik=self.cik or self.sec_registrant_cik,
                                       legal_name=self.legal_name, lineage_version=self.lineage_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "company_display_name": self.company_display_name or self.company_name,
            "cik": self.cik,
            "source": self.source,
            "source_url": self.source_url,
            "worksheet": self.worksheet,
            "worksheet_row": self.worksheet_row,
            "requested_ticker": self.ticker,
            "resolved_ticker": self.ticker,
            "issuer_cik": self.cik,
            "sec_registrant_cik": self.cik or self.sec_registrant_cik,
            "sec_registrant_name": self.legal_name,
            "legal_name": self.legal_name,
            "normalized_legal_name": self.normalized_legal_name,
            "source_of_identity": self.source,
            "exchange": self.exchange,
            "security_title": self.security_title,
            "historical_registrant_names": list(self.historical_registrant_names),
            "successor_predecessor_links": list(self.successor_predecessor_links),
            "corporate_action_status": self.corporate_action_status,
            "effective_date": self.effective_date,
            "identity_confidence": self.identity_confidence or ("HIGH" if self.cik and self.source in {"sec_edgar", "cached_acquisition", "cached_sec_filing_identity"} else "MEDIUM"),
            "identity_version": self.identity_version,
            "lineage_version": self.lineage_version,
            "identity_hash": self.identity_hash,
            "corporate_brand_name": self.corporate_brand_name,
            "corporate_domain": self.corporate_domain,
            "investor_relations_url": self.investor_relations_url,
        }


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _filing_identity_evidence(record: Any) -> list[dict[str, str]]:
    """Extract registrant evidence from a manifest record without trusting names.

    Accession and archive-path evidence are both retained because an accession
    can belong to a filing agent while the archive path carries the registrant
    (and a stale cache can have the inverse problem). Inline XBRL's unsegmented
    registrant context is stronger still and supplies the legal name when the
    filing contains it.
    """
    if not isinstance(record, dict):
        return []
    values: list[dict[str, str]] = []
    urls: list[str] = []
    for key in ("source_url", "artifact_url", "pdf_source_url", "download_url", "local_source_path"):
        if record.get(key):
            urls.append(str(record[key]))
    for provenance in record.get("provenance") or []:
        if isinstance(provenance, dict):
            urls.extend(str(provenance[key]) for key in ("source_url", "artifact_url") if provenance.get(key))
            accession = str(provenance.get("accession_number") or "")
            match = _ACCESSION_RE.search(accession)
            if match:
                values.append({"cik": match.group(1), "source": "accession", "strength": "4"})
            if provenance.get("issuer_cik") and not accession:
                values.append({"cik": str(provenance["issuer_cik"]).zfill(10), "source": "provenance", "strength": "2"})
    for url in urls:
        if not re.search(r"sec\.gov|sec-filings|edgar/data", url, re.I):
            continue
        match = _ACCESSION_RE.search(url)
        if match:
            values.append({"cik": match.group(1), "source": "accession_url", "strength": "4"})
        match = _ARCHIVE_CIK_RE.search(url)
        if match:
            values.append({"cik": match.group(1).zfill(10), "source": "archive_path", "strength": "1"})

    html = str(record.get("content_html") or "")
    if html:
        contexts = {m.group(1): m.group(2) for m in _ENTITY_CONTEXT_RE.finditer(html)}
        context_ciks: dict[str, str] = {}
        # The context identifier is the reliable join key when present.  A
        # tiny scanner handles XBRL attributes whose order varies by issuer.
        for context_id, body in contexts.items():
            cik_match = re.search(r"(?:<xbrli:identifier|<identifier)\b[^>]*>\s*(\d{1,10})\s*</", body, re.I)
            if cik_match:
                context_ciks[context_id] = cik_match.group(1).zfill(10)
        for match in _ENTITY_NONNUMERIC_RE.finditer(html):
            attrs, raw_name = match.groups()
            if not re.search(r'name=["\'](?:dei:)?EntityRegistrantName["\']', attrs, re.I):
                continue
            context_match = re.search(r'contextRef=["\']([^"\']+)["\']', attrs, re.I)
            if not context_match:
                continue
            context_id = context_match.group(1)
            cik = context_ciks.get(context_id)
            name = _strip_markup(raw_name)
            if cik and name:
                body = contexts.get(context_id, "")
                values.append({"cik": cik, "name": name, "source": "xbrl_registrant_context",
                               "strength": "6" if "<xbrli:segment" not in body.lower() else "5"})
    return values


def _record_date(record: dict[str, Any]) -> str | None:
    for key in ("available_date", "filing_date", "date", "published", "publication_date", "retrieved_at"):
        value = str(record.get(key) or "").strip()
        if value:
            return value[:10]
    for item in record.get("provenance") or []:
        if isinstance(item, dict):
            value = str(item.get("filing_date") or item.get("available_date") or "").strip()
            if value:
                return value[:10]
    return None


def _manifest_source_identity(data: dict[str, Any], *, as_of_date: str | None = None) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    records = list(data.get("reports") or []) + list(data.get("news") or [])
    if as_of_date:
        records = [record for record in records if not isinstance(record, dict) or not _record_date(record) or _record_date(record) <= as_of_date]
    evidence: list[dict[str, str]] = []
    for record in records:
        evidence.extend(_filing_identity_evidence(record))
    if not evidence:
        return None, None, []
    scores: dict[str, int] = {}
    names: dict[str, dict[str, int]] = {}
    for item in evidence:
        cik = str(item.get("cik") or "").zfill(10)
        if not re.fullmatch(r"\d{10}", cik):
            continue
        weight = int(item.get("strength") or 1)
        scores[cik] = scores.get(cik, 0) + weight
        if item.get("name"):
            names.setdefault(cik, {})[str(item["name"])] = names.setdefault(cik, {}).get(str(item["name"]), 0) + weight
    if not scores:
        return None, None, []
    cik = max(scores, key=lambda value: (scores[value], value))
    name = max(names.get(cik, {}), key=names[cik].get) if names.get(cik) else None
    links = [{"source_cik": str(data.get("issuer_cik") or data.get("cik") or "").zfill(10),
              "target_cik": cik, "relation": "AFFILIATE",
              "evidence": "SEC accession/XBRL registrant evidence; source CIK was not selected as canonical"}] if str(data.get("issuer_cik") or data.get("cik") or "") != cik else []
    return cik, name, links


def _manifest_security_metadata(data: dict[str, Any], ticker: str, *, as_of_date: str | None = None) -> tuple[str | None, str | None]:
    """Recover security-level exchange/title evidence from cached inline XBRL."""
    records = list(data.get("reports") or []) + list(data.get("news") or [])
    exchange = None
    title = None
    for record in records:
        if not isinstance(record, dict) or (as_of_date and _record_date(record) and _record_date(record) > as_of_date):
            continue
        html = str(record.get("content_html") or "")
        if not html:
            continue
        facts: dict[str, list[str]] = {}
        for attrs, fact, raw_value in _SECURITY_FACT_RE.findall(html):
            value = _strip_markup(raw_value)
            if value:
                facts.setdefault(fact.lower(), []).append(value)
        symbols = {value.upper() for value in facts.get("tradingsymbol", [])}
        if symbols and ticker.upper() not in symbols:
            continue
        exchange_value = next(iter(facts.get("securityexchangename", [])), None)
        if exchange_value and not exchange:
            exchange = "NYSE" if "new york stock exchange" in exchange_value.casefold() else exchange_value
        if facts.get("securitytitle") and not title:
            title = facts["securitytitle"][0]
    return exchange, title


def _candidate_from_manifest(path: Path, ticker: str, *, as_of_date: str | None = None) -> IssuerMetadata | None:
    data = _json_object(path)
    if not data or str(data.get("ticker") or "").upper() != ticker:
        return None
    company_name = str(data.get("company_name") or "").strip()
    if not company_name:
        return None
    cik = str(data.get("issuer_cik") or data.get("cik") or "").zfill(10) or None
    source_cik, source_name, links = _manifest_source_identity(data, as_of_date=as_of_date)
    exchange, security_title = _manifest_security_metadata(data, ticker, as_of_date=as_of_date)
    historical = (company_name,) if source_name and source_name.casefold() != company_name.casefold() else ()
    if source_cik and source_cik != cik:
        cik = source_cik
        company_name = source_name or company_name
        source = "cached_sec_filing_identity"
        action = "LINEAGE_RECONCILED"
    elif source_name and source_name.casefold() != company_name.casefold():
        company_name = source_name
        source = "cached_sec_filing_identity"
        action = "NAME_RECONCILED"
    else:
        source = "cached_acquisition"
        action = "UNCHANGED"
    return IssuerMetadata(
        ticker=ticker,
        company_name=company_name,
        cik=str(cik) if cik else None,
        source=source,
        source_url=str(data.get("source_url") or data.get("investor_relations_url") or "") or None,
        worksheet=str(data.get("worksheet") or "") or None,
        worksheet_row=int(data["spreadsheet_row"]) if str(data.get("spreadsheet_row") or "").isdigit() else None,
        sec_registrant_cik=cik,
        sec_registrant_name=company_name,
        exchange=exchange,
        security_title=security_title,
        historical_registrant_names=historical,
        successor_predecessor_links=tuple(links),
        corporate_action_status=action,
        identity_confidence="HIGH" if source_cik else None,
        company_display_name=str(data.get("company_display_name") or company_name) or company_name,
        corporate_brand_name=str(data.get("corporate_brand_name") or "") or None,
        corporate_domain=str(data.get("corporate_domain") or "") or None,
        investor_relations_url=str(data.get("investor_relations_url") or data.get("ir_official_url") or "") or None,
    )


def _cached_identity(ticker: str, roots: list[Path], *, as_of_date: str | None = None) -> IssuerMetadata | None:
    """Recover identity from completed acquisition manifests, newest first."""
    candidates: list[tuple[float, Path]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            paths = root.rglob("manifest.json")
        except OSError:
            continue
        for path in paths:
            try:
                resolved = path.resolve(strict=False)
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for _, path in sorted(candidates, reverse=True):
        candidate = _candidate_from_manifest(path, ticker, as_of_date=as_of_date)
        if candidate:
            return candidate
    return None


def _worksheet_cache(ticker: str, roots: list[Path]) -> IssuerMetadata | None:
    """Read an already cached worksheet row without contacting Google Sheets."""
    for root in roots:
        data = _json_object(root / "stock_metadata.json")
        if not data:
            continue
        stocks = data.get("stocks")
        if not isinstance(stocks, dict):
            continue
        for key, value in stocks.items():
            if str(key).upper() != ticker or not isinstance(value, dict):
                continue
            company_name = str(value.get("company_name") or "").strip()
            if not company_name:
                continue
            row = value.get("spreadsheet_row")
            return IssuerMetadata(
                ticker=ticker,
                company_name=company_name,
                source="worksheet_cache",
                worksheet=str(value.get("worksheet") or "") or None,
                worksheet_row=int(row) if str(row or "").isdigit() else None,
                sec_registrant_name=company_name,
                company_display_name=company_name,
            )
    return None


def resolve_issuer_metadata(
    ticker: str,
    *,
    download_root: str | Path,
    data_root: str | Path,
    sec_provider: Any | None = None,
    as_of_date: str | None = None,
) -> IssuerMetadata:
    """Resolve a manual ticker using authoritative, non-worksheet sources.

    Existing local acquisition identity is preferred for repeatability.  SEC
    ticker metadata is the normal authoritative fallback for US issuers.  A
    cached worksheet row is used only if those sources are unavailable; no
    Google Sheets network read is performed here, so a missing row cannot gate
    an explicit ticker run.
    """
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise IssuerResolutionError("ISSUER_RESOLUTION_FAILED: ticker is empty")
    cache_root = Path(download_root)
    production_root = Path(data_root) / symbol / "01_Acquisition"
    cached = _cached_identity(symbol, [cache_root, production_root], as_of_date=as_of_date)
    worksheet = _worksheet_cache(symbol, [cache_root, Path(data_root)])
    if cached and cached.cik:
        return cached

    try:
        if sec_provider is None:
            from insider_intelligence.sec_ingestion import SECSourceProvider

            sec_provider = SECSourceProvider(cache_root=cache_root / ".issuer_resolution" / "sec")
        identity = sec_provider.resolve_issuer(symbol)
        return IssuerMetadata(
            ticker=symbol,
            company_name=str(identity.company_name).strip() or symbol,
            cik=str(identity.issuer_cik),
            source="sec_edgar",
            source_url=str(identity.source_url),
            worksheet=(cached.worksheet if cached else worksheet.worksheet if worksheet else None),
            worksheet_row=(cached.worksheet_row if cached else worksheet.worksheet_row if worksheet else None),
            exchange=getattr(identity, "exchange", None),
            security_title=getattr(identity, "security_title", None),
            sec_registrant_cik=str(identity.issuer_cik),
            sec_registrant_name=str(getattr(identity, "sec_registrant_name", None) or identity.company_name),
            historical_registrant_names=tuple(getattr(identity, "historical_registrant_names", ()) or ()),
            successor_predecessor_links=tuple(getattr(identity, "successor_predecessor_links", ()) or ()),
            corporate_action_status=str(getattr(identity, "corporate_action_status", "UNCHANGED") or "UNCHANGED"),
            effective_date=getattr(identity, "effective_date", None),
            identity_confidence=str(getattr(identity, "identity_confidence", None) or "HIGH"),
            company_display_name=str(getattr(identity, "company_display_name", None) or identity.company_name),
            corporate_brand_name=getattr(identity, "corporate_brand_name", None),
            corporate_domain=getattr(identity, "corporate_domain", None),
            investor_relations_url=getattr(identity, "investor_relations_url", None),
        )
    except Exception as sec_error:
        if cached:
            return cached
        if worksheet:
            return worksheet
        raise IssuerResolutionError(f"ISSUER_RESOLUTION_FAILED: {symbol} ({sec_error})") from sec_error
