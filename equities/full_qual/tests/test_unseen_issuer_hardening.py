from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from qualitative_analysis.extraction_rules import guidance_subtype, matching_rules
from qualitative_analysis.extraction_models import QualitativeClaim
from qualitative_analysis.state_synthesis import DeterministicStateSynthesisProvider
from qualitative_analysis.normalization import build_normalized_document
from qualitative_analysis.ingestion import ingest_company
from qualitative_analysis.document_store import DocumentStore
from research_output.v2 import write_institutional_dossier
from research_output.v2_models import InstitutionalDossier, AnalystHandoff, QualityGateResult, quality_gate_from_records
from research_output.v2 import _coverage_diagnostics
from research_output.v2_engines import build_business_quality
from qualitative_analysis.risk_catalyst import extract_catalysts, extract_risks
from qualitative_analysis.extraction_rules import temporal_status
from qualitative_ir_downloader.main import _newsroom_coverage_diagnostic
from scoring_engine.qualitative_scoring import _latest_state


def _claim(text: str, *, period: str, period_type_value: str, end: str) -> QualitativeClaim:
    return QualitativeClaim(
        claim_id=f"c-{period}", ticker="TST", company_name="Test", document_id=f"d-{period}",
        document_type="annual_report" if period_type_value == "ANNUAL_ACTUAL" else "quarterly_report",
        fiscal_year=2026, fiscal_quarter=None if period_type_value == "ANNUAL_ACTUAL" else 2,
        period_label=period, dimension="revenue", topic="revenue", subtopic=None,
        claim_text=text, direction="increasing", magnitude=None, certainty="actual",
        evidence_text=text, source_location={"start_char": 0, "end_char": len(text)},
        source_url="https://sec.example/test", local_path="test.txt", extraction_method="test",
        extraction_confidence=1.0, created_at="2026-09-25T00:00:00Z", period_type=period_type_value,
        period_end_date=end, available_date=end, availability_status="EXPLICIT",
    )


def test_governance_expectation_is_not_management_guidance():
    sentence = "Directors are expected to attend the annual meeting."
    assert guidance_subtype(sentence) is None
    assert all(rule.dimension != "guidance" for rule in matching_rules(sentence))


def test_explicit_non_numeric_management_guidance_is_retained_as_non_comparable():
    sentence = "Management guidance for FY 2026 is maintained."
    assert guidance_subtype(sentence) == "FINANCIAL_GUIDANCE"


def test_legal_and_accounting_expectations_are_not_guidance():
    assert guidance_subtype("The accounting assumption is expected to change with tax law.") is None
    assert guidance_subtype("The legal obligation is expected to continue through the term.") is None


def test_business_guidance_subtypes_are_contextual():
    assert guidance_subtype("We expect revenue of $10 billion next year.") == "FINANCIAL_GUIDANCE"
    assert guidance_subtype("Our gross margin outlook is 75%.") == "FINANCIAL_GUIDANCE"
    assert guidance_subtype("We target production capacity of 100 units.") == "STRATEGIC_TARGET"
    assert guidance_subtype("Our strategic plan targets 20% customer growth.") == "STRATEGIC_TARGET"


def test_state_uses_latest_reported_annual_period_by_period_end():
    result = DeterministicStateSynthesisProvider().synthesize(
        [_claim("Revenue increased in Q2.", period="Q2 FY2026", period_type_value="QUARTER_ACTUAL", end="2025-12-31"),
         _claim("Revenue increased for the fiscal year.", period="FY 2026", period_type_value="ANNUAL_ACTUAL", end="2026-06-30")],
        [],
    )
    assert result.period_label == "FY 2026"
    assert result.latest_annual_period == "FY 2026"
    assert result.latest_quarterly_period == "Q2 FY2026"
    assert result.state_freshness == "LATEST_QUARTERLY_STATE_STALE"


def test_scoring_uses_latest_annual_state_by_period_end():
    latest = _latest_state([
        {"dimension": "revenue", "period_label": "Q2 FY2026", "period_end_date": "2025-12-31", "as_of_fiscal_year": 2026, "as_of_fiscal_quarter": 2},
        {"dimension": "revenue", "period_label": "FY 2026", "period_end_date": "2026-06-30", "as_of_fiscal_year": 2026, "as_of_fiscal_quarter": None},
    ], "revenue")
    assert latest["period_label"] == "FY 2026"


def test_no_standalone_q4_is_fabricated_from_annual_actual():
    result = DeterministicStateSynthesisProvider().synthesize(
        [_claim("Annual revenue increased.", period="FY 2026", period_type_value="ANNUAL_ACTUAL", end="2026-06-30"),
         _claim("Quarterly revenue increased.", period="Q2 FY2026", period_type_value="QUARTER_ACTUAL", end="2025-12-31")], [],
    )
    assert result.period_label == "FY 2026"
    assert result.period_label != "Q4 FY2026"


def test_quality_diagnostics_distinguish_extraction_failure_from_unavailable():
    docs = [{"document_id": "proxy", "ticker": "TST", "document_type": "proxy_statement", "metadata": {"sec_form": "DEF 14A"}, "text": "Election of directors; Audit Committee; independent directors."}]
    failed_base = SimpleNamespace(management={"people_count": 0}, governance={"available": False})
    diagnostics = _coverage_diagnostics(documents=docs, claims=[], base=failed_base, state_rows=[], as_of_date="2026-09-25")
    codes = {item["code"] for item in diagnostics}
    assert "MANAGEMENT_SOURCES_AVAILABLE_BUT_ROSTER_EMPTY" in codes
    assert "AUTHORITATIVE_PROXY_AVAILABLE_BUT_NOT_ROUTED" in codes
    assert _coverage_diagnostics(documents=[], claims=[], base=failed_base, state_rows=[], as_of_date="2026-09-25") == []


def test_governance_completeness_diagnostic_keeps_optional_proxy_gaps_noncritical():
    docs = [{"document_id": "proxy", "ticker": "TST", "document_type": "proxy_statement", "metadata": {"sec_form": "DEF 14A"}, "text": "Election of directors; Audit Committee; independent directors."}]
    base = SimpleNamespace(management={"people_count": 4}, governance={"available": True, "board_size": 3, "committees": [], "independence": {"known_count": 0}})
    diagnostics = _coverage_diagnostics(documents=docs, claims=[], base=base, state_rows=[], as_of_date="2026-09-25")
    # A validated board roster satisfies the minimum governance gate; missing
    # optional committee/independence detail remains a coverage limitation.
    assert not any(item["code"] == "CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE" for item in diagnostics)
    no_board = SimpleNamespace(management={"people_count": 4}, governance={"available": True, "board_size": 0, "committees": [], "independence": {"known_count": 0}})
    blocked = _coverage_diagnostics(documents=docs, claims=[], base=no_board, state_rows=[], as_of_date="2026-09-25")
    assert any(item["code"] == "CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE" for item in blocked)


def test_governance_member_independence_map_counts_as_committee_membership():
    docs = [{"document_type": "proxy_statement", "text": "Audit Committee Independent Director"}]
    base = SimpleNamespace(
        management={"people_count": 4},
        governance={
            "available": True,
            "board_size": 13,
            "committees": [{"name": "Audit Committee", "member_independence": {"person:1": "INDEPENDENT"}}],
            "independence": {"known_count": 12},
        },
    )
    diagnostics = _coverage_diagnostics(documents=docs, claims=[], base=base, state_rows=[], as_of_date="2026-09-25")
    assert not any(item["code"] == "CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE" for item in diagnostics)


def test_state_freshness_ignores_claim_rows_with_legacy_period_aliases():
    """Claim caches must not be mistaken for state materializations."""
    docs = [{"document_id": "d", "ticker": "TST", "document_type": "quarterly_report", "period_end_date": "2026-06-30", "metadata": {"form_type": "10-Q"}}]
    claims = [{"claim_id": "c", "ticker": "TST", "period_label": "Q2 FY2026", "period_end_date": "2026-06-30", "period_type": "QUARTER_ACTUAL"}]
    base = SimpleNamespace(management={"people_count": 1}, governance={"available": False})
    state_rows = [
        {"claim_id": "legacy-claim", "period_label": "Q3 FY2026", "period_end_date": "2025-12-31", "period_type": "QUARTER_ACTUAL"},
        {"state_id": "state-current", "current_state": "healthy", "period_label": "Q2 2026", "period_end_date": "2026-06-30", "period_type": "QUARTER_ACTUAL"},
    ]
    diagnostics = _coverage_diagnostics(documents=docs, claims=claims, base=base, state_rows=state_rows, as_of_date="2026-09-25")
    assert not any(item["code"] == "CURRENT_STATE_STALE_DESPITE_NEWER_ACTUAL_REPORT" for item in diagnostics)


def test_quality_gate_treats_extraction_failure_as_release_blocker():
    result = quality_gate_from_records(documents=[{"document_id": "d"}], claims=[{"claim_id": "c"}], diagnostics=[{"code": "MANAGEMENT_SOURCES_AVAILABLE_BUT_ROSTER_EMPTY", "module": "management_intelligence", "severity": "CRITICAL", "reason": "source exists"}])
    assert result.status == "NOT_RESEARCH_READY"
    assert "MANAGEMENT_SOURCES_AVAILABLE_BUT_ROSTER_EMPTY" in result.blockers


def test_business_quality_layer_has_ten_audited_factors_without_forcing_score():
    result = build_business_quality([{"claim_id": "c", "claim_text": "Revenue growth is supported by recurring customer demand."}])
    assert len(result["factors"]) == 10
    assert result["factors"][0]["favourability"] == "UNRESOLVED"


def test_sec_acceptance_date_propagates_to_normalized_document():
    doc = build_normalized_document(ticker="TST", company_name="Test", title="TST 10-K", metadata={"category": "reports", "sec_form": "10-K", "source_url": "https://www.sec.gov/Archives/edgar/data/1/000000000026000001/tst-10k.htm", "acceptance_datetime": "2026-07-30T16:01:00Z"}, text="Annual report revenue increased.", local_path="tst-10k.htm")
    assert doc.available_date == "2026-07-30"


def test_newsroom_empty_states_are_explicit():
    assert _newsroom_coverage_diagnostic({"official_source_status": {}}) == "NEWSROOM_NOT_DISCOVERED"
    assert _newsroom_coverage_diagnostic({"official_source_status": {"OFFICIAL_NEWSROOM": "SUCCESS"}}) == "NEWSROOM_NO_ELIGIBLE_ITEMS"
    assert _newsroom_coverage_diagnostic({"official_source_status": {"OFFICIAL_NEWSROOM": "ACCESS_BLOCKED"}}) == "NEWSROOM_SOURCE_UNAVAILABLE"


def test_metadata_company_row_is_not_a_missing_artifact():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        company = root / "MSFT_Microsoft_qualitative analysis_2026-01-01"
        company.mkdir()
        (company / "manifest.json").write_text(
            '{"schema_version":"acquisition-v1","ticker":"MSFT","company_name":"Microsoft Corporation","reports":[{"title":"Microsoft Corp","name":"Microsoft Corp","source_url":"https://www.microsoft.com/en-us/Investor"}],"news":[],"events":[]}',
            encoding="utf-8",
        )
        result = ingest_company(company, DocumentStore(root / "store"))
        assert result.documents_failed == 0
        assert result.documents_discovered == 0


def test_v2_markdown_is_materialized_with_json():
    with tempfile.TemporaryDirectory() as temp:
        dossier = InstitutionalDossier(
            dossier_id="d", ticker="TST", company_name="Test", as_of_date="2026-09-25", source_cutoff="2026-09-25",
            quality_gate=QualityGateResult("RESEARCH_READY_WITH_LIMITATIONS"), analyst_handoff=AnalystHandoff(),
        )
        result = write_institutional_dossier(dossier, temp, force=True)
        assert Path(result["json"]).exists()
        assert Path(result["markdown"]).exists()


def test_coverage_warning_does_not_count_as_negative_score():
    result = quality_gate_from_records(documents=[{"document_id": "d", "publication_date": "2026-09-20"}], claims=[], cutoff="2026-09-25")
    assert result.status == "RESEARCH_READY_WITH_LIMITATIONS"
    assert not result.blockers


def test_ownership_transaction_is_not_demand():
    sentence = "Pritzker and her family acquired an indirect majority interest in 3Q."
    assert not any(rule.dimension == "demand" for rule in matching_rules(sentence))


def test_historical_narrative_is_not_current_catalyst():
    sentence = "Fiscal year 2020 began with the opportunity to build on the momentum of our record-setting 2019 performance."
    assert temporal_status(sentence) == "HISTORICAL"
    assert extract_catalysts([{"claim_id": "historical", "claim_text": sentence}]) == []


def test_forward_event_is_current_catalyst_and_inherits_public_date():
    rows = extract_catalysts([{"claim_id": "launch", "claim_text": "We plan to launch a major product in 2027.", "public_availability_date": "2026-08-01"}])
    assert len(rows) == 1
    assert rows[0].public_availability_date == "2026-08-01"
    assert rows[0].temporal_status == "FUTURE"


def test_risk_and_catalyst_records_preserve_public_availability():
    risks = extract_risks([{"claim_id": "risk", "claim_text": "Supply chain risk remains material.", "available_date": "2026-07-01"}])
    assert risks[0].public_availability_date == "2026-07-01"
    assert risks[0].available_date == "2026-07-01"


def test_unknown_public_availability_is_bucketed_and_material_release_blocks():
    result = quality_gate_from_records(
        documents=[{"document_id": "d", "document_type": "annual_report"}],
        claims=[{"claim_id": "c", "dimension": "demand"}],
        additional_records=[{"record_type": "governance", "materiality": "HIGH", "released": True}],
        cutoff="2026-09-25",
    )
    assert any(item.startswith("UNKNOWN_PUBLIC_AVAILABILITY_BY_TYPE:") for item in result.warnings)
    assert "MATERIAL_RELEASED_FINDING_PUBLIC_AVAILABILITY_UNKNOWN" in result.blockers
