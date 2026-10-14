import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "outputs" / "COT Reports.py"
SPEC = importlib.util.spec_from_file_location("cot_reports_summary_tests", SCRIPT)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def history(dates, flips):
    return pd.DataFrame({"Date": pd.to_datetime(dates), "Flip": flips})


def summary_sheet(width=4):
    return m.SheetInfo("Summary", 1, 1000, width)


class SummaryReconciliationTests(unittest.TestCase):
    def run_sync(self, values, histories, authoritative_complete=True):
        updates = []

        def collect(_service, _spreadsheet_id, requests, _dry_run):
            updates.extend(requests)
            return len(requests)

        with patch.object(m, "_batch_update_values", side_effect=collect), \
             patch.object(m, "_batch_clear_values", return_value=0), \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_format_data_rows", return_value=0):
            result = m.sync_summary(None, "id", summary_sheet(len(values[0])),
                                    values, histories, True,
                                    authoritative_complete=authoritative_complete)
        return result, updates

    def test_blank_existing_date_universe_backfills_every_2y_cell(self):
        dates = ["2026-01-06", "2026-01-13", "2026-01-20"]
        values = [["Date", "2Y"]] + [[m._date_serial(date), ""] for date in dates]
        result, updates = self.run_sync(values, {"2Y": history(dates, [0.1, 0.2, 0.3])})
        self.assertEqual(result.backfilled, 3)
        self.assertEqual(result.corrected, 0)
        self.assertEqual(result.cleared, 0)
        self.assertEqual(result.coverage["2Y"]["final_populated"], 3)
        self.assertTrue(any("B2:B4" in item["range"] for item in updates))

    def test_new_date_does_not_skip_historical_backfill(self):
        old_dates = ["2026-01-06", "2026-01-13"]
        all_dates = old_dates + ["2026-01-20"]
        values = [["Date", "2Y"]] + [[m._date_serial(date), ""] for date in old_dates]
        result, updates = self.run_sync(
            values, {"2Y": history(all_dates, [0.1, 0.2, 0.3])}
        )
        self.assertEqual(result.added, 1)
        self.assertEqual(result.backfilled, 3)
        self.assertEqual(updates[0]["range"], "'Summary'!A4")
        self.assertTrue(any("B2:B3" in item["range"] for item in updates))

    def test_corrections_and_guarded_stale_clear_are_classified(self):
        dates = ["2026-01-06", "2026-01-13", "2026-01-20"]
        values = [["Date", "2Y"]] + [
            [m._date_serial(date), value] for date, value in zip(dates, [0.1, 0.2, 0.7])
        ]
        result, _ = self.run_sync(
            values, {
                "2Y": history(dates[:2], [0.15, 0.25]),
                "5Y": history([dates[2]], [0.4]),
            }
        )
        self.assertEqual(result.corrected, 2)
        self.assertEqual(result.cleared, 1)
        self.assertEqual(result.coverage["2Y"]["coverage"], 100.0)

    def test_sparse_history_is_not_forward_filled_when_another_instrument_supplies_date(self):
        dates = ["2026-01-06", "2026-01-13", "2026-01-20"]
        values = [["Date", "2Y", "5Y"]] + [
            [m._date_serial(date), "", ""] for date in dates
        ]
        result, updates = self.run_sync(values, {
            "2Y": history([dates[0], dates[2]], [0.1, 0.3]),
            "5Y": history(dates, [0.2, 0.25, 0.35]),
        })
        self.assertEqual(result.backfilled, 5)
        self.assertTrue(any(item["range"].endswith("!B2") for item in updates))
        self.assertTrue(any(item["range"].endswith("!B4") for item in updates))

    def test_headers_are_name_mapped_and_new_instrument_is_reported(self):
        dates = ["2026-01-06"]
        values = [["Date", "5Y", "2Y"], [m._date_serial(dates[0]), "", ""]]
        result, _ = self.run_sync(values, {
            "2Y": history(dates, [0.1]),
            "NEW": history(dates, [0.2]),
        })
        self.assertEqual(result.coverage["2Y"]["final_populated"], 1)
        self.assertNotIn("NEW", result.coverage)
        self.assertEqual(result.backfilled, 1)

    def test_second_run_is_a_no_op_after_reconciliation(self):
        dates = ["2026-01-06", "2026-01-13"]
        histories = {"2Y": history(dates, [0.1, 0.2])}
        values = [["Date", "2Y"]] + [[m._date_serial(date), value]
                                      for date, value in zip(dates, [0.1, 0.2])]
        result, updates = self.run_sync(values, histories)
        self.assertFalse(result.changed)
        self.assertEqual(updates, [])

    def test_incomplete_authority_preserves_stale_managed_cells(self):
        dates = ["2026-01-06", "2026-01-13"]
        values = [["Date", "2Y"]] + [[m._date_serial(date), value]
                                      for date, value in zip(dates, [0.1, 0.2])]
        result, updates = self.run_sync(
            values, {"2Y": history([dates[0]], [0.15])}, authoritative_complete=False
        )
        self.assertEqual(result.cleared, 0)
        self.assertEqual(result.corrected, 1)
        self.assertEqual(result.removed, 0)
        self.assertTrue(updates)


if __name__ == "__main__":
    unittest.main()
