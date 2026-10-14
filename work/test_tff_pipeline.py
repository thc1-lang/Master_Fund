import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pandas as pd


SCRIPT = Path("outputs/COT Reports.py").resolve()
SPEC = importlib.util.spec_from_file_location("cot_tff_tests", SCRIPT)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def source_frame(rows, year=2026):
    return pd.DataFrame(rows, columns=[
        "Market_and_Exchange_Names", "CFTC_Contract_Market_Code",
        "Report_Date_as_MM_DD_YYYY", "Open_Interest_All",
        "Asset_Mgr_Positions_Long_All", "Asset_Mgr_Positions_Short_All",
        "Lev_Money_Positions_Long_All", "Lev_Money_Positions_Short_All",
    ])


def compact_dataset(rows, year=2026):
    return m.validate_tff_source_dataset(source_frame(rows, year), year, m.build_tff_futures_only_url(year))


class TFFSourceTests(unittest.TestCase):
    def test_url_is_tff_futures_only(self):
        self.assertEqual(
            m.build_tff_futures_only_url(2010),
            "https://www.cftc.gov/files/dea/history/fut_fin_txt_2010.zip",
        )
        self.assertIn("fut_fin_txt_2028.zip", m.build_tff_futures_only_url(2028))
        self.assertNotIn("fut_disagg", m.build_tff_futures_only_url(2028))
        self.assertEqual(m._report_stem(m.TFF_SECTION_LABEL), "fut_fin")
        with self.assertRaises(ValueError):
            m._report_stem("Traders in Financial Futures ; Futures-and-Options Combined")

    def test_validation_requires_tff_headers_and_compacts_eight_columns(self):
        rows = [(f"A{i}", "13874+", "01/06/2026", 100, 60, 20, 30, 10)
                for i in range(50)]
        dataset = compact_dataset(rows)
        self.assertEqual(list(dataset.dataframe.columns), m.TFF_COMPACT_RAW_HEADERS)
        self.assertEqual(dataset.source_column_count, 8)
        self.assertEqual(dataset.dataframe[m.RAW_DATE_HEADER].iloc[0], "2026-01-06")
        self.assertEqual(dataset.dataframe[m.RAW_CONTRACT_HEADER].iloc[0], "13874+")

    def test_missing_tff_participant_header_is_rejected(self):
        rows = source_frame([
            (f"A{i}", "001", "01/06/2026", 100, 60, 20, 30, 10)
            for i in range(50)
        ]).drop(columns=["Lev_Money_Positions_Short_All"])
        with self.assertRaisesRegex(RuntimeError, "Missing required TFF header"):
            m.validate_tff_source_dataset(rows, 2026, "fut_fin_txt_2026.zip")

    def test_2010_allows_partial_first_calendar_year(self):
        rows = [(f"A{i}", "001", "07/20/2010", 100, 60, 20, 30, 10)
                for i in range(50)]
        dataset = compact_dataset(rows, 2010)
        self.assertEqual(dataset.dates.min().date().isoformat(), "2010-07-20")


class TFFHistoryTests(unittest.TestCase):
    def setUp(self):
        self.dataset = compact_dataset([
            ("OLD NAME", "001602", "01/06/2026", 100, 60, 20, 30, 10),
            ("OLD NAME", "001602", "01/13/2026", 200, 80, 20, 50, 10),
        ] * 25)

    def test_code_symbols_and_g1_rules(self):
        self.assertEqual(m.parse_tff_contract_code_config("S&P", [["13874+"]]).code, "13874+")
        self.assertTrue(m.parse_tff_contract_code_config("S&P", [["13874+"]]).valid)
        self.assertTrue(m.parse_tff_contract_code_config("VIX", [["1170E1"]]).valid)
        self.assertFalse(m.parse_tff_contract_code_config("bad", [["market name"]]).valid)
        self.assertEqual(m.parse_tff_contract_code_config("blank", [[], ["ignored"]]).code, "")

    def test_asset_and_leveraged_flips_and_zero_open_interest(self):
        dataset = compact_dataset([
            ("A", "001602", "01/06/2026", 100, 60, 20, 50, 10),
            ("B", "002602", "01/06/2026", 0, 60, 20, 50, 10),
        ] * 25)
        result, _ = m.build_tff_code_history({2026: dataset}, "001602", "Test")
        self.assertAlmostEqual(result.iloc[0]["Asset Manager Flip"], 0.4)
        self.assertAlmostEqual(result.iloc[0]["Leveraged Fund Flip"], 0.4)
        zero, _ = m.build_tff_code_history({2026: dataset}, "002602", "Zero")
        self.assertTrue(pd.isna(zero.iloc[0]["Asset Manager Flip"]))
        self.assertTrue(pd.isna(zero.iloc[0]["Leveraged Fund Flip"]))

    def test_market_name_change_stays_one_code_series(self):
        old = compact_dataset([("OLD", "001602", "01/06/2026", 100, 60, 20, 30, 10)] * 50)
        new = compact_dataset([
            ("NEW", "001602", "01/06/2027", 150, 70, 20, 40, 10),
            ("NEW", "001602", "01/13/2027", 200, 80, 20, 50, 10),
        ] * 25, 2027)
        history, diagnostics = m.build_tff_code_history({2026: old}, "001602", "Test")
        self.assertEqual(len(history), 1)
        history2, diagnostics2 = m.build_tff_code_history({2026: old, 2027: new}, "001602", "Test")
        self.assertEqual(len(history2), 3)
        self.assertEqual(len(diagnostics2["name_infos"]), 2)

    def test_identical_duplicate_collapses_and_conflict_raises(self):
        duplicate = compact_dataset([("A", "001602", "01/06/2026", 100, 60, 20, 30, 10)] * 50)
        history, _ = m.build_tff_code_history({2026: duplicate}, "001602", "Test")
        self.assertEqual(len(history), 1)
        conflict_rows = [
            ("A", "001602", "01/06/2026", 100, 60, 20, 30, 10),
            ("B", "001602", "01/06/2026", 101, 60, 20, 30, 10),
        ] * 25
        conflicting = compact_dataset(conflict_rows)
        with self.assertRaisesRegex(RuntimeError, "conflicting TFF code/date"):
            m.build_tff_code_history({2026: conflicting}, "001602", "Test")


class TFFSyncTests(unittest.TestCase):
    def test_tff_raw_identical_second_run_is_noop(self):
        rows = [("A", "001602", date.strftime("%m/%d/%Y"), 100 + i, 60, 20, 30, 10)
                for i, date in enumerate(pd.date_range("2026-01-06", periods=50, freq="7D"))]
        dataset = compact_dataset(rows)
        values = [m.TFF_COMPACT_RAW_HEADERS] + dataset.dataframe.drop_duplicates(
            subset=[m.RAW_CONTRACT_HEADER, m.RAW_DATE_HEADER]
        ).values.tolist()
        with patch.object(m, "_batch_update_values") as update:
            result = m.sync_tff_raw_sheet(
                None, "id", m.SheetInfo("COT 2026", 1, 3, 8), dataset, True,
                existing_values=values,
            )
        update.assert_not_called()
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.unchanged, 50)

    def test_tff_raw_historical_correction_updates_only_changed_row(self):
        rows = [("A", "001602", date.strftime("%m/%d/%Y"), 100 + i, 60, 20, 30, 10)
                for i, date in enumerate(pd.date_range("2026-01-06", periods=50, freq="7D"))]
        dataset = compact_dataset(rows)
        existing = [m.TFF_COMPACT_RAW_HEADERS] + dataset.dataframe.drop_duplicates(
            subset=[m.RAW_CONTRACT_HEADER, m.RAW_DATE_HEADER]
        ).values.tolist()
        existing[1][3] = 999
        captured = []
        def collect(_service, _id, updates, _dry):
            captured.extend(updates)
            return 1
        with patch.object(m, "_batch_update_values", side_effect=collect), \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_resize_tff_raw_grid", return_value=0):
            result = m.sync_tff_raw_sheet(
                None, "id", m.SheetInfo("COT 2026", 1, 3, 8), dataset, False,
                existing_values=existing,
            )
        self.assertEqual(result.updated, 1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["range"], "'COT 2026'!D2:D2")

    def test_grid_projection_uses_eight_columns(self):
        dataset = compact_dataset([("A", "001602", "01/06/2026", 100, 60, 20, 30, 10)] * 50)
        sheets = {"COT 2026": m.SheetInfo("COT 2026", 1, 1000, 191)}
        current, projected = m._projected_tff_grid_cells(sheets, {2026: dataset})
        self.assertEqual(current, 191000)
        self.assertEqual(projected, 51 * 8)

    def test_legacy_prepared_layout_is_preserved_and_uses_asset_manager(self):
        dataset = compact_dataset([("A", "001602", "01/06/2026", 100, 60, 20, 30, 10)] * 50)
        history, _ = m.build_tff_code_history({2026: dataset}, "001602", "Test")
        values = [m.TFF_LEGACY_HEADERS]
        with patch.object(m, "_batch_update_values") as update, \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_format_tff_history_rows", return_value=0):
            result, final, layout = m.sync_tff_instrument_sheet(
                None, "id", m.SheetInfo("Test", 1, 1000, 26), values, history, True
            )
        update.assert_called_once()
        self.assertEqual(layout, "asset_legacy")
        self.assertAlmostEqual(final.iloc[0]["Asset Manager Flip"], 0.4)
        self.assertEqual(result.added, 1)

    def test_g1_configuration_column_is_never_written(self):
        dataset = compact_dataset([("A", "001602", "01/06/2026", 100, 60, 20, 30, 10)] * 50)
        history, _ = m.build_tff_code_history({2026: dataset}, "001602", "Test")
        values = [["Date", "Open Interest", "Asset Manager Long", "Asset Manager Short",
                   "Asset Manager Flip", "", "001602", "Leveraged Fund Short", "Leveraged Fund Flip"]]
        captured = []
        def collect(_service, _id, updates, _dry):
            captured.extend(updates)
            return 1
        with patch.object(m, "_batch_update_values", side_effect=collect), \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_format_tff_history_rows", return_value=0):
            m.sync_tff_instrument_sheet(
                None, "id", m.SheetInfo("Test", 1, 1000, 26), values, history, False
            )
        self.assertTrue(captured)
        self.assertTrue(all("!G" not in update["range"] and ":G" not in update["range"]
                            for update in captured))

    def test_duplicate_codes_are_workbook_local(self):
        first = {"A": m.parse_tff_contract_code_config("A", [["001602"]]),
                 "B": m.parse_tff_contract_code_config("B", [["001602"]])}
        invalid, collisions = m.contract_code_preflight(first)
        self.assertFalse(invalid)
        self.assertEqual(collisions, {"001602": ["A", "B"]})
        other_workbook = {"C": m.parse_tff_contract_code_config("C", [["001602"]])}
        self.assertEqual(m.contract_code_preflight(other_workbook)[1], {})


class TFFScopeTests(unittest.TestCase):
    def test_scope_flags_are_optional_and_mutually_exclusive(self):
        with patch.object(sys, "argv", ["COT Reports.py", "--dry-run", "--tff-only"]):
            args = m.parse_args()
        self.assertTrue(args.tff_only)
        self.assertFalse(args.commodities_only)
        with patch.object(sys, "argv", ["COT Reports.py", "--dry-run", "--tff-workbook", "2"]):
            args = m.parse_args()
        self.assertEqual(args.tff_workbook, 2)
        with patch.object(sys, "argv", ["COT Reports.py", "--dry-run", "--commodities-only"]):
            args = m.parse_args()
        self.assertTrue(args.commodities_only)


if __name__ == "__main__":
    unittest.main(verbosity=2)
