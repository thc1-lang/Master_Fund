import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pandas as pd


SCRIPT = Path("outputs/COT Reports.py").resolve()
SPEC = importlib.util.spec_from_file_location("cot_historical_sync", SCRIPT)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def source_frame(rows, year=2026):
    frame = pd.DataFrame(rows, columns=[
        "Market_and_Exchange_Names",
        "Report_Date_as_YYYY-MM-DD",
        "CFTC_Contract_Market_Code",
        "Open_Interest_All",
        "M_Money_Positions_Long_All",
        "M_Money_Positions_Short_All",
    ])
    frame = frame[m.COMPACT_RAW_HEADERS]
    frame[m.RAW_CONTRACT_HEADER] = frame[m.RAW_CONTRACT_HEADER].map(
        m.normalize_cftc_contract_code
    )
    return m.YearDataset(
        year=year,
        url="https://example.test/source.zip",
        dataframe=frame,
        market_col="Market_and_Exchange_Names",
        date_col="Report_Date_as_YYYY-MM-DD",
        contract_col="CFTC_Contract_Market_Code",
        open_interest_col="Open_Interest_All",
        manager_long_col="M_Money_Positions_Long_All",
        manager_short_col="M_Money_Positions_Short_All",
        dates=pd.to_datetime(frame["Report_Date_as_YYYY-MM-DD"]),
    )


def history(rows):
    result = pd.DataFrame(rows, columns=m.COMMODITY_HEADERS)
    if not result.empty:
        result["Date"] = pd.to_datetime(result["Date"])
    return result


def sheet_info(name="Test"):
    return m.SheetInfo(name, 1, 5000, 250)


def raw_sheet_info(rows=51, columns=6):
    return m.SheetInfo("COT 2026", 1, rows, columns)


class CommodityExtractionTests(unittest.TestCase):
    def test_contract_code_match_order_and_flip(self):
        dataset = source_frame([
            ("NEW NAME", "2026-01-13", "067651", "200", "80", "20"),
            ("OLD NAME", "2026-01-06", "067651", "100", "60", "20"),
            ("NEW NAME", "2026-01-06", "067652", "999", "999", "0"),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            result = m.extract_commodity_history({2026: dataset}, "067651", "Test")
        self.assertEqual(result["Date"].dt.strftime("%Y-%m-%d").tolist(),
                         ["2026-01-06", "2026-01-13"])
        self.assertEqual(result["Open Interest"].tolist(), [100, 200])
        self.assertAlmostEqual(result.iloc[0]["Flip"], 0.4)

    def test_zero_open_interest_has_blank_flip(self):
        dataset = source_frame([("TARGET", "2026-01-06", "067651", "0", "5", "2")])
        result = m.extract_commodity_history({2026: dataset}, "067651", "Test")
        self.assertIsNone(result.iloc[0]["Flip"])

    def test_conflicting_same_code_date_is_blocked(self):
        dataset = source_frame([
            ("OLD NAME", "2026-01-06", "067651", "100", "60", "20"),
            ("NEW NAME", "2026-01-06", "067651", "200", "100", "40"),
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                m.extract_commodity_history({2026: dataset}, "067651", "Test")

    def test_historical_names_combine_by_code(self):
        old = source_frame([
            ("CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
             "2010-01-05", "067651", "100", "60", "20"),
        ], year=2010)
        new = source_frame([
            ("WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE",
             "2026-01-06", "067651", "200", "80", "20"),
        ], year=2026)
        history_frame, diagnostics = m.build_contract_code_history(
            {2010: old, 2026: new}, "067651", "WTI Crude"
        )
        self.assertEqual(len(history_frame), 2)
        self.assertEqual(len(diagnostics["name_infos"]), 2)
        self.assertEqual(diagnostics["years"], {2010, 2026})

    def test_wheat_rename_and_similar_name_different_code(self):
        old = source_frame([
            ("WHEAT - CHICAGO BOARD OF TRADE", "2010-01-05", "001602", "100", "60", "20"),
            ("WHEAT - CHICAGO BOARD OF TRADE", "2010-01-05", "001603", "999", "999", "0"),
        ], year=2010)
        new = source_frame([
            ("WHEAT-SRW - CHICAGO BOARD OF TRADE", "2026-01-06", "001602", "200", "80", "20"),
        ], year=2026)
        result, diagnostics = m.build_contract_code_history(
            {2010: old, 2026: new}, "001602", "Chicago SRW Wheat"
        )
        self.assertEqual(result["Open Interest"].tolist(), [100, 200])
        self.assertEqual(len(diagnostics["name_infos"]), 2)


class ContractCodeTests(unittest.TestCase):
    def test_code_normalisation_preserves_identifiers(self):
        cases = {
            "067651": "067651",
            67651: "067651",
            67651.0: "067651",
            "67651.0": "067651",
            "001602": "001602",
            1602: "001602",
            37021: "037021",
            "06765T": "06765T",
            "06665O": "06665O",
            "06765t": "06765T",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(m.normalize_cftc_contract_code(source), expected)

    def test_blank_invalid_and_valid_g1(self):
        blank = m.parse_contract_code_config("Blank", [])
        invalid = m.parse_contract_code_config("Invalid", [["GOLD - EXCHANGE"]])
        valid = m.parse_contract_code_config("WTI", [[67651]])
        ignores_g2 = m.parse_contract_code_config("WTI", [["067651"], ["088691"]])
        self.assertEqual(blank.code, "")
        self.assertTrue(blank.valid)
        self.assertFalse(invalid.valid)
        self.assertEqual(valid.code, "067651")
        self.assertTrue(valid.valid)
        self.assertEqual(ignores_g2.code, "067651")

    def test_duplicate_codes_across_commodities(self):
        configs = {
            "Gold": m.parse_contract_code_config("Gold", [["088691"]]),
            "Silver": m.parse_contract_code_config("Silver", [[88691]]),
            "WTI": m.parse_contract_code_config("WTI", [["067651"]]),
        }
        invalid, collisions = m.contract_code_preflight(configs)
        self.assertFalse(invalid)
        self.assertEqual(collisions, {"088691": ["Gold", "Silver"]})

    def test_zero_historical_matches(self):
        dataset = source_frame([
            ("TARGET", "2026-01-06", "067651", "100", "60", "20"),
        ])
        result, diagnostics = m.build_contract_code_history(
            {2026: dataset}, "999999", "Missing"
        )
        self.assertTrue(result.empty)
        self.assertEqual(diagnostics["rows_found"], 0)

    def test_contract_column_is_located_by_header_not_position(self):
        dataset = source_frame([
            ("RENAMED MARKET", "2026-01-06", "067651", "100", "60", "20"),
        ])
        order = [
            m.RAW_DATE_HEADER,
            m.RAW_OPEN_INTEREST_HEADER,
            m.RAW_MARKET_HEADER,
            m.RAW_MANAGER_SHORT_HEADER,
            m.RAW_MANAGER_LONG_HEADER,
            m.RAW_CONTRACT_HEADER,
        ]
        dataset.dataframe = dataset.dataframe[order]
        dataset.market_col = m.RAW_MARKET_HEADER
        dataset.date_col = m.RAW_DATE_HEADER
        dataset.contract_col = m.RAW_CONTRACT_HEADER
        result, _ = m.build_contract_code_history(
            {2026: dataset}, "067651", "WTI"
        )
        self.assertEqual(len(result), 1)

    def test_missing_year_preserves_existing_history(self):
        target = history([("2026-01-06", 100, 60, 20, 0.4)])
        existing = history([
            ("2025-12-30", 90, 50, 20, 1 / 3),
            ("2026-01-06", 100, 60, 20, 0.4),
        ])
        merged = m._merge_unavailable_years(target, existing, {2026}, 2025, 2026)
        self.assertEqual(merged["Date"].dt.year.tolist(), [2025, 2026])


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.base = history([
            ("2026-01-06", 100, 60, 20, 0.4),
            ("2026-01-13", 200, 80, 20, 0.3),
        ])

    def test_identical_second_run_has_zero_diff(self):
        diff = m.compare_histories(self.base, self.base.copy())
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["updated"], [])
        self.assertEqual(diff["removed"], [])
        self.assertEqual(len(diff["unchanged"]), len(self.base))

    def test_one_new_week_only_adds_that_date(self):
        target = pd.concat([
            self.base,
            history([("2026-01-20", 300, 120, 30, 0.3)]),
        ], ignore_index=True)
        diff = m.compare_histories(self.base, target)
        self.assertEqual(diff["added"], [pd.Timestamp("2026-01-20")])
        self.assertEqual(diff["updated"], [])

    def test_one_historical_correction_only_updates_that_date(self):
        target = self.base.copy()
        target.loc[0, "Open Interest"] = 110
        target.loc[0, "Flip"] = (60 - 20) / 110
        diff = m.compare_histories(self.base, target)
        self.assertEqual(diff["updated"], [pd.Timestamp("2026-01-06")])
        self.assertEqual(diff["added"], [])

    def test_flip_tolerance_ignores_binary_noise(self):
        target = self.base.copy()
        target.loc[0, "Flip"] += 1e-13
        self.assertEqual(m.compare_histories(self.base, target)["updated"], [])


class CommoditySyncTests(unittest.TestCase):
    def setUp(self):
        self.target = history([
            ("2026-01-06", 100, 60, 20, 0.4),
            ("2026-01-13", 200, 80, 20, 0.3),
        ])
        self.values = [m.COMMODITY_HEADERS,
                       [m._date_serial("2026-01-06"), 100, 60, 20, 0.4],
                       [m._date_serial("2026-01-13"), 200, 80, 20, 0.3]]

    def test_identical_sheet_makes_no_write_or_clear(self):
        with patch.object(m, "_batch_update_values") as update, \
             patch.object(m, "_batch_clear_values") as clear:
            result, _ = m.sync_commodity_sheet(
                None, "id", sheet_info(), self.values, self.target, False, True
            )
        update.assert_not_called()
        clear.assert_not_called()
        self.assertFalse(result.changed)

    def test_new_week_only_writes_suffix(self):
        target = pd.concat([
            self.target,
            history([("2026-01-20", 300, 120, 30, 0.3)]),
        ], ignore_index=True)
        captured = []

        def collect(_service, _id, updates, _dry):
            captured.extend(updates)
            return 1

        with patch.object(m, "_batch_update_values", side_effect=collect), \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_format_data_rows", return_value=0):
            result, _ = m.sync_commodity_sheet(
                None, "id", sheet_info(), self.values, target, False, False
            )
        self.assertEqual(result.added, 1)
        self.assertEqual(captured[0]["range"], "'Test'!A4")
        self.assertEqual(len(captured[0]["values"]), 1)

    def test_missing_g1_preserves_existing_data(self):
        result, final = m.sync_commodity_sheet(
            None, "id", sheet_info(), self.values,
            pd.DataFrame(columns=m.COMMODITY_HEADERS), True, True,
        )
        self.assertTrue(result.skipped)
        self.assertEqual(len(final), 2)

    def test_large_historical_removal_is_guarded(self):
        dates = pd.date_range("2026-01-06", periods=10, freq="7D")
        existing = history([(date, 100, 60, 20, 0.4) for date in dates])
        values = [m.COMMODITY_HEADERS] + m._history_values(existing)
        target = existing.iloc[5:].reset_index(drop=True)
        result, final = m.sync_commodity_sheet(
            None, "id", sheet_info(), values, target, False, True
        )
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.removal_candidates_preserved, 5)
        self.assertEqual(len(final), 10)


class RawSyncTests(unittest.TestCase):
    def setUp(self):
        rows = [("TARGET", f"2026-01-{day:02d}", "001", str(100 + day), "60", "20")
                for day in range(1, 29)]
        rows += [("OTHER", f"2026-02-{day:02d}", "002", str(200 + day), "80", "30")
                 for day in range(1, 23)]
        self.dataset = source_frame(rows)
        self.headers = list(self.dataset.dataframe.columns)
        self.values = [self.headers] + self.dataset.dataframe.values.tolist()

    def test_raw_identical_second_run_is_noop(self):
        with patch.object(m, "_get_values", return_value=self.values), \
             patch.object(m, "_batch_update_values") as update:
            result = m.sync_raw_cot_sheet(
                None, "id", raw_sheet_info(), self.dataset, True
            )
        update.assert_not_called()
        self.assertEqual(result.unchanged, 50)

    def test_raw_one_correction_only_updates_one_record(self):
        existing = [row[:] for row in self.values]
        existing[1][3] = "999"
        captured = []

        def collect(_service, _id, updates, _dry):
            captured.extend(updates)
            return 1

        with patch.object(m, "_get_values", return_value=existing), \
             patch.object(m, "_batch_update_values", side_effect=collect), \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_resize_compact_raw_grid", return_value=0), \
             patch.object(m, "_format_raw_code_column_as_text", return_value=0):
            result = m.sync_raw_cot_sheet(
                None, "id", raw_sheet_info(), self.dataset, False
            )
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.added, 0)
        self.assertEqual(len(captured), 1)

    def test_numeric_stored_code_is_repaired_to_zero_padded_text(self):
        existing = [row[:] for row in self.values]
        existing[1][1] = 1
        captured = []

        def collect(_service, _id, updates, _dry):
            captured.extend(updates)
            return 1

        with patch.object(m, "_get_values", return_value=existing), \
             patch.object(m, "_batch_update_values", side_effect=collect), \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_resize_compact_raw_grid", return_value=0), \
             patch.object(m, "_format_raw_code_column_as_text", return_value=0):
            result = m.sync_raw_cot_sheet(
                None, "id", raw_sheet_info(), self.dataset, False
            )
        self.assertEqual(result.updated, 1)
        code_updates = [update for update in captured if update["range"] == "'COT 2026'!B2:B2"]
        self.assertEqual(code_updates[0]["values"], [["000001"]])

    def test_partial_source_cannot_delete_valid_existing(self):
        source_records = {("A", f"2026-01-{i:02d}", "1"): (i, [], ())
                          for i in range(1, 11)}
        existing_records = {("A", f"2026-01-{i:02d}", "1"): (i, [], ())
                            for i in range(1, 21)}
        safe, reason = m._source_comparison_safe(source_records, existing_records, 2026)
        self.assertFalse(safe)
        self.assertIn("source has", reason)

    def test_validation_rejects_small_broken_source(self):
        broken = self.dataset.dataframe.iloc[:10]
        with self.assertRaises(RuntimeError):
            m.validate_source_dataset(broken, 2026, "broken.zip")

    def test_current_year_accepts_first_single_report_week(self):
        current = m.get_current_year()
        rows = [(f"MARKET {index}", f"{current}-01-06", f"{index:03d}",
                 "100", "60", "20") for index in range(50)]
        dataset = source_frame(rows, year=current)
        validated = m.validate_source_dataset(dataset.dataframe, current, "first-week.zip")
        self.assertEqual(validated.dates.nunique(), 1)

    def test_validation_compacts_wide_legacy_date_header_to_six_columns(self):
        rows = []
        for index in range(50):
            rows.append({
                "Market_and_Exchange_Names": f"MARKET {index}",
                "CFTC_Contract_Market_Code": f"{index:03d}",
                "Report_Date_as_MM_DD_YYYY": "01/06/2025" if index < 25 else "01/13/2025",
                "Open_Interest_All": "100",
                "M_Money_Positions_Long_All": "60",
                "M_Money_Positions_Short_All": "20",
                **{f"Unused_{extra}": "x" for extra in range(185)},
            })
        validated = m.validate_source_dataset(pd.DataFrame(rows), 2025, "wide.zip")
        self.assertEqual(list(validated.dataframe.columns), m.COMPACT_RAW_HEADERS)
        self.assertEqual(validated.source_column_count, 191)
        self.assertEqual(set(validated.dataframe[m.RAW_DATE_HEADER]),
                         {"2025-01-06", "2025-01-13"})
        self.assertEqual(set(validated.dataframe[m.RAW_CONTRACT_HEADER]),
                         {f"{index:06d}" for index in range(50)})

    def test_stable_key_prefers_contract_and_falls_back_to_market(self):
        headers = m.COMPACT_RAW_HEADERS
        records, _ = m._keyed_rows(headers, [
            ["MARKET A", "067651", "2026-01-06", 100, 60, 20],
            ["MARKET B", "", "2026-01-06", 100, 60, 20],
            ["  MARKET   C  ", "", "2026-01-06", 100, 60, 20],
        ], "source")
        self.assertIn(("code:067651", "2026-01-06"), records)
        self.assertIn(("market:MARKET B", "2026-01-06"), records)
        self.assertIn(("market:MARKET C", "2026-01-06"), records)

    def test_wide_raw_tab_is_planned_as_one_time_compact_migration(self):
        legacy_headers = self.headers + ["Unused_Field"]
        legacy_values = [legacy_headers] + [row + ["ignored"] for row in self.values[1:]]
        with patch.object(m, "_get_values", return_value=legacy_values), \
             patch.object(m, "_batch_update_values") as update:
            result = m.sync_raw_cot_sheet(
                None, "id", raw_sheet_info(columns=191), self.dataset, True
            )
        update.assert_called_once()
        self.assertTrue(result.schema_migrated)
        self.assertTrue(result.changed)

    def test_preflight_replaces_wide_raw_grid_with_six_column_projection(self):
        sheets = {"COT 2026": m.SheetInfo("COT 2026", 1, 1000, 191)}
        current, projected = m._projected_grid_cells(sheets, {2026: self.dataset})
        self.assertEqual(current, 191000)
        self.assertEqual(projected, 51 * 6)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.histories = {
            "WTI Crude": history([
                ("2026-01-06", 100, 60, 20, 0.4),
                ("2026-01-13", 200, 80, 20, 0.3),
            ])
        }
        self.values = [
            ["Date", "WTI Crude"],
            [m._date_serial("2026-01-06"), 0.4],
            [m._date_serial("2026-01-13"), 0.3],
        ]

    def test_summary_identical_rerun_has_zero_writes(self):
        with patch.object(m, "_batch_update_values") as update, \
             patch.object(m, "_batch_clear_values") as clear:
            result = m.sync_summary(
                None, "id", sheet_info("Summary"), self.values, self.histories, True
            )
        update.assert_not_called()
        clear.assert_not_called()
        self.assertFalse(result.changed)

    def test_summary_new_week_writes_only_suffix(self):
        histories = {"WTI Crude": pd.concat([
            self.histories["WTI Crude"],
            history([("2026-01-20", 300, 120, 30, 0.3)]),
        ], ignore_index=True)}
        captured = []

        def collect(_service, _id, updates, _dry):
            captured.extend(updates)
            return 1

        with patch.object(m, "_batch_update_values", side_effect=collect), \
             patch.object(m, "_ensure_grid_size", return_value=0), \
             patch.object(m, "_format_data_rows", return_value=0):
            result = m.sync_summary(
                None, "id", sheet_info("Summary"), self.values, histories, False
            )
        self.assertEqual(result.added, 1)
        self.assertEqual(captured[0]["range"], "'Summary'!A4")


class FutureProofingTests(unittest.TestCase):
    def test_year_range_is_dynamic(self):
        class FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                return pd.Timestamp("2028-06-01", tz="UTC")

        with patch.object(m, "datetime", FakeDatetime):
            self.assertEqual(m.get_current_year(), 2028)
            self.assertEqual(list(range(m.START_YEAR, m.get_current_year() + 1))[-1], 2028)

    def test_all_56_commodity_tabs_are_present_in_exact_order(self):
        self.assertEqual(len(m.COMMODITY_SHEETS), 56)
        self.assertEqual(m.COMMODITY_SHEETS[0], "WTI Crude")
        self.assertEqual(m.COMMODITY_SHEETS[-1], "Uranium")


if __name__ == "__main__":
    unittest.main(verbosity=2)
