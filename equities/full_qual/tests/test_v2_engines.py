from __future__ import annotations

import unittest

from research_output.v2_engines import (
    build_business_quality,
    build_competitive_advantage,
    build_guidance_reliability,
    build_industry_structure,
    build_operating_execution,
    canonicalize_catalysts,
    canonicalize_risks,
    materiality_rank,
)
from pipeline_runner.models import StageStatus
from pipeline_runner.runner import PipelineRunner


class V2EngineTests(unittest.TestCase):
    def test_competitive_advantage_requires_evidence_and_retains_conflict(self):
        claims = [
            {"claim_id": "c1", "claim_text": "The platform is embedded in customer workflows and difficult to switch."},
            {"claim_id": "c2", "claim_text": "Customers may substitute competing technology."},
        ]
        result = build_competitive_advantage(claims)
        record = next(item for item in result["records"] if item["type"] in {"SWITCHING_COSTS", "TECHNOLOGY_SUBSTITUTION"})
        self.assertTrue(record["supporting_evidence"])
        self.assertIn(record["assessment_status"], {"SUPPORTED", "CONFLICTED", "INFERRED"})
        self.assertEqual(build_competitive_advantage([])["status"], "NO_PROVEN_ADVANTAGE")

    def test_industry_generic_fallback_and_substitutes(self):
        result = build_industry_structure([{"claim_id": "c", "claim_text": "The industry faces technology substitution and customer bargaining pressure."}])
        self.assertEqual(result["adapter"], "generic-v1")
        self.assertIn("SUBSTITUTES", {row["category"] for row in result["records"]})

    def test_operating_execution_does_not_mark_unresolved_overdue_commitment_failed(self):
        result = build_operating_execution([], [{"commitment_id": "x", "commitment_text": "Plan to launch the product by 2026-01-01", "target_date": "2026-01-01"}], "2026-09-25")
        self.assertEqual(result["records"][0]["status"], "UNKNOWN")
        self.assertNotEqual(result["records"][0]["status"], "FAILED")

    def test_guidance_range_and_incompatible_missing_actual(self):
        result = build_guidance_reliability([], {}, [{"guidance_id": "g", "issue_date": "2026-01-01", "metric": "revenue", "target_period": "FY2026", "low": 100, "high": 120, "unit": "USD"}], [], "2026-09-25")
        self.assertEqual(result["records"][0]["midpoint"], 110)
        self.assertEqual(result["records"][0]["outcome"], "OPEN")
        self.assertEqual(result["coverage"]["open"], 1)

    def test_quality_factors_are_separate_from_trajectory(self):
        result = build_business_quality([{"claim_id": "c", "claim_text": "Recurring revenue and customer retention improved."}])
        self.assertIn("REVENUE_QUALITY", {row["factor"] for row in result["factors"]})
        self.assertNotEqual(result["factors"][0]["status"], "ABSOLUTE_QUALITY")

    def test_risk_and_catalyst_canonicalization_and_materiality(self):
        risks = canonicalize_risks([
            {"claim_id": "r1", "claim_text": "A material supply chain risk remains."},
            {"claim_id": "r2", "claim_text": "A material supply chain risk remains."},
        ], "2026-09-25")
        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]["source_count"], 2)
        catalysts = canonicalize_catalysts([{ "claim_id": "c1", "claim_text": "A major product launch is planned for 2026-12-01", "event_date": "2026-12-01"}], "2026-09-25")
        self.assertEqual(catalysts[0]["current_status"], "UPCOMING")
        self.assertTrue(materiality_rank(risks))

    def test_execution_log_separates_compatibility_from_rebuild(self):
        runner = PipelineRunner("ABC", "2026-09-25", profile="core_v2")
        stage = runner._stage("dossier_generation", StageStatus.SUCCESS, provenance={"compatibility": {"action": "REBUILD_REQUIRED"}})
        self.assertEqual(stage.provenance["input_compatibility"], "REBUILD_REQUIRED")
        self.assertEqual(stage.provenance["stage_execution_action"], "REBUILT")
        self.assertEqual(stage.change, "REBUILT")


if __name__ == "__main__":
    unittest.main()
