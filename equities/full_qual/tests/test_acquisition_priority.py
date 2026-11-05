import unittest

from qualitative_ir_downloader.acquisition_priority import (
    TIER_1_CRITICAL_FINANCIAL,
    TIER_2_GOVERNANCE_MANAGEMENT,
    TIER_5_GENERAL_NEWS,
    critical_source_coverage,
    priority_key,
    priority_tier,
    sort_documents,
    source_roles,
)
from qualitative_ir_downloader.models import Document
from qualitative_ir_downloader.crawler import archive_low_yield_stop
from qualitative_ir_downloader.config import Config


class AcquisitionPriorityTests(unittest.TestCase):
    def test_critical_documents_precede_large_news_archive(self):
        news = [Document(f"News {i}", "2026-09-20", f"https://ir.example.test/news/{i}", "news") for i in range(500)]
        rows = news + [
            Document("2026 Interim Results", "2026-08-01", "https://ir.example.test/reports/interim.pdf", "reports"),
            Document("2025 Annual Report", "2026-02-01", "https://ir.example.test/reports/annual.pdf", "reports"),
            Document("Corporate Governance Report", "2026-04-01", "https://ir.example.test/reports/governance.pdf", "reports"),
        ]
        ordered = sort_documents(rows, as_of_date="2026-09-25")
        self.assertTrue(all(priority_tier(row) < TIER_5_GENERAL_NEWS for row in ordered[:3]))
        self.assertEqual(priority_tier(ordered[-1]), TIER_5_GENERAL_NEWS)

    def test_current_pit_document_precedes_future_document(self):
        rows = [
            Document("Future trading update", "2026-10-01", "https://ir.example.test/future.pdf", "reports"),
            Document("Interim results", "2026-09-01", "https://ir.example.test/current.pdf", "reports"),
            Document("Annual report", "2025-05-01", "https://ir.example.test/annual.pdf", "reports"),
        ]
        ordered = sort_documents(rows, as_of_date="2026-09-25")
        self.assertEqual(ordered[0].title, "Interim results")
        self.assertEqual(priority_key(ordered[-1], as_of_date="2026-09-25").pit_rank, 2)

    def test_foreign_form_roles_are_jurisdiction_neutral(self):
        annual = {"title": "Issuer 20-F", "source_url": "https://sec.test/annual.htm", "category": "reports"}
        interim = {"title": "Issuer 6-K Half-Year Results", "source_url": "https://sec.test/current.htm", "category": "reports"}
        self.assertIn("ANNUAL_FINANCIAL_REPORT", source_roles(annual))
        self.assertIn("INTERIM_FINANCIAL_REPORT", source_roles(interim))
        self.assertEqual(priority_tier(annual), TIER_1_CRITICAL_FINANCIAL)

    def test_generic_6k_does_not_claim_interim_financial_role(self):
        generic = {
            "title": "Issuer 6-K Call Option Exercise",
            "source_url": "https://sec.test/current.htm",
            "category": "reports",
            "provenance": [{"form_type": "6-K"}],
        }
        self.assertNotIn("INTERIM_FINANCIAL_REPORT", source_roles(generic))

    def test_coverage_checkpoint_is_independent_of_raw_news_volume(self):
        rows = [
            {"title": "Annual report", "date": "2026-02-01", "source_url": "https://ir.test/annual.pdf", "category": "reports", "status": "DOWNLOADED"},
            {"title": "Interim results", "date": "2026-08-01", "source_url": "https://ir.test/interim.pdf", "category": "reports", "status": "DOWNLOADED"},
            {"title": "Governance and leadership report", "date": "2026-04-01", "source_url": "https://ir.test/governance.pdf", "category": "reports", "status": "DOWNLOADED"},
        ] + [
            {"title": f"News {i}", "date": "2026-09-01", "source_url": f"https://ir.test/news/{i}", "category": "news", "status": "DISCOVERED"}
            for i in range(500)
        ]
        coverage = critical_source_coverage(rows, as_of_date="2026-09-25")
        self.assertTrue(coverage["financial"])
        self.assertTrue(coverage["governance"])
        self.assertTrue(coverage["minimum_research_corpus_acquired"])

    def test_future_critical_documents_do_not_establish_pit_coverage(self):
        rows = [
            {"title": "2026 Annual Report", "date": "2026-02-01", "source_url": "https://ir.test/annual.pdf", "category": "reports", "status": "DOWNLOADED"},
            {"title": "2026 Interim Results", "date": "2026-08-01", "source_url": "https://ir.test/interim.pdf", "category": "reports", "status": "DOWNLOADED"},
            {"title": "2026 Governance", "date": "2026-10-01", "source_url": "https://ir.test/governance.pdf", "category": "reports", "status": "DOWNLOADED"},
        ]
        coverage = critical_source_coverage(rows, as_of_date="2026-09-25")
        self.assertFalse(coverage["governance"])
        self.assertFalse(coverage["minimum_research_corpus_acquired"])

    def test_stage_budgets_keep_news_below_reserved_critical_budget(self):
        config = Config()
        self.assertGreater(config.critical_financial_budget, config.news_budget)
        self.assertGreater(config.governance_budget, config.deep_archive_budget)

    def test_archive_low_yield_stop_is_deterministic(self):
        self.assertFalse(archive_low_yield_stop(2, 3))
        self.assertTrue(archive_low_yield_stop(3, 3))
        self.assertTrue(archive_low_yield_stop(1, 0))


if __name__ == "__main__":
    unittest.main()
