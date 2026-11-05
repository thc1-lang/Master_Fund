from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import qualitative_ir_downloader.google_sheets_publisher as publisher


class GoogleSheetsV5Tests(unittest.TestCase):
    def _kpi(self, name="GROSS_MARGIN", evidence=True, value=25):
        return {
            "kpi_id": f"kpi:{name.lower()}", "kpi": name, "display_name": name,
            "kpi_type": "MODEL_READY_KPI", "definition": name,
            "evidence_ids": ["kpi-evidence"] if evidence else [],
            "evidence_strength": "HIGH" if evidence else "LOW",
            "source_status": "VERIFIED_INTERNAL" if evidence else "UNKNOWN",
            "historical_data_availability": "HIGH", "latest_value": value,
        }

    def _driver(self, name="PRICING", evidence=True, mechanism=""):
        return {
            "driver_id": f"driver:{name.lower()}", "driver": name,
            "driver_type": "MODEL_READY_DRIVER", "definition": name,
            "evidence_ids": ["driver-evidence"] if evidence else [],
            "evidence_strength": "HIGH" if evidence else "LOW",
            "source_status": "VERIFIED_INTERNAL" if evidence else "UNKNOWN",
            "mechanism": mechanism,
        }

    def test_direct_evidence_relationship_is_accepted(self):
        kpi = self._kpi()
        driver = self._driver(mechanism="higher pricing increases gross margin")
        accepted, rejected, diagnostics = publisher._v5_relationships([kpi], [driver], "ABC", sector="retail")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["inference_type"], "DIRECT_EVIDENCE")
        self.assertEqual(accepted[0]["validation_status"], "QUALITATIVE_CANDIDATE")
        self.assertEqual(rejected, [])
        self.assertGreaterEqual(accepted[0]["mechanism_confidence"], 65)
        self.assertEqual(diagnostics["cartesian_relationships"] if "cartesian_relationships" in diagnostics else 0, 0)

    def test_evidence_backed_inference_is_accepted_without_literal_sentence(self):
        kpi = self._kpi("REVENUE")
        driver = self._driver("TRAFFIC", mechanism="company reports traffic observations")
        accepted, _rejected, _diagnostics = publisher._v5_relationships([kpi], [driver], "ABC", sector="retail")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["inference_type"], "QUALITATIVE_INFERENCE")
        self.assertIn("transactions", accepted[0]["economic_mechanism"])

    def test_sector_rules_do_not_create_unrelated_cost_to_revenue_pair(self):
        kpis = [self._kpi("REVENUE")]
        driver = self._driver("FULFILLMENT COST", mechanism="company reports fulfillment cost")
        accepted, _rejected, diagnostics = publisher._v5_relationships(kpis, [driver], "ABC", sector="retail")
        self.assertEqual(accepted, [])
        self.assertEqual(diagnostics["potential_pairs"], 0)

    def test_coexistence_no_company_evidence_and_no_mechanism_are_rejected(self):
        kpi = self._kpi("REVENUE", evidence=False)
        driver = self._driver("TRAFFIC", evidence=False, mechanism="")
        accepted, rejected, diagnostics = publisher._v5_relationships([kpi], [driver], "ABC", sector="retail")
        self.assertEqual(accepted, [])
        self.assertEqual(diagnostics["accepted_relationships"], 0)
        self.assertTrue(any(row["reason"] == "NO_COMPANY_EVIDENCE" for row in rejected))

    def test_contradiction_blocks_relationship(self):
        kpi = self._kpi("REVENUE")
        driver = self._driver("TRAFFIC", mechanism="higher traffic is offset by lower conversion and is not expected to increase revenue")
        accepted, rejected, _diagnostics = publisher._v5_relationships([kpi], [driver], "ABC", sector="retail")
        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]["reason"], "CONTRADICTED")

    def test_numeric_semantics_reject_basis_point_as_absolute_level(self):
        row = {"metric": "Gross margin", "claim_text": "Gross margin increased 26 basis points", "unit": "basis points", "value": 26}
        self.assertEqual(publisher._v5_numeric_semantic_type("GROSS_MARGIN", row), "BPS_CHANGE")
        self.assertEqual(publisher._v5_numeric_semantic_type("OPERATING_EXPENSE_RATE", {"claim_text": "Operating expenses as a percentage of net sales decreased 58 basis points", "unit": "basis points", "value": 58}), "BPS_CHANGE")
        payload = {"ticker": "ABC", "as_of_date": "2026-09-25", "demand_assessment": {"factors": [{"factor": "Gross margin", "evidence": ["Gross margin increased 26 basis points"], "source_url": "https://example.test"}]}}
        state = publisher.build_canonical_state(payload)
        kpis = [row for row in state.model_handoff["kpi_concepts"] if row.get("kpi") == "GROSS_MARGIN"]
        if kpis:
            self.assertNotEqual(kpis[0].get("current_value_confidence"), "HIGH")

    def test_watchlist_rejects_definition_and_accepts_direct_risk(self):
        payload = {"risks": {"records": [
            {"canonical_risk": "Regulatory definition", "mechanism": "This is a measure of risk weighted assets."},
            {"risk_id": "risk:1", "canonical_risk": "Demand pressure", "mechanism": "This risk: lower demand reduces revenue", "evidence_ids": ["risk-evidence"], "source_url": "https://example.test/risk"},
        ]}}
        rows, rejected, diagnostics = publisher._v5_watchlist(payload, [self._kpi("REVENUE")], [self._driver("TRAFFIC", mechanism="traffic changes revenue")], "risk")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inference_type"], "DIRECT_RISK_DISCLOSURE")
        self.assertTrue(rejected)
        self.assertEqual(diagnostics["definitions_rejected"], 1)

    def test_generic_catalyst_is_rejected_but_planned_event_is_retained(self):
        payload = {"catalysts": {"records": [
            {"canonical_event": "Future expectations", "mechanism": "The event could change future operating or strategic expectations; magnitude remains evidence-dependent."},
            {"canonical_event": "Planned fulfillment expansion", "mechanism": "planned fulfillment expansion can increase revenue", "evidence": ["approved expansion"], "source_url": "https://example.test/catalyst"},
        ]}}
        rows, rejected, diagnostics = publisher._v5_watchlist(payload, [self._kpi("REVENUE")], [self._driver("FULFILLMENT", mechanism="fulfillment changes revenue")], "catalyst")
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["inference_type"], {"DIRECT_CATALYST", "EVIDENCE_BACKED_CATALYST_INFERENCE"})
        self.assertTrue(rejected)
        self.assertEqual(diagnostics["accepted"], 1)

    def test_management_summary_never_uses_available_assessment(self):
        rows = publisher._v5_management_summary({"management": {"leaders": [{"name": "Jane Doe", "role": "CEO"}]}}, {}, "ABC")
        self.assertTrue(rows)
        allowed = {"POSITIVE", "MIXED", "NEGATIVE", "STABLE", "INSUFFICIENT_EVIDENCE", "NOT_APPLICABLE"}
        self.assertTrue(all(row["assessment"] in allowed for row in rows))

    def test_machine_handoff_is_written_atomically(self):
        payload = {"ticker": "ABC", "as_of_date": "2026-09-25", "demand_assessment": {"factors": [{"factor": "Revenue", "evidence": ["sales disclosed"], "source_url": "https://example.test"}]}}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp) / "ABC"
            state = publisher.build_canonical_state(payload, production_root=root)
            path = root / "13_Model_Handoff" / "ABC_2026-09-25_qual-model-handoff-v1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state.model_handoff), encoding="utf-8")
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["schema_version"], "qual-model-handoff-v1")
        self.assertIn("kpi_series", loaded)
        self.assertIn("driver_series", loaded)
        self.assertIn("relationships", loaded)

    def test_canonical_period_identity_overrides_comparative_flattened_label(self):
        context = {
            "documents": [{
                "document_id": "doc:comparative", "period_label": "Q3 FY2019",
                "period_end_date": "2018-12-31",
                "document_type": "quarterly_report",
                "canonical_period_identity": "JPM|Q4 2018||2018-12-31|QUARTER_ACTUAL",
                "pit_eligible": True,
            }],
            "claims": [], "states": [], "facts": [], "future": [],
            "sources": [], "management": [], "governance": [], "temporal": [],
        }
        periods = publisher._v4_period_summary(context, {}, "2026-09-25")
        integrity = publisher._v5_period_integrity(context, periods)
        self.assertEqual(periods["current_period"], "Q4 FY2018")
        self.assertFalse(integrity["fiscal_calendar_inconsistent"])
        self.assertTrue(integrity["canonical_label_mismatches"])

    def test_authoritative_period_conflict_blocks_quality_gate(self):
        context = {
            "documents": [
                {"document_id": "doc:a", "document_type": "quarterly_report", "canonical_period_identity": "ABC|Q2 FY2026|2026-01-31|2026-04-30|QUARTER_ACTUAL", "pit_eligible": True},
                {"document_id": "doc:b", "document_type": "quarterly_report", "canonical_period_identity": "ABC|Q2 FY2026|2026-04-30|2026-07-31|QUARTER_ACTUAL", "pit_eligible": True},
            ],
            "claims": [], "states": [], "facts": [], "future": [],
            "sources": [], "management": [], "governance": [], "temporal": [],
        }
        integrity = publisher._v5_period_integrity(context, {"current_period": "Q2 FY2026"})
        self.assertEqual(integrity["status"], "BLOCKED")
        self.assertIn("FISCAL_CALENDAR_INCONSISTENT", integrity["issues"])

    def test_rejected_ambiguous_numeric_is_safe_and_does_not_enter_current_state(self):
        payload = {"ticker": "ABC", "as_of_date": "2026-09-25", "demand_assessment": {"factors": [{
            "factor": "Gross margin", "evidence": ["Gross margin increased 26 basis points"],
            "source_url": "https://example.test",
        }]}}
        rows = publisher._v5_kpis(payload, {"claims": [], "facts": [], "states": [], "documents": [], "future": [], "sources": [], "management": [], "governance": [], "temporal": []}, "ABC", "2026-09-25")
        self.assertTrue(rows)
        self.assertNotEqual(rows[0].get("current_value_confidence"), "HIGH")
        self.assertIn(rows[0].get("value_semantic_type"), {"RATIO", "BPS_CHANGE", "PERCENTAGE_POINT_CHANGE", "AMBIGUOUS_NUMERIC_SEMANTICS"})

    def test_governance_rows_resolve_person_ids_to_human_names(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "VOD"
            (root / "07_Management_Intelligence").mkdir(parents=True)
            (root / "08_Board_Governance").mkdir(parents=True)
            (root / "07_Management_Intelligence" / "people.jsonl").write_text(
                json.dumps({
                    "ticker": "VOD",
                    "person_id": "person:alice",
                    "full_name": "Alice Example",
                }) + "\n",
                encoding="utf-8",
            )
            (root / "08_Board_Governance" / "governance_board_memberships.jsonl").write_text(
                json.dumps({
                    "ticker": "VOD",
                    "person_id": "person:alice",
                    "board_membership_id": "governance-board:VOD:person:alice",
                    "board_role": "Independent Non-Executive Director",
                    "is_independent": "INDEPENDENT",
                    "committee_memberships": ["Audit Committee"],
                    "source_url": "https://example.test/20-f",
                    "public_availability_date": "2026-05-22",
                    "board_status": "CURRENT",
                }) + "\n",
                encoding="utf-8",
            )
            state = publisher.build_canonical_state(
                {"ticker": "VOD", "company_name": "Vodafone", "as_of_date": "2026-09-25"},
                ticker="VOD",
                as_of_date="2026-09-25",
                production_root=root,
            )

        rows = [
            row for row in state.records
            if row.get("section_id") == "20_governance" and row.get("row_kind") == "record"
        ]
        board = next(row for row in rows if row["cells"][1] == "Independent Non-Executive Director")
        self.assertEqual(board["cells"][0], "Alice Example")
        self.assertNotIn("person:", board["cells"][0])


if __name__ == "__main__":
    unittest.main()
