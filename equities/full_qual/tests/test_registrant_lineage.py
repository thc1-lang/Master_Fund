from __future__ import annotations

import json
from pathlib import Path

from pipeline_runner.issuer_resolution import _candidate_from_manifest, canonical_identity_hash
from pipeline_runner.registrant_lineage import LineageEdge, RegistrantCandidate, choose_security_registrant, relation_for_source
from qualitative_ir_downloader.main import annotate_source_identity
from qualitative_ir_downloader.models import Document
from pipeline_runner.runner import PipelineRunner
from pipeline_runner.issuer_resolution import IssuerMetadata
from qualitative_analysis.ingestion import _reconcile_source_identity


def _manifest(path: Path, *, issuer_cik: str, company_name: str, html: str = "") -> None:
    path.write_text(json.dumps({
        "ticker": "TEST", "company_name": company_name, "issuer_cik": issuer_cik,
        "reports": [{
            "title": "10-Q", "source_url": "https://www.sec.gov/Archives/edgar/data/9999999/000001234526000001/test.htm",
            "content_html": html, "provenance": [{"accession_number": "0000012345-26-000001", "issuer_cik": issuer_cik}],
        }],
    }), encoding="utf-8")


def test_same_cik_name_change_preserves_lineage(tmp_path):
    candidate = RegistrantCandidate("0000000001", "New Name", ticker="TEST", security_evidence=True)
    selected = choose_security_registrant([candidate], "TEST")
    assert selected.cik == "0000000001"
    assert relation_for_source("0000000001", "0000000001") == ("CANONICAL_REGISTRANT", True)


def test_parent_subsidiary_and_holding_reorganisation_require_security_evidence():
    parent = RegistrantCandidate("0000000001", "Listed Parent", ticker="TEST", security_evidence=True)
    affiliate = RegistrantCandidate("0000000002", "Listed Parent Holdings")
    assert choose_security_registrant([affiliate, parent], "TEST").cik == "0000000001"
    assert relation_for_source("0000000002", "0000000001") == ("AFFILIATE", False)


def test_merger_party_does_not_replace_canonical_without_transfer():
    edge = LineageEdge("0000000001", "0000000002", "MERGER_PARTY", "2026-07-01", security_transfer=False)
    assert relation_for_source("0000000002", "0000000001", edges=[edge], as_of="2026-09-25") == ("MERGER_PARTY", False)


def test_successor_requires_explicit_effective_security_transfer():
    edge = LineageEdge("0000000001", "0000000002", "SUCCESSOR", "2026-07-01", security_transfer=True)
    assert relation_for_source("0000000002", "0000000001", edges=[edge], as_of="2026-06-30") == ("AFFILIATE", False)
    assert relation_for_source("0000000001", "0000000002", edges=[edge], as_of="2026-09-25") == ("SUCCESSOR", False)


def test_historical_predecessor_is_eligible_only_with_explicit_continuity():
    edge = LineageEdge("0000000001", "0000000002", "PREDECESSOR", "2025-01-01", security_transfer=True)
    assert relation_for_source("0000000001", "0000000002", edges=[edge], as_of="2026-09-25") == ("PREDECESSOR", True)


def test_cached_manifest_uses_accession_and_unsegmented_xbrl_registrant(tmp_path):
    html = (
        '<xbrli:context id="c-1"><xbrli:entity><xbrli:identifier>0000012345</xbrli:identifier>'
        '</xbrli:entity></xbrli:context>'
        '<ix:nonNumeric contextRef="c-1" name="dei:EntityRegistrantName">Canonical Listed Corp</ix:nonNumeric>'
        '<ix:nonNumeric contextRef="c-1" name="dei:TradingSymbol">TEST</ix:nonNumeric>'
        '<ix:nonNumeric contextRef="c-1" name="dei:SecurityExchangeName">New York Stock Exchange</ix:nonNumeric>'
    )
    path = tmp_path / "manifest.json"
    _manifest(path, issuer_cik="0009999999", company_name="Related Holdings", html=html)
    result = _candidate_from_manifest(path, "TEST")
    assert result is not None
    assert result.cik == "0000012345"
    assert result.company_name == "Canonical Listed Corp"
    assert result.exchange == "NYSE"
    assert result.historical_registrant_names == ("Related Holdings",)


def test_cached_identity_does_not_use_future_manifest_evidence(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({
        "ticker": "TEST", "company_name": "Old Listed Corp", "issuer_cik": "0000000002",
        "reports": [{
            "title": "Future reorganisation filing", "date": "2026-10-16",
            "source_url": "https://www.sec.gov/Archives/edgar/data/1/0000000001-26-000001/future.htm",
        }],
    }), encoding="utf-8")
    current = _candidate_from_manifest(path, "TEST", as_of_date="2026-09-25")
    assert current is not None
    assert current.cik == "0000000002"
    future = _candidate_from_manifest(path, "TEST")
    assert future is not None
    assert future.cik == "0000000001"


def test_source_identity_gate_rejects_incompatible_filing_and_accepts_ir_without_cik():
    incompatible = Document("10-Q", "2026-06-30", "https://www.sec.gov/Archives/edgar/data/2/0000000002000001/x.htm", "reports", provenance=[{"accession_number": "0000000002-26-000001"}])
    annotate_source_identity(incompatible, canonical_cik="0000000001", canonical_name="Listed Corp")
    assert incompatible.source_lineage_relation == "AFFILIATE"
    assert incompatible.source_identity_eligible is False
    ir = Document("Release", "2026-06-30", "https://ir.example.test/release", "news")
    annotate_source_identity(ir, canonical_cik="0000000001", canonical_name="Listed Corp")
    assert ir.source_identity_eligible is True


def test_normalization_reconciles_stale_manifest_cik_from_sec_accession():
    metadata = {
        "source_url": "https://www.sec.gov/Archives/edgar/data/34088/000003408826000093/x.htm",
        "provenance": [{"issuer_cik": "0002115436", "accession_number": "0000034088-26-000093"}],
    }
    _reconcile_source_identity(metadata, "0000034088")
    assert metadata["source_registrant_cik"] == "0000034088"
    assert metadata["source_identity_eligible"] is True
    assert metadata["provenance"][0]["issuer_cik"] == "0000034088"
    assert metadata["provenance"][0]["raw_issuer_cik"] == "0002115436"


def test_normalization_rejects_filing_from_different_registrant():
    metadata = {"source_url": "https://www.sec.gov/Archives/edgar/data/999/0000000999-26-000001/x.htm"}
    try:
        _reconcile_source_identity(metadata, "0000034088")
    except ValueError as exc:
        assert str(exc) == "SOURCE_IDENTITY_INCOMPATIBLE"
    else:
        raise AssertionError("incompatible SEC filing was admitted")


def test_filing_agent_accession_does_not_override_registrant_archive_cik():
    document = Document(
        "10-Q", "2026-06-30",
        "https://www.sec.gov/Archives/edgar/data/789019/000119312526191507/msft.htm",
        "reports",
        provenance=[{"issuer_cik": "0000789019", "accession_number": "0001193125-26-191507"}],
    )
    annotate_source_identity(document, canonical_cik="0000789019", canonical_name="Microsoft Corporation")
    assert document.source_registrant_cik == "0000789019"
    assert document.source_identity_eligible is True


def test_identity_hash_includes_exchange_and_lineage_version():
    one = canonical_identity_hash(ticker="TEST", exchange="NYSE", cik="0000000001", legal_name="Listed Corp")
    two = canonical_identity_hash(ticker="TEST", exchange="NASDAQ", cik="0000000001", legal_name="Listed Corp")
    three = canonical_identity_hash(ticker="TEST", exchange="NYSE", cik="0000000001", legal_name="Listed Corp", lineage_version="issuer-lineage-v2")
    assert len({one, two, three}) == 3


def test_canonical_cik_change_forces_downstream_rebuild_without_touching_source_cache(tmp_path):
    artifacts = tmp_path / "artifacts"
    identity_dir = artifacts / "issuer_identity"
    identity_dir.mkdir(parents=True)
    (identity_dir / "TEST.json").write_text(json.dumps({
        "ticker": "TEST", "issuer_cik": "0000000001", "legal_name": "Old Listed Corp",
        "identity_hash": canonical_identity_hash(ticker="TEST", exchange="NYSE", cik="0000000001", legal_name="Old Listed Corp"),
    }), encoding="utf-8")
    runner = PipelineRunner("TEST", "2026-09-25", profile="core_v2", artifacts_root=artifacts, data_root=tmp_path / "data", download_root=tmp_path / "cache")
    runner._issuer_metadata = IssuerMetadata("TEST", "New Listed Corp", "0000000002", "cached_sec_filing_identity", exchange="NYSE", sec_registrant_name="New Listed Corp")
    runner._persist_issuer_identity()
    assert runner._canonical_identity_changed is True
    assert runner._compatibility_for("normalization").reason == "CANONICAL_REGISTRANT_CHANGED"
    assert not (tmp_path / "cache").exists()
