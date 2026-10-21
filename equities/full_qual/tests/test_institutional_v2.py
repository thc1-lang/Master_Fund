from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from empirical_validation.calibration import GoldStandardAnnotation, GoldStandardStore, calibration_summary
from management_intelligence.guidance_database import GuidanceDatabase, GuidanceRecord, evaluate_guidance, guidance_coverage_status
from qualitative_analysis.business_intelligence import BusinessModelEngine, IndustryAdapterRegistry, KPIDefinition, build_trajectory
from qualitative_analysis.entity_resolution import CanonicalPerson, EntityResolver, normalize_name
from qualitative_analysis.extraction_safety import looks_like_pdf_corruption, validate_extracted_text
from qualitative_analysis.numeric import MetricValue, NumericValidationError, compare_values, range_midpoint
from qualitative_analysis.point_in_time import assert_point_in_time, filter_as_of
from qualitative_analysis.reconciliation import SourceReconciler
from qualitative_analysis.schema import EvidenceAnchor, MetricRecord, PersonRole, SchemaValidationError, assert_valid
from qualitative_analysis.status import SourceStatus
from research_output.v2_models import quality_gate, quality_gate_from_records
from research_output.v2_models import InstitutionalDossier
from pipeline_runner.compatibility import ArtifactCompatibilityStore
from management_intelligence.governance_sources import CachedGovernanceSource
from management_intelligence.shareholder_rights import extract_shareholder_rights
from scoring_engine.eligibility import decide_score_eligibility, release_score


class InstitutionalV2Tests(unittest.TestCase):
    def test_core_v2_requires_explicit_compatible_metadata_and_reuses_it(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            unknown = store.decide("governance_intelligence", "NVDA", profile="core_v2")
            self.assertEqual(unknown.status, "UNKNOWN_VERSION")
            self.assertEqual(unknown.action, "REBUILD_REQUIRED")
            store.record("governance_intelligence", "NVDA", dependency_values={"source": "normalized-v2"})
            compatible = store.decide("governance_intelligence", "NVDA", profile="core_v2")
            self.assertEqual(compatible.status, "COMPATIBLE_REUSE")
            self.assertEqual(compatible.action, "REUSE")

    def test_dependency_change_requires_rebuild_and_quarantine_is_append_only(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            store.record("dossier_generation", "ABC", dependency_values={"fingerprint": "old"})
            changed = store.decide("dossier_generation", "ABC", profile="core_v2", dependency_values={"fingerprint": "new"})
            self.assertEqual(changed.reason, "DEPENDENCY_FINGERPRINT_CHANGED")
            self.assertEqual(changed.action, "REBUILD_REQUIRED")
            store.quarantine("dossier_generation", "ABC", reason=changed.reason)
            self.assertIn("CORRUPT", Path(root, "artifact_quarantine.jsonl").read_text(encoding="utf-8"))

    def test_raw_pdf_governance_source_is_rejected_at_parser_boundary(self):
        source = CachedGovernanceSource("ABC", "0001", "DEF 14A", "2026-01-01", None, "https://sec.example", "proxy.pdf", "%PDF-1.7\n1 0 obj")
        rights = extract_shareholder_rights("ABC", source)
        self.assertEqual(rights.extraction_status, "EXTRACTION_FAILED")
        self.assertEqual(rights.source_status, "EXTRACTION_FAILED")
        self.assertEqual(rights.evidence_text, "")

    def test_v2_dossier_declares_versions_and_withholds_non_ready_score(self):
        dossier = InstitutionalDossier("d:1", "ABC", "Example", "2026-09-25", "2026-09-25")
        payload = dossier.to_dict()
        self.assertEqual(payload["research_output_version"], "research-output-v2")
        self.assertIn("governance_version", payload)
        self.assertIn("management_track_record_version", payload)
        self.assertIn("insider_version", payload)
        self.assertFalse(payload["decision_grade"])
    def test_schema_rejects_numeric_role_and_raw_pdf(self):
        anchor = EvidenceAnchor("src:1", "doc:1", exact_text="Alex Smith")
        with self.assertRaises(SchemaValidationError):
            assert_valid(PersonRole("p:1", "alex smith", "66", "executive", "Example", "src:1", anchor))
        with self.assertRaises(ValueError):
            validate_extracted_text("%PDF-1.7\n1 0 obj")
        self.assertTrue(looks_like_pdf_corruption("xref\nendstream\nendobj"))

    def test_metric_percent_and_units_are_safe(self):
        self.assertEqual(range_midpoint(10, 20), 15)
        self.assertEqual(MetricValue("revenue", 2, "USD", scale="billion").normalized_value(), 2_000_000_000)
        with self.assertRaises(NumericValidationError):
            MetricValue("revenue", 2, "USD", scale="mystery").normalized_value()
        result = compare_values(MetricValue("margin", 20, "%"), MetricValue("margin", 25, "%"))
        self.assertEqual(result["status"], "COMPARABLE")
        self.assertEqual(result["percentage_change"], 25.0)

    def test_entity_resolution_merges_aliases_but_refuses_similar_names(self):
        self.assertEqual(normalize_name("DOE, Jane Q."), "jane q doe")
        resolver = EntityResolver()
        resolver.add_person(CanonicalPerson("person:1", "Jane Doe", aliases=["Jane Q. Doe"], issuer_ids=["issuer:1"]))
        self.assertEqual(resolver.resolve_person("Doe, Jane Q.", issuer_id="issuer:1").person_id, "person:1")
        resolver.add_person(CanonicalPerson("person:2", "John Doe", issuer_ids=["issuer:1"]))
        self.assertTrue(resolver.resolve_person("J. Doe", issuer_id="issuer:1").ambiguous or resolver.resolve_person("J. Doe", issuer_id="issuer:1").person_id is None)

    def test_reconciliation_retains_equal_authority_conflict(self):
        result = SourceReconciler().reconcile([
            {"ticker": "ABC", "metric": "revenue", "period": "Q1", "value": 10, "source_authority": "issuer_official", "source_id": "a"},
            {"ticker": "ABC", "metric": "revenue", "period": "Q1", "value": 11, "source_authority": "issuer_official", "source_id": "b"},
        ])
        self.assertEqual(result.status, SourceStatus.CONFLICTED.value)
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(len(result.retained), 2)

    def test_point_in_time_excludes_future_and_unknown_by_default(self):
        passed, future, unknown = filter_as_of([
            {"source_id": "past", "publication_date": "2026-09-24"},
            {"source_id": "future", "publication_date": "2026-09-26"},
            {"source_id": "unknown"},
        ], "2026-09-25")
        self.assertEqual([row["source_id"] for row in passed], ["past"])
        self.assertEqual([row["source_id"] for row in future], ["future"])
        self.assertEqual([row["source_id"] for row in unknown], ["unknown"])
        with self.assertRaises(ValueError):
            assert_point_in_time([{"source_id": "unknown"}], "2026-09-25")
        assert_point_in_time([{"source_id": "unknown"}], "2026-09-25", allow_unknown=True)

    def test_guidance_lifecycle_does_not_resolve_before_public_actual(self):
        record = GuidanceRecord("g:1", "Example", "ABC", "2026-09-24", "2026-09-24", "revenue", "FY2026", low=100, high=120, source_id="src:1")
        actual = evaluate_guidance(record, 130, actual_period="FY2026", actual_available_date="2026-09-26", source_id="src:2")
        self.assertEqual(actual.result, "BEAT")
        with self.assertRaises(ValueError):
            evaluate_guidance(record, 130, actual_period="FY2026", actual_available_date="2026-09-23")
        self.assertEqual(guidance_coverage_status(document_count=5, claim_count=0, guidance_claim_count=0, captured_count=0), "GUIDANCE_DOCUMENTS_PRESENT_BUT_NOT_EXTRACTED")
        with tempfile.TemporaryDirectory() as root:
            db = GuidanceDatabase(Path(root) / "guidance")
            self.assertEqual(db.upsert(record), "added")
            self.assertEqual(db.resolve(record, actual), "BEAT")
            self.assertEqual(db.outcomes("ABC", as_of_date="2026-09-25"), [])

    def test_quality_gate_blocks_leakage_and_keeps_missing_neutral(self):
        blocked = quality_gate_from_records(documents=[{"document_id": "d", "publication_date": "2026-09-26"}], claims=[], cutoff="2026-09-25")
        self.assertEqual(blocked.status, "NOT_RESEARCH_READY")
        self.assertIn("POINT_IN_TIME_LEAKAGE", blocked.blockers)
        limited = quality_gate(documents=5, material_claims=0, score_eligible=False)
        self.assertEqual(limited.status, "RESEARCH_READY_WITH_LIMITATIONS")
        self.assertIn("DOCUMENTS_PRESENT_BUT_EXTRACTION_COVERAGE_INSUFFICIENT", limited.warnings)
        contaminated = quality_gate_from_records(
            documents=[{"document_id": "d", "publication_date": "2026-09-24"}],
            additional_records=[{"rights_id": "governance-rights:ABC", "evidence_text": "%PDF-1.7\n1 0 obj"}],
            cutoff="2026-09-25",
        )
        self.assertEqual(contaminated.status, "NOT_RESEARCH_READY")
        self.assertIn("RAW_PDF_EVIDENCE", contaminated.blockers)

    def test_score_eligibility_withholds_partial_calculation(self):
        decision = decide_score_eligibility(calculated_score=70, evidence_coverage=.8, scoring_coverage=.2, minimum_coverage=.5)
        self.assertFalse(decision.score_eligible)
        self.assertIsNone(decision.released_score)
        released = release_score({"calculated_score": 70, "overall_scoring_coverage": .8, "overall_evidence_coverage": .9}, minimum_coverage=.5)
        self.assertEqual(released["released_score"], 70)
        self.assertEqual(released["score_status"], "RELEASED")

    def test_business_adapter_and_trajectory_are_company_generic(self):
        registry = IndustryAdapterRegistry()
        class Adapter:
            name = "services"
            def kpis(self):
                return [KPIDefinition("retention", "customer retention", "%")]
        registry.register(Adapter())
        self.assertEqual(registry.get("services").kpis()[0].name, "retention")
        rows = [{"claim_id": "c:1", "claim_text": "Recurring revenue and customer retention improved", "evidence_text": "Recurring revenue and customer retention improved"}]
        self.assertTrue(BusinessModelEngine().analyze(rows))
        self.assertEqual(build_trajectory("revenue", level="HIGH", rate="GROWING").qualitative_transition, "SUPPORTED")

    def test_gold_standard_store_remains_unestablished_until_threshold(self):
        annotation = GoldStandardAnnotation("a:1", "claim:1", "ABC", "CORRECT_EXTRACTION", "YES", reviewer="analyst", source_id="src:1")
        with tempfile.TemporaryDirectory() as root:
            store = GoldStandardStore(Path(root) / "gold")
            self.assertEqual(store.upsert(annotation), "added")
            self.assertEqual(store.upsert(annotation), "unchanged")
            summary = calibration_summary(store.list_all())
        self.assertEqual(summary.status, "NOT_YET_ESTABLISHED")
        self.assertIn("MINIMUM_ANNOTATIONS_NOT_MET", summary.reasons)


if __name__ == "__main__":
    unittest.main()
