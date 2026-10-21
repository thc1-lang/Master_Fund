from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline_runner.compatibility import (
    ArtifactCompatibilityStore,
    STAGE_CONTRACTS,
    implementation_fingerprint_for,
)
from pipeline_runner.main import build_parser
from pipeline_runner.models import StageStatus
from pipeline_runner.runner import PipelineRunner


class SemanticInvalidationTests(unittest.TestCase):
    def test_old_implementation_fingerprint_requires_rebuild(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            store.record("qualitative_extraction", "MSFT", implementation_fingerprint="old-fingerprint")
            decision = store.decide("qualitative_extraction", "MSFT", profile="core_v2")
            self.assertEqual(decision.action, "REBUILD_REQUIRED")
            self.assertEqual(decision.reason, "IMPLEMENTATION_FINGERPRINT_CHANGED")

    def test_unchanged_implementation_fingerprint_reuses(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            store.record("qualitative_extraction", "MSFT")
            decision = store.decide("qualitative_extraction", "MSFT", profile="core_v2")
            self.assertEqual(decision.action, "REUSE")
            self.assertEqual(decision.reason, "COMPATIBLE_METADATA")

    def test_changed_upstream_dependency_requires_downstream_rebuild(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            store.record("normalization", "MSFT")
            store.record("qualitative_extraction", "MSFT")
            store.record("normalization", "MSFT", implementation_fingerprint="changed-upstream")
            decision = store.decide("qualitative_extraction", "MSFT", profile="core_v2")
            self.assertEqual(decision.action, "REBUILD_REQUIRED")
            self.assertEqual(decision.reason, "UPSTREAM_DEPENDENCY_CHANGED")

    def test_source_cache_contract_is_reusable_after_analytical_change(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            store.record("acquisition", "MSFT", implementation_fingerprint=implementation_fingerprint_for("acquisition"))
            store.record("qualitative_extraction", "MSFT", implementation_fingerprint="old-analytical")
            acquisition = store.decide("acquisition", "MSFT", profile="core_v2")
            self.assertEqual(acquisition.action, "REUSE")
            self.assertNotEqual(acquisition.required_implementation_fingerprint, "old-analytical")

    def test_forced_rebuild_and_subsequent_reuse(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            store.record("governance_intelligence", "MSFT", implementation_fingerprint="old-governance")
            runner = PipelineRunner("MSFT", "2026-09-25", profile="core_v2", artifacts_root=root,
                                    rebuild_analysis=True)
            forced = runner._compatibility_for("governance_intelligence")
            self.assertEqual(forced.reason, "EXPLICIT_REBUILD_ANALYSIS")
            self.assertEqual(forced.action, "REBUILD_REQUIRED")
            store.record("governance_intelligence", "MSFT")
            after = store.decide("governance_intelligence", "MSFT", profile="core_v2")
            self.assertEqual(after.action, "REUSE")
            self.assertEqual(after.stored_implementation_fingerprint, implementation_fingerprint_for("governance_intelligence"))

    def test_existing_runner_reuses_artifact_after_rebuild(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            store.record("governance_intelligence", "MSFT", implementation_fingerprint="old-governance")
            first = PipelineRunner("MSFT", "2026-09-25", profile="core_v2", artifacts_root=root)
            decision = first._compatibility_for("governance_intelligence")
            first._stage("governance_intelligence", StageStatus.SUCCESS, outputs=[Path(root) / "governance"],
                         provenance={"compatibility": decision.to_dict()})
            second = PipelineRunner("MSFT", "2026-09-25", profile="core_v2", artifacts_root=root)
            self.assertEqual(second._compatibility_for("governance_intelligence").action, "REUSE")

    def test_compatibility_rows_store_required_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            row = store.record("state_synthesis", "MSFT", dependency_values={"input": "fixture"})
            for key in ("producer_version", "schema_version", "implementation_fingerprint", "dependency_fingerprints", "input_fingerprint", "created_at"):
                self.assertIn(key, row)
            self.assertEqual(row["producer_version"], STAGE_CONTRACTS["state_synthesis"].producer_version)

    def test_cli_exposes_rebuild_analysis(self):
        parsed = build_parser().parse_args(["--ticker", "MSFT", "--as-of-date", "2026-09-25", "--rebuild-analysis"])
        self.assertTrue(parsed.rebuild_analysis)

    def test_dossier_depends_on_all_material_analytical_stages(self):
        self.assertEqual(
            set(STAGE_CONTRACTS["dossier_generation"].dependencies),
            {
                "normalization", "qualitative_extraction", "temporal_comparison",
                "state_synthesis", "insider_intelligence", "management_intelligence",
                "management_track_record", "governance_intelligence", "scoring",
            },
        )


if __name__ == "__main__":
    unittest.main()
