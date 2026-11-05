"""Official SEC source access for Component 6A.

The provider delegates issuer resolution, filing discovery, rate limiting and
cache handling to the already frozen SEC ingestion layer.  No search engine or
third-party biography is used.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

from insider_intelligence.proxy_baseline import ProxyBaselineProvider
from insider_intelligence.sec_ingestion import SECSourceProvider
from insider_intelligence.models import IssuerIdentity, SECFiling

from .models import OfficialSource
from .normalized_sources import NormalizedSECSourceCatalog
from .source_roles import source_role, source_roles


class SECManagementSourceProvider:
    def __init__(self, sec: SECSourceProvider | None = None, *, normalized_root: str | Path | None = None):
        self.sec = sec or SECSourceProvider()
        self.proxy = ProxyBaselineProvider(sec_provider=self.sec)
        self.normalized = NormalizedSECSourceCatalog(normalized_root) if normalized_root else None

    def issuer(self, ticker: str) -> IssuerIdentity:
        if self.normalized is not None:
            source = self.normalized.latest_proxy(ticker) or next(iter(reversed(self.normalized.sources(ticker, forms=("20-F", "10-K", "6-K")))), None)
            if source is not None:
                return IssuerIdentity(source.ticker, source.company_name, source.issuer_cik, source.source_url)
        return self.sec.resolve_issuer(ticker)

    def latest_proxy(self, ticker: str, *, force: bool = False, as_of_date: str | None = None) -> tuple[SECFiling, bytes]:
        if self.normalized is not None:
            source = self.normalized.latest_proxy(ticker, as_of_date=as_of_date)
            if source is not None:
                return source.filing(), source.body()
        filings = self.sec.discover_filings(ticker, forms=("DEF 14A", "DEFA14A"), to_date=as_of_date, force=force)
        if not filings:
            raise LookupError(f"No DEF 14A proxy found for {ticker}")
        definitive = [item for item in filings if item.form_type.upper() == "DEF 14A"]
        filing = max(definitive or filings, key=lambda item: (item.filing_date or "", item.accession_number))
        body, cached = self.sec.download_filing(filing, force=force)
        return cached, body

    def management_sources(self, ticker: str, *, force: bool = False, as_of_date: str | None = None) -> list[SECFiling]:
        """Return cached, PIT-eligible foreign or domestic roster sources.

        The normalized catalog is authoritative.  Network discovery remains a
        compatibility fallback for domestic issuers and is never required for
        a cache-only foreign-issuer rebuild.
        """
        forms = ("DEF 14A", "DEFA14A", "20-F", "10-K", "6-K", "8-K", "8-K/A")
        if self.normalized is not None:
            values = self.normalized.sources(ticker, forms=forms, as_of_date=as_of_date)
            if values:
                eligible = []
                for item in values:
                    roles = source_roles(form_type=item.form_type, title=item.title, text=item.text)
                    if not roles & {"MANAGEMENT_PRIMARY", "BOARD_PRIMARY", "GOVERNANCE_PRIMARY", "AGM_PRIMARY", "ANNUAL_REPORT_PRIMARY"}:
                        continue
                    if item.form_type.upper() == "6-K" and not re.search(
                        r"annual general meeting|result(?:s)? of (?:the )?AGM|"
                        r"appointed\s+(?:as|to)\s+(?:the\s+)?(?:chief|executive|financial|operating|technology|legal|director|board)\b|"
                        r"(?:stepped down|resigned|retired).{0,120}(?:chief|executive|financial|operating|director|board)",
                        item.text or "", re.I | re.S,
                    ):
                        continue
                    eligible.append(item.filing())
                return eligible
        # Keep the old SEC proxy path for issuers without a normalized handoff.
        filings = self.sec.discover_filings(ticker, forms=("DEF 14A", "DEFA14A", "10-K", "20-F", "6-K"), to_date=as_of_date, force=force)
        return sorted(filings, key=lambda item: (item.filing_date or "", item.accession_number))

    def supporting_filings(self, ticker: str, *, force: bool = False, as_of_date: str | None = None) -> list[SECFiling]:
        """Discover only official filings that can establish role changes."""
        normalized = self.normalized.sources(ticker, forms=("10-K",), as_of_date=as_of_date) if self.normalized else []
        filings = [item.filing() for item in normalized]
        if not filings:
            filings = self.sec.discover_filings(ticker, forms=("10-K",), to_date=as_of_date, force=force)
        return filings + self.role_change_filings(ticker, force=force, as_of_date=as_of_date)

    def role_change_filings(self, ticker: str, *, after: str | None = None, force: bool = False, as_of_date: str | None = None) -> list[SECFiling]:
        """Discover 8-K filings without changing the frozen SEC form set."""
        normalized = self.normalized.sources(ticker, forms=("8-K", "8-K/A"), as_of_date=as_of_date) if self.normalized else []
        if normalized:
            return [item.filing() for item in normalized if not after or (item.filing_date or "") > after]
        identity = self.sec.resolve_issuer(ticker, force=force)
        payload = self.sec._submissions(identity, force=force)  # official cached SEC submissions endpoint
        filings = self.sec._filings_from_payload(identity, payload, {"8-K"}, after, as_of_date)
        return sorted(filings, key=lambda item: (item.filing_date or "", item.accession_number))

    def _downloaded_filing(self, filing: SECFiling, *, force: bool = False) -> tuple[SECFiling, bytes]:
        if self.normalized is not None:
            values = self.normalized.sources(filing.ticker, forms=(filing.form_type,), as_of_date=filing.filing_date)
            source = next((item for item in values if item.accession_number == filing.accession_number), None)
            if source is not None:
                return source.filing(), source.body()
        body, cached = self.sec.download_filing(filing, force=force)
        return cached, body

    def download_filing(self, filing: SECFiling, *, force: bool = False) -> tuple[bytes, SECFiling]:
        cached, body = self._downloaded_filing(filing, force=force)
        return body, cached

    @staticmethod
    def source_for_filing(filing: SECFiling, *, title: str | None = None) -> OfficialSource:
        source_type = filing.form_type.upper()
        return OfficialSource(
            source_id=f"sec:{filing.issuer_cik}:{filing.accession_number}",
            source_authority="sec_official_filing",
            source_type=source_type,
            url=filing.source_url,
            local_path=filing.local_source_path,
            issuer_cik=filing.issuer_cik,
            accession_number=filing.accession_number,
            filing_date=filing.filing_date,
            report_date=filing.report_date,
            title=title,
            public_availability_date=filing.available_date or filing.filing_date,
            source_role=source_role(form_type=source_type),
        )


__all__ = ["SECManagementSourceProvider"]
