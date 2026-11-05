from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import qualitative_ir_downloader.google_sheets_publisher as publisher


class _Worksheet:
    def __init__(self):
        self.id = 1
        self.values = []
        self.batch_calls = []

    def get_all_values(self):
        return self.values

    def update(self, _range, values):
        self.values = [list(row) for row in values]

    def batch_update(self, requests):
        self.batch_calls.append(requests)

    def freeze(self, rows=1):
        return None

    def format(self, *_args, **_kwargs):
        return None


class _Book:
    def __init__(self):
        self.sheet = None

    def worksheet(self, _ticker):
        if self.sheet is None:
            raise Exception("WorksheetNotFound")
        return self.sheet

    def add_worksheet(self, **_kwargs):
        self.sheet = _Worksheet()
        return self.sheet


class _Client:
    def __init__(self):
        self.book = _Book()

    def open_by_key(self, _key):
        return self.book


class GoogleSheetsV4Tests(unittest.TestCase):
    def test_overloaded_terms_are_evidence_safe(self):
        self.assertFalse(publisher._v4_semantic_compatible("CAPACITY_STRUCTURE", "borrowing capacity at Federal Home Loan Banks"))
        self.assertFalse(publisher._v4_semantic_compatible("CAPACITY_STRUCTURE", "capacity as an agent"))
        self.assertTrue(publisher._v4_semantic_compatible("CAPACITY_STRUCTURE", "distribution capacity expansion increased throughput"))
        self.assertFalse(publisher._v4_semantic_compatible("MARGIN", "collateral margin requirement"))
        self.assertTrue(publisher._v4_semantic_compatible("GROSS_MARGIN", "gross profit rate increased"))
        self.assertFalse(publisher._v4_semantic_compatible("INDUSTRY_CYCLICALITY", "compensation risk-taking cycle"))
        self.assertFalse(publisher._v4_semantic_compatible("PRICING_REALIZATION", "price input is expressed assuming a par value of $100"))

    def test_numeric_kpi_resolution_and_periods_use_authoritative_artifacts(self):
        context = {
            "claims": [{
                "claim_id": "clm_gm", "dimension": "gross_margin",
                "claim_text": "Gross profit rate increased.", "evidence_text": "Gross profit rate increased.",
                "magnitude": {"value": 42, "unit": "basis points"}, "period_label": "Q2 FY2027",
                "period_end_date": "2026-07-31", "public_availability_date": "2026-08-28",
                "source_url": "https://sec.example/gm", "pit_eligible": True,
            }],
            "facts": [],
            "states": [
                {"state_id": "s2", "period_label": "Q2 FY2027", "period_end_date": "2026-07-31", "as_of_fiscal_year": 2027, "as_of_fiscal_quarter": 2, "period_type": "QUARTER_ACTUAL", "public_availability_date": "2026-08-28", "pit_eligible": True},
                {"state_id": "s1", "period_label": "Q1 FY2027", "period_end_date": "2026-04-30", "as_of_fiscal_year": 2027, "as_of_fiscal_quarter": 1, "period_type": "QUARTER_ACTUAL", "public_availability_date": "2026-05-30", "pit_eligible": True},
            ],
            "future": [], "sources": [], "management": [], "governance": [], "temporal": [],
        }
        periods = publisher._v4_period_summary(context, {}, "2026-09-25")
        self.assertEqual(periods["current_period"], "Q2 FY2027")
        self.assertEqual(periods["previous_period"], "Q1 FY2027")
        rows = publisher._v4_kpis({"demand_assessment": {"factors": [{"factor": "Gross margin", "evidence": ["gross profit rate increased"]}]}}, context, "WMT", "2026-09-25")
        self.assertEqual(rows[0]["latest_value"], 42)
        self.assertEqual(rows[0]["latest_period"], "Q2 FY2027")
        self.assertEqual(rows[0]["source_status"], "VERIFIED_INTERNAL_SOURCE")

    def test_driver_relationship_gate_rejects_cartesian_and_placeholder(self):
        kpis = [{"kpi_id": "k1", "kpi": "GROSS_MARGIN", "definition": "gross margin"}, {"kpi_id": "k2", "kpi": "REVENUE", "definition": "revenue"}]
        good = {"driver_id": "d1", "driver": "PRICING", "classification": "MODEL_READY_DRIVER", "affected_kpis": ["GROSS_MARGIN"], "mechanism": "higher price realization increases gross margin", "evidence_ids": ["clm1"], "evidence_strength": "HIGH", "confidence": "HIGH", "source_status": "VERIFIED_INTERNAL_SOURCE"}
        bad = {"driver_id": "d2", "driver": "VAGUE", "classification": "MODEL_READY_DRIVER", "affected_kpis": ["GROSS_MARGIN", "REVENUE"], "mechanism": publisher._V4_PLACEHOLDER_MECHANISM, "evidence_ids": [], "confidence": "UNKNOWN", "source_status": "UNKNOWN"}
        accepted, rejected = publisher._v4_relationships(kpis, [good, bad], "ABC")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["kpi"], "GROSS_MARGIN")
        self.assertTrue(any(row["reason"] == "SPECIFIC_MECHANISM_REQUIRED" for row in rejected))

    def test_driver_relationship_gate_rejects_not_captured_mechanism(self):
        kpis = [{"kpi_id": "k1", "kpi": "REVENUE", "definition": "revenue"}]
        driver = {"driver_id": "d1", "driver": "VOLUME", "classification": "PROXY_REQUIRED", "affected_kpis": ["REVENUE"], "mechanism": "NOT_CAPTURED", "evidence_ids": ["clm1"], "source_status": "VERIFIED_INTERNAL_SOURCE"}
        accepted, rejected = publisher._v4_relationships(kpis, [driver], "ABC")
        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]["reason"], "SPECIFIC_MECHANISM_REQUIRED")

    def test_financial_fact_resolution_prefers_latest_authoritative_period(self):
        context = {
            "claims": [],
            "facts": [{
                "fact_id": "f1", "metric": "revenue", "value": 100, "unit": "USD",
                "fiscal_period": "FY", "fiscal_year": 2025, "period_end": "2025-12-31",
                "created_at": "2026-02-13", "pit_eligible": True,
            }, {
                "fact_id": "f2", "metric": "revenue", "value": 110, "unit": "USD",
                "fiscal_period": "Q2", "fiscal_year": 2026, "period_end": "2026-06-30",
                "created_at": "2026-08-06", "pit_eligible": True,
            }],
            "states": [{"period_label": "Q2 2026", "period_end_date": "2026-06-30", "pit_eligible": True}],
            "documents": [], "future": [], "sources": [], "management": [], "governance": [], "temporal": [],
        }
        rows = publisher._v4_kpis({"demand_assessment": {"factors": [{"factor": "Revenue", "evidence": ["revenue disclosed"]}]}}, context, "ABC", "2026-09-25")
        self.assertEqual(rows[0]["latest_value"], 110)
        self.assertEqual(rows[0]["latest_period"], "Q2 FY2026")

    def test_pit_integrity_reports_used_future_records_as_zero(self):
        payload = {"ticker": "ABC", "company_name": "Acme", "as_of_date": "2026-09-25"}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "ABC"
            (root / "04_Temporal_Analysis").mkdir(parents=True)
            (root / "04_Temporal_Analysis" / "temporal_changes.jsonl").write_text('{"ticker":"ABC","claim_id":"future","public_availability_date":"2026-10-01"}\n', encoding="utf-8")
            state = publisher.build_canonical_state(payload, ticker="ABC", as_of_date="2026-09-25", production_root=root)
        pit = {row["cells"][0]: row["cells"][1:] for row in state.records if row["section_id"] == "27_integrity" and row["row_kind"] == "record"}
        self.assertEqual(pit["Future Documents"][0], 0)
        self.assertEqual(pit["Future Claims"][0], 0)

    def test_definition_risk_is_rejected_but_real_risk_survives(self):
        rows, definitions, procedural = publisher._v4_filter_watchlist([
            {"canonical_risk": "CET1 and RWA reflect", "mechanism": "CET1 and RWA reflect regulatory methodology."},
            {"canonical_risk": "Demand pressure", "mechanism": "lower demand could reduce comparable sales and gross profit", "evidence_ids": ["clm1"]},
        ], "risk")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["canonical"], "Demand pressure")
        self.assertGreaterEqual(definitions, 1)
        self.assertEqual(procedural, 0)

    def test_generic_catalyst_placeholder_is_not_promoted(self):
        rows, _definitions, procedural = publisher._v4_filter_watchlist([
            {"canonical_event": "approval of a plan", "mechanism": "The event could change future operating or strategic expectations; magnitude remains evidence-dependent.", "evidence": []},
        ], "catalyst")
        self.assertEqual(rows, [])
        self.assertEqual(procedural, 1)

    def test_stale_watchlist_rows_are_archived_not_promoted_to_pm(self):
        payload = {"ticker": "ABC", "company_name": "Acme", "as_of_date": "2026-09-25", "risks": {"records": [{"risk_id": "r1", "canonical_risk": "Historical demand pressure", "mechanism": "lower demand reduces revenue", "current_status": "STALE", "evidence_ids": ["clm1"]}]}}
        state = publisher.build_canonical_state(payload, ticker="ABC", as_of_date="2026-09-25")
        pm = [row for row in state.records if row.get("section_id") == "22_risks" and row.get("row_kind") == "record"]
        deep = [row for row in state.records if row.get("section_id") == "22_risks_deep" and row.get("row_kind") == "record"]
        self.assertEqual(pm, [])
        self.assertEqual(deep, [])

    def test_sector_ontology_does_not_leak_between_bank_and_retail(self):
        retail = {"demand_assessment": {"industry_adapter": "retail-v1"}}
        bank = {"demand_assessment": {"industry_adapter": "bank-v1"}}
        self.assertTrue(publisher._v4_sector_compatible("COMPARABLE_SALES", retail, "positive comparable sales"))
        self.assertFalse(publisher._v4_sector_compatible("COMPARABLE_SALES", bank, "positive comparable sales"))
        self.assertTrue(publisher._v4_sector_compatible("DEPOSITS", bank, "deposit balances increased"))
        self.assertFalse(publisher._v4_sector_compatible("DEPOSITS", retail, "deposit balances increased"))

    def test_unsupported_legacy_adapter_hint_falls_back_to_generic(self):
        legacy_hint = {"demand_assessment": {"industry_adapter": "retail-v1", "factors": [{"factor": "GROSS_MARGIN_DURABILITY", "evidence": ["gross profit rate increased"]}]}}
        self.assertEqual(publisher._v4_sector_adapter(legacy_hint), "generic")
        self.assertFalse(publisher._v4_sector_compatible("INVENTORY", legacy_hint, "inventory balance"))

    def test_economics_does_not_promote_false_capacity_mapping(self):
        payload = {"ticker": "ABC", "company_name": "Acme", "as_of_date": "2026-09-25", "business_model": {"mechanisms": [{"dimension": "CAPACITY_STRUCTURE", "mechanism": "borrowing capacity at Federal Home Loan Banks", "evidence": ["borrowing capacity at Federal Home Loan Banks"]}]}}
        state = publisher.build_canonical_state(payload)
        economics = [row for row in state.records if row.get("section_id") == "03_economics" and row.get("row_kind") == "record"]
        self.assertEqual(economics, [])

    def test_v3_to_v4_migration_and_identical_rerun(self):
        payload = {"ticker": "ABC", "company_name": "Acme", "as_of_date": "2026-09-25", "risks": {"records": [{"risk_id": "r1", "canonical_risk": "Demand", "mechanism": "lower demand reduces revenue"}]}}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "ABC"
            client = _Client()
            worksheet = _Worksheet()
            worksheet.values = [["__record_key", "__content_hash", "__schema_version"], ["old", "old", "full-qual-sheet-v3"]]
            client.book.sheet = worksheet
            first = publisher.GoogleSheetsPublisher(client=client, now=lambda: "2026-10-28T00:00:00Z", sleep=lambda _value: None).publish(payload, production_root=root)
            self.assertEqual(first.status, "UPDATED")
            self.assertEqual(first.migration_status, "V3_TO_V4_MIGRATED")
            self.assertTrue((root / "12_Google_Sheets/ABC_v3-sheet-backup.json").exists())
            second = publisher.GoogleSheetsPublisher(client=client, now=lambda: "2026-10-28T00:00:00Z", sleep=lambda _value: None).publish(payload, production_root=root)
            self.assertEqual(second.status, "NO_CHANGES")
            self.assertEqual(second.records_updated, 0)

    def test_stale_records_are_retained_without_repeating_changes(self):
        payload = {"ticker": "ABC", "company_name": "Acme", "as_of_date": "2026-09-25", "risks": {"records": [{"risk_id": "r1", "canonical_risk": "Demand", "mechanism": "lower demand reduces revenue", "source_count": 1}]}}
        reduced = {"ticker": "ABC", "company_name": "Acme", "as_of_date": "2026-09-25", "risks": {"records": []}}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "ABC"
            client = _Client()
            pub = publisher.GoogleSheetsPublisher(client=client, now=lambda: "2026-10-28T00:00:00Z", sleep=lambda _value: None)
            pub.publish(payload, production_root=root)
            first_reduced = pub.publish(reduced, production_root=root)
            second_reduced = pub.publish(reduced, production_root=root)
        self.assertEqual(first_reduced.status, "UPDATED")
        self.assertEqual(second_reduced.status, "NO_CHANGES")


if __name__ == "__main__":
    unittest.main()
