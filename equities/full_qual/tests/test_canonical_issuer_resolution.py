from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.issuer_identity import is_valid_cik, normalize_cik
from insider_intelligence.sec_ingestion import SECResponse, SECSourceProvider
from pipeline_runner.issuer_resolution import IssuerResolutionError, resolve_issuer_metadata
from pipeline_runner.runner import PipelineRunner


def test_cik_normalization_rejects_placeholders_and_malformed_values():
    assert normalize_cik("1807794") == "0001807794"
    for value in (None, "", "0", "0000000000", "null", "missing", "0001807794-26-000001", "abc"):
        assert normalize_cik(value) is None
        assert is_valid_cik(value) is False


def test_invalid_cached_placeholder_forces_authoritative_resolution(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "CRDO_bad").mkdir()
    (cache / "CRDO_bad" / "manifest.json").write_text(json.dumps({
        "ticker": "CRDO", "company_name": "Credo Technology Group", "issuer_cik": "0000000000",
    }), encoding="utf-8")

    class Provider:
        def resolve_issuer(self, ticker):
            return SimpleNamespace(
                ticker=ticker,
                company_name="Credo Technology Group Holding Ltd",
                sec_registrant_name="Credo Technology Group Holding Ltd",
                issuer_cik="1807794",
                source_url="https://www.sec.gov/files/company_tickers.json",
                exchange="NASDAQ",
                security_title="Ordinary Shares",
                identity_confidence="HIGH",
            )

    identity = resolve_issuer_metadata("CRDO", download_root=cache, data_root=tmp_path / "data", sec_provider=Provider())
    assert identity.cik == "0001807794"
    assert identity.legal_name == "Credo Technology Group Holding Ltd"
    assert identity.cik != "0000000000"
    cached = json.loads((cache / ".issuer_resolution" / "identities" / "CRDO.json").read_text(encoding="utf-8"))
    assert cached["canonical_registrant_cik"] == "0001807794"
    assert cached["resolution_version"] == "issuer-resolution-v2"


def test_invalid_authoritative_cik_fails_closed(tmp_path: Path):
    class Provider:
        def resolve_issuer(self, ticker):
            return SimpleNamespace(ticker=ticker, company_name="Unknown", issuer_cik="0000000000")

    with pytest.raises(IssuerResolutionError, match="CANONICAL_REGISTRANT_UNRESOLVED"):
        resolve_issuer_metadata("CRDO", download_root=tmp_path / "cache", data_root=tmp_path / "data", sec_provider=Provider())


def test_brand_display_does_not_replace_legal_registrant(tmp_path: Path):
    class Provider:
        def resolve_issuer(self, ticker):
            return SimpleNamespace(
                ticker=ticker,
                company_name="Credo Technology Group Holding Ltd",
                sec_registrant_name="Credo Technology Group Holding Ltd",
                company_display_name="Credo",
                corporate_brand_name="Credo",
                issuer_cik="0001807794",
                source_url="sec",
            )

    identity = resolve_issuer_metadata("CRDO", download_root=tmp_path / "cache", data_root=tmp_path / "data", sec_provider=Provider())
    assert identity.legal_name == "Credo Technology Group Holding Ltd"
    assert identity.company_display_name == "Credo"
    assert identity.corporate_brand_name == "Credo"


def test_acquisition_invocation_rejects_placeholder_cik(tmp_path: Path):
    runner = PipelineRunner("CRDO", "2026-09-25", data_root=tmp_path / "data", download_root=tmp_path / "cache")
    from pipeline_runner.issuer_resolution import IssuerMetadata
    with pytest.raises(IssuerResolutionError, match="CANONICAL_REGISTRANT_UNRESOLVED"):
        runner.acquisition_invocation(IssuerMetadata("CRDO", "Credo", "0000000000", "cached_acquisition"))


class _SECFixtureTransport:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.calls: list[str] = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        body = self.payloads.get(url)
        if body is None:
            return SECResponse(404, b"{}", {})
        return SECResponse(200, body, {"content-type": "application/json"})


def test_sec_ticker_cik_and_domestic_filing_regime_are_consistent(tmp_path: Path):
    import json as _json
    from insider_intelligence.sec_ingestion import SEC_DATA, SEC_TICKERS, SEC_TICKERS_EXCHANGE

    ticker_payload = _json.dumps({"0": {"cik_str": 1807794, "ticker": "CRDO", "title": "Credo Technology Group Holding Ltd"}}).encode()
    submissions_payload = _json.dumps({
        "name": "Credo Technology Group Holding Ltd",
        "tickers": ["CRDO"], "exchanges": ["Nasdaq"],
        "stateOfIncorporation": "KY",
        "filings": {"recent": {"form": ["10-K", "10-Q", "8-K"]}},
    }).encode()
    transport = _SECFixtureTransport({
        SEC_TICKERS: ticker_payload,
        SEC_TICKERS_EXCHANGE: _json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [[1807794, "Credo Technology Group Holding Ltd", "CRDO", "Nasdaq"]]}).encode(),
        f"{SEC_DATA}/submissions/CIK0001807794.json": submissions_payload,
    })
    identity = SECSourceProvider(tmp_path / "sec", transport=transport, min_interval=0, sleep=lambda _: None).resolve_issuer("CRDO")
    assert identity.issuer_cik == "0001807794"
    assert identity.company_name == "Credo Technology Group Holding Ltd"
    assert identity.exchange == "NASDAQ"
    assert identity.filing_regime == "DOMESTIC_SEC_REPORTING_REGIME"
    assert identity.identity_diagnostics["TICKER_CIK_MATCH"] == "YES"
    assert identity.identity_diagnostics["CIK_VALID"] == "YES"
