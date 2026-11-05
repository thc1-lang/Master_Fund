from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_runner.compatibility import (
    ArtifactCompatibilityStore,
    STAGE_CONTRACTS,
    _atomic_replace_with_retry,
    implementation_fingerprint_for,
)
from pipeline_runner.main import build_parser
from pipeline_runner.models import StageStatus
from pipeline_runner.runner import PipelineRunner


class SemanticInvalidationTests(unittest.TestCase):
    def test_atomic_replace_retries_permission_once_and_writes_complete_json(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            root_path = Path(root)
            source = root_path / "source.tmp"
            destination = root_path / "artifact_compatibility.json"
            source.write_text('{"stages": {"x": {"status": "ACTIVE"}}}\n', encoding="utf-8")
            real_replace = __import__("pipeline_runner.compatibility", fromlist=["os"]).os.replace
            calls = {"count": 0}

            def flaky_replace(src, dst):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise PermissionError(5, "access denied")
                return real_replace(src, dst)

            with patch("pipeline_runner.compatibility.os.replace", side_effect=flaky_replace), patch("pipeline_runner.compatibility.time.sleep") as pause:
                _atomic_replace_with_retry(source, destination)
            self.assertEqual(calls["count"], 2)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["stages"]["x"]["status"], "ACTIVE")
            self.assertFalse(source.exists())
            pause.assert_called_once()

    def test_atomic_replace_retries_three_failures_then_succeeds(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            source = Path(root) / "source.tmp"
            destination = Path(root) / "destination.json"
            source.write_text("{}", encoding="utf-8")
            real_replace = __import__("pipeline_runner.compatibility", fromlist=["os"]).os.replace
            calls = {"count": 0}

            def flaky_replace(src, dst):
                calls["count"] += 1
                if calls["count"] <= 3:
                    raise PermissionError(5, "sharing contention")
                return real_replace(src, dst)

            with patch("pipeline_runner.compatibility.os.replace", side_effect=flaky_replace), patch("pipeline_runner.compatibility.time.sleep") as pause:
                _atomic_replace_with_retry(source, destination)
            self.assertEqual(calls["count"], 4)
            self.assertEqual(pause.call_count, 3)
            self.assertTrue(destination.exists())

    def test_atomic_replace_persistent_permission_error_propagates_after_bound(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            source = Path(root) / "source.tmp"
            destination = Path(root) / "destination.json"
            source.write_text("{}", encoding="utf-8")
            with patch("pipeline_runner.compatibility.os.replace", side_effect=PermissionError(5, "locked")) as replace, patch("pipeline_runner.compatibility.time.sleep") as pause:
                with self.assertRaises(PermissionError):
                    _atomic_replace_with_retry(source, destination)
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(pause.call_count, 5)

    def test_atomic_replace_non_transient_error_is_not_retried(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            source = Path(root) / "source.tmp"
            destination = Path(root) / "destination.json"
            source.write_text("{}", encoding="utf-8")
            error = OSError("invalid path")
            with patch("pipeline_runner.compatibility.os.replace", side_effect=error) as replace, patch("pipeline_runner.compatibility.time.sleep") as pause:
                with self.assertRaisesRegex(OSError, "invalid path"):
                    _atomic_replace_with_retry(source, destination)
            self.assertEqual(replace.call_count, 1)
            pause.assert_not_called()

    def test_store_cleans_temporary_file_after_persistent_replace_failure(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            store = ArtifactCompatibilityStore(Path(root))
            with patch("pipeline_runner.compatibility.os.replace", side_effect=PermissionError(5, "locked")), patch("pipeline_runner.compatibility.time.sleep"):
                with self.assertRaises(PermissionError):
                    store.record("normalization", "MSFT")
            self.assertEqual(list(Path(root).glob("artifact-compatibility-*.tmp")), [])
            self.assertFalse(store.path.exists())

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
