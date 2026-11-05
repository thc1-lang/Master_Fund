from __future__ import annotations

import qualitative_ir_downloader.google_sheets_publisher as publisher


def _context(*, facts=(), claims=(), governance=(), management=(), documents=(), states=()):
    return {
        "facts": list(facts), "claims": list(claims), "governance": list(governance),
        "management": list(management), "documents": list(documents), "states": list(states),
        "temporal": [], "sources": [], "future": [],
    }


def test_quarter_only_wins_over_ytd_and_history_remains_traceable():
    facts = [
        {"fact_id": "ytd", "metric": "revenue", "value": 600, "unit": "USD", "period_start": "2026-01-01", "period_end": "2026-06-30", "fiscal_period": "Q2", "fiscal_year": 2026, "pit_eligible": True},
        {"fact_id": "quarter", "metric": "revenue", "value": 250, "unit": "USD", "period_start": "2026-04-01", "period_end": "2026-06-30", "fiscal_period": "Q2", "fiscal_year": 2026, "pit_eligible": True},
    ]
    result = publisher._v51_resolve_numeric("REVENUE", _context(facts=facts), "2026-09-25", "Q2 FY2026")
    assert result["latest_value"] == 250
    assert result["period_basis"] == "QUARTER_ONLY"
    assert {row["period_basis"] for row in result["observation_history"]} == {"QUARTER_ONLY", "YTD"}


def test_generic_recurring_metric_discovery_promotes_repeated_labels_only():
    claims = [
        {"claim_id": "rpm-q1", "metric": "Revenue Passenger Miles (RPM)", "value": 100, "unit": "miles", "period_label": "Q1 2026", "period_end": "2026-03-31", "dimension": "volume"},
        {"claim_id": "rpm-q2", "metric": "Revenue Passenger Miles", "value": 110, "unit": "miles", "period_label": "Q2 2026", "period_end": "2026-06-30", "dimension": "volume"},
        {"claim_id": "one-off", "metric": "One-off table amount", "value": 7, "unit": "USD", "period_label": "Q2 2026", "period_end": "2026-06-30", "dimension": "other"},
    ]
    candidates, rejected, diagnostics = publisher._v51_discover_recurring_metrics(_context(claims=claims), "TST", "2026-09-25")
    assert any(row["recurrence_count"] == 2 for row in candidates)
    assert diagnostics["recurring_metrics_discovered"] >= 1
    assert any(row["reason"] == "NOT_RECURRING" for row in rejected)


def test_period_basis_duration_and_point_in_time():
    assert publisher._v51_period_basis({"period_start": "2026-01-01", "period_end": "2026-03-31", "fiscal_period": "Q1"}) == "QUARTER_ONLY"
    assert publisher._v51_period_basis({"period_start": "2026-01-01", "period_end": "2026-06-30", "fiscal_period": "Q2"}) == "YTD"
    assert publisher._v51_period_basis({"period_end": "2026-06-30", "period_type": "POINT_IN_TIME"}) == "POINT_IN_TIME"


def test_period_display_style_is_consistent_for_calendar_issuers():
    calendar = {"calendar_type": "calendar", "fiscal_year_end_month": 12}
    context = _context(states=[
        {"period_label": "Q2 2026", "period_end_date": "2026-06-30", "issuer_fiscal_calendar": calendar},
        {"period_label": "Q1 FY2026", "period_end_date": "2026-03-31", "issuer_fiscal_calendar": calendar},
    ])
    result = publisher._v51_periods(context, {}, "2026-09-25")
    assert result["current_period"] == "Q2 2026"
    assert result["previous_period"] == "Q1 2026"


def test_period_display_style_preserves_fy_for_non_calendar_issuers():
    calendar = {"calendar_type": "non_calendar", "fiscal_year_end_month": 7}
    context = _context(states=[
        {"period_label": "Q2 FY2027", "period_end_date": "2026-01-31", "issuer_fiscal_calendar": calendar},
        {"period_label": "Q1 2027", "period_end_date": "2025-10-31", "issuer_fiscal_calendar": calendar},
    ])
    result = publisher._v51_periods(context, {}, "2026-09-25")
    assert result["current_period"] == "Q2 FY2027"
    assert result["previous_period"] == "Q1 FY2027"


def test_scope_mismatch_and_equity_repricing_are_rejected():
    kpi = [{"kpi_id": "k", "kpi": "REVENUE", "kpi_type": "MODEL_READY_KPI", "evidence_ids": ["k"], "source_status": "VERIFIED_INTERNAL", "scope_type": "CONSOLIDATED", "scope_name": "CONSOLIDATED"}]
    driver = [{"driver_id": "d", "driver": "PRICING", "driver_type": "MODEL_READY_DRIVER", "evidence_ids": ["d"], "source_status": "VERIFIED_INTERNAL", "mechanism": "No Repricing without Shareholder Approval", "scope_type": "CONSOLIDATED", "scope_name": "CONSOLIDATED"}]
    assert publisher._v51_operating_pricing(driver[0]) is False
    driver[0].update({"driver": "REFINERY_PRICING", "scope_type": "BUSINESS_UNIT", "scope_name": "Refinery", "mechanism": "Higher refinery pricing increases sales revenue"})
    accepted, rejected, _ = publisher._v51_relationships(kpi, driver, "TST", sector="generic")
    assert accepted == [] or any(row["reason"] == "SCOPE_MISMATCH" for row in rejected)


def test_person_validation_rejects_prose_and_accepts_role_name():
    assert publisher._v51_person_validation("Our financial performance", {"role": "Named Executive Officer"}) == "REJECTED_NON_PERSON"
    assert publisher._v51_person_validation("Board of Directors", {}) == "REJECTED_NON_PERSON"
    assert publisher._v51_person_validation("Jane Doe", {"role": "Chief Executive Officer"}) == "VERIFIED_PERSON"


def test_risk_recall_keeps_direct_exposure_and_rejects_definition():
    payload = {"risks": {"records": [
        {"risk_id": "accident", "canonical_risk": "Aircraft accident losses", "mechanism": "We are at risk of losses and adverse publicity stemming from a serious accident involving our aircraft.", "current_status": "OPEN"},
        {"risk_id": "definition", "canonical_risk": "Risk definition", "mechanism": "Value at risk is a measure of potential loss."},
    ]}}
    rows, rejected, diagnostics = publisher._v51_watchlist(payload, [], [], "risk")
    assert rows[0]["inference_type"] == "DIRECT_RISK_DISCLOSURE"
    assert any(item["rejection_reason"] == "RISK_DEFINITION" for item in rejected)
    assert diagnostics["false_negative_explicit_risks"] == 0


def test_risk_exposure_plus_economic_damage_is_retained_as_inference():
    payload = {"risks": {"records": [{
        "risk_id": "fuel", "canonical_risk": "Fuel exposure", "mechanism": "We have exposure to fuel prices that could increase costs and compress margin.", "current_status": "OPEN"
    }]}}
    rows, _rejected, diagnostics = publisher._v51_watchlist(payload, [], [], "risk")
    assert rows and rows[0]["inference_type"] in {"DIRECT_RISK_DISCLOSURE", "EVIDENCE_BACKED_RISK_INFERENCE"}
    assert diagnostics["false_negative_explicit_risks"] == 0


def test_consistency_uses_one_canonical_relationship_collection():
    relationship = {"relationship_id": "relationship:t:d:k", "economic_mechanism": "driver changes KPI"}
    check = publisher._v51_consistency([], [], [relationship], [], [], [{"evidence_ids": [relationship["relationship_id"]]}])
    assert check["consistent"] is True
    assert check["pm_relationship_ids"] == check["handoff_relationship_ids"]


def test_governance_sec_proxy_fallback_is_explicit():
    context = _context(governance=[
        {"source_url": "https://www.sec.gov/Archives/edgar/data/x/def14a.htm", "committee_type": "audit"},
        {"source_url": "https://www.sec.gov/Archives/edgar/data/x/def14a.htm", "full_name": "Jane Doe", "role": "Director"},
    ])
    result = publisher._v51_governance_status(context, {"ir_status": "BLOCKED"})
    assert result["sec_proxy_available"] is True
    assert result["fallback_attempted"] is True
    assert result["status"] in {"PARTIAL", "AVAILABLE"}
    assert result["critical_blocker"] is False


def test_governance_proxy_parse_gap_remains_critical_when_ir_is_available():
    context = _context(governance=[{"source_url": "https://www.sec.gov/Archives/edgar/data/x/def14a.htm", "committee_type": "audit"}])
    result = publisher._v51_governance_status(context, {"ir_status": "SUCCESS"})
    assert result["sec_proxy_available"] is True
    assert result["status"] == "GOVERNANCE_INSUFFICIENT"
    assert result["critical_blocker"] is True


def test_management_summary_does_not_promote_invalid_proxy_person():
    context = _context(management=[{"full_name": "Our financial performance", "title": "Named Executive Officer"}])
    rows = publisher._v51_management_summary({}, context, "TST")
    assert rows
    assert all(row["assessment"] == "INSUFFICIENT_VALID_PERSON_EVIDENCE" for row in rows)

