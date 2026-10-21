from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from management_intelligence.board_governance_models import GovernanceCoverage
from management_intelligence.governance_store import GovernanceStore
from scoring_engine.factor_registry import load_profile_definition
from scoring_engine.governance_scoring import score_governance
from scoring_engine.sources import EvidenceSources
from pipeline_runner.models import StageStatus
from pipeline_runner.runner import PipelineRunner
from types import SimpleNamespace


class GovernanceAtomicityTests(unittest.TestCase):
    def _coverage(self, ticker: str, suffix: str) -> GovernanceCoverage:
        return GovernanceCoverage(f"coverage:{ticker}:{suffix}", ticker, "2026-09-25", board_composition="COMPLETE")

    def test_transient_replace_retries_and_verifies(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = GovernanceStore(directory)
            destination = Path(directory) / "probe.jsonl"
            real_replace = __import__("os").replace
            calls = {"count": 0}

            def replace(source, target):
                calls["count"] += 1
                if calls["count"] < 3:
                    error = PermissionError("sharing violation")
                    error.winerror = 5
                    raise error
                return real_replace(source, target)

            with patch("management_intelligence.governance_store.os.replace", side_effect=replace):
                store._atomic_replace(destination, "{}\n", generation_id="test")
            self.assertEqual(destination.read_text(encoding="utf-8"), "{}\n")
            self.assertEqual(calls["count"], 3)

    def test_failed_generation_withholds_last_good_rows(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = GovernanceStore(directory)
            store.replace_ticker_transaction("MSFT", {"governance_coverage": [self._coverage("MSFT", "A")]}, run_id="generation-a")
            self.assertEqual(store.current_status("MSFT"), "ACTIVE")
            original = store._atomic_replace

            def fail_snapshots(path, content, **kwargs):
                if Path(path).name == "governance_snapshots.jsonl":
                    raise PermissionError("locked")
                return original(path, content, **kwargs)

            with patch.object(store, "_atomic_replace", side_effect=fail_snapshots):
                with self.assertRaises(PermissionError):
                    store.replace_ticker_transaction("MSFT", {"governance_coverage": [self._coverage("MSFT", "B")], "governance_snapshots": []}, run_id="generation-b")
            self.assertEqual(store.current_status("MSFT"), "FAILED")
            self.assertEqual(store.list("governance_coverage", "MSFT"), [])
            self.assertTrue((Path(directory) / "governance_generations" / "generation-a").exists())

    def test_failed_governance_status_makes_factors_unscored(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            store = GovernanceStore(root / "management_intelligence")
            store.replace_ticker_transaction("MSFT", {"governance_coverage": [self._coverage("MSFT", "A")]}, run_id="generation-a")
            store.mark_failed("MSFT", run_id="run-b", reason="locked")
            definitions = load_profile_definition("core_v2", "config/scoring").factors
            factors = score_governance("MSFT", "2026-09-25", "core_v2", definitions, EvidenceSources(root), "config/scoring")
            self.assertTrue(factors)
            self.assertTrue(all(item.status == "UNSCORED" for item in factors))
            self.assertTrue(all(item.coverage_reason == "UPSTREAM_GOVERNANCE_STAGE_FAILED" for item in factors))

    def test_runner_nonzero_governance_process_is_failed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory) / "artifacts"
            runner = PipelineRunner(
                "MSFT", "2026-09-25", artifacts_root=root,
                executor=lambda module, args: SimpleNamespace(returncode=9, stdout="", stderr="governance locked"),
            )
            result = runner._run_optional_fresh("governance_intelligence", "governance", [], {}, provenance={})
            self.assertEqual(result.status, StageStatus.FAILED)
            self.assertEqual(GovernanceStore(root / "management_intelligence").current_status("MSFT"), "FAILED")


if __name__ == "__main__":
    unittest.main()
