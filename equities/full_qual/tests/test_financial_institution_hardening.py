from __future__ import annotations

import json
from pypdf import PdfWriter

from insider_intelligence.models import SECFiling
from management_intelligence.capital_allocation_engine import classify_capital_event
from management_intelligence.career_history import parse_proxy_people
from qualitative_analysis.extraction_rules import classify_regulatory_semantics, matching_rules
from qualitative_analysis.fiscal_periods import canonical_period_identity
from qualitative_analysis.ingestion import ingest_company
from qualitative_analysis.risk_catalyst import classify_risk_semantics, extract_risks, risk_coverage_diagnostics
from qualitative_analysis.state_rules import bank_balance_sheet_state
from research_output.v2_engines import build_business_quality


def _filing() -> SECFiling:
    return SECFiling(
        ticker="TST", issuer_cik="0000000001", form_type="DEF 14A",
        accession_number="0000000000-26-000001", filing_date="2026-04-01",
        report_date=None, primary_document="proxy.htm", source_url="https://sec.test/proxy",
    )


def test_flattened_proxy_roster_and_neo_list_are_generic():
    text = """
    Director nominees
    All are independent other than our CEO.
    Alice B. Smith
    Lead Independent Director
    Director since
    2020
    62 Retired executive
    Bob C. Jones
    Director since
    2022
    58 Professor
    Our 2025 NEOs are:
    Casey Adams
    Chief Executive Officer
    Jordan Finance
    Chief Financial Officer
    2025 Firm performance
    """
    result = parse_proxy_people(text, _filing(), "Test Financial Group")
    names = {person.full_name for person in result.people}
    assert {"Alice B. Smith", "Bob C. Jones", "Casey Adams", "Jordan Finance"} <= names
    assert len(result.boards) == 2
    assert any(role.role == "Lead Independent Director" for role in result.roles)


def test_bank_operating_dimensions_and_quality_adapter():
    assert any(rule.dimension == "net_interest_income" for rule in matching_rules("Net interest income increased 8% and CET1 was 15%."))
    quality = build_business_quality([
        {"claim_id": "1", "dimension": "net_interest_income", "claim_text": "Net interest income increased."},
        {"claim_id": "2", "dimension": "cet1", "claim_text": "CET1 capital ratio remained strong."},
    ])
    assert quality["industry_adapter"] == "bank-v1"
    assert len(quality["factors"]) >= 10


def test_bank_semantic_safeguards():
    assert classify_risk_semantics("VaR methodology is calculated using a non-additive model.") == "RISK_MEASUREMENT_METHOD"
    assert extract_risks([{"claim_id": "v", "claim_text": "VaR methodology is calculated using a non-additive model."}]) == []
    assert classify_risk_semantics("Commercial real-estate credit losses increased materially.") in {"RISK_TREND", "RISK_OUTCOME", "RISK_FACTOR"}
    assert classify_capital_event("The acquisition increased the NII run rate.") == "OTHER"
    assert classify_capital_event("The merger agreement closed for $2 billion.") == "M_AND_A"
    assert classify_regulatory_semantics("A pending capital requirement will constrain distributions.") == "PENDING_RULE_CHANGE"


def test_risk_coverage_diagnostics_keep_methodology_out_of_active_groups():
    rows = [
        {"claim_id": "method", "claim_text": "VaR methodology is calculated using a model."},
        {"claim_id": "risk", "claim_text": "Credit risk remains material."},
        {"claim_id": "risk-dup", "claim_text": "Credit risk remains material."},
        {"claim_id": "old", "claim_text": "Historical risk was resolved.", "temporal_status": "HISTORICAL"},
    ]
    extracted = extract_risks(rows)
    canonical = [{"current_status": "OPEN"}]
    diagnostics = risk_coverage_diagnostics(rows, extracted, canonical)
    assert diagnostics["raw_candidate_count"] == 4
    assert diagnostics["methodology_excluded"] == 1
    assert diagnostics["historical_excluded"] == 1
    assert diagnostics["active_risk_groups"] == 1
    assert "method" not in {item.evidence_ids[0] for item in extracted}


def test_period_identity_deduplicates_calendar_labels_but_preserves_non_calendar():
    calendar = {"calendar_type": "calendar"}
    non_calendar = {"calendar_type": "non_calendar"}
    a = canonical_period_identity(issuer="TST", fiscal_year=2025, fiscal_quarter=1, period_end_date="2025-03-31", period_type="QUARTER_ACTUAL", issuer_fiscal_calendar=calendar)
    b = canonical_period_identity(issuer="TST", fiscal_year=2025, fiscal_quarter=1, period_end_date="2025-03-31", period_type="QUARTER_ACTUAL", issuer_fiscal_calendar=calendar)
    c = canonical_period_identity(issuer="TST", fiscal_year=2027, fiscal_quarter=1, period_end_date="2026-09-30", period_type="QUARTER_ACTUAL", issuer_fiscal_calendar=non_calendar)
    assert a == b
    assert "Q1 FY2027" in c


def test_bank_balance_sheet_magnitude_does_not_set_quality():
    claim = type("Claim", (), {"evidence_text": "Loans and deposits increased 20%."})()
    assert bank_balance_sheet_state([claim], "weak") == "unknown"
    strong = type("Claim", (), {"evidence_text": "The bank remains well-capitalized and liquid."})()
    assert bank_balance_sheet_state([strong], "strong") == "strong"


def test_image_only_agenda_is_excluded_without_normalization_failure(tmp_path):
    company = tmp_path / "JPM_JPMorgan_qualitative analysis_2026-09-25"
    company.mkdir()
    agenda = company / "Reports" / "2026 Company Update Agenda.pdf"
    agenda.parent.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with agenda.open("wb") as handle:
        writer.write(handle)
    (company / "manifest.json").write_text(json.dumps({
        "ticker": "JPM",
        "company_name": "JPMorgan Chase & Co.",
        "reports": [{
            "title": "2026 Company Update Agenda",
            "local_filename": "Reports/2026 Company Update Agenda.pdf",
            "category": "reports",
        }],
    }), encoding="utf-8")
    result = ingest_company(company, dry_run=True)
    assert result.documents_normalized == 0
    assert result.documents_failed == 0
    assert result.non_substantive_excluded == 1
    assert result.failures[0].failure_type == "NON_SUBSTANTIVE_EXCLUDED"
    assert result.failures[0].warning is True
