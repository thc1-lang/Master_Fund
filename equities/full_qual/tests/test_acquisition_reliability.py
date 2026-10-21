import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipeline_runner.runner import _run_module
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.reliability import (
    AcquisitionDeadlineExceeded,
    AcquisitionProgressTimeout,
    ProgressTracker,
)
from qualitative_ir_downloader.models import IREcosystem, Stock
from qualitative_ir_downloader.official_sources import OfficialRoute, OfficialSourceStrategy, SOURCE_NEWSROOM
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.webcast_media import MediaAcquirer
from qualitative_ir_downloader.models import CollectionError


class AcquisitionReliabilityTests(unittest.TestCase):
    def test_progress_watchdog_ignores_log_only_wait(self):
        with tempfile.TemporaryDirectory() as temp:
            tracker = ProgressTracker(Path(temp), "WMT", no_progress_timeout=0.01, deadline=10)
            time.sleep(0.03)
            with self.assertRaises(AcquisitionProgressTimeout) as raised:
                tracker.check()
            self.assertEqual(raised.exception.code, "ACQUISITION_PROGRESS_TIMEOUT")

    def test_global_deadline_is_distinct_from_no_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            tracker = ProgressTracker(Path(temp), "WMT", no_progress_timeout=10, deadline=0.01)
            time.sleep(0.03)
            with self.assertRaises(AcquisitionDeadlineExceeded) as raised:
                tracker.check()
            self.assertEqual(raised.exception.code, "ACQUISITION_DEADLINE_EXCEEDED")

    def test_checkpoint_is_atomic_and_resume_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            tracker = ProgressTracker(folder, "WMT", no_progress_timeout=60, deadline=60)
            tracker.start("REPORT_DOWNLOAD", item="report-1")
            tracker.progress("REPORT_DOWNLOAD", item="report-1", processed=1, saved=1)
            payload = json.loads((folder / "acquisition_progress.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "RUNNING")
            self.assertEqual(payload["counters"]["saved"], 1)
            self.assertEqual(payload["current_substage"], "REPORT_DOWNLOAD")

    def test_large_stdout_and_stderr_do_not_deadlock(self):
        result = _run_module("tests.pipe_writer", [], timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertGreater(len(result.stdout), 200_000)
        self.assertGreater(len(result.stderr), 200_000)

    def test_official_route_budget_is_finite(self):
        async def run():
            browser = SimpleNamespace(
                config=Config(max_official_routes=2), driver=None,
                access_events=[], accessible_hosts=set(), blocked_hosts=set(),
            )
            context = SimpleNamespace()
            strategy = OfficialSourceStrategy(
                browser, context, Stock("WMT", "Walmart Inc.", "manual", 0),
                IREcosystem(), None,
            )
            strategy.routes = lambda: [
                OfficialRoute(f"https://ir.example.test/{i}", SOURCE_NEWSROOM)
                for i in range(10)
            ]
            strategy._observe_route = AsyncMock()
            await strategy.discover()
            self.assertEqual(strategy._observe_route.await_count, 2)

        asyncio.run(run())

    def test_media_operation_has_wall_clock_bound(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                acquirer = MediaAcquirer(SimpleNamespace(cookies=AsyncMock(return_value=[])),
                                         EventConfig(media_timeout=0.01), Path(temp))
                async def hung(*args, **kwargs):
                    await asyncio.sleep(1)
                acquirer._fetch_impl = hung
                with self.assertRaises(CollectionError) as raised:
                    await acquirer.fetch("https://media.example.test/a", Path(temp) / "a")
                self.assertEqual(raised.exception.code, "MEDIA_TIMEOUT")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
