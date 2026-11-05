from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qualitative_ir_downloader.google_sheets_publisher import (
    ALL_HEADERS,
    FULL_QUAL_SOFTWARE_RELEASE,
    FULL_QUAL_SOFTWARE_VERSION,
    GOOGLE_SHEET_REPORT_SCHEMA_VERSION,
    MAX_GOOGLE_CELL_CHARS,
    GoogleSheetsPublisher,
    GoogleSheetsPublicationError,
    PUBLISHER_VERSION,
    build_sheet_hyperlink,
    build_canonical_state,
    canonical_content_hash,
    resolve_google_configuration,
)
from pipeline_runner.models import StageStatus
from pipeline_runner.runner import PipelineRunner


class FakeWorksheet:
    def __init__(self, worksheet_id: int = 17):
        self.id = worksheet_id
        self.values: list[list[str]] = []
        self.update_calls = []
        self.update_options = []
        self.batch_calls = []

    def get_all_values(self):
        return self.values

    def update(self, cell_range, values, **kwargs):
        self.update_calls.append((cell_range, values))
        self.update_options.append(dict(kwargs))
        self.values = [list(row) for row in values]

    def batch_update(self, requests):
        self.batch_calls.append(list(requests))
        for request in requests:
            start = int(str(request["range"]).split(":", 1)[0][1:]) - 1
            while len(self.values) <= start:
                self.values.append([""] * len(ALL_HEADERS))
            self.values[start] = list(request["values"][0])

    def freeze(self, rows=1):
        return None

    def format(self, *_args, **_kwargs):
        return None


class FakeBook:
    def __init__(self):
        self.worksheet_value: FakeWorksheet | None = None

    def worksheet(self, _title):
        if self.worksheet_value is None:
            raise Exception("WorksheetNotFound")
        return self.worksheet_value

    def add_worksheet(self, **_kwargs):
        self.worksheet_value = FakeWorksheet()
        return self.worksheet_value


class FakeClient:
    def __init__(self):
        self.book = FakeBook()

    def open_by_key(self, _key):
        return self.book


class GoogleSheetsPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        self.dossier = self.root / "ABC_2026-09-25_research-output-v2.json"
        self.payload = {
            "ticker": "ABC", "company_name": "Acme Holdings", "as_of_date": "2026-09-25",
            "dossier_id": "dossier:ABC:2026-09-25", "quality_gate": {"status": "RESEARCH_READY"},
            "risks": {"records": [{"risk_id": "risk:1", "canonical_risk": "Demand", "mechanism": "lower demand reduces revenue", "affected_kpi": "Revenue", "financial_transmission": "Revenue", "current_status": "OPEN", "source_url": "https://example.test/risk", "evidence_ids": ["clm1"]}]},
            "management": {"leaders": [{"person_id": "person:1", "name": "A. Leader", "role": "CEO"}]},
        }
        self.dossier.write_text(json.dumps(self.payload), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def publisher(self, client=None, now=lambda: "2026-10-27T00:00:00Z"):
        return GoogleSheetsPublisher(client=client or FakeClient(), now=now, sleep=lambda _seconds: None)

    def test_creation_and_identical_rerun_are_incremental(self):
        client = FakeClient()
        publisher = self.publisher(client)
        first = publisher.publish(self.dossier, production_root=self.root / "ABC", run_id="run-one")
        self.assertEqual(first.status, "CREATED")
        self.assertTrue((self.root / "ABC/12_Google_Sheets/ABC_2026-09-25_sheet-publication.json").exists())
        snapshot = json.loads((self.root / "ABC/12_Google_Sheets/ABC_2026-09-25_sheet-publication.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["software_version"], FULL_QUAL_SOFTWARE_RELEASE)
        row_count = len(client.book.worksheet_value.values)
        second = publisher.publish(self.dossier, production_root=self.root / "ABC", run_id="run-two")
        self.assertEqual(second.status, "NO_CHANGES")
        self.assertEqual(len(client.book.worksheet_value.values), row_count)

    def test_presentation_version_and_header_contract_are_authoritative(self):
        module = __import__("qualitative_ir_downloader.google_sheets_publisher", fromlist=["_v51_header_row"])
        self.assertEqual(FULL_QUAL_SOFTWARE_VERSION, "5.3.2")
        self.assertEqual(FULL_QUAL_SOFTWARE_RELEASE, "full-qual-v5.3.2")
        self.assertEqual(GOOGLE_SHEET_REPORT_SCHEMA_VERSION, "full-qual-sheet-v5.1")
        self.assertEqual(PUBLISHER_VERSION, "full-qual-google-sheets-v5.4")
        header = module._v51_header_row("XOM", "2026-09-25")
        self.assertEqual(header[0], "XOM — Full Qualitative Research V5.3.2")
        self.assertIn("As of 2026-09-25", header)
        state = build_canonical_state({**self.payload, "ticker": "XOM", "company_name": "Exxon Mobil Corporation"})
        title = next(row for row in state.records if row.get("record_key") == "layout:header:title")
        subtitle = next(row for row in state.records if row.get("record_key") == "layout:header:subtitle")
        company = next(row for row in state.records if row.get("record_key") == "layout:header:company")
        self.assertEqual(title["cells"][0], "XOM — Full Qualitative Research V5.3.2")
        self.assertEqual(subtitle["cells"][0], "INSTITUTIONAL EQUITY RESEARCH | PM DECISION SURFACE")
        self.assertEqual(company["cells"][0], "Exxon Mobil Corporation")
        software = next(row for row in state.records if row.get("record_key") == "header:software_version")
        self.assertEqual(software["cells"][1], FULL_QUAL_SOFTWARE_RELEASE)

    def test_hyperlinks_are_safe_concise_and_written_user_entered(self):
        url = 'https://www.sec.gov/Archives/edgar/data/1/DEF14A.htm?a="b'
        formula = build_sheet_hyperlink(url)
        self.assertTrue(formula.startswith('=HYPERLINK("'))
        self.assertNotIn("'=HYPERLINK", formula)
        self.assertIn('SEC DEF 14A', formula)
        self.assertIn('a=""b', formula)
        long_formula = build_sheet_hyperlink("https://www.sec.gov/Archives/edgar/data/1/" + ("x" * 2_000) + ".htm", "SEC Filing")
        self.assertLessEqual(len(long_formula), MAX_GOOGLE_CELL_CHARS)
        self.assertNotIn("https://www.sec.gov/", long_formula.split('","', 1)[-1])
        self.assertEqual(build_sheet_hyperlink("=IMPORTXML(\"https://evil.test\")"), "")
        self.assertEqual(build_sheet_hyperlink("javascript:alert(1)"), "")

        payload = dict(self.payload)
        payload["demand_assessment"] = {"factors": [{"factor": "Revenue", "evidence": ["SEC filing"], "source_url": url}]}
        payload["risks"] = {"records": [{"risk_id": "risk:link", "canonical_risk": "SEC filing", "current_status": "OPEN", "source_url": url}]}
        payload["catalysts"] = {"records": [{"catalyst_id": "cat:inject", "canonical_catalyst": "Input", "current_status": "OPEN", "source_url": "=IMPORTXML(\"https://evil.test\")"}]}
        production = self.root / "ABC"
        normalized = production / "02_Normalized_Documents"
        normalized.mkdir(parents=True)
        (normalized / "documents.jsonl").write_text(json.dumps({"ticker": "ABC", "document_id": "def14a-1", "title": "Proxy", "document_type": "annual_report", "source_url": url, "publication_date": "2026-05-01"}) + "\n", encoding="utf-8")
        client = FakeClient()
        result = self.publisher(client).publish(payload, production_root=production)
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(client.book.worksheet_value.update_options[-1]["value_input_option"], "USER_ENTERED")
        values = [str(cell) for row in client.book.worksheet_value.values for cell in row]
        links = [value for value in values if value.startswith("=HYPERLINK(")]
        self.assertTrue(any("SEC DEF 14A" in value for value in links))
        self.assertFalse(any(value.startswith("'=HYPERLINK(") for value in values))
        module = __import__("qualitative_ir_downloader.google_sheets_publisher", fromlist=["_safe_sheet_value"])
        self.assertEqual(module._safe_sheet_value("=IMPORTXML(\"https://evil.test\")"), "'=IMPORTXML(\"https://evil.test\")")

    def test_presentation_contract_is_sector_neutral(self):
        for ticker in ("XOM", "CAT", "DAL", "JPM", "WMT", "MSFT", "NVDA"):
            payload = dict(self.payload)
            payload.update({"ticker": ticker, "company_name": f"{ticker} Company"})
            state = build_canonical_state(payload, ticker=ticker, as_of_date="2026-09-25")
            title = next(row for row in state.records if row.get("record_key") == "layout:header:title")
            subtitle = next(row for row in state.records if row.get("record_key") == "layout:header:subtitle")
            self.assertEqual(title["cells"][0], f"{ticker} — Full Qualitative Research V5.3.2")
            self.assertEqual(subtitle["cells"][0], "INSTITUTIONAL EQUITY RESEARCH | PM DECISION SURFACE")

    def test_empty_or_low_coverage_sections_render_explicit_neutral_diagnostics(self):
        state = build_canonical_state({"ticker": "LOW", "company_name": "Low Coverage", "as_of_date": "2026-09-25", "quality_gate": {"status": "RESEARCH_READY_WITH_LIMITATIONS"}})
        flattened = "\n".join(str(cell) for row in state.records for cell in row["cells"])
        for diagnostic in ("NO_KPI_SERIES", "NO_MODEL_READY_DRIVERS", "INSUFFICIENT_EVIDENCE", "NO_CURRENT_MANAGEMENT_EVIDENCE", "NO_GOVERNANCE_EVIDENCE"):
            self.assertIn(diagnostic, flattened)

    def test_single_record_change_is_patched_and_timestamped(self):
        client = FakeClient()
        publisher = self.publisher(client)
        publisher.publish(self.dossier, production_root=self.root / "ABC")
        before_calls = len(client.book.worksheet_value.batch_calls)
        self.payload["risks"]["records"][0]["current_status"] = "ESCALATING"
        self.dossier.write_text(json.dumps(self.payload), encoding="utf-8")
        result = publisher.publish(self.dossier, production_root=self.root / "ABC")
        self.assertEqual(result.status, "UPDATED")
        self.assertGreater(len(client.book.worksheet_value.batch_calls), before_calls)
        self.assertGreaterEqual(result.records_updated, 1)

    def test_notes_and_first_seen_are_preserved(self):
        client = FakeClient()
        publisher = self.publisher(client)
        publisher.publish(self.dossier, production_root=self.root / "ABC")
        worksheet = client.book.worksheet_value
        risk_row = next(index for index, row in enumerate(worksheet.values) if any("risk:1" in str(cell) for cell in row))
        notes_column = ALL_HEADERS.index("Analyst Notes")
        created_column = ALL_HEADERS.index("__created_at")
        worksheet.values[risk_row][notes_column] = "PM note"
        original_created = worksheet.values[risk_row][created_column]
        self.payload["risks"]["records"][0]["current_status"] = "IMPROVING"
        self.dossier.write_text(json.dumps(self.payload), encoding="utf-8")
        publisher.publish(self.dossier, production_root=self.root / "ABC")
        self.assertEqual(worksheet.values[risk_row][notes_column], "PM note")
        self.assertEqual(worksheet.values[risk_row][created_column], original_created)

    def test_semantic_hash_ignores_mapping_order_and_whitespace(self):
        left = {"details": {"b": 2, "a": "  one   two"}, "status": "CURRENT"}
        right = {"status": "CURRENT", "details": {"a": "one two", "b": 2.0}}
        self.assertEqual(canonical_content_hash(left), canonical_content_hash(right))

    def test_configuration_precedence(self):
        spreadsheet, path = resolve_google_configuration(service_account="cli.json", environ={"FULL_QUAL_GOOGLE_SERVICE_ACCOUNT": "env.json"})
        self.assertEqual(spreadsheet, "1GKmx61XDkLYvui2tA1lsU1K76CO9fUm2LN_7Vaf8kgU")
        self.assertEqual(path.name, "cli.json")

    def test_legacy_tab_is_backed_up_and_migrated(self):
        client = FakeClient()
        worksheet = FakeWorksheet()
        worksheet.values = [["Old", "Report"], ["manual", "content"]]
        client.book.worksheet_value = worksheet
        result = self.publisher(client).publish(self.dossier, production_root=self.root / "ABC")
        self.assertEqual(result.status, "UPDATED")
        self.assertEqual(result.migration_status, "LEGACY_SHEET_MIGRATED")
        self.assertTrue((self.root / "ABC/12_Google_Sheets/ABC_legacy-sheet-backup.json").exists())

    def test_v1_migration_preserves_analyst_notes_but_replaces_json_rows(self):
        client = FakeClient()
        worksheet = FakeWorksheet()
        old_headers = [
            "Section", "Record", "Value", "Details", "Source Date", "As-of Date", "Status", "Confidence", "Source", "Analyst Notes", "First Seen", "Last Verified", "Last Changed", "Sheet Updated At",
            "__section_id", "__record_key", "__content_hash", "__source_run_id", "__source_as_of_date", "__created_at", "__updated_at", "__change_type", "__schema_version",
        ]
        worksheet.values = [old_headers, ["SECTION 17", "Demand", "OPEN", '{"records":[{"risk":"Demand"}]}', "2026-01-01", "2026-09-25", "OPEN", "", "", "Keep this PM note", "", "", "", "", "17_risks", "17_risks:risk:1", "old", "run", "2026-09-25", "2026-01-01", "2026-01-01", "ADDED", "full-qual-sheet-v1"]]
        client.book.worksheet_value = worksheet
        result = self.publisher(client).publish(self.dossier, production_root=self.root / "ABC")
        self.assertEqual(result.migration_status, "V1_TO_V4_MIGRATED")
        self.assertTrue(any("Keep this PM note" in str(cell) for row in worksheet.values for cell in row))
        self.assertFalse(any('{"records":' in str(cell) for row in worksheet.values for cell in row))

    def test_v2_migration_is_in_place_and_backed_up(self):
        client = FakeClient()
        worksheet = FakeWorksheet()
        v2_headers = [
            "Primary", "Assessment", "Direction", "Confidence", "Current State", "Previous State", "Latest Value", "Unit", "Period", "Evidence", "Source", "Category", "Materiality", "Validation", "Timing", "As-of Date", "Analyst Notes", "First Seen", "Last Verified", "Last Changed",
            "__section_id", "__record_key", "__content_hash", "__source_run_id", "__source_as_of_date", "__created_at", "__updated_at", "__change_type", "__schema_version",
        ]
        worksheet.values = [v2_headers, ["Demand", "OPEN", "", "", "", "", "", "", "", "", "", "", "", "", "", "2026-09-25", "Keep V2 note", "", "", "", "17_risks", "17_risks:risk:1", "old", "run", "2026-09-25", "2026-01-01", "2026-01-01", "ADDED", "full-qual-sheet-v2"]]
        client.book.worksheet_value = worksheet
        result = self.publisher(client).publish(self.dossier, production_root=self.root / "ABC")
        self.assertEqual(result.migration_status, "V2_TO_V4_MIGRATED")
        self.assertTrue((self.root / "ABC/12_Google_Sheets/ABC_v2-sheet-backup.json").exists())
        self.assertTrue(any("Keep V2 note" in str(cell) for row in worksheet.values for cell in row))

    def test_future_cached_source_is_explicitly_ineligible(self):
        normalized = self.root / "production/02_Normalized_Documents/documents.jsonl"
        normalized.parent.mkdir(parents=True)
        normalized.write_text(json.dumps({"ticker": "ABC", "document_id": "future-1", "title": "Future", "public_availability_date": "2026-10-01", "source_url": "https://example.test/future"}) + "\n", encoding="utf-8")
        state = build_canonical_state(self.payload, production_root=self.root / "production", as_of_date="2026-09-25")
        future = next(row for row in state.records if row.get("record_key", "").endswith("future-1"))
        self.assertFalse(future["pit_eligible"])
        self.assertEqual(future["pit_status"], "FUTURE_NOT_ELIGIBLE")

    def test_missing_credentials_and_permission_are_explicit(self):
        missing = GoogleSheetsPublisher(service_account=self.root / "missing.json").publish(self.dossier)
        self.assertEqual(missing.code, "GOOGLE_SHEETS_CREDENTIALS_MISSING")

        class Denied:
            def open_by_key(self, _key):
                raise Exception("403 permission denied")

        denied = GoogleSheetsPublisher(client=Denied()).publish(self.dossier)
        self.assertEqual(denied.code, "GOOGLE_SHEETS_PERMISSION_DENIED")

    def test_retry_is_bounded_for_transient_429(self):
        class Retrying(FakeClient):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def open_by_key(self, key):
                self.calls += 1
                if self.calls < 3:
                    raise Exception("429 rate limit")
                return super().open_by_key(key)

        client = Retrying()
        result = self.publisher(client).publish(self.dossier, production_root=self.root / "ABC")
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(client.calls, 3)

    def test_pipeline_keeps_research_status_separate_from_publication_failure(self):
        class FailedPublisher:
            def publish(self, *_args, **_kwargs):
                return type("Summary", (), {"to_dict": lambda _self: {"status": "FAILED", "code": "GOOGLE_SHEETS_PERMISSION_DENIED", "message": "denied"}})()

        runner = PipelineRunner("ABC", "2026-09-25", data_root=self.root / "data", artifacts_root=self.root / "artifacts", output_root=self.root / "output", google_sheets=True, google_sheets_publisher=FailedPublisher())
        runner._ensure_production_layout()
        runner._manifest_ready = True
        dossier = runner.ticker_root / "10_Research_Dossier" / "ABC_2026-09-25_research-output-v2.json"
        dossier.parent.mkdir(parents=True, exist_ok=True)
        dossier.write_text(json.dumps(self.payload), encoding="utf-8")
        stage = runner._publish_google_sheets(dossier)
        self.assertEqual(stage.status, StageStatus.FAILED)
        self.assertEqual(runner._google_sheets_status, "FAILED")
        self.assertEqual(runner._research_status, "SUCCESS")

    def test_append_uses_last_nonempty_row_not_formatted_grid_length(self):
        client = FakeClient()
        publisher = self.publisher(client)
        publisher.publish(self.dossier, production_root=self.root / "ABC")
        worksheet = client.book.worksheet_value
        worksheet.values.extend([[""] * len(ALL_HEADERS) for _ in range(50)])
        self.payload["risks"]["records"].append({"risk_id": "risk:2", "canonical_risk": "Liquidity", "current_status": "OPEN"})
        self.dossier.write_text(json.dumps(self.payload), encoding="utf-8")
        result = publisher.publish(self.dossier, production_root=self.root / "ABC")
        self.assertEqual(result.status, "UPDATED")
        nonempty = [index for index, row in enumerate(worksheet.values) if any(str(cell).strip() for cell in row)]
        # V2 is a vertically stacked institutional report, so it has more
        # rows than the old V1 generic table.  The invariant is that the
        # publisher rewrites the worksheet without retaining the 50 formatted
        # blank rows appended above.
        self.assertLess(max(nonempty), len(worksheet.values))
        self.assertTrue(all(any(str(cell).strip() for cell in row) for row in worksheet.values[: max(nonempty) + 1]))

    def test_v2_uses_section_specific_tables_without_generic_details_json(self):
        state = build_canonical_state(self.payload, as_of_date="2026-09-25")
        self.assertEqual("full-qual-sheet-v5.1", __import__("qualitative_ir_downloader.google_sheets_publisher", fromlist=["GOOGLE_SHEET_REPORT_SCHEMA_VERSION"]).GOOGLE_SHEET_REPORT_SCHEMA_VERSION)
        rows = [row["cells"] for row in state.records]
        flattened = "\n".join(str(cell) for row in rows for cell in row)
        self.assertNotIn("Section\nRecord\nValue", flattened)
        self.assertNotIn('"records":', flattened)
        self.assertTrue(any(row["row_kind"] == "section_heading" for row in state.records))
        self.assertTrue(any(row["row_kind"] == "table_header" and row["section_id"] == "02_executive" for row in state.records))

    def test_large_nested_objects_and_evidence_are_flattened_and_safe(self):
        payload = dict(self.payload)
        payload["governance"] = {"board_size": 11, "shareholder_rights": {"evidence_text": "proxy " + ("x" * 100_000), "poison_pill": "UNKNOWN"}}
        payload["scoring"] = {"pillars": {"QUALITY": {"status": "SUPPORTED", "confidence": 0.8}}, "v2_factor_inputs": {"QUALITY": [{"factor": "X", "status": "SUPPORTED", "evidence": ["e1"]}]}}
        payload["management"] = {"leaders": [{"person_id": "p1", "name": "Leader", "role": "CEO", "experience": "y" * 100_000}]}
        state = build_canonical_state(payload)
        maximum = max(len(str(cell)) for row in state.records for cell in row["cells"])
        self.assertLessEqual(maximum, MAX_GOOGLE_CELL_CHARS)
        self.assertLessEqual(maximum, 40_000)
        rights = [row for row in state.records if row["record_key"].endswith(":poison_pill")]
        self.assertTrue(rights)
        self.assertLessEqual(len(rights[0]["cells"][2]), 1_500)

    def test_cell_safety_fails_locally_with_diagnostic(self):
        with self.assertRaises(GoogleSheetsPublicationError) as context:
            from qualitative_ir_downloader.google_sheets_publisher import assert_max_cell_lengths
            assert_max_cell_lengths([["ABC", "x" * (MAX_GOOGLE_CELL_CHARS + 1)]], ticker="ABC", section="management")
        self.assertIn("ticker=ABC", str(context.exception))
        self.assertIn("column=B", str(context.exception))

    def test_jpm_sized_dossier_renders_and_identical_rerun_is_no_changes(self):
        payload = dict(self.payload)
        payload["ticker"] = "JPM"
        payload["company_name"] = "JPMorgan Chase & Co."
        payload["risks"] = {"records": [{"risk_id": f"risk:{i}", "canonical_risk": f"Risk {i}", "current_status": "OPEN", "evidence": [{"snippet": "concise evidence"}]} for i in range(1_200)]}
        client = FakeClient()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            path = Path(temp) / "JPM.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            publisher = self.publisher(client)
            first = publisher.publish(path, production_root=Path(temp) / "JPM")
            second = publisher.publish(path, production_root=Path(temp) / "JPM", run_id="second-run")
        self.assertEqual(first.status, "CREATED")
        self.assertEqual(second.status, "NO_CHANGES")
        self.assertEqual(second.records_updated, 0)
        self.assertLessEqual(first.largest_cell, MAX_GOOGLE_CELL_CHARS)

    def test_v3_pm_surface_precedes_deep_archive_and_has_stable_ids(self):
        payload = dict(self.payload)
        payload["demand_assessment"] = {"factors": [
            {"factor": "Comparable sales", "evidence": ["sales grew"], "source_url": "https://example.test/q"},
            {"factor": "Comparable sales", "evidence": ["duplicate"], "source_url": "https://example.test/q"},
        ]}
        state = build_canonical_state(payload, as_of_date="2026-09-25")
        first_deep = next(index for index, row in enumerate(state.records) if row.get("layer") == "deep")
        self.assertLessEqual(first_deep, 100)
        self.assertTrue(all(row.get("layer") == "pm" for row in state.records[:first_deep]))
        kpis = [row for row in state.records if row.get("section_id") == "10_kpis" and row.get("row_kind") == "record"]
        self.assertEqual(len(kpis), 1)
        self.assertTrue(kpis[0]["record_key"].startswith("kpi:abc:"))

    def test_v3_does_not_emit_contradictory_kpi_placeholder(self):
        payload = dict(self.payload)
        payload["demand_assessment"] = {"factors": [{"factor": "Revenue growth", "evidence": ["evidence"], "source_url": "https://example.test/q"}]}
        state = build_canonical_state(payload)
        flattened = "\n".join(str(cell) for row in state.records for cell in row["cells"])
        self.assertNotIn("No KPI candidate captured", flattened)
        self.assertIn("Revenue growth", flattened)

    def test_v3_does_not_promote_claim_ids_to_executive_evidence(self):
        payload = dict(self.payload)
        payload["demand_assessment"] = {"status": "SUPPORTED_EVIDENCE", "evidence": ["clm_abc123", "clm_def456"]}
        state = build_canonical_state(payload)
        executive = [str(cell) for row in state.records if row.get("section_id") == "02_executive" and row.get("row_kind") == "record" for cell in row["cells"]]
        self.assertFalse(any("clm_abc123" in cell for cell in executive))

    def test_v3_rejects_management_prose_fragments_and_caps_watchlists(self):
        payload = dict(self.payload)
        payload["management"] = {"leaders": [
            {"name": "Amounts paid to peer CEOs", "role": "CEO"},
            {"name": "Jane Doe", "role": "CFO"},
        ]}
        payload["risks"] = {"records": [{"risk_id": f"risk:{i}", "canonical_risk": f"Demand risk {i}", "mechanism": "lower demand reduces revenue", "affected_kpi": "Revenue", "financial_transmission": "Revenue", "current_status": "OPEN", "evidence_ids": [f"clm{i}"]} for i in range(30)]}
        state = build_canonical_state(payload)
        people = [row for row in state.records if row.get("section_id") == "17_management" and row.get("row_kind") == "record"]
        risks = [row for row in state.records if row.get("section_id") == "22_risks" and row.get("row_kind") == "record"]
        self.assertEqual(len(people), 1)
        self.assertLessEqual(len(risks), 30)  # unsupported candidates do not become an unbounded PM surface.
        self.assertTrue(any("Top risk watchlist contains 15" in str(cell) for row in state.records for cell in row["cells"]))

    def test_v3_separates_current_and_historical_management(self):
        payload = dict(self.payload)
        payload["management"] = {
            "leaders": [{"name": "Jane Doe", "role": "CEO"}],
            "historical_leaders": [{"name": "John Roe", "role": "Former CEO"}],
        }
        state = build_canonical_state(payload)
        roles = {row["cells"][0]: row["cells"][4] for row in state.records if row.get("section_id") == "17_management" and row.get("row_kind") == "record"}
        self.assertEqual(roles["Jane Doe"], "CURRENT")
        self.assertEqual(roles["John Roe"], "HISTORICAL")

    def test_v3_change_rows_distinguish_baseline_and_no_change(self):
        from qualitative_ir_downloader.google_sheets_publisher import _v3_change_rows
        current = {"ticker": "ABC", "generated_at": "2026-09-25T00:00:00Z", "risks": {"status": "OPEN"}}
        baseline = _v3_change_rows(current, None)
        stable = _v3_change_rows(current, dict(current))
        self.assertEqual(baseline[0]["area"], "INITIAL_BASELINE")
        self.assertEqual(stable[0]["area"], "NO_MATERIAL_ANALYTICAL_CHANGE")


if __name__ == "__main__":
    unittest.main()
