from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline_runner.compatibility import (
    ArtifactCompatibilityStore,
    STAGE_SOURCE_FILES,
    canonical_json,
    configuration_fingerprint,
    implementation_fingerprint,
    implementation_fingerprint_for,
    implementation_fingerprint_inputs,
)
from pipeline_runner.runner import PipelineRunner


class FingerprintStabilityTests(unittest.TestCase):
    def test_same_code_fingerprint_is_stable_in_one_process(self):
        for stage in ("temporal_comparison", "state_synthesis"):
            self.assertEqual(implementation_fingerprint_for(stage), implementation_fingerprint_for(stage))

    def test_same_code_fingerprint_is_stable_across_processes(self):
        code = "from pipeline_runner.compatibility import implementation_fingerprint_for; print(implementation_fingerprint_for('temporal_comparison'))"
        first = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
        second = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
        self.assertEqual(first, second)

    def _copy_sources(self, root: Path, stage: str) -> Path:
        for relative in STAGE_SOURCE_FILES[stage]:
            source = Path(relative)
            target = root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return root

    def test_mtime_only_change_does_not_change_fingerprint(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = self._copy_sources(Path(directory), "temporal_comparison")
            before = implementation_fingerprint("temporal_comparison", root=root)
            source = root / "qualitative_analysis" / "temporal_comparison.py"
            source.touch()
            self.assertEqual(before, implementation_fingerprint("temporal_comparison", root=root))

    def test_source_content_change_changes_fingerprint(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = self._copy_sources(Path(directory), "temporal_comparison")
            before = implementation_fingerprint("temporal_comparison", root=root)
            source = root / "qualitative_analysis" / "temporal_comparison.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# semantic test mutation\n", encoding="utf-8")
            self.assertNotEqual(before, implementation_fingerprint("temporal_comparison", root=root))

    def test_generated_files_and_output_generation_do_not_enter_implementation_hash(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = self._copy_sources(Path(directory), "state_synthesis")
            before = implementation_fingerprint("state_synthesis", root=root)
            (root / "qualitative_analysis" / "__pycache__").mkdir()
            (root / "qualitative_analysis" / "__pycache__" / "state_synthesis.cpython-313.pyc").write_bytes(b"generated")
            (root / "qualitative_analysis" / "stage-output.jsonl").write_text("output\n", encoding="utf-8")
            (root / "run.log").write_text("runtime\n", encoding="utf-8")
            self.assertEqual(before, implementation_fingerprint("state_synthesis", root=root))
            paths = {row["path"] for row in implementation_fingerprint_inputs("state_synthesis", root=root)}
            self.assertNotIn("qualitative_analysis/stage-output.jsonl", paths)

    def test_canonical_configuration_key_order_is_irrelevant(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))
        self.assertEqual(configuration_fingerprint("state_synthesis", {"b": 2, "a": 1}),
                         configuration_fingerprint("state_synthesis", {"a": 1, "b": 2}))
        self.assertNotEqual(configuration_fingerprint("state_synthesis", {"mode": "actuals"}),
                            configuration_fingerprint("state_synthesis", {"mode": "guidance"}))

    def test_upstream_change_invalidates_dependent_stage(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = ArtifactCompatibilityStore(Path(directory))
            store.record("normalization", "JPM")
            store.record("qualitative_extraction", "JPM")
            store.record("normalization", "JPM", implementation_fingerprint="changed-upstream")
            decision = store.decide("qualitative_extraction", "JPM", profile="core_v2")
            self.assertEqual(decision.reason, "UPSTREAM_DEPENDENCY_CHANGED")
            self.assertEqual(store.decide("qualitative_extraction", "JPM", profile="core_v2").reason,
                             "UPSTREAM_DEPENDENCY_CHANGED")

    def test_rebuild_persists_new_fingerprint_and_next_run_reuses(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = ArtifactCompatibilityStore(Path(directory))
            store.record("temporal_comparison", "JPM", implementation_fingerprint="old")
            first = store.decide("temporal_comparison", "JPM", profile="core_v2")
            self.assertEqual(first.action, "REBUILD_REQUIRED")
            store.record("temporal_comparison", "JPM")
            second = store.decide("temporal_comparison", "JPM", profile="core_v2")
            self.assertEqual(second.action, "REUSE")
            self.assertEqual(second.stored_implementation_fingerprint, implementation_fingerprint_for("temporal_comparison"))
            row = store.get("temporal_comparison", "JPM")
            self.assertIn("configuration_fingerprint", row)
            self.assertIn("input_fingerprint", row)

    def test_unchanged_row_count_after_qualitative_rebuild_updates_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = ArtifactCompatibilityStore(Path(directory))
            store.record("temporal_comparison", "JPM", implementation_fingerprint="old")
            runner = PipelineRunner(
                "JPM", "2026-09-25", profile="core_v2", artifacts_root=directory,
                executor=lambda _module, _args: SimpleNamespace(returncode=0, stdout="", stderr=""),
            )
            decision = runner._compatibility_for("temporal_comparison")
            result = runner._run_qualitative(
                "temporal_comparison", ["--compare"], {"temporal_changes": 0},
                "temporal_changes", provenance={"compatibility": decision.to_dict()},
            )
            self.assertEqual(result.status.value, "SUCCESS")
            self.assertEqual(store.decide("temporal_comparison", "JPM", profile="core_v2").action, "REUSE")

    def test_diagnostic_reports_stored_current_and_differences(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = ArtifactCompatibilityStore(Path(directory))
            store.record("state_synthesis", "JPM", implementation_fingerprint="old")
            diagnostic = store.diagnose("state_synthesis", "JPM")
            self.assertEqual(diagnostic["stage"], "state_synthesis")
            self.assertEqual(diagnostic["match"], False)
            self.assertIn("implementation_fingerprint", diagnostic["differing_components"])


if __name__ == "__main__":
    unittest.main()
