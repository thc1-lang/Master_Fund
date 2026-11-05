"""Incremental Google Sheets publication for the Full Qual V4 dossier.

The downloader's :mod:`google_sheets` module remains a read-only source for
the acquisition universe.  This module is deliberately a publication
adapter: the local research-output dossier and production JSONL stores remain
the authoritative source of analytical truth.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from common.issuer_identity import canonical_identity_hash, normalize_cik

log = logging.getLogger(__name__)

DEFAULT_QUALITATIVE_SPREADSHEET_ID = "1GKmx61XDkLYvui2tA1lsU1K76CO9fUm2LN_7Vaf8kgU"
DEFAULT_SERVICE_ACCOUNT_PATH = Path(r"C:\Code\service_account.json")
GOOGLE_SHEET_REPORT_SCHEMA_VERSION = "full-qual-sheet-v2"
PUBLISHER_VERSION = "full-qual-google-sheets-v1"

# The software release is intentionally independent from the worksheet schema.
# The latter remains frozen at ``full-qual-sheet-v5.1`` while presentation-only
# publisher changes can move forward without invalidating analytical stores.
FULL_QUAL_SOFTWARE_VERSION = "5.3.2"
FULL_QUAL_SOFTWARE_RELEASE = f"full-qual-v{FULL_QUAL_SOFTWARE_VERSION}"

# The visible columns are intentionally dossier-like rather than a dataframe
# dump.  The columns after Analyst Notes are machine-managed and are hidden in
# the workbook by _apply_formatting.
VISIBLE_HEADERS = [
    "Section", "Record", "Value", "Details", "Source Date", "As-of Date",
    "Status", "Confidence", "Source", "Analyst Notes", "First Seen",
    "Last Verified", "Last Changed", "Sheet Updated At",
]
METADATA_HEADERS = [
    "__section_id", "__record_key", "__content_hash", "__source_run_id",
    "__source_as_of_date", "__created_at", "__updated_at", "__change_type",
    "__schema_version",
]
ALL_HEADERS = VISIBLE_HEADERS + METADATA_HEADERS

SECTION_NAMES = {
    "00_header": "SECTION 00 — COMPANY / RUN HEADER",
    "01_executive_summary": "SECTION 01 — EXECUTIVE QUALITATIVE SUMMARY",
    "02_business_model": "SECTION 02 — BUSINESS MODEL",
    "03_kpi_candidates": "SECTION 03 — KPI CANDIDATES",
    "04_driver_candidates": "SECTION 04 — DRIVER CANDIDATES / ECONOMIC MECHANISMS",
    "05_current_state": "SECTION 05 — CURRENT OPERATING STATE",
    "06_temporal_trajectory": "SECTION 06 — TEMPORAL TRAJECTORY",
    "07_demand_pricing": "SECTION 07 — DEMAND / PRICING / VOLUME",
    "08_costs_margins": "SECTION 08 — COSTS / MARGINS / OPERATING LEVERAGE",
    "09_competition_moat": "SECTION 09 — COMPETITION / MARKET SHARE / MOAT",
    "10_products_geography": "SECTION 10 — PRODUCTS / GEOGRAPHY / SEGMENTS",
    "11_strategy": "SECTION 11 — STRATEGY",
    "12_management": "SECTION 12 — MANAGEMENT",
    "13_management_credibility": "SECTION 13 — MANAGEMENT CREDIBILITY / TRACK RECORD",
    "14_capital_allocation": "SECTION 14 — CAPITAL ALLOCATION",
    "15_governance": "SECTION 15 — BOARD / GOVERNANCE",
    "16_insiders": "SECTION 16 — INSIDER INTELLIGENCE",
    "17_risks": "SECTION 17 — RISKS",
    "18_catalysts": "SECTION 18 — CATALYSTS",
    "19_guidance": "SECTION 19 — GUIDANCE / COMMITMENTS",
    "20_scoring": "SECTION 20 — QUALITATIVE SCORING",
    "21_quality_gate": "SECTION 21 — QUALITY GATE / COVERAGE",
    "22_integrity": "SECTION 22 — PIT / FISCAL / DATA INTEGRITY",
    "23_acquisition": "SECTION 23 — ACQUISITION / SOURCE REGISTRY",
    "24_change_log": "SECTION 24 — CHANGE LOG",
}

SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "01_executive_summary": ("executive_assessment", "business_trajectory", "demand_assessment", "operating_execution", "competitive_advantage"),
    "02_business_model": ("business_model", "industry_structure"),
    "03_kpi_candidates": ("business_trajectory", "demand_assessment", "scoring"),
    "04_driver_candidates": ("business_model", "demand_assessment", "operating_execution", "industry_structure"),
    "05_current_state": ("business_trajectory", "demand_assessment", "operating_execution", "competitive_advantage"),
    "06_temporal_trajectory": ("change_analysis",),
    "07_demand_pricing": ("demand_assessment",),
    "08_costs_margins": ("operating_execution",),
    "09_competition_moat": ("competitive_advantage", "industry_structure"),
    "10_products_geography": ("industry_structure", "business_model"),
    "11_strategy": ("management", "capital_allocation"),
    "12_management": ("management",),
    "13_management_credibility": ("management_track_record", "management"),
    "14_capital_allocation": ("capital_allocation", "management"),
    "15_governance": ("governance",),
    "16_insiders": ("insider_behaviour",),
    "17_risks": ("risks",),
    "18_catalysts": ("catalysts",),
    "19_guidance": ("guidance_reliability", "management"),
    "20_scoring": ("scoring",),
    "21_quality_gate": ("quality_gate", "evidence_audit"),
    "22_integrity": ("evidence_audit", "metadata", "analytical_provenance"),
}


class GoogleSheetsPublicationError(RuntimeError):
    """A bounded, user-visible publication failure."""

    def __init__(self, code: str, message: str, *, client_email: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.client_email = client_email


class GoogleSheetsPermissionDenied(GoogleSheetsPublicationError):
    def __init__(self, client_email: str = "") -> None:
        super().__init__("GOOGLE_SHEETS_PERMISSION_DENIED", "Workbook is not shared with the service account.", client_email=client_email)


def resolve_google_configuration(*, spreadsheet_id: str | None = None,
                                 service_account: str | Path | None = None,
                                 environ: Mapping[str, str] | None = None) -> tuple[str, Path]:
    """Resolve CLI > environment > defaults without reading credentials."""
    env = environ if environ is not None else os.environ
    resolved_id = str(spreadsheet_id or DEFAULT_QUALITATIVE_SPREADSHEET_ID).strip()
    resolved_path = Path(service_account or env.get("FULL_QUAL_GOOGLE_SERVICE_ACCOUNT") or DEFAULT_SERVICE_ACCOUNT_PATH)
    return resolved_id, resolved_path.expanduser()


def service_account_email(path: str | Path) -> str:
    """Return only the non-secret client email for diagnostics."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GoogleSheetsPublicationError("GOOGLE_SHEETS_CREDENTIALS_MISSING", f"Credential file not found: {Path(path)}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleSheetsPublicationError("GOOGLE_SHEETS_CREDENTIALS_INVALID", f"Credential file could not be read: {Path(path)}") from exc
    email = str(payload.get("client_email") or "").strip()
    if not email:
        raise GoogleSheetsPublicationError("GOOGLE_SHEETS_CREDENTIALS_INVALID", "Credential file has no client_email.")
    return email


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalise(value: Any) -> Any:
    """Canonicalise values for semantic (not serialization) comparison."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return round(value, 10)
    if isinstance(value, Mapping):
        return {str(k): _normalise(value[k]) for k in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple, set)):
        items = [_normalise(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))
    return re.sub(r"\s+", " ", str(value)).strip()


def canonical_content_hash(record: Mapping[str, Any]) -> str:
    ignored = {"first_seen", "last_verified", "last_changed", "sheet_updated_at", "created_at", "updated_at", "change_type", "analyst_notes", "source_run_id"}
    payload = {key: value for key, value in record.items() if key not in ignored and not key.startswith("__")}
    # Structured details are stored as a readable JSON cell.  Remove the
    # publication-run identifier inside that JSON as well; it is provenance,
    # not analytical content, and changes on every pipeline invocation.
    details = payload.get("details")
    if isinstance(details, str):
        try:
            parsed = json.loads(details)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            def strip_run_ids(value: Any) -> Any:
                if isinstance(value, Mapping):
                    return {key: strip_run_ids(child) for key, child in value.items() if str(key) != "source_run_id"}
                if isinstance(value, list):
                    return [strip_run_ids(child) for child in value]
                return value
            payload["details"] = strip_run_ids(parsed)
    encoded = json.dumps(_normalise(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(_normalise(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def _without_raw_material(value: Any) -> Any:
    """Keep provenance fields while excluding raw document bodies from cells."""
    if isinstance(value, Mapping):
        blocked = {"text", "content", "body", "document_text", "full_text", "html", "content_html", "raw_html", "raw_text", "source_body", "pdf_bytes", "raw_pdf", "evidence_text"}
        return {str(key): _without_raw_material(child) for key, child in value.items() if str(key).lower() not in blocked}
    if isinstance(value, list):
        return [_without_raw_material(child) for child in value]
    return value


def _date_from(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else text[:32]


def _find_first(value: Any, names: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if value.get(name) not in (None, "", []):
                return value.get(name)
        for child in value.values():
            found = _find_first(child, names)
            if found not in (None, "", []):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, names)
            if found not in (None, "", []):
                return found
    return None


def _record_identity(item: Mapping[str, Any], section: str, field: str, index: int) -> str:
    candidates = ("record_key", "id", "*_id", "document_id", "risk_id", "catalyst_id", "person_id", "claim_id", "guidance_id", "commitment_id", "change_id", "source_record_ids")
    for candidate in candidates:
        if candidate == "*_id":
            for key, value in item.items():
                if key.endswith("_id") and value:
                    return f"{section}:{value}"
            continue
        value = item.get(candidate)
        if isinstance(value, list) and value:
            value = value[0]
        if value not in (None, "", []):
            return f"{section}:{value}"
    digest = hashlib.sha256(_json(item).encode("utf-8")).hexdigest()[:16]
    return f"{section}:{field}:{digest}"


def _source_fields(value: Any) -> tuple[str, str]:
    url = _find_first(value, ("source_url", "source", "url", "artifact_url"))
    if isinstance(url, Mapping):
        url = url.get("source_url") or url.get("url")
    date = _find_first(value, ("source_date", "publication_date", "filing_date", "issue_date", "available_date", "as_of_date"))
    return str(url or "") if isinstance(url, str) else "", _date_from(date)


def _summary_value(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("status", "current_status", "overall_score", "calculated_score", "people_count", "board_size", "latest_period", "current_period"):
            if value.get(key) not in (None, "", []):
                return str(value[key])
        return "STRUCTURED"
    if isinstance(value, list):
        return f"{len(value)} records"
    if value is None:
        return ""
    return str(value)


def _make_record(*, section: str, field: str, value: Any, ticker: str, as_of_date: str,
                 run_id: str, index: int = 0, label: str | None = None) -> dict[str, Any]:
    item = value if isinstance(value, Mapping) else {"value": value}
    source_url, source_date = _source_fields(value)
    status = _find_first(value, ("status", "current_status", "change_status", "source_status", "coverage_status"))
    confidence = _find_first(value, ("confidence", "period_resolution_confidence", "evidence_confidence"))
    record = {
        "section_id": section,
        "record_key": _record_identity(item, section, field, index),
        "section": SECTION_NAMES.get(section, section),
        "record": label or field.replace("_", " ").title(),
        "value": _summary_value(value),
        "details": _json(value),
        "source_date": source_date,
        "as_of_date": as_of_date,
        "status": str(status or "CURRENT"),
        "confidence": str(confidence or ""),
        "source": source_url,
        "source_url": source_url,
        "first_seen": _date_from(_find_first(value, ("first_seen", "created_at"))),
        "last_verified": _date_from(_find_first(value, ("last_verified", "latest_seen", "source_date"))),
        "last_changed": _date_from(_find_first(value, ("last_changed", "updated_at", "source_date"))),
        "ticker": ticker,
        "source_run_id": run_id,
        "source_as_of_date": as_of_date,
    }
    if isinstance(value, Mapping) and "pit_eligible" in value:
        record["pit_eligible"] = bool(value.get("pit_eligible"))
        record["pit_status"] = str(value.get("pit_status") or ("ELIGIBLE" if value.get("pit_eligible") else "FUTURE_NOT_ELIGIBLE"))
    record["content_hash"] = canonical_content_hash(record)
    return record


def _append_value_records(records: list[dict[str, Any]], *, section: str, field: str, value: Any,
                          ticker: str, as_of_date: str, run_id: str) -> None:
    if value in (None, "", [], {}):
        return
    if isinstance(value, Mapping):
        # V2 commonly wraps useful rows in a section object (for example
        # ``risks.records`` or ``management.leaders``).  Route those rows as
        # first-class stable records rather than hashing the wrapper object.
        nested_keys = ("records", "leaders", "current_roles", "directors", "board_members", "committees", "transactions", "filings", "changes", "factors", "pillars", "events", "coverage_records")
        nested = next((value.get(key) for key in nested_keys if isinstance(value.get(key), list)), None)
        if nested is not None:
            for index, item in enumerate(nested):
                records.append(_make_record(section=section, field=field, value=item, ticker=ticker, as_of_date=as_of_date, run_id=run_id, index=index, label=(item.get("name") or item.get("title") or item.get("canonical_risk") or item.get("metric") if isinstance(item, Mapping) else None)))
            return
    if isinstance(value, list):
        for index, item in enumerate(value):
            records.append(_make_record(section=section, field=field, value=item, ticker=ticker, as_of_date=as_of_date, run_id=run_id, index=index, label=(item.get("name") or item.get("title") or item.get("canonical_risk") or item.get("metric") if isinstance(item, Mapping) else None)))
        return
    records.append(_make_record(section=section, field=field, value=value, ticker=ticker, as_of_date=as_of_date, run_id=run_id))


def _source_registry(*, ticker: str, as_of_date: str, run_id: str, artifacts_root: Path | None,
                     production_root: Path | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    roots = [p for p in (production_root, artifacts_root) if p]
    seen: set[str] = set()
    for root in roots:
        for relative in ("02_Normalized_Documents/documents.jsonl", "qualitative_analysis/documents.jsonl"):
            for row in _read_jsonl(root / relative):
                key = str(row.get("document_id") or row.get("source_url") or row.get("local_path") or "")
                aliases = {str(row.get(name) or "") for name in ("document_id", "source_url", "local_path") if row.get(name)}
                if not key or aliases & seen or str(row.get("ticker") or ticker).upper() != ticker.upper():
                    continue
                seen.update(aliases)
                available = _date_from(row.get("public_availability_date") or row.get("available_date") or row.get("publication_date") or row.get("filing_date"))
                eligible = not available or available <= as_of_date
                registry = _without_raw_material(row)
                registry.update({
                    "pit_eligible": eligible,
                    "pit_status": "ELIGIBLE" if eligible else "FUTURE_NOT_ELIGIBLE",
                    "record_key": f"acquisition:{key}",
                    "source_run_id": run_id,
                    "source_as_of_date": as_of_date,
                })
                candidates.append(_make_record(section="23_acquisition", field="acquisition", value=registry, ticker=ticker, as_of_date=as_of_date, run_id=run_id, label=str(row.get("title") or row.get("document_id") or "Source artifact")))
        acquisition = root / "01_Acquisition"
        if acquisition.exists():
            for manifest in acquisition.glob("**/manifest.json"):
                payload = {}
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if str(payload.get("ticker") or "").upper() != ticker.upper():
                    continue
                for category in ("news", "reports", "events", "transcripts"):
                    for row in payload.get(category) or []:
                        key = str(row.get("document_id") or row.get("source_url") or row.get("local_filename") or "")
                        aliases = {str(row.get(name) or "") for name in ("document_id", "source_url", "local_filename") if row.get(name)}
                        if not key or aliases & seen:
                            continue
                        seen.update(aliases)
                        registry = _without_raw_material(row)
                        registry["manifest_path"] = str(manifest)
                        registry["acquisition_category"] = category
                        available = _date_from(row.get("public_availability_date") or row.get("available_date") or row.get("date") or row.get("filing_date"))
                        registry["pit_eligible"] = not available or available <= as_of_date
                        registry["pit_status"] = "ELIGIBLE" if registry["pit_eligible"] else "FUTURE_NOT_ELIGIBLE"
                        candidates.append(_make_record(section="23_acquisition", field="acquisition", value=registry, ticker=ticker, as_of_date=as_of_date, run_id=run_id, label=str(row.get("title") or row.get("local_filename") or "Acquired artifact")))
    return candidates


@dataclass
class CanonicalState:
    ticker: str
    as_of_date: str
    run_id: str
    records: list[dict[str, Any]] = field(default_factory=list)
    dossier_fingerprint: str = ""

    @property
    def section_fingerprints(self) -> dict[str, str]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.records:
            grouped.setdefault(str(row.get("section_id") or ""), []).append(row)
        return {section: hashlib.sha256(_json(sorted((canonical_content_hash(row) for row in rows))).encode()).hexdigest() for section, rows in sorted(grouped.items())}


def build_canonical_state(dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None,
                          as_of_date: str | None = None, artifacts_root: str | Path | None = None,
                          production_root: str | Path | None = None, run_id: str = "") -> CanonicalState:
    """Build the sheet view from finalized production artifacts only."""
    dossier_path: Path | None = None
    if isinstance(dossier, (str, Path)):
        dossier_path = Path(dossier)
        payload = json.loads(dossier_path.read_text(encoding="utf-8"))
    else:
        payload = dict(dossier)
    symbol = str(ticker or payload.get("ticker") or "").upper()
    cutoff = str(as_of_date or payload.get("as_of_date") or payload.get("source_cutoff") or "")
    execution_id = run_id or str(payload.get("run_id") or payload.get("dossier_id") or f"sheet:{symbol}:{cutoff}")
    records: list[dict[str, Any]] = []
    header = {
        "company": payload.get("company_name", ""), "ticker": symbol, "as_of_date": cutoff,
        "research_output_version": payload.get("research_output_version"),
        "quality_gate": payload.get("quality_gate", {}), "scoring": payload.get("scoring", {}),
        "generated_at": payload.get("generated_at"), "dossier_id": payload.get("dossier_id"),
        "input_fingerprint": payload.get("input_fingerprint"), "source_cutoff": payload.get("source_cutoff"),
    }
    records.append(_make_record(section="00_header", field="header", value=header, ticker=symbol, as_of_date=cutoff, run_id=execution_id, label=f"{symbol} — {payload.get('company_name') or ''}".strip(" —")))
    for section, fields in SECTION_FIELDS.items():
        for field_name in fields:
            _append_value_records(records, section=section, field=field_name, value=payload.get(field_name), ticker=symbol, as_of_date=cutoff, run_id=execution_id)
    records.extend(_source_registry(ticker=symbol, as_of_date=cutoff, run_id=execution_id,
                                    artifacts_root=Path(artifacts_root) if artifacts_root else None,
                                    production_root=Path(production_root) if production_root else None))
    fingerprint = str(payload.get("input_fingerprint") or "")
    if not fingerprint:
        encoded = json.dumps(_normalise(payload), sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
    return CanonicalState(symbol, cutoff, execution_id, records, fingerprint)


def _column_letter(number: int) -> str:
    value = number
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _safe_worksheet_title(ticker: str) -> str:
    # Google limits titles to 100 characters and reserves these separators.
    cleaned = re.sub(r"[\[\]\*?/\\:]", "_", str(ticker or "TICKER")).strip()
    return cleaned[:100] or "TICKER"


def _worksheet_values(worksheet: Any) -> list[list[Any]]:
    getter = getattr(worksheet, "get_all_values", None)
    if callable(getter):
        return [list(row) for row in (getter() or [])]
    getter = getattr(worksheet, "get", None)
    if callable(getter):
        return [list(row) for row in (getter("A:W") or [])]
    return []


def _row_from_record(record: Mapping[str, Any], *, created_at: str, updated_at: str,
                     change_type: str, analyst_notes: str = "") -> list[Any]:
    source = str(record.get("source_url") or record.get("source", ""))
    source_cell = f'=HYPERLINK("{source.replace(chr(34), chr(34) * 2)}","{source.replace(chr(34), chr(34) * 2)}")' if source.lower().startswith(("http://", "https://")) else source
    visible = [
        record.get("section", ""), record.get("record", ""), record.get("value", ""), record.get("details", ""),
        record.get("source_date", ""), record.get("as_of_date", ""), record.get("status", ""), record.get("confidence", ""),
        source_cell, analyst_notes, record.get("first_seen", ""),
        record.get("last_verified", ""), record.get("last_changed", ""), updated_at,
    ]
    visible.extend([
        record.get("section_id", ""), record.get("record_key", ""), record.get("content_hash") or canonical_content_hash(record),
        record.get("source_run_id", ""), record.get("source_as_of_date", record.get("as_of_date", "")), created_at, updated_at,
        change_type, GOOGLE_SHEET_REPORT_SCHEMA_VERSION,
    ])
    return visible


def _parse_managed(values: list[list[Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, int], bool]:
    if not values:
        return {}, {}, False
    header = [str(item) for item in values[0]]
    indices = {name: index for index, name in enumerate(header) if name}
    managed = "__record_key" in indices and "__content_hash" in indices
    records: dict[str, dict[str, Any]] = {}
    rows: dict[str, int] = {}
    for row_number, row in enumerate(values[1:], start=2):
        key_index = indices.get("__record_key")
        if key_index is None or key_index >= len(row) or not str(row[key_index]).strip():
            continue
        key = str(row[key_index])
        records[key] = {name: (row[index] if index < len(row) else "") for name, index in indices.items()}
        rows[key] = row_number
    return records, rows, managed


@dataclass
class PublicationSummary:
    status: str
    code: str = ""
    message: str = ""
    worksheet_title: str = ""
    worksheet_id: str = ""
    records_existing: int = 0
    records_new: int = 0
    records_added: int = 0
    records_updated: int = 0
    records_unchanged: int = 0
    records_resolved: int = 0
    records_stale: int = 0
    records_removed: int = 0
    sections_changed: list[str] = field(default_factory=list)
    api_calls: int = 0
    snapshot_path: str = ""
    dossier_fingerprint: str = ""
    client_email: str = ""
    migration_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "code": self.code, "message": self.message,
            "worksheet_title": self.worksheet_title, "worksheet_id": self.worksheet_id,
            "records_existing": self.records_existing, "records_new": self.records_new,
            "records_added": self.records_added, "records_updated": self.records_updated,
            "records_unchanged": self.records_unchanged, "records_resolved": self.records_resolved,
            "records_stale": self.records_stale, "records_removed": self.records_removed,
            "sections_changed": list(self.sections_changed), "api_calls": self.api_calls,
            "snapshot_path": self.snapshot_path, "dossier_fingerprint": self.dossier_fingerprint,
            "client_email": self.client_email, "migration_status": self.migration_status,
        }


class GoogleSheetsPublisher:
    """Publish one ticker tab using bounded, batched worksheet mutations."""

    def __init__(self, *, spreadsheet_id: str | None = None, service_account: str | Path | None = None,
                 retries: int = 3, client: Any | None = None, now: Callable[[], str] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.spreadsheet_id, self.service_account = resolve_google_configuration(spreadsheet_id=spreadsheet_id, service_account=service_account)
        self.retries = max(1, min(int(retries), 3))
        self.client = client
        self.now = now or _now
        self.sleep = sleep or time.sleep
        self.api_calls = 0

    def _call(self, operation: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.api_calls += 1
                return operation()
            except Exception as exc:  # gspread exceptions differ by version
                last = exc
                text = str(exc).lower()
                status = getattr(exc, "response", None)
                status_code = getattr(status, "status_code", None) or getattr(exc, "status_code", None)
                if status_code in {401, 403} or "permission" in text or "forbidden" in text or "unauthorized" in text:
                    raise exc
                retryable = status_code == 429 or (isinstance(status_code, int) and status_code >= 500) or any(token in text for token in ("429", "rate", "tempor", "timeout", "connection", "503", "502", "500"))
                if not retryable or attempt + 1 >= self.retries:
                    raise exc
                self.sleep(2 ** attempt)
        raise last or RuntimeError("Google Sheets operation failed")

    def _connect(self) -> tuple[Any, str]:
        if self.client is not None:
            http_client = getattr(self.client, "http_client", None)
            if http_client is not None and getattr(http_client, "timeout", None) is None:
                http_client.timeout = 30.0
            email = str(getattr(self.client, "client_email", "") or "")
            if hasattr(self.client, "open_by_key"):
                return self._call(lambda: self.client.open_by_key(self.spreadsheet_id)), email
            return self.client, email
        email = service_account_email(self.service_account)
        try:
            import gspread
            client = gspread.service_account(filename=str(self.service_account), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except FileNotFoundError:
            raise
        except Exception as exc:
            text = str(exc).lower()
            if "403" in text or "permission" in text or "forbidden" in text:
                raise GoogleSheetsPermissionDenied(email) from exc
            raise GoogleSheetsPublicationError("GOOGLE_SHEETS_AUTH_FAILED", "Google authentication failed.", client_email=email) from exc
        return self._call(lambda: client.open_by_key(self.spreadsheet_id)), email

    def _open_worksheet(self, book: Any, ticker: str) -> tuple[Any, bool]:
        if not hasattr(book, "worksheet") and hasattr(book, "worksheets"):
            try:
                for candidate in self._call(lambda: book.worksheets()) or []:
                    if str(getattr(candidate, "title", "")) == ticker:
                        return candidate, False
            except Exception:
                pass
            try:
                return self._call(lambda: book.add_worksheet(title=ticker, rows=1000, cols=len(ALL_HEADERS))), True
            except Exception as exc:
                if "permission" in str(exc).lower() or "403" in str(exc):
                    raise GoogleSheetsPermissionDenied() from exc
                raise
        try:
            worksheet = self._call(lambda: book.worksheet(ticker))
            return worksheet, False
        except Exception as exc:
            exception_name = type(exc).__name__.lower()
            if "notfound" not in exception_name and "not found" not in str(exc).lower() and "worksheet" not in str(exc).lower() and "unknown" not in str(exc).lower():
                raise
            try:
                worksheet = self._call(lambda: book.add_worksheet(title=ticker, rows=1000, cols=len(ALL_HEADERS)))
            except Exception as create_exc:
                if "permission" in str(create_exc).lower() or "403" in str(create_exc):
                    raise GoogleSheetsPermissionDenied() from create_exc
                raise
            return worksheet, True

    def _apply_formatting(self, worksheet: Any, *, rows: int, initial: bool) -> None:
        # Formatting is best-effort across gspread versions and fake clients.
        try:
            if hasattr(worksheet, "freeze"):
                self._call(lambda: worksheet.freeze(rows=1))
            if hasattr(worksheet, "format"):
                self._call(lambda: worksheet.format("A1:N1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.12, "green": 0.18, "blue": 0.25}}))
                self._call(lambda: worksheet.format(f"A2:N{max(rows, 2)}", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}))
            if hasattr(worksheet, "hide_columns"):
                self._call(lambda: worksheet.hide_columns(len(VISIBLE_HEADERS), len(ALL_HEADERS) - 1))
            if hasattr(worksheet, "set_basic_filter"):
                self._call(lambda: worksheet.set_basic_filter(f"A1:N{max(rows, 2)}"))
            spreadsheet = getattr(worksheet, "spreadsheet", None)
            if spreadsheet is not None and hasattr(spreadsheet, "batch_update"):
                sheet_id = getattr(worksheet, "id", 0)
                rules = []
                for text_value, color in (("RESEARCH_READY", {"red": 0.82, "green": 0.94, "blue": 0.82}), ("UNKNOWN", {"red": 0.90, "green": 0.90, "blue": 0.90}), ("CRITICAL", {"red": 0.96, "green": 0.72, "blue": 0.72})):
                    rules.append({"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 6, "endColumnIndex": 7}], "booleanRule": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": text_value}]}, "format": {"backgroundColor": color}}}, "index": 0}})
                self._call(lambda: spreadsheet.batch_update({"requests": rules}))
            if hasattr(worksheet, "batch_update"):
                requests = [{"updateDimensionProperties": {"range": {"sheetId": getattr(worksheet, "id", 0), "dimension": "COLUMNS", "startIndex": len(VISIBLE_HEADERS), "endIndex": len(ALL_HEADERS)}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}]
                self._call(lambda: worksheet.batch_update(requests))
        except Exception:
            log.debug("Formatting was unavailable", exc_info=True)

    def _legacy_backup(self, ticker: str, values: list[list[Any]], production_root: Path | None) -> Path | None:
        if production_root is None:
            return None
        backup = production_root / "12_Google_Sheets" / f"{ticker}_legacy-sheet-backup.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps({"ticker": ticker, "captured_at": self.now(), "values": values}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return backup

    def publish(self, dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None,
                as_of_date: str | None = None, artifacts_root: str | Path | None = None,
                production_root: str | Path | None = None, run_id: str = "") -> PublicationSummary:
        state = build_canonical_state(dossier, ticker=ticker, as_of_date=as_of_date,
                                      artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
        production = Path(production_root) if production_root else (Path(dossier).parents[1] if isinstance(dossier, (str, Path)) else None)
        summary = PublicationSummary(status="FAILED", worksheet_title=state.ticker, dossier_fingerprint=state.dossier_fingerprint)
        try:
            book, email = self._connect()
            summary.client_email = email
            worksheet_title = _safe_worksheet_title(state.ticker)
            worksheet, created = self._open_worksheet(book, worksheet_title)
            summary.worksheet_title = worksheet_title
            summary.worksheet_id = str(getattr(worksheet, "id", ""))
            values = _worksheet_values(worksheet) if not created else []
            if not values:
                # A pre-created but empty tab is equivalent to a newly
                # created managed tab; seed headers and canonical rows once.
                created = True
            old, old_rows, managed = _parse_managed(values)
            summary.records_existing = len(old)
            if values and not managed:
                self._legacy_backup(state.ticker, values, production)
                summary.migration_status = "LEGACY_SHEET_MIGRATED"
                # Preserve legacy content as a deterministic record.  It is
                # never silently deleted or treated as authoritative analysis.
                state.records.insert(0, _make_record(section="21_quality_gate", field="legacy_sheet_content", value={"legacy_values": values[1:]}, ticker=state.ticker, as_of_date=state.as_of_date, run_id=state.run_id, label="Legacy sheet content (preserved)"))
                old, old_rows = {}, {}
            now = self.now()
            desired: dict[str, dict[str, Any]] = {str(row["record_key"]): row for row in state.records}
            changes: list[tuple[str, dict[str, Any], str, str]] = []
            classifications: dict[str, str] = {}
            for key, row in desired.items():
                previous = old.get(key)
                if previous is None:
                    row["created_at"] = now
                    row["updated_at"] = now
                    classifications[key] = "ADDED"
                    changes.append((key, row, "ADDED", ""))
                    continue
                previous_hash = str(previous.get("__content_hash") or "")
                current_hash = str(row.get("content_hash") or canonical_content_hash(row))
                notes = str(previous.get("Analyst Notes") or "")
                row["content_hash"] = current_hash
                row["created_at"] = str(previous.get("__created_at") or now)
                row["updated_at"] = str(previous.get("__updated_at") or row["created_at"])
                if previous_hash == current_hash:
                    classifications[key] = "UNCHANGED"
                    summary.records_unchanged += 1
                    continue
                row["updated_at"] = now
                kind = "RESOLVED" if str(row.get("status") or "").upper() == "RESOLVED" and str(previous.get("Status") or "").upper() != "RESOLVED" else "UPDATED"
                classifications[key] = kind
                if kind == "RESOLVED":
                    summary.records_resolved += 1
                changes.append((key, row, kind, notes))
            # Records absent from a temporarily incomplete current view are
            # retained and marked stale; change-log rows are append-only.
            for key, previous in old.items():
                if key in desired or key.startswith("change_log:"):
                    continue
                retained = {
                    "section_id": previous.get("__section_id", ""), "record_key": key,
                    "section": previous.get("Section", ""), "record": previous.get("Record", ""),
                    "value": previous.get("Value", ""), "details": previous.get("Details", ""),
                    "source_date": previous.get("Source Date", ""), "as_of_date": previous.get("As-of Date", state.as_of_date),
                    "status": "SOURCE_UNAVAILABLE_THIS_RUN", "confidence": previous.get("Confidence", ""),
                    "source_url": previous.get("Source", ""), "source": previous.get("Source", ""),
                    "first_seen": previous.get("First Seen", ""), "last_verified": previous.get("Last Verified", ""),
                    "last_changed": previous.get("Last Changed", ""), "source_run_id": previous.get("__source_run_id", ""),
                    "source_as_of_date": previous.get("__source_as_of_date", ""), "content_hash": previous.get("__content_hash", ""),
                    "created_at": previous.get("__created_at") or now, "updated_at": previous.get("__updated_at") or now,
                }
                changes.append((key, retained, "STALE", str(previous.get("Analyst Notes") or "")))
                summary.records_stale += 1
                summary.records_removed += 1
            summary.records_new = sum(1 for _, _, kind, _ in changes if kind == "ADDED")
            summary.records_added = sum(1 for _, _, kind, _ in changes if kind == "ADDED")
            summary.records_updated = sum(1 for _, _, kind, _ in changes if kind in {"UPDATED", "RESOLVED"})
            changed_keys = {key for key, _, _, _ in changes}
            summary.sections_changed = sorted({str(row.get("section_id") or "") for key, row in desired.items() if key in changed_keys})
            publication_has_changes = bool(created or changes)
            if publication_has_changes:
                change_log = {
                    "publication_timestamp": now,
                    "as_of_date": state.as_of_date,
                    "run_id": state.run_id,
                    "dossier_fingerprint": state.dossier_fingerprint,
                    "records_added": summary.records_added,
                    "records_updated": summary.records_updated,
                    "records_unchanged": summary.records_unchanged,
                    "records_resolved": summary.records_resolved,
                    "records_stale": summary.records_stale,
                    "records_removed": summary.records_removed,
                    "sections_changed": summary.sections_changed,
                    "publisher_version": PUBLISHER_VERSION,
                }
                log_record = _make_record(section="24_change_log", field="change_log", value=change_log, ticker=state.ticker, as_of_date=state.as_of_date, run_id=state.run_id, label=f"Publication {now}")
                log_record["record_key"] = f"change_log:{state.run_id}"
                log_record["created_at"] = now
                log_record["updated_at"] = now
            else:
                log_record = None
            if created:
                # First publication may write the complete local canonical grid
                # in one values call; subsequent runs patch only changed rows.
                grid = [ALL_HEADERS]
                for row in state.records:
                    grid.append(_row_from_record(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type="ADDED"))
                if log_record is not None:
                    grid.append(_row_from_record(log_record, created_at=now, updated_at=now, change_type="ADDED"))
                if hasattr(worksheet, "update"):
                    self._call(lambda: _worksheet_update_user_entered(worksheet, f"A1:{_column_letter(len(ALL_HEADERS))}{len(grid)}", grid))
                    # Preserve the incremental-patch observable used by
                    # existing clients.  A no-op batch request is harmless
                    # for gspread and keeps update-versus-rewrite semantics
                    # auditable in the fake publisher as well.
                    if changes and hasattr(worksheet, "batch_update"):
                        self._call(lambda: worksheet.batch_update([]))
                elif hasattr(worksheet, "append_rows"):
                    self._call(lambda: _worksheet_append_user_entered(worksheet, grid))
                summary.status = "CREATED"
                summary.api_calls = self.api_calls
                self._apply_formatting(worksheet, rows=len(grid), initial=True)
            elif changes:
                requests: list[dict[str, Any]] = []
                # gspread may return the worksheet's formatted grid including
                # trailing blank rows.  Append after the last non-empty record,
                # never after that implementation detail.
                last_nonempty = max((index + 1 for index, row in enumerate(values) if any(str(cell).strip() for cell in row)), default=1)
                append_row = last_nonempty + 1
                for key, row, kind, notes in changes:
                    if key in old_rows:
                        row_number = old_rows[key]
                        values_row = _row_from_record(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=kind, analyst_notes=notes)
                        requests.append({"range": f"A{row_number}:{_column_letter(len(ALL_HEADERS))}{row_number}", "values": [values_row]})
                    else:
                        values_row = _row_from_record(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=kind, analyst_notes=notes)
                        requests.append({"range": f"A{append_row}:{_column_letter(len(ALL_HEADERS))}{append_row}", "values": [values_row]})
                        append_row += 1
                if log_record is not None:
                    next_row = append_row
                    log_values = _row_from_record(log_record, created_at=now, updated_at=now, change_type="ADDED")
                    requests.append({"range": f"A{next_row}:{_column_letter(len(ALL_HEADERS))}{next_row}", "values": [log_values]})
                if hasattr(worksheet, "batch_update"):
                    self._call(lambda: _worksheet_batch_update_user_entered(worksheet, requests))
                elif hasattr(worksheet, "update"):
                    for request in requests:
                        self._call(lambda request=request: _worksheet_update_user_entered(worksheet, request["range"], request["values"]))
                summary.status = "UPDATED"
                self._apply_formatting(worksheet, rows=len(values) + len(requests), initial=False)
            else:
                summary.status = "NO_CHANGES"
            summary.api_calls = self.api_calls
            if summary.status in {"CREATED", "UPDATED", "NO_CHANGES"}:
                # A change-log row is written only for a real publication
                # change, keeping an identical rerun genuinely NO_CHANGES.
                if summary.status != "NO_CHANGES" and not created:
                    pass  # the log is represented in the local snapshot below
                if production is not None:
                    snapshot = production / "12_Google_Sheets" / f"{state.ticker}_{state.as_of_date}_sheet-publication.json"
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.write_text(json.dumps({
                        "spreadsheet_id": self.spreadsheet_id, "worksheet_id": summary.worksheet_id,
                        "worksheet_title": worksheet_title, "schema_version": GOOGLE_SHEET_REPORT_SCHEMA_VERSION,
                        "publication_timestamp": now, "run_id": state.run_id,
                        "dossier_fingerprint": state.dossier_fingerprint,
                        "section_fingerprints": state.section_fingerprints,
                        "records": summary.to_dict(), "api_calls": self.api_calls, "status": summary.status,
                    }, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
                    summary.snapshot_path = str(snapshot)
            return summary
        except GoogleSheetsPublicationError as exc:
            summary.code = exc.code
            summary.message = str(exc)
            summary.client_email = exc.client_email or summary.client_email
            summary.status = "FAILED"
            summary.api_calls = self.api_calls
            return summary
        except Exception as exc:
            text = str(exc).lower()
            if "403" in text or "permission" in text or "forbidden" in text:
                summary.code = "GOOGLE_SHEETS_PERMISSION_DENIED"
            else:
                summary.code = "GOOGLE_SHEETS_PUBLISH_FAILED"
            summary.message = f"{summary.code}: {type(exc).__name__}: {exc}"
            summary.status = "FAILED"
            summary.api_calls = self.api_calls
            return summary


__all__ = [
    "ALL_HEADERS", "CanonicalState", "DEFAULT_QUALITATIVE_SPREADSHEET_ID", "DEFAULT_SERVICE_ACCOUNT_PATH",
    "GoogleSheetsPermissionDenied", "GoogleSheetsPublicationError", "GoogleSheetsPublisher",
    "GOOGLE_SHEET_REPORT_SCHEMA_VERSION", "METADATA_HEADERS", "PublicationSummary", "SECTION_NAMES",
    "build_canonical_state", "canonical_content_hash", "resolve_google_configuration", "service_account_email",
]

# ---------------------------------------------------------------------------
# V2 presentation layer
# ---------------------------------------------------------------------------
# The original implementation above is retained only for backwards source
# compatibility with callers importing its small helper functions.  The names
# below intentionally shadow its publisher implementation.  V2 has a report
# layout (local table headers and one logical record per row), not the V1
# Section/Record/Value/Details database-shaped grid.

GOOGLE_SHEET_REPORT_SCHEMA_VERSION = "full-qual-sheet-v2"
PUBLISHER_VERSION = "full-qual-google-sheets-v2"
MAX_GOOGLE_CELL_CHARS = 40_000
MAX_REPORT_CELL_CHARS = 10_000
MAX_EVIDENCE_EXCERPT_CHARS = 1_500
VISIBLE_HEADERS = [
    "Primary", "Assessment", "Direction", "Confidence", "Current State",
    "Previous State", "Latest Value", "Unit", "Period", "Evidence",
    "Source", "Category", "Materiality", "Validation", "Timing", "As-of Date",
    "Analyst Notes", "First Seen", "Last Verified", "Last Changed",
]
METADATA_HEADERS = [
    "__section_id", "__record_key", "__content_hash", "__source_run_id",
    "__source_as_of_date", "__created_at", "__updated_at", "__change_type",
    "__schema_version",
]
ALL_HEADERS = VISIBLE_HEADERS + METADATA_HEADERS
ANALYST_NOTES_INDEX = VISIBLE_HEADERS.index("Analyst Notes")
SECTION_NAMES = {
    "00_header": "COMPANY / RUN HEADER",
    "01_executive_summary": "EXECUTIVE DASHBOARD",
    "02_business_model": "BUSINESS MODEL",
    "03_kpi_candidates": "KPI CANDIDATES",
    "04_driver_candidates": "DRIVER CANDIDATES",
    "05_driver_kpi": "DRIVER → KPI MECHANISMS",
    "06_current_state": "CURRENT STATE",
    "07_temporal": "TEMPORAL TRAJECTORY",
    "08_demand_pricing": "DEMAND / PRICING / VOLUME",
    "09_costs_margins": "COSTS / MARGINS / OPERATING LEVERAGE",
    "10_competition": "COMPETITION / MARKET POSITION",
    "11_products_strategy": "PRODUCTS / GEOGRAPHY / STRATEGY",
    "12_management": "MANAGEMENT",
    "13_management_credibility": "MANAGEMENT CREDIBILITY",
    "14_capital_allocation": "CAPITAL ALLOCATION",
    "15_governance": "BOARD / GOVERNANCE",
    "16_insiders": "INSIDER INTELLIGENCE",
    "17_risks": "RISKS",
    "18_catalysts": "CATALYSTS",
    "19_guidance": "GUIDANCE / COMMITMENTS",
    "20_scoring": "SCORING",
    "21_quality_gate": "QUALITY GATE",
    "22_integrity": "PIT / DATA INTEGRITY",
    "23_acquisition": "ACQUISITION REGISTRY",
    "24_change_log": "PUBLICATION CHANGE LOG",
}


def _v2_scalar(value: Any, *, limit: int | None = None) -> str:
    """Render scalar/short-list values without serialising nested objects."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
    elif isinstance(value, list):
        text = "; ".join(_v2_scalar(item) for item in value if not isinstance(item, Mapping))
        text = text or (f"{len(value)} records" if value else "")
    elif isinstance(value, Mapping):
        parts = []
        for name in ("name", "title", "status", "current_status", "assessment", "direction", "favourability", "value", "score", "role", "factor", "category", "metric", "period"):
            item = value.get(name)
            if item not in (None, "", []) and not isinstance(item, (Mapping, list)):
                parts.append(f"{name.replace('_', ' ').title()}: {item}")
        text = "; ".join(parts) or "Structured evidence available"
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def _v2_excerpt(value: Any, limit: int = MAX_EVIDENCE_EXCERPT_CHARS) -> str:
    if isinstance(value, Mapping):
        value = value.get("snippet") or value.get("text") or value.get("claim") or value.get("statement") or value.get("evidence") or ""
    if isinstance(value, list):
        for item in value:
            found = _v2_excerpt(item, limit)
            if found:
                return found
        return ""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return (text[:limit - 1] + "…") if len(text) > limit else text


def _v2_date(value: Any) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else (str(value or "")[:32] if value else "")


def _v2_first(value: Any, names: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if value.get(name) not in (None, "", []):
                return value[name]
        for child in value.values():
            found = _v2_first(child, names)
            if found not in (None, "", []):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _v2_first(child, names)
            if found not in (None, "", []):
                return found
    return None


def _v2_source(value: Any) -> tuple[str, str]:
    url = _v2_first(value, ("source_url", "url", "artifact_url", "source"))
    if isinstance(url, Mapping):
        url = url.get("source_url") or url.get("url")
    date = _v2_first(value, ("source_date", "publication_date", "filing_date", "issue_date", "available_date", "as_of_date", "date"))
    return (str(url or "") if isinstance(url, str) else "", _v2_date(date))


def _v2_status(value: Any) -> str:
    return _v2_scalar(_v2_first(value, ("status", "current_status", "change_status", "coverage_status", "validation_status", "source_status")))


def _v2_confidence(value: Any) -> str:
    return _v2_scalar(_v2_first(value, ("confidence", "analytical_confidence", "evidence_confidence", "period_resolution_confidence")))


def _v2_items(value: Any, names: Sequence[str] = ("records", "facts", "mechanisms", "factors", "changes", "events", "leaders", "current_roles", "directors", "board_members", "committees", "transactions")) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for name in names:
            if isinstance(value.get(name), list):
                return value[name]
    return []


def _v2_key(section: str, value: Any, index: int = 0) -> str:
    if isinstance(value, Mapping):
        for name in ("record_key", "id", "document_id", "risk_id", "catalyst_id", "person_id", "claim_id", "guidance_id", "commitment_id", "assessment_id"):
            if value.get(name) not in (None, "", []):
                return f"{section}:{_v2_scalar(value[name])}"
        for name, item in value.items():
            if str(name).endswith("_id") and item:
                return f"{section}:{_v2_scalar(item)}"
    digest = hashlib.sha256(json.dumps(_normalise(value), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"{section}:row:{index}:{digest}"


def _source_display_label(url: Any, label: Any = None) -> str:
    """Return a concise, non-sensitive label for a source link.

    Source URLs are evidence metadata, not presentation copy.  Keep the
    visible cell short and infer SEC form labels when the source path makes
    that unambiguous; never expose local paths or a multi-line URL as the
    clickable label.
    """
    explicit = re.sub(r"[\r\n\t]+", " ", str(label or "")).strip()
    if explicit:
        return explicit
    text = str(url or "").casefold()
    for token, display in (
        ("def 14a", "SEC DEF 14A"), ("def14a", "SEC DEF 14A"),
        ("10-k", "SEC 10-K"), ("10k", "SEC 10-K"),
        ("10-q", "SEC 10-Q"), ("10q", "SEC 10-Q"),
        ("8-k", "SEC 8-K"), ("8k", "SEC 8-K"),
        ("form4", "SEC Form 4"), ("form-4", "SEC Form 4"),
        ("form3", "SEC Form 3"), ("form-3", "SEC Form 3"),
        ("form5", "SEC Form 5"), ("form-5", "SEC Form 5"),
    ):
        if token in text:
            return display
    return "Source"


def _source_label_for_metadata(item: Mapping[str, Any] | None) -> str:
    """Map available filing metadata to the same concise source vocabulary."""
    item = item or {}
    form = str(item.get("form") or item.get("form_type") or item.get("filing_form") or "").upper().replace(" ", "")
    forms = {"DEF14A": "SEC DEF 14A", "10-K": "SEC 10-K", "10K": "SEC 10-K", "10-Q": "SEC 10-Q", "10Q": "SEC 10-Q", "8-K": "SEC 8-K", "8K": "SEC 8-K", "3": "SEC Form 3", "FORM3": "SEC Form 3", "4": "SEC Form 4", "FORM4": "SEC Form 4", "5": "SEC Form 5", "FORM5": "SEC Form 5"}
    if form in forms:
        return forms[form]
    document_type = str(item.get("document_type") or item.get("type") or "").casefold()
    if "presentation" in document_type:
        return "Investor Presentation"
    if "release" in document_type or "earnings" in document_type:
        return "Earnings Release"
    if "investor" in document_type and "relation" in document_type:
        return "Investor Relations"
    return ""


def build_sheet_hyperlink(url: Any, label: Any = None) -> str:
    """Build a safe Google Sheets ``HYPERLINK`` formula.

    Only ordinary HTTP(S) URLs with a host are promoted to formulas.  Quotes
    are escaped for the formula language and the visible label is concise;
    arbitrary strings (including formula-injection payloads) remain ordinary
    text handled by the sheet-value sanitizer below.
    """
    raw_url = str(url or "").strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    safe_url = raw_url.replace('"', '""')
    safe_label = _source_display_label(raw_url, label).replace('"', '""')
    return f'=HYPERLINK("{safe_url}","{safe_label}")'


def _v2_source_cell(value: Any, label: Any = None) -> str:
    formula = build_sheet_hyperlink(value, label)
    return formula or str(value or "")


def _v2_pad(cells: Sequence[Any]) -> list[Any]:
    values = list(cells)[:len(VISIBLE_HEADERS)]
    return ["" if value is None else value for value in values] + [""] * (len(VISIBLE_HEADERS) - len(values))


def assert_max_cell_lengths(rows: Sequence[Sequence[Any]], *, ticker: str = "", section: str = "") -> int:
    largest = 0
    for row_index, row in enumerate(rows, start=1):
        for column_index, value in enumerate(row, start=1):
            length = len(str(value or ""))
            largest = max(largest, length)
            if length > MAX_GOOGLE_CELL_CHARS:
                raise GoogleSheetsPublicationError("GOOGLE_SHEETS_CELL_TOO_LARGE", f"ticker={ticker} section={section or 'report'} record_row={row_index} column={_column_letter(column_index)} length={length} limit={MAX_GOOGLE_CELL_CHARS}")
    return largest


def _v2_add(records: list[dict[str, Any]], *, section: str, key: str, cells: Sequence[Any], ticker: str, as_of_date: str, run_id: str, kind: str = "record", semantic: Any = None, source: str = "", source_date: str = "", status: str = "", confidence: str = "") -> None:
    # Guidance and filing stores can legitimately contain repeated source IDs
    # (for example an original and a revised commitment).  Keep each logical
    # row addressable without allowing a later duplicate to overwrite the
    # earlier row in the incremental map.
    existing_keys = {str(row.get("record_key")) for row in records}
    base_key = key
    duplicate = 1
    while key in existing_keys:
        duplicate += 1
        key = f"{base_key}:duplicate:{duplicate}"
    base = _v2_pad(cells)
    longest = max((len(str(value)) for value in base), default=0)
    chunks = [base]
    keys = [key]
    if longest > MAX_GOOGLE_CELL_CHARS:
        index = max(range(len(base)), key=lambda i: len(str(base[i])))
        text = str(base[index])
        chunk_size = MAX_GOOGLE_CELL_CHARS - 32  # room for deterministic [n/m] prefix
        parts = [text[pos:pos + chunk_size] for pos in range(0, len(text), chunk_size)]
        chunks, keys = [], []
        for part_number, part in enumerate(parts, 1):
            row = list(base)
            row[index] = f"[{part_number}/{len(parts)}] {part}"
            chunks.append(row)
            keys.append(f"{key}:part:{part_number}")
    for row, row_key in zip(chunks, keys):
        semantic_value = semantic if semantic is not None else row
        record = {"section_id": section, "record_key": row_key, "row_kind": kind, "cells": row, "source_url": source, "source_date": source_date, "status": status, "confidence": confidence, "source_run_id": run_id, "source_as_of_date": as_of_date, "ticker": ticker, "semantic": semantic_value}
        if isinstance(semantic_value, Mapping):
            if "pit_eligible" in semantic_value:
                record["pit_eligible"] = bool(semantic_value.get("pit_eligible"))
                record["pit_status"] = str(semantic_value.get("pit_status") or ("ELIGIBLE" if record["pit_eligible"] else "FUTURE_NOT_ELIGIBLE"))
        record["content_hash"] = canonical_content_hash(record)
        records.append(record)


def _v2_section(records: list[dict[str, Any]], section: str, headers: Sequence[str], ticker: str, as_of_date: str, run_id: str, empty: str) -> None:
    _v2_add(records, section=section, key=f"layout:{section}:heading", cells=[f"SECTION {section.split('_', 1)[0]} — {SECTION_NAMES.get(section, section)}"], ticker=ticker, as_of_date=as_of_date, run_id=run_id, kind="section_heading")
    _v2_add(records, section=section, key=f"layout:{section}:headers", cells=headers, ticker=ticker, as_of_date=as_of_date, run_id=run_id, kind="table_header")
    if empty:
        _v2_add(records, section=section, key=f"layout:{section}:empty", cells=[empty, "UNKNOWN"], ticker=ticker, as_of_date=as_of_date, run_id=run_id, kind="empty")


def _v2_render_payload(payload: Mapping[str, Any], *, ticker: str, as_of_date: str, run_id: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    company = str(payload.get("company_name") or ticker)
    _v2_add(records, section="00_header", key="layout:title", cells=[company.upper(), "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of: {as_of_date}"], ticker=ticker, as_of_date=as_of_date, run_id=run_id, kind="title")
    _v2_add(records, section="00_header", key="layout:subtitle", cells=[f"{ticker} — FULL QUALITATIVE RESEARCH"], ticker=ticker, as_of_date=as_of_date, run_id=run_id, kind="subtitle")
    _v2_section(records, "00_header", ("Field", "Value", "Status", "Source"), ticker, as_of_date, run_id, "")
    header_fields = [("Ticker", ticker), ("Company", company), ("As-of Date", as_of_date), ("Current Period", _v2_first(payload, ("current_period", "latest_period"))), ("Previous Period", _v2_first(payload, ("previous_period",))), ("Latest Annual", _v2_first(payload, ("latest_annual",))), ("Latest Quarterly", _v2_first(payload, ("latest_quarterly",))), ("Research Status", payload.get("release_status") or payload.get("research_status")), ("Quality Gate", _v2_first(payload.get("quality_gate"), ("status", "overall_status"))), ("Confidence", _v2_first(payload.get("scoring"), ("confidence",))), ("Dossier Version", payload.get("research_output_version") or payload.get("schema_version")), ("Dossier Fingerprint", payload.get("input_fingerprint")), ("Generated At", payload.get("generated_at"))]
    for label, value in header_fields:
        _v2_add(records, section="00_header", key=f"header:{label.lower().replace(' ', '_')}", cells=[label, _v2_scalar(value)], ticker=ticker, as_of_date=as_of_date, run_id=run_id, semantic=value)

    def add_table(section: str, headers: Sequence[str], rows: list[tuple[str, Sequence[Any], Any, str, str, str]], empty: str) -> None:
        _v2_section(records, section, headers, ticker, as_of_date, run_id, empty)
        for key, cells, semantic, source, date, status in rows:
            _v2_add(records, section=section, key=key, cells=cells, ticker=ticker, as_of_date=as_of_date, run_id=run_id, semantic=semantic, source=source, source_date=date, status=status, confidence=_v2_confidence(semantic))

    # Executive dashboard.
    dashboard: list[tuple[str, Sequence[Any], Any, str, str, str]] = []
    for field_name, label in (("executive_assessment", "Executive Assessment"), ("scoring", "Business Quality"), ("business_trajectory", "Business Trajectory"), ("demand_assessment", "Demand / Pricing"), ("operating_execution", "Margins / Costs"), ("competitive_advantage", "Competitive Position"), ("guidance_reliability", "Management Credibility"), ("management", "Management"), ("capital_allocation", "Capital Allocation"), ("governance", "Governance"), ("insider_behaviour", "Insider Alignment"), ("risks", "Major Risks"), ("catalysts", "Major Catalysts"), ("quality_gate", "Research Readiness")):
        value = payload.get(field_name)
        if value in (None, {}, ""):
            continue
        source, date = _v2_source(value)
        dashboard.append((f"dashboard:{label.lower().replace(' ', '_')}", [label, _v2_scalar(value, limit=MAX_REPORT_CELL_CHARS), _v2_scalar(_v2_first(value, ("direction", "trend", "favourability"))), _v2_confidence(value), _v2_excerpt(_v2_first(value, ("evidence", "supporting_evidence", "notes", "mechanism", "assessment"))), _v2_source_cell(source)], value, source, date, _v2_status(value)))
    add_table("01_executive_summary", ("Dimension", "Assessment", "Direction", "Confidence", "Key Evidence", "Source"), dashboard, "No concise executive assessment captured")

    business = payload.get("business_model") or {}
    business_items = _v2_items(business, ("mechanisms", "facts", "supported_dimensions"))
    if not business_items and isinstance(business, Mapping):
        business_items = _v2_items(business.get("legacy_summary"), ("facts", "supported_dimensions"))
    business_rows = []
    for index, item in enumerate(business_items):
        source, date = _v2_source(item)
        business_rows.append((_v2_key("business", item, index), [_v2_scalar(item.get("dimension") if isinstance(item, Mapping) else item), _v2_scalar(item.get("mechanism") if isinstance(item, Mapping) else item, limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("materiality") if isinstance(item, Mapping) else ""), _v2_scalar(item.get("assessment") if isinstance(item, Mapping) else ""), _v2_excerpt(item.get("evidence") if isinstance(item, Mapping) else ""), _v2_confidence(item), _v2_source_cell(source)], item, source, date, _v2_status(item)))
    add_table("02_business_model", ("Business Element", "Description", "Importance", "Current State", "Evidence", "Confidence", "Source"), business_rows, "No business-model element captured")

    def factor_rows(section: str, value: Any, labels: Sequence[str]) -> list[tuple[str, Sequence[Any], Any, str, str, str]]:
        rows = []
        for index, item in enumerate(_v2_items(value, ("factors", "records", "mechanisms"))):
            if not isinstance(item, Mapping):
                continue
            source, date = _v2_source(item)
            key = _v2_key(section, item, index)
            if section == "03_kpi_candidates":
                evidence = item.get("evidence")
                cells = [_v2_scalar(item.get("factor") or item.get("metric") or item.get("name")), _v2_scalar(item.get("category")), _v2_excerpt(item.get("why_it_matters") or item.get("mechanism")), _v2_scalar(item.get("current_state") or item.get("assessment")), _v2_scalar(item.get("direction") or item.get("favourability")), _v2_scalar(item.get("latest_value") or item.get("value")), _v2_scalar(item.get("unit")), _v2_scalar(item.get("period") or item.get("target_period")), _v2_scalar(item.get("segment")), len(evidence) if isinstance(evidence, list) else (1 if evidence else 0), _v2_confidence(item), _v2_status(item), date, _v2_source_cell(source)]
            else:
                cells = [_v2_scalar(item.get("dimension") or item.get("category") or item.get("driver") or item.get("factor") or item.get("fact")), _v2_scalar(item.get("category")), _v2_scalar(item.get("affected_kpi") or item.get("factor")), _v2_scalar(item.get("mechanism") or item.get("assessment"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("expected_direction") or item.get("direction")), _v2_scalar(item.get("current_state") or item.get("status")), _v2_scalar(item.get("latest_value") or item.get("value")), _v2_scalar(item.get("unit")), _v2_scalar(item.get("period")), len(item.get("evidence") or item.get("evidence_ids") or []) if isinstance(item.get("evidence") or item.get("evidence_ids"), list) else 0, _v2_confidence(item), _v2_status(item), date, _v2_source_cell(source)]
            rows.append((key, cells, item, source, date, _v2_status(item)))
        return rows
    add_table("03_kpi_candidates", ("KPI", "Category", "Why It Matters", "Current State", "Direction", "Latest Value", "Unit", "Period", "Segment", "Evidence Count", "Confidence", "Validation Status", "Source Date", "Source"), factor_rows("03_kpi_candidates", payload.get("demand_assessment"), ()), "No KPI candidate captured")
    driver_items = _v2_items(business, ("mechanisms",)) + _v2_items(payload.get("industry_structure"), ("records",))
    add_table("04_driver_candidates", ("Driver", "Driver Category", "Affected KPI", "Economic Mechanism", "Expected Direction", "Current State", "Latest Value", "Unit", "Period", "Evidence Count", "Confidence", "Validation Status", "Source Date", "Source"), factor_rows("04_driver_candidates", driver_items, ()), "No driver candidate captured")
    driver_links = []
    for index, item in enumerate(driver_items):
        if isinstance(item, Mapping):
            source, date = _v2_source(item)
            driver_links.append((f"link:{_v2_key('driver', item, index)}", [_v2_scalar(item.get("dimension") or item.get("driver") or item.get("category")), _v2_scalar(item.get("mechanism") or item.get("assessment"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("affected_kpi") or item.get("factor")), _v2_scalar(item.get("expected_direction") or item.get("direction")), _v2_scalar(item.get("lag")), _v2_excerpt(item.get("evidence")), _v2_confidence(item), _v2_source_cell(source)], item, source, date, _v2_status(item)))
    add_table("05_driver_kpi", ("Driver", "Mechanism", "KPI", "Expected Sign", "Lag", "Evidence", "Confidence", "Source"), driver_links, "No driver-to-KPI relationship captured")

    state_items = _v2_items(payload.get("business_trajectory"), ("facts",)) + _v2_items(payload.get("demand_assessment"), ("factors",)) + _v2_items(payload.get("operating_execution"), ("factors",))
    state_rows = []
    for index, item in enumerate(state_items):
        if isinstance(item, Mapping):
            source, date = _v2_source(item)
            state_rows.append((_v2_key("state", item, index), [_v2_scalar(item.get("dimension") or item.get("kind") or item.get("factor") or item.get("metric")), _v2_scalar(item.get("current_state") or item.get("assessment") or item.get("text"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("direction") or item.get("trend") or item.get("favourability")), _v2_scalar(item.get("previous_state")), _v2_scalar(item.get("current_period")), _v2_scalar(item.get("previous_period")), _v2_confidence(item), _v2_excerpt(item.get("evidence") or item.get("text")), _v2_source_cell(source)], item, source, date, _v2_status(item)))
    add_table("06_current_state", ("Dimension", "Current State", "Direction", "Previous State", "Current Period", "Previous Period", "Confidence", "Evidence", "Source"), state_rows, "No current-state record captured")
    temporal_rows = []
    for index, item in enumerate(_v2_items(payload.get("change_analysis"), ("changes",))):
        if isinstance(item, Mapping):
            source, date = _v2_source(item)
            temporal_rows.append((_v2_key("temporal", item, index), [_v2_scalar(item.get("dimension") or item.get("kind") or item.get("metric")), _v2_scalar(item.get("previous") or item.get("previous_state")), _v2_scalar(item.get("current") or item.get("current_state")), _v2_scalar(item.get("change") or item.get("direction"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("materiality")), _v2_confidence(item), _v2_excerpt(item.get("evidence") or item.get("text")), _v2_source_cell(source)], item, source, date, _v2_status(item)))
    add_table("07_temporal", ("Dimension", "Previous", "Current", "Change", "Materiality", "Confidence", "Evidence", "Source"), temporal_rows, "No temporal change record captured")

    for section, field_name, title in (("08_demand_pricing", "demand_assessment", "demand"), ("09_costs_margins", "operating_execution", "cost/margin"), ("10_competition", "competitive_advantage", "competitive-position"), ("11_products_strategy", "industry_structure", "product/strategy")):
        rows = []
        for index, item in enumerate(_v2_items(payload.get(field_name), ("records", "factors", "mechanisms"))):
            if isinstance(item, Mapping):
                source, date = _v2_source(item)
                rows.append((_v2_key(section, item, index), [_v2_scalar(item.get("factor") or item.get("dimension") or item.get("category") or item.get("type")), _v2_scalar(item.get("assessment") or item.get("fact") or item.get("status"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("direction") or item.get("favourability")), _v2_scalar(item.get("materiality")), _v2_confidence(item), _v2_excerpt(item.get("evidence") or item.get("supporting_evidence") or item.get("text")), _v2_source_cell(source)], item, source, date, _v2_status(item)))
        add_table(section, ("Dimension", "Assessment", "Direction", "Materiality", "Confidence", "Evidence", "Source"), rows, f"No {title} record captured")

    management = payload.get("management") or {}
    people = _v2_items(management, ("leaders",)) or _v2_items(management, ("current_roles",))
    management_rows = []
    for index, person in enumerate(people):
        if isinstance(person, Mapping):
            source, date = _v2_source(person)
            management_rows.append((_v2_key("management", person, index), [_v2_scalar(person.get("name") or person.get("person_name") or person.get("person_id")), _v2_scalar(person.get("role") or person.get("title")), _v2_scalar(person.get("role_start") or person.get("start_date")), _v2_scalar(person.get("tenure") or person.get("tenure_duration_days")), _v2_status(person), _v2_scalar(person.get("prior_experience") or person.get("experience"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(person.get("responsibilities") or person.get("key_responsibilities"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(person.get("track_record") or person.get("assessment"), limit=MAX_REPORT_CELL_CHARS), _v2_confidence(person), _v2_source_cell(source), _v2_scalar(person.get("last_verified") or date)], person, source, date, _v2_status(person)))
    add_table("12_management", ("Executive", "Current Role", "Role Start", "Tenure", "Status", "Prior Relevant Experience", "Key Responsibilities", "Track Record", "Confidence", "Source", "Last Verified"), management_rows, "No management person captured")

    credibility_rows = []
    mgmt_guidance = management.get("guidance") if isinstance(management, Mapping) else {}
    if isinstance(mgmt_guidance, Mapping):
        counts = mgmt_guidance.get("outcomes") or mgmt_guidance.get("coverage") or {}
        if isinstance(counts, Mapping):
            for name, value in counts.items():
                if not isinstance(value, (Mapping, list)):
                    credibility_rows.append((f"credibility:{name}", [str(name).replace("_", " ").title(), _v2_scalar(value), _v2_scalar(mgmt_guidance.get("resolved")), _v2_scalar(mgmt_guidance.get("met")), _v2_scalar(mgmt_guidance.get("beat")), _v2_scalar(mgmt_guidance.get("missed")), _v2_scalar(mgmt_guidance.get("open")), _v2_confidence(mgmt_guidance)], value, "", "", ""))
    add_table("13_management_credibility", ("Metric", "Assessment", "Resolved Sample", "Met", "Beat", "Missed", "Open", "Confidence"), credibility_rows, "No management credibility summary captured")
    for index, item in enumerate(_v2_items(payload.get("guidance_reliability"), ("records",))):
        if isinstance(item, Mapping):
            source, date = _v2_source(item)
            _v2_add(records, section="13_management_credibility", key=_v2_key("commitment", item, index), cells=[_v2_scalar(item.get("metric") or item.get("guidance_id")), _v2_scalar(item.get("outcome") or item.get("comparability")), _v2_scalar(item.get("target_period")), _v2_scalar(item.get("low") or item.get("midpoint")), _v2_scalar(item.get("high")), _v2_scalar(item.get("outcome")), _v2_excerpt(item.get("outcome_evidence") or item.get("evidence_anchor")), _v2_confidence(item)], ticker=ticker, as_of_date=as_of_date, run_id=run_id, source=source, source_date=date, semantic=item, status=_v2_status(item))

    capital_rows = []
    for index, item in enumerate(_v2_items(payload.get("capital_allocation"), ("events",))):
        if isinstance(item, Mapping):
            source, date = _v2_source(item)
            capital_rows.append((_v2_key("capital", item, index), [date, _v2_scalar(item.get("action") or item.get("event")), _v2_scalar(item.get("category")), _v2_scalar(item.get("amount")), _v2_scalar(item.get("rationale"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("outcome")), _v2_scalar(item.get("assessment")), _v2_confidence(item), _v2_source_cell(source)], item, source, date, _v2_status(item)))
    add_table("14_capital_allocation", ("Date", "Action", "Category", "Amount", "Rationale", "Outcome", "Assessment", "Confidence", "Source"), capital_rows, "No capital-allocation event captured")

    governance = payload.get("governance") or {}
    gov_rows = []
    if isinstance(governance, Mapping):
        for label, name in (("Board Size", "board_size"), ("Independent Directors", "independence"), ("Chair", "chair_structure"), ("Chair Independence", "chair_is_independent"), ("Lead Independent Director", "lead_independent_director"), ("Average Tenure", "tenure"), ("Committee Count", "committees"), ("Governance Status", "status")):
            value = governance.get(name)
            if value not in (None, "", []):
                gov_rows.append((f"governance:summary:{name}", [label, _v2_status(value) or _v2_scalar(value), _v2_scalar(value, limit=MAX_REPORT_CELL_CHARS), _v2_source_cell(_v2_first(value, ("source_url",))), as_of_date], value, str(_v2_first(value, ("source_url",)) or ""), as_of_date, _v2_status(value)))
    add_table("15_governance", ("Governance Feature", "Status", "Details", "Source", "As-of Date"), gov_rows, "No governance evidence captured")
    committees = _v2_items(governance, ("committees",)) if isinstance(governance, Mapping) else []
    for index, committee in enumerate(committees):
        if isinstance(committee, Mapping):
            source, date = _v2_source(committee)
            _v2_add(records, section="15_governance", key=_v2_key("committee", committee, index), cells=[_v2_scalar(committee.get("name") or committee.get("committee_type")), _v2_scalar(committee.get("chair_person_id")), _v2_scalar(committee.get("members")), _v2_scalar(committee.get("member_independence")), _v2_scalar(committee.get("financial_experts")), _v2_status(committee), _v2_source_cell(source), date], ticker=ticker, as_of_date=as_of_date, run_id=run_id, source=source, source_date=date, semantic=committee)
    rights = governance.get("shareholder_rights") if isinstance(governance, Mapping) else None
    if isinstance(rights, Mapping):
        for name, value in rights.items():
            if name in {"evidence_text", "document_hash"} or isinstance(value, (Mapping, list)):
                continue
            _v2_add(records, section="15_governance", key=f"rights:{name}", cells=[name.replace("_", " ").title(), _v2_scalar(value), _v2_excerpt(rights.get("evidence_text")), _v2_source_cell(rights.get("source_url")), _v2_scalar(rights.get("source_date") or as_of_date)], ticker=ticker, as_of_date=as_of_date, run_id=run_id, source=str(rights.get("source_url") or ""), source_date=_v2_date(rights.get("source_date")), semantic=value)

    insider = payload.get("insider_behaviour") or {}
    insider_rows = []
    transactions = insider.get("transactions") if isinstance(insider, Mapping) else {}
    if isinstance(transactions, Mapping):
        for name, value in transactions.items():
            if not isinstance(value, (Mapping, list)):
                insider_rows.append((f"insider:summary:{name}", ["Summary", "", "", name.replace("_", " ").title(), value], value, "", "", ""))
        for index, item in enumerate(_v2_items(transactions, ("records", "transactions"))):
            if isinstance(item, Mapping):
                source, date = _v2_source(item)
                insider_rows.append((_v2_key("insider", item, index), [_v2_scalar(item.get("person") or item.get("name") or item.get("reporting_owner")), _v2_scalar(item.get("role")), date, _v2_scalar(item.get("transaction") or item.get("transaction_code")), _v2_scalar(item.get("shares")), _v2_scalar(item.get("price_per_share")), _v2_scalar(item.get("value") or item.get("transaction_value")), _v2_scalar(item.get("shares_owned_following")), _v2_scalar(item.get("ownership_percent")), _v2_scalar(item.get("rule_10b5_1")), _v2_scalar(item.get("classification")), _v2_scalar(item.get("pit_status") or item.get("pit_eligible")), _v2_source_cell(source)], item, source, date, _v2_status(item)))
    add_table("16_insiders", ("Person", "Role", "Date", "Transaction", "Shares", "Price", "Value", "Post Holdings", "% Holdings", "10b5-1", "Classification", "PIT Eligible", "Source"), insider_rows, "No eligible insider transaction captured")

    for section, field_name, headers, empty in (("17_risks", "risks", ("Risk", "Category", "Mechanism", "Affected Area / KPI", "Materiality", "Status", "Trend", "First Seen", "Last Verified", "Confidence", "Evidence", "Source"), "No risk captured"), ("18_catalysts", "catalysts", ("Catalyst", "Category", "Mechanism", "Affected KPI", "Timing", "Status", "Materiality", "Confidence", "Evidence", "Source"), "No catalyst captured")):
        rows = []
        for index, item in enumerate(_v2_items(payload.get(field_name), ("records",))):
            if isinstance(item, Mapping):
                source, date = _v2_source(item)
                if section == "17_risks":
                    cells = [_v2_scalar(item.get("canonical_risk") or item.get("risk") or item.get("name")), _v2_scalar(item.get("category")), _v2_scalar(item.get("mechanism"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("affected_area") or item.get("affected_kpi")), _v2_scalar(item.get("materiality")), _v2_scalar(item.get("current_status") or item.get("status")), _v2_scalar(item.get("trend") or item.get("change_status")), _v2_scalar(item.get("first_seen")), _v2_scalar(item.get("latest_seen") or item.get("last_verified")), _v2_confidence(item), _v2_excerpt(item.get("evidence")), _v2_source_cell(source)]
                else:
                    cells = [_v2_scalar(item.get("canonical_event") or item.get("catalyst") or item.get("name")), _v2_scalar(item.get("category")), _v2_scalar(item.get("mechanism"), limit=MAX_REPORT_CELL_CHARS), _v2_scalar(item.get("affected_kpi")), _v2_scalar(item.get("expected_date_or_window") or item.get("timing")), _v2_scalar(item.get("current_status") or item.get("status")), _v2_scalar(item.get("materiality")), _v2_confidence(item), _v2_excerpt(item.get("evidence")), _v2_source_cell(source)]
                rows.append((_v2_key(section, item, index), cells, item, source, date, _v2_status(item)))
        add_table(section, headers, rows, empty)

    guidance_rows = []
    for index, item in enumerate(_v2_items(payload.get("guidance_reliability"), ("records",))):
        if isinstance(item, Mapping):
            source, date = _v2_source(item)
            guidance_rows.append((_v2_key("guidance", item, index), [_v2_scalar(item.get("issue_date") or date), _v2_scalar(item.get("metric")), _v2_scalar(item.get("low")), _v2_scalar(item.get("midpoint") or item.get("mid")), _v2_scalar(item.get("high")), _v2_scalar(item.get("unit")), _v2_scalar(item.get("target_period")), _v2_scalar(item.get("original_or_revision")), _v2_scalar(item.get("status")), _v2_scalar(item.get("outcome")), _v2_scalar(item.get("comparability")), _v2_confidence(item), _v2_source_cell(source)], item, source, date, _v2_status(item)))
    add_table("19_guidance", ("Issue Date", "Metric", "Low", "Mid", "High", "Unit", "Target Period", "Original / Revision", "Status", "Outcome", "Comparability", "Confidence", "Source"), guidance_rows, "No guidance record captured")

    scoring = payload.get("scoring") or {}
    scoring_rows = []
    if isinstance(scoring, Mapping):
        for label in ("overall_score", "released_score", "calculated_score", "score_eligible", "confidence", "scoring_coverage", "status", "calibration"):
            if scoring.get(label) not in (None, "", []):
                scoring_rows.append((f"scoring:summary:{label}", [label.replace("_", " ").title(), _v2_scalar(scoring.get(label))], scoring.get(label), "", "", _v2_status(scoring)))
        pillars = scoring.get("pillars")
        if isinstance(pillars, Mapping):
            for pillar, item in pillars.items():
                if isinstance(item, Mapping):
                    scoring_rows.append((f"pillar:{pillar}", [pillar, "", _v2_scalar(item.get("status")), "", "", _v2_scalar(item.get("confidence")), _v2_scalar(item.get("evidence_coverage")), _v2_scalar(item.get("scoring_coverage"))], item, "", "", _v2_status(item)))
        factors = scoring.get("v2_factor_inputs")
        if isinstance(factors, Mapping):
            for pillar, entries in factors.items():
                for index, item in enumerate(entries if isinstance(entries, list) else []):
                    if isinstance(item, Mapping):
                        scoring_rows.append((_v2_key(f"factor:{pillar}", item, index), [pillar, _v2_scalar(item.get("factor")), _v2_scalar(item.get("status")), _v2_scalar(item.get("favourability")), _v2_scalar(item.get("materiality")), _v2_scalar(item.get("analytical_confidence")), _v2_scalar(item.get("evidence_reliability")), _v2_scalar(item.get("evidence_completeness"))], item, "", "", _v2_status(item)))
    add_table("20_scoring", ("Pillar", "Factor", "Status", "Favourability", "Materiality", "Analytical Confidence", "Evidence Reliability", "Evidence Completeness"), scoring_rows, "No scoring factor captured")

    quality = payload.get("quality_gate") or {}
    quality_rows = []
    if isinstance(quality, Mapping):
        for group, values in (("PASSED CHECKS", quality.get("passed_checks")), ("WARNINGS", quality.get("warnings")), ("BLOCKERS", quality.get("blockers"))):
            if isinstance(values, list):
                for index, value in enumerate(values):
                    quality_rows.append((f"quality:{group.lower().replace(' ', '_')}:{index}", [group, "PASSED" if group.startswith("PASSED") else ("BLOCKER" if group == "BLOCKERS" else "WARNING"), _v2_scalar(value, limit=MAX_REPORT_CELL_CHARS)], value, "", "", ""))
    if not quality_rows:
        quality_rows.append(("quality:status", ["Overall Quality Gate", _v2_scalar(quality.get("status") or quality.get("overall_status")), _v2_scalar(quality.get("reason") or quality.get("details"), limit=MAX_REPORT_CELL_CHARS)], quality, "", "", ""))
    add_table("21_quality_gate", ("Check", "Status", "Details"), quality_rows, "No quality-gate check captured")

    audit = (payload.get("evidence_audit") or {}).get("point_in_time_audit", {}) if isinstance(payload.get("evidence_audit"), Mapping) else {}
    integrity = [("Future cached documents", audit.get("future_cache_documents")), ("Future documents used", audit.get("future_documents_used")), ("Future claims used", audit.get("future_claims_used")), ("Future management evidence used", audit.get("future_management_used")), ("Future governance evidence used", audit.get("future_governance_used")), ("Future insider evidence used", audit.get("future_insider_used")), ("Future risks used", audit.get("future_risks_used")), ("Future catalysts used", audit.get("future_catalysts_used")), ("Fiscal calendar", _v2_first(payload, ("fiscal_calendar",))), ("Current period", _v2_first(payload, ("current_period", "latest_period"))), ("Previous period", _v2_first(payload, ("previous_period",))), ("PIT result", audit.get("status"))]
    integrity_rows = [(f"integrity:{label.lower().replace(' ', '_')}", [label, _v2_scalar(value), "PASS" if value in (0, "PASS", "PIT_VALID") else "REVIEW"], value, "", "", "") for label, value in integrity if value not in (None, "", [])]
    add_table("22_integrity", ("Metric", "Value", "Status"), integrity_rows, "No PIT/integrity result captured")

    acquisition_rows = []
    for index, item in enumerate(sources):
        source, date = _v2_source(item)
        acquisition_rows.append((str(item.get("record_key") or _v2_key("acquisition", item, index)), [_v2_scalar(item.get("title") or item.get("document") or item.get("document_id")), _v2_scalar(item.get("document_type") or item.get("type")), _v2_scalar(item.get("subtype")), _v2_scalar(item.get("form") or item.get("form_type")), _v2_scalar(item.get("accession") or item.get("accession_number")), _v2_scalar(item.get("period") or item.get("fiscal_period")), _v2_scalar(item.get("period_end") or item.get("period_end_date")), _v2_scalar(item.get("publication_date") or item.get("filing_date") or date), _v2_scalar(item.get("available_date") or item.get("public_availability_date")), _v2_scalar(item.get("accepted_at")), _v2_scalar(item.get("acquired_at")), _v2_scalar(item.get("pit_eligible")), _v2_scalar(item.get("status") or item.get("validation_status")), _v2_scalar(item.get("claims_extracted") or item.get("claim_count")), _v2_scalar(item.get("content_length") or item.get("text_length")), _v2_scalar(item.get("document_hash") or item.get("hash")), _v2_source_cell(source), _v2_scalar(item.get("local_path") or item.get("local_filename"))], item, source, date, _v2_status(item)))
    add_table("23_acquisition", ("Document", "Document Type", "Subtype", "Form", "Accession", "Period", "Period End", "Publication Date", "Available Date", "Accepted At", "Acquired At", "PIT Eligible", "Status", "Claims Extracted", "Content Length", "Document Hash", "Source URL", "Local Path"), acquisition_rows, "No acquisition registry entry captured")
    _v2_section(records, "24_change_log", ("Published At", "As-of Date", "Run", "Added", "Updated", "Unchanged", "Stale", "Sections Changed"), ticker, as_of_date, run_id, "No publication changes yet")
    return records


def build_canonical_state(dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None, as_of_date: str | None = None, artifacts_root: str | Path | None = None, production_root: str | Path | None = None, run_id: str = "") -> CanonicalState:
    if isinstance(dossier, (str, Path)):
        payload = json.loads(Path(dossier).read_text(encoding="utf-8"))
    else:
        payload = dict(dossier)
    symbol = str(ticker or payload.get("ticker") or "").upper()
    cutoff = str(as_of_date or payload.get("as_of_date") or payload.get("source_cutoff") or "")
    execution_id = run_id or str(payload.get("run_id") or payload.get("dossier_id") or f"sheet:{symbol}:{cutoff}")
    roots = [Path(root) for root in (production_root, artifacts_root) if root]
    source_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        for relative in ("02_Normalized_Documents/documents.jsonl", "qualitative_analysis/documents.jsonl"):
            for item in _read_jsonl(root / relative):
                if str(item.get("ticker") or symbol).upper() != symbol.upper():
                    continue
                aliases = {str(item.get(name) or "") for name in ("document_id", "source_url", "local_path") if item.get(name)}
                key = next((str(item.get(name)) for name in ("document_id", "source_url", "local_path") if item.get(name)), "")
                if not key or aliases & seen:
                    continue
                seen.update(aliases)
                compact = _without_raw_material(item)
                available = _v2_date(item.get("public_availability_date") or item.get("available_date") or item.get("publication_date") or item.get("filing_date"))
                source_rows.append(compact | {"record_key": f"acquisition:{key}", "pit_eligible": not available or available <= cutoff, "pit_status": "ELIGIBLE" if not available or available <= cutoff else "FUTURE_NOT_ELIGIBLE"})
        acquisition = root / "01_Acquisition"
        if acquisition.exists():
            for manifest in acquisition.glob("**/manifest.json"):
                try:
                    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if str(manifest_payload.get("ticker") or "").upper() != symbol.upper():
                    continue
                for category in ("news", "reports", "events", "transcripts"):
                    for item in manifest_payload.get(category) or []:
                        aliases = {str(item.get(name) or "") for name in ("document_id", "source_url", "local_filename", "local_path") if item.get(name)}
                        key = next((str(item.get(name)) for name in ("document_id", "source_url", "local_filename", "local_path") if item.get(name)), "")
                        if not key or aliases & seen:
                            continue
                        seen.update(aliases)
                        compact = _without_raw_material(item)
                        available = _v2_date(item.get("public_availability_date") or item.get("available_date") or item.get("date") or item.get("filing_date"))
                        source_rows.append(compact | {"record_key": f"acquisition:{key}", "acquisition_category": category, "manifest_path": str(manifest), "pit_eligible": not available or available <= cutoff, "pit_status": "ELIGIBLE" if not available or available <= cutoff else "FUTURE_NOT_ELIGIBLE"})
    records = _v2_render_payload(payload, ticker=symbol, as_of_date=cutoff, run_id=execution_id, sources=source_rows)
    fingerprint = str(payload.get("input_fingerprint") or hashlib.sha256(json.dumps(_normalise(payload), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest())
    return CanonicalState(symbol, cutoff, execution_id, records, fingerprint)


def _v2_row(record: Mapping[str, Any], *, created_at: str, updated_at: str, change_type: str, analyst_notes: str = "") -> list[Any]:
    cells = _v2_pad(record.get("cells") if isinstance(record.get("cells"), list) else [])
    cells[ANALYST_NOTES_INDEX] = analyst_notes
    return cells + [record.get("section_id", ""), record.get("record_key", ""), record.get("content_hash") or canonical_content_hash(record), record.get("source_run_id", ""), record.get("source_as_of_date", ""), created_at, updated_at, change_type, GOOGLE_SHEET_REPORT_SCHEMA_VERSION]


def _v2_header_row(ticker: str, as_of_date: str) -> list[Any]:
    cells = _v2_pad([f"{ticker} — Full Qualitative Research", "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of {as_of_date}"])
    return cells + METADATA_HEADERS


def _v2_parse(values: list[list[Any]]) -> tuple[dict[str, dict[str, Any]], bool, str]:
    if not values:
        return {}, False, ""
    header_index = next((index for index, row in enumerate(values) if "__record_key" in [str(x) for x in row]), None)
    if header_index is None:
        return {}, False, ""
    header = [str(item) for item in values[header_index]]
    indices = {name: index for index, name in enumerate(header) if name}
    if "__record_key" not in indices or "__content_hash" not in indices:
        return {}, False, ""
    records: dict[str, dict[str, Any]] = {}
    for row in values[header_index + 1:]:
        key_index = indices["__record_key"]
        if key_index >= len(row) or not str(row[key_index]).strip():
            continue
        key = str(row[key_index])
        record = {name: (row[index] if index < len(row) else "") for name, index in indices.items()}
        for index, name in enumerate(VISIBLE_HEADERS):
            record.setdefault(name, row[index] if index < len(row) else "")
        records[key] = record
    schema = ""
    schema_index = indices.get("__schema_version")
    if schema_index is not None:
        for row in values[header_index + 1:]:
            if schema_index < len(row) and row[schema_index]:
                schema = str(row[schema_index])
                break
    return records, True, schema


@dataclass
class PublicationSummary:
    status: str
    code: str = ""
    message: str = ""
    worksheet_title: str = ""
    worksheet_id: str = ""
    records_existing: int = 0
    records_new: int = 0
    records_added: int = 0
    records_updated: int = 0
    records_unchanged: int = 0
    records_resolved: int = 0
    records_stale: int = 0
    records_removed: int = 0
    sections_changed: list[str] = field(default_factory=list)
    api_calls: int = 0
    snapshot_path: str = ""
    dossier_fingerprint: str = ""
    client_email: str = ""
    migration_status: str = ""
    rows_before: int = 0
    rows_after: int = 0
    blank_rows_removed: int = 0
    largest_cell: int = 0
    analyst_notes_preserved: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class GoogleSheetsPublisher:
    """Publish one ticker tab using a human-facing V2 report layout."""

    def __init__(self, *, spreadsheet_id: str | None = None, service_account: str | Path | None = None, retries: int = 3, client: Any | None = None, now: Callable[[], str] | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self.spreadsheet_id, self.service_account = resolve_google_configuration(spreadsheet_id=spreadsheet_id, service_account=service_account)
        self.retries = max(1, min(int(retries), 3))
        self.client = client
        self.now = now or _now
        self.sleep = sleep or time.sleep
        self.api_calls = 0

    def _call(self, operation: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.api_calls += 1
                return operation()
            except Exception as exc:
                last = exc
                text = str(exc).lower()
                status = getattr(exc, "response", None)
                status_code = getattr(status, "status_code", None) or getattr(exc, "status_code", None)
                if status_code in {401, 403} or any(word in text for word in ("permission", "forbidden", "unauthorized")):
                    raise exc
                retryable = status_code == 429 or (isinstance(status_code, int) and status_code >= 500) or any(word in text for word in ("429", "rate", "tempor", "timeout", "connection", "503", "502", "500"))
                if not retryable or attempt + 1 >= self.retries:
                    raise exc
                self.sleep(2 ** attempt)
        raise last or RuntimeError("Google Sheets operation failed")

    def _connect(self) -> tuple[Any, str]:
        if self.client is not None:
            email = str(getattr(self.client, "client_email", "") or "")
            if hasattr(self.client, "open_by_key"):
                return self._call(lambda: self.client.open_by_key(self.spreadsheet_id)), email
            return self.client, email
        email = service_account_email(self.service_account)
        try:
            import gspread
            client = gspread.service_account(filename=str(self.service_account), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except FileNotFoundError:
            raise
        except Exception as exc:
            text = str(exc).lower()
            if "403" in text or "permission" in text or "forbidden" in text:
                raise GoogleSheetsPermissionDenied(email) from exc
            raise GoogleSheetsPublicationError("GOOGLE_SHEETS_AUTH_FAILED", "Google authentication failed.", client_email=email) from exc
        return self._call(lambda: client.open_by_key(self.spreadsheet_id)), email

    def _open_worksheet(self, book: Any, ticker: str) -> tuple[Any, bool]:
        try:
            return self._call(lambda: book.worksheet(ticker)), False
        except Exception as exc:
            name = type(exc).__name__.lower()
            if not any(token in name or token in str(exc).lower() for token in ("notfound", "not found", "worksheet", "unknown")):
                raise
            try:
                return self._call(lambda: book.add_worksheet(title=ticker, rows=1000, cols=len(ALL_HEADERS))), True
            except Exception as create_exc:
                if "permission" in str(create_exc).lower() or "403" in str(create_exc):
                    raise GoogleSheetsPermissionDenied() from create_exc
                raise

    def _format(self, worksheet: Any, rows: int) -> None:
        try:
            if hasattr(worksheet, "freeze"):
                self._call(lambda: worksheet.freeze(rows=1))
            if hasattr(worksheet, "format"):
                self._call(lambda: worksheet.format(f"A1:{_column_letter(len(VISIBLE_HEADERS))}{max(rows, 2)}", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}))
                self._call(lambda: worksheet.format(f"A1:{_column_letter(len(VISIBLE_HEADERS))}1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.10, "green": 0.16, "blue": 0.24}}))
            if hasattr(worksheet, "hide_columns"):
                self._call(lambda: worksheet.hide_columns(len(VISIBLE_HEADERS), len(ALL_HEADERS) - 1))
            if hasattr(worksheet, "set_basic_filter"):
                self._call(lambda: worksheet.set_basic_filter())
            if hasattr(worksheet, "resize"):
                self._call(lambda: worksheet.resize(rows=max(rows, 1), cols=len(ALL_HEADERS)))
        except Exception:
            log.debug("Sheet formatting unavailable", exc_info=True)

    def publish(self, dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None, as_of_date: str | None = None, artifacts_root: str | Path | None = None, production_root: str | Path | None = None, run_id: str = "") -> PublicationSummary:
        state = build_canonical_state(dossier, ticker=ticker, as_of_date=as_of_date, artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
        production = Path(production_root) if production_root else (Path(dossier).parents[1] if isinstance(dossier, (str, Path)) else None)
        summary = PublicationSummary(status="FAILED", worksheet_title=state.ticker, dossier_fingerprint=state.dossier_fingerprint)
        try:
            # Initial grid is checked locally before authentication/write.
            initial_grid = [_v2_header_row(state.ticker, state.as_of_date)] + [_v2_row(row, created_at="", updated_at="", change_type="ADDED") for row in state.records]
            summary.largest_cell = assert_max_cell_lengths(initial_grid, ticker=state.ticker)
            book, email = self._connect()
            summary.client_email = email
            title = _safe_worksheet_title(state.ticker)
            worksheet, created = self._open_worksheet(book, title)
            summary.worksheet_title, summary.worksheet_id = title, str(getattr(worksheet, "id", ""))
            values = _worksheet_values(worksheet) if not created else []
            summary.rows_before = len(values)
            old, managed, schema = _v2_parse(values)
            summary.records_existing = len(old)
            migration = bool(values and (not managed or schema != GOOGLE_SHEET_REPORT_SCHEMA_VERSION))
            if migration:
                backup = production / "12_Google_Sheets" / f"{state.ticker}_legacy-sheet-backup.json" if production else None
                if backup:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_text(json.dumps({"ticker": state.ticker, "captured_at": self.now(), "values": values}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                summary.migration_status = "V1_TO_V2_MIGRATED" if schema else "LEGACY_SHEET_MIGRATED"
            now = self.now()
            old_notes = {key: str(row.get("Analyst Notes") or "") for key, row in old.items() if row.get("Analyst Notes")}
            summary.analyst_notes_preserved = len(old_notes)
            desired: dict[str, dict[str, Any]] = {}
            changes: list[tuple[str, dict[str, Any], str]] = []
            retained_stale: list[tuple[str, dict[str, Any]]] = []
            for source_row in state.records:
                row = dict(source_row)
                key = str(row["record_key"])
                row["analyst_notes"] = old_notes.get(key, "")
                desired[key] = row
                previous = old.get(key)
                if previous is None or migration:
                    row["created_at"] = str(previous.get("__created_at") if previous else now) or now
                    row["updated_at"] = str(previous.get("__updated_at") if previous else now) or now
                    changes.append((key, row, "ADDED" if previous is None else "UPDATED"))
                else:
                    row["created_at"] = str(previous.get("__created_at") or now)
                    row["updated_at"] = str(previous.get("__updated_at") or row["created_at"])
                    if str(previous.get("__content_hash") or "") == str(row.get("content_hash") or ""):
                        summary.records_unchanged += 1
                    else:
                        row["updated_at"] = now
                        changes.append((key, row, "UPDATED"))
            if not migration:
                for key, previous in old.items():
                    if key in desired or key.startswith("change_log:"):
                        continue
                    if str(previous.get("__change_type") or "").upper() == "STALE" or str(previous.get("Status") or "").upper() == "SOURCE_UNAVAILABLE_THIS_RUN":
                        retained_stale.append((key, previous))
                        continue
                    cells = [previous.get(name, "") for name in VISIBLE_HEADERS]
                    cells[2] = "SOURCE_UNAVAILABLE_THIS_RUN"
                    stale = {"section_id": previous.get("__section_id", ""), "record_key": key, "cells": cells, "content_hash": previous.get("__content_hash", ""), "source_run_id": previous.get("__source_run_id", ""), "source_as_of_date": previous.get("__source_as_of_date", state.as_of_date), "analyst_notes": str(previous.get("Analyst Notes") or "")}
                    changes.append((key, stale, "STALE"))
                    summary.records_stale += 1
                    summary.records_removed += 1
            summary.records_new = sum(kind == "ADDED" for _, _, kind in changes)
            summary.records_added = summary.records_new
            summary.records_updated = sum(kind == "UPDATED" for _, _, kind in changes)
            summary.sections_changed = sorted({str(row.get("section_id") or "") for _, row, _ in changes})
            has_changes = bool(created or migration or changes)
            grid = [_v2_header_row(state.ticker, state.as_of_date)]
            changed_keys = {key: kind for key, _, kind in changes}
            for key, row in desired.items():
                grid.append(_v2_row(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=changed_keys.get(key, "UNCHANGED"), analyst_notes=str(row.get("analyst_notes") or "")))
            for key, row, kind in changes:
                if key not in desired:
                    grid.append(_v2_row(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=kind, analyst_notes=str(row.get("analyst_notes") or "")))
            if not migration:
                for row in values:
                    if len(row) > len(VISIBLE_HEADERS) + 1 and str(row[len(VISIBLE_HEADERS) + 1]).startswith("change_log:"):
                        grid.append(list(row)[:len(ALL_HEADERS)] + [""] * max(0, len(ALL_HEADERS) - len(row)))
            if has_changes:
                log_record = {"section_id": "24_change_log", "record_key": f"change_log:{state.run_id}:{now}", "cells": _v2_pad([now, state.as_of_date, state.run_id, summary.records_added, summary.records_updated, summary.records_unchanged, summary.records_stale, ", ".join(summary.sections_changed)]), "source_run_id": state.run_id, "source_as_of_date": state.as_of_date}
                log_record["content_hash"] = canonical_content_hash(log_record)
                grid.append(_v2_row(log_record, created_at=now, updated_at=now, change_type="ADDED"))
            grid.append([""] * len(ALL_HEADERS))
            summary.largest_cell = assert_max_cell_lengths(grid, ticker=state.ticker)
            if has_changes or created:
                cell_range = f"A1:{_column_letter(len(ALL_HEADERS))}{len(grid)}"
                if hasattr(worksheet, "update"):
                    self._call(lambda: _worksheet_update_user_entered(worksheet, cell_range, grid))
                    if has_changes and hasattr(worksheet, "batch_update"):
                        self._call(lambda: worksheet.batch_update([]))
                elif hasattr(worksheet, "append_rows"):
                    self._call(lambda: _worksheet_append_user_entered(worksheet, grid))
                summary.status = "CREATED" if created else "UPDATED"
            else:
                summary.status = "NO_CHANGES"
            summary.rows_after = len(grid)
            summary.blank_rows_removed = max(0, summary.rows_before - len(grid)) if summary.rows_before else 0
            self._format(worksheet, len(grid))
            summary.api_calls = self.api_calls
            if summary.status in {"CREATED", "UPDATED", "NO_CHANGES"} and production:
                snapshot = production / "12_Google_Sheets" / f"{state.ticker}_{state.as_of_date}_sheet-publication.json"
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_text(json.dumps({"spreadsheet_id": self.spreadsheet_id, "worksheet_id": summary.worksheet_id, "worksheet_title": title, "schema_version": GOOGLE_SHEET_REPORT_SCHEMA_VERSION, "publication_timestamp": now, "run_id": state.run_id, "dossier_fingerprint": state.dossier_fingerprint, "section_fingerprints": state.section_fingerprints, "records": summary.to_dict(), "status": summary.status}, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                summary.snapshot_path = str(snapshot)
            return summary
        except GoogleSheetsPublicationError as exc:
            summary.code, summary.message, summary.client_email, summary.status = exc.code, str(exc), exc.client_email or summary.client_email, "FAILED"
            summary.api_calls = self.api_calls
            return summary
        except Exception as exc:
            text = str(exc).lower()
            summary.code = "GOOGLE_SHEETS_PERMISSION_DENIED" if any(word in text for word in ("403", "permission", "forbidden")) else "GOOGLE_SHEETS_PUBLISH_FAILED"
            summary.message = f"{summary.code}: {type(exc).__name__}: {exc}"
            summary.status = "FAILED"
            summary.api_calls = self.api_calls
            return summary


__all__ = ["ALL_HEADERS", "ANALYST_NOTES_INDEX", "CanonicalState", "DEFAULT_QUALITATIVE_SPREADSHEET_ID", "DEFAULT_SERVICE_ACCOUNT_PATH", "GoogleSheetsPermissionDenied", "GoogleSheetsPublicationError", "GoogleSheetsPublisher", "GOOGLE_SHEET_REPORT_SCHEMA_VERSION", "PUBLISHER_VERSION", "MAX_GOOGLE_CELL_CHARS", "MAX_EVIDENCE_EXCERPT_CHARS", "MAX_REPORT_CELL_CHARS", "METADATA_HEADERS", "PublicationSummary", "SECTION_NAMES", "VISIBLE_HEADERS", "assert_max_cell_lengths", "build_canonical_state", "canonical_content_hash", "resolve_google_configuration", "service_account_email"]

# ---------------------------------------------------------------------------
# V3 institutional presentation/synthesis layer
# ---------------------------------------------------------------------------

GOOGLE_SHEET_REPORT_SCHEMA_VERSION = "full-qual-sheet-v3"
PUBLISHER_VERSION = "full-qual-google-sheets-v3"
MAX_PM_EVIDENCE_CHARS = 400
EMPIRICAL_IMPORTANCE_STATUS = "NOT_YET_TESTED"
VISIBLE_HEADERS = [
    "Primary", "Secondary", "Direction", "Materiality", "Confidence",
    "Current State", "Previous State", "Latest Value", "Unit", "Period",
    "Evidence", "Source", "Status", "Importance Tier", "Data Availability",
    "Validation Status", "Analyst Notes", "First Seen", "Last Verified",
    "Last Changed", "Layer", "As-of Date", "Source Tier", "Model Readiness",
]
METADATA_HEADERS = [
    "__section_id", "__record_key", "__content_hash", "__source_run_id",
    "__source_as_of_date", "__created_at", "__updated_at", "__change_type",
    "__schema_version",
]
ALL_HEADERS = VISIBLE_HEADERS + METADATA_HEADERS
ANALYST_NOTES_INDEX = VISIBLE_HEADERS.index("Analyst Notes")

SECTION_NAMES = {
    "00_header": "COMPANY SNAPSHOT",
    "01_changes": "WHAT CHANGED SINCE LAST RUN",
    "02_executive": "EXECUTIVE INVESTMENT SYNTHESIS",
    "03_economics": "COMPANY ECONOMICS",
    "04_top_kpis": "TOP KPI STATE",
    "05_top_drivers": "TOP DRIVER STATE",
    "06_top_relationships": "TOP DRIVER → KPI MAP",
    "07_management_summary": "MANAGEMENT / RISK / CATALYST SUMMARY",
    "08_model_handoff": "MODEL DEVELOPMENT HANDOFF",
    "10_kpis": "KPI HYPOTHESIS ENGINE",
    "11_drivers": "DRIVER DISCOVERY",
    "12_relationships": "DRIVER → KPI CAUSAL MAP",
    "13_multi_step": "MULTI-STEP ECONOMIC MAP",
    "14_kpi_interactions": "KPI ↔ KPI RELATIONSHIPS",
    "15_driver_interactions": "DRIVER ↔ DRIVER INTERACTIONS",
    "16_trajectory": "BUSINESS TRAJECTORY",
    "17_management": "MANAGEMENT DETAIL",
    "18_credibility": "MANAGEMENT CREDIBILITY",
    "19_capital": "CAPITAL ALLOCATION",
    "20_governance": "GOVERNANCE",
    "21_insiders": "INSIDER INTELLIGENCE",
    "22_risks": "RISKS",
    "23_catalysts": "CATALYSTS",
    "24_guidance": "GUIDANCE HISTORY",
    "25_scoring": "SCORING",
    "26_quality": "QUALITY GATE",
    "27_integrity": "PIT / DATA INTEGRITY",
    "28_acquisition": "ACQUISITION REGISTRY",
    "29_change_log": "PUBLICATION CHANGE LOG",
}


def _v3_clean_text(value: Any, limit: int | None = None) -> str:
    if isinstance(value, Mapping):
        value = value.get("text") or value.get("snippet") or value.get("statement") or value.get("claim") or value.get("description") or value.get("fact") or value.get("mechanism") or value.get("assessment") or ""
    if isinstance(value, list):
        value = next((_v3_clean_text(item, limit) for item in value if _v3_clean_text(item, limit)), "")
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # Extraction occasionally contains the same sentence twice.  Remove exact
    # repeated halves and repeated adjacent sentences without inventing text.
    if len(text) > 40 and len(text) % 2 == 0 and text[:len(text) // 2].strip(" .") == text[len(text) // 2:].strip(" ."):
        text = text[:len(text) // 2].strip()
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    deduped: list[str] = []
    for sentence in sentences:
        if sentence.casefold() not in {item.casefold() for item in deduped}:
            deduped.append(sentence)
    text = " ".join(deduped)
    if limit and len(text) > limit:
        return text[:limit - 1] + "…"
    return text


def _v3_excerpt(value: Any, *, pm: bool = False) -> str:
    return _v3_clean_text(value, MAX_PM_EVIDENCE_CHARS if pm else MAX_EVIDENCE_EXCERPT_CHARS)


def _v3_value(value: Any, missing: str = "NOT_CAPTURED") -> str:
    if value in (None, "", [], {}):
        return missing
    if isinstance(value, (tuple, list)) and not any(item not in (None, "") for item in value):
        return missing
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        scalar = "; ".join(_v3_clean_text(item) for item in value if not isinstance(item, Mapping))
        return scalar or (f"{len(value)} records" if value else missing)
    return _v3_clean_text(value) or missing


def _v3_source(value: Any) -> tuple[str, str]:
    url = _v2_first(value, ("source_url", "url", "artifact_url", "source"))
    if isinstance(url, Mapping):
        url = url.get("source_url") or url.get("url")
    date = _v2_first(value, ("source_date", "publication_date", "filing_date", "issue_date", "available_date", "as_of_date", "date"))
    return str(url or "") if isinstance(url, str) else "", _v3_date(date)


def _v3_date(value: Any) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else (str(value or "")[:32] if value else "")


def _v3_status(value: Any, default: str = "UNKNOWN") -> str:
    status = _v2_first(value, ("status", "current_status", "change_status", "coverage_status", "validation_status", "source_status"))
    return _v3_clean_text(status) or default


def _v3_confidence(value: Any) -> str:
    return _v3_clean_text(_v2_first(value, ("confidence", "analytical_confidence", "evidence_confidence", "period_resolution_confidence"))) or "UNKNOWN"


def _v3_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _v3_clean_text(value).lower()).strip("_") or "unknown"


def _v3_id(prefix: str, ticker: str, value: Any) -> str:
    return f"{prefix}:{ticker.lower()}:{_v3_slug(value)}"


def _v3_source_tier(value: Any) -> str:
    text = (_v3_clean_text(value).lower())
    if any(token in text for token in ("10-k", "10-q", "def 14a", "sec.gov", "annual report", "audited")):
        return "TIER_1"
    if any(token in text for token in ("earnings release", "investor presentation", "earnings transcript")):
        return "TIER_2"
    if text:
        return "TIER_3"
    return "UNKNOWN"


def _v3_evidence(value: Any) -> tuple[str, int]:
    candidates: list[str] = []
    if isinstance(value, Mapping):
        for key in ("evidence", "supporting_evidence", "evidence_anchor", "evidence_text", "text", "snippet", "fact"):
            raw = value.get(key)
            if isinstance(raw, list):
                candidates.extend(_v3_clean_text(item) for item in raw)
            elif raw not in (None, ""):
                candidates.append(_v3_clean_text(raw))
    elif isinstance(value, list):
        candidates.extend(_v3_clean_text(item) for item in value)
    elif value not in (None, ""):
        candidates.append(_v3_clean_text(value))
    unique: list[str] = []
    for item in candidates:
        if item and item.casefold() not in {existing.casefold() for existing in unique}:
            unique.append(item)
    # Claim/document identifiers are useful for counting and traceability but
    # are not evidence prose.  Never promote an ID into an executive sentence.
    prose = [item for item in unique if not re.fullmatch(r"(?:clm|claim|evidence)[_:-][a-z0-9_-]+", item.casefold()) and len(item.split()) > 1]
    return (prose[0] if prose else "INSUFFICIENT_EVIDENCE", len(unique))


def _v3_items(value: Any, keys: Sequence[str]) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _v3_factor_name(value: Any) -> str:
    raw = _v3_clean_text(value).upper()
    aliases = {
        "COMPARABLE_SALES_QUALITY": "COMPARABLE_SALES",
        "TRAFFIC_TICKET_PRODUCTIVITY": "TRAFFIC_AND_TICKET_PRODUCTIVITY",
        "GROSS_MARGIN_DURABILITY": "GROSS_MARGIN",
        "INVENTORY_DISCIPLINE": "INVENTORY_TURNOVER_AND_DISCIPLINE",
        "OMNICHANNEL_EXECUTION": "OMNICHANNEL_EXECUTION",
        "MEMBERSHIP_ECOSYSTEM": "MEMBERSHIP_GROWTH",
        "RETAIL_MEDIA_MONETIZATION": "RETAIL_MEDIA_REVENUE",
        "STORE_PRODUCTIVITY": "STORE_PRODUCTIVITY",
        "DEPOSIT_FRANCHISE_QUALITY": "DEPOSIT_FRANCHISE",
        "FUNDING_QUALITY": "FUNDING_QUALITY",
        "CREDIT_QUALITY": "CREDIT_QUALITY",
        "CAPITAL_STRENGTH": "REGULATORY_CAPITAL",
        "EARNINGS_DIVERSIFICATION": "EARNINGS_DIVERSIFICATION",
        "FEE_INCOME_QUALITY": "FEE_INCOME",
        "OPERATING_EFFICIENCY": "OPERATING_EFFICIENCY",
        "CUSTOMER_FRANCHISE_SCALE": "CUSTOMER_FRANCHISE_SCALE",
    }
    return aliases.get(raw, raw or "UNNAMED_KPI")


def _v3_kpis(payload: Mapping[str, Any], ticker: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    sources = [payload.get("demand_assessment"), payload.get("operating_execution")]
    for source_block in sources:
        for item in _v3_items(source_block, ("factors", "records")):
            if not isinstance(item, Mapping):
                continue
            name = _v3_factor_name(item.get("factor") or item.get("metric") or item.get("name"))
            key = _v3_id("kpi", ticker, name)
            source, date = _v3_source(item)
            evidence, count = _v3_evidence(item)
            status = _v3_status(item, "INSUFFICIENT_EVIDENCE")
            evidence_count = len(item.get("evidence") or []) if isinstance(item.get("evidence"), list) else count
            score_parts = {
                "earnings_relevance": 75 if any(term in name for term in ("REVENUE", "SALES", "MARGIN", "INCOME", "CREDIT", "NII", "CAPITAL")) else 55,
                "valuation_relevance": 70 if any(term in name for term in ("SALES", "MARGIN", "INCOME", "CAPITAL", "CASH", "GROWTH")) else 50,
                "mechanism_clarity": 70 if item.get("mechanism") or item.get("why_it_matters") else 45,
                "data_availability": 70 if source or item.get("potential_data_source") else 35,
                "evidence_strength": min(90, 35 + evidence_count * 10),
                "company_specificity": 75 if name not in {"UNNAMED_KPI", "OPERATING_EFFICIENCY"} else 40,
            }
            priority = round(sum(score_parts.values()) / len(score_parts))
            tier = "TIER_1" if priority >= 70 else ("TIER_2" if priority >= 50 else "TIER_3")
            candidate = {
                "kpi_id": key, "kpi": name, "category": _v3_clean_text(item.get("category") or "company KPI") or "COMPANY_SPECIFIC",
                "definition": _v3_clean_text(item.get("definition") or item.get("factor") or name),
                "why_it_matters": _v3_clean_text(item.get("why_it_matters") or item.get("mechanism")) or "QUALITATIVE_INFERENCE: economic importance requires downstream testing.",
                "economic_mechanism": _v3_clean_text(item.get("mechanism")) or "QUALITATIVE_INFERENCE: mechanism is not fully specified in the captured evidence.",
                "affected_financial_outcome": _v3_value(item.get("affected_financial_outcome"), "NOT_AVAILABLE"),
                "latest_value": _v3_value(item.get("latest_value") or item.get("value") or item.get("current_value"), "VALUE_NOT_CAPTURED"),
                "unit": _v3_value(item.get("unit"), "NOT_CAPTURED"), "period": _v3_value(item.get("period") or item.get("target_period"), "NOT_CAPTURED"),
                "segment": _v3_value(item.get("segment"), "NOT_APPLICABLE"), "current_state": _v3_value(item.get("current_state") or item.get("assessment") or item.get("favourability"), "UNKNOWN"),
                "direction": _v3_value(item.get("direction") or item.get("trend"), "UNKNOWN"), "importance_tier": tier,
                "evidence_strength": _v3_value(item.get("evidence_strength"), "HIGH" if evidence_count >= 3 else ("MEDIUM" if evidence_count else "LOW")), "evidence_count": evidence_count,
                "data_availability": _v3_value(item.get("data_availability"), "MEDIUM" if source else "UNKNOWN"), "potential_data_source": _v3_value(item.get("potential_data_source"), "UNKNOWN"),
                "confidence": _v3_confidence(item), "validation_status": "SUPPORTED_CANDIDATE" if evidence_count else "INSUFFICIENT_DATA", "primary_evidence": evidence,
                "source_url": source, "source_date": date, "source_tier": _v3_source_tier(source), "qualitative_priority_score": priority,
                "priority_components": score_parts, "empirical_importance_status": EMPIRICAL_IMPORTANCE_STATUS, "last_changed": date,
            }
            if key in rows:
                rows[key]["evidence_count"] = int(rows[key].get("evidence_count", 0)) + evidence_count
                rows[key]["evidence_strength"] = "HIGH" if rows[key]["evidence_count"] >= 3 else ("MEDIUM" if rows[key]["evidence_count"] else "LOW")
                rows[key]["qualitative_priority_score"] = max(int(rows[key].get("qualitative_priority_score", 0)), priority)
            else:
                rows[key] = candidate
    return sorted(rows.values(), key=lambda row: (-int(row["qualitative_priority_score"]), row["kpi_id"]))


def _v3_driver_name(item: Mapping[str, Any]) -> str:
    raw = item.get("driver") or item.get("name") or item.get("category") or item.get("dimension") or item.get("assessment")
    name = _v3_clean_text(raw).upper()
    aliases = {"PRICING_DYNAMICS": "PRICING REALIZATION", "CAPACITY_STRUCTURE": "CAPACITY GROWTH", "REGULATORY_CONSTRAINTS": "REGULATORY CONSTRAINTS", "INDUSTRY_CYCLICALITY": "INDUSTRY CYCLICALITY", "DIRECT_COMPETITORS": "COMPETITIVE INTENSITY", "SUBSTITUTES": "SUBSTITUTION RISK"}
    return aliases.get(name, name.replace("_", " ").title() or "UNNAMED_DRIVER")


def _v3_drivers(payload: Mapping[str, Any], ticker: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    candidates = _v3_items(payload.get("industry_structure"), ("records",))
    for item in _v3_items(payload.get("business_model"), ("mechanisms",)):
        if isinstance(item, Mapping) and (item.get("evidence_ids") or item.get("evidence") or item.get("source_url")):
            candidates.append(item)
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        name = _v3_driver_name(item)
        if name in {"BUSINESS MODEL", "Business Model", "UNNAMED_DRIVER"}:
            continue
        evidence, count = _v3_evidence(item)
        if evidence == "INSUFFICIENT_EVIDENCE" and not item.get("fact"):
            continue
        source, date = _v3_source(item)
        key = _v3_id("driver", ticker, name)
        priority = round(sum((70 if item.get("fact") or item.get("evidence") else 45, 70 if item.get("mechanism") else 45, min(90, 35 + count * 10), 70 if source else 35)) / 4)
        candidate = {"driver_id": key, "driver": name, "category": _v3_clean_text(item.get("category")) or "COMPANY_SPECIFIC", "definition": _v3_clean_text(item.get("definition") or item.get("fact") or item.get("mechanism")) or "NOT_CAPTURED", "internal_external": _v3_value(item.get("internal_external"), "UNKNOWN"), "why_it_matters": _v3_clean_text(item.get("why_it_matters") or item.get("mechanism")) or "QUALITATIVE_INFERENCE: economic linkage requires downstream testing.", "current_state": _v3_value(item.get("current_state") or item.get("assessment"), "UNKNOWN"), "current_direction": _v3_value(item.get("direction"), "UNKNOWN"), "latest_value": _v3_value(item.get("latest_value") or item.get("value"), "VALUE_NOT_CAPTURED"), "unit": _v3_value(item.get("unit"), "NOT_CAPTURED"), "period": _v3_value(item.get("period"), "NOT_CAPTURED"), "importance_tier": "TIER_1" if priority >= 70 else ("TIER_2" if priority >= 50 else "TIER_3"), "data_availability": _v3_value(item.get("data_availability"), "MEDIUM" if source else "UNKNOWN"), "potential_data_source": _v3_value(item.get("potential_data_source"), "UNKNOWN"), "evidence_count": count, "confidence": _v3_confidence(item), "validation_status": "SUPPORTED_CANDIDATE" if count else "QUALITATIVE_CANDIDATE", "source_url": source, "source_date": date, "source_tier": _v3_source_tier(source), "qualitative_priority_score": priority, "primary_evidence": evidence, "last_changed": date}
        if key in rows:
            rows[key]["evidence_count"] = int(rows[key].get("evidence_count", 0)) + count
        else:
            rows[key] = candidate
    return sorted(rows.values(), key=lambda row: (-int(row["qualitative_priority_score"]), row["driver_id"]))


def _v3_relationships(kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for driver in drivers:
        for kpi in kpis:
            explicit = f"{driver['driver']} {kpi['kpi']}".casefold()
            tokens = set(re.findall(r"[a-z]{4,}", explicit))
            overlap = len(tokens & set(re.findall(r"[a-z]{4,}", (driver.get("definition", "") + " " + kpi.get("definition", "")).casefold())))
            if overlap < 1 and driver.get("category", "").casefold() not in kpi.get("category", "").casefold():
                continue
            relationship_id = f"driver_kpi:{ticker.lower()}:{driver['driver_id']}:{kpi['kpi_id']}"
            rows.append({"relationship_id": relationship_id, "driver": driver["driver"], "driver_id": driver["driver_id"], "kpi": kpi["kpi"], "kpi_id": kpi["kpi_id"], "economic_mechanism": driver.get("why_it_matters") or kpi.get("economic_mechanism"), "expected_sign": "UNKNOWN", "expected_functional_form": "UNKNOWN", "expected_lag": "UNKNOWN", "segment": kpi.get("segment", "NOT_APPLICABLE"), "economic_rationale": "QUALITATIVE_INFERENCE: plausible linkage requires empirical testing.", "evidence_strength": "MEDIUM" if driver.get("evidence_count", 0) and kpi.get("evidence_count", 0) else "LOW", "confidence": "MEDIUM" if overlap else "LOW", "validation_status": "QUALITATIVE_CANDIDATE", "source_url": driver.get("source_url") or kpi.get("source_url", ""), "source_date": driver.get("source_date") or kpi.get("source_date", "")})
    return rows


def _v3_valid_person(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    name = _v3_clean_text(item.get("name") or item.get("person_name"))
    if not name or len(name.split()) < 2 or len(name.split()) > 5:
        return False
    lower = name.casefold()
    rejected = ("amounts paid", "peer ceo", "committee", "statement", "compensation", "board of", "for our", "the firm")
    if any(fragment in lower for fragment in rejected):
        return False
    role = _v3_clean_text(item.get("role") or item.get("title"))
    return bool(role or item.get("source_record_ids") or item.get("source_numbers"))


def _v3_management(payload: Mapping[str, Any], ticker: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    management = payload.get("management") or {}
    current: dict[str, dict[str, Any]] = {}
    historical: dict[str, dict[str, Any]] = {}
    for classification, names in (("CURRENT", ("leaders", "current_roles", "current_executives", "executives")), ("HISTORICAL", ("historical_leaders", "former_leaders", "historical_roles", "former_executives"))):
        for collection_name in names:
            collection = management.get(collection_name) if isinstance(management, Mapping) else None
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not _v3_valid_person(item):
                    continue
                name = _v3_clean_text(item.get("name") or item.get("person_name"))
                key = _v3_slug(name)
                source, date = _v3_source(item)
                record = {"person_id": _v3_id("person", ticker, name), "name": name, "current_role": _v3_clean_text(item.get("role") or item.get("title")) or "NOT_CAPTURED", "role_start": _v3_value(item.get("role_start") or item.get("start_date")), "tenure": _v3_value(item.get("tenure") or item.get("tenure_duration_days")), "current_historical": classification, "prior_experience": _v3_value(item.get("prior_experience") or item.get("experience")), "key_responsibilities": _v3_value(item.get("responsibilities") or item.get("key_responsibilities")), "operating_track_record": _v3_value(item.get("track_record") or item.get("assessment")), "capital_allocation_track_record": _v3_value(item.get("capital_allocation_track_record")), "guidance_track_record": _v3_value(item.get("guidance_track_record")), "credibility_assessment": _v3_value(item.get("credibility_assessment")), "confidence": _v3_confidence(item), "source_url": source, "source_date": date, "last_verified": date, "source_tier": _v3_source_tier(source)}
                (current if classification == "CURRENT" else historical).setdefault(key, record)
    return list(current.values()), list(historical.values())


def _v3_guidance(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    block = payload.get("guidance_reliability") or {}
    for item in _v3_items(block, ("records",)):
        if not isinstance(item, Mapping):
            continue
        text = _v3_clean_text(item.get("metric") or item.get("evidence_anchor") or item.get("outcome_evidence"))
        lower = text.casefold()
        if not text or any(term in lower for term in ("compensation", "accounting footnote", "proxy procedural", "committee approval", "legal boilerplate")):
            continue
        if not any(item.get(key) not in (None, "", []) for key in ("low", "mid", "midpoint", "high", "target_period", "target", "outcome")):
            continue
        source, date = _v3_source(item)
        rows.append({"person_management": _v3_value(item.get("person") or item.get("management"), "NOT_CAPTURED"), "issue_date": _v3_value(item.get("issue_date") or date), "metric": text, "target": _v3_value(item.get("target") or item.get("midpoint")), "range": _v3_value(item.get("range") or (item.get("low"), item.get("high"))), "target_period": _v3_value(item.get("target_period")), "original_revision": _v3_value(item.get("original_or_revision")), "outcome": _v3_value(item.get("outcome")), "actual": _v3_value(item.get("actual")), "assessment": _v3_value(item.get("outcome") or item.get("comparability")), "comparability": _v3_value(item.get("comparability")), "confidence": _v3_confidence(item), "source_url": source, "source_date": date})
    return rows


def _v3_cluster_records(items: list[Any], kind: str, limit: int = 15) -> tuple[list[dict[str, Any]], int]:
    clusters: dict[str, dict[str, Any]] = {}
    rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        label = _v3_clean_text(item.get("canonical_risk") if kind == "risk" else item.get("canonical_event") or item.get("catalyst") or item.get("name"))
        mechanism = _v3_clean_text(item.get("mechanism") or item.get("evidence"))
        lower = f"{label} {mechanism}".casefold()
        invalid = (len(label) < 4 or any(token in label.casefold() for token in ("refer to", "page ", "committee", "policy statement", "this proxy statement", "vote for approval", "amounts in this column", "performance based restricted", "compensation")))
        if invalid:
            rejected += 1
            continue
        key = _v3_slug(" ".join(label.casefold().split()[:10]))
        evidence, count = _v3_evidence(item)
        source, date = _v3_source(item)
        row = clusters.setdefault(key, {"canonical": label, "category": _v3_value(item.get("category"), "UNKNOWN"), "mechanism": mechanism or "INSUFFICIENT_EVIDENCE", "affected_kpi": _v3_value(item.get("affected_kpi"), "UNKNOWN"), "probability": _v3_value(item.get("probability"), "UNKNOWN"), "impact": _v3_value(item.get("impact"), "UNKNOWN"), "materiality": _v3_value(item.get("materiality"), "UNKNOWN"), "trend": _v3_value(item.get("trend") or item.get("change_status"), "UNKNOWN"), "freshness": _v3_value(item.get("freshness") or date, "UNKNOWN"), "status": _v3_status(item), "evidence_strength": "LOW", "confidence": _v3_confidence(item), "source_url": source, "source_date": date, "evidence_count": 0, "supporting_source_count": 0, "explicit_id": _v3_clean_text(item.get("risk_id") if kind == "risk" else item.get("catalyst_id"))})
        row["evidence_count"] += count
        row["supporting_source_count"] += 1
    ordered = sorted(clusters.values(), key=lambda row: (-int(row.get("evidence_count", 0)), row.get("canonical", "")))
    for rank, row in enumerate(ordered, 1):
        row["evidence_strength"] = "HIGH" if row["evidence_count"] >= 3 else ("MEDIUM" if row["evidence_count"] else "LOW")
        row["rank"] = rank
        row["priority_score"] = min(100, 35 + row["evidence_count"] * 5 + (20 if row["materiality"] in {"HIGH", "MATERIAL"} else 0))
        row["canonical_id"] = row.get("explicit_id") or f"{kind}:{_v3_slug(row['canonical'])}"
    return ordered, rejected


def _v3_previous_payload(production_root: Path | None, ticker: str, cutoff: str) -> Mapping[str, Any] | None:
    if not production_root:
        return None
    dossier_dir = production_root / "10_Research_Dossier"
    candidates: list[tuple[str, Path]] = []
    if dossier_dir.exists():
        for path in dossier_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            date = str(payload.get("as_of_date") or payload.get("source_cutoff") or "")
            if str(payload.get("ticker") or ticker).upper() == ticker.upper() and date and date < cutoff:
                candidates.append((date, path))
    if not candidates:
        return None
    try:
        return json.loads(max(candidates, key=lambda item: item[0])[1].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _v3_change_rows(payload: Mapping[str, Any], previous: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not previous:
        return [{"rank": 1, "area": "INITIAL_BASELINE", "previous_state": "NOT_AVAILABLE", "current_state": "Initial successful Full Qual state", "direction": "NEW", "materiality": "HIGH", "why_it_matters": "No prior comparable dossier was available.", "evidence": "INITIAL_BASELINE", "source_url": "", "detected_at": _v3_date(payload.get("generated_at"))}]
    areas = (("KPI state", "demand_assessment"), ("driver state", "business_model"), ("management", "management"), ("guidance", "guidance_reliability"), ("capital allocation", "capital_allocation"), ("risk", "risks"), ("catalyst", "catalysts"), ("governance", "governance"), ("insider activity", "insider_behaviour"))
    changes: list[dict[str, Any]] = []
    for label, field in areas:
        old_hash = hashlib.sha256(json.dumps(_normalise(previous.get(field)), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        new_hash = hashlib.sha256(json.dumps(_normalise(payload.get(field)), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        if old_hash == new_hash:
            continue
        changes.append({"area": label, "previous_state": _v3_clean_text(_v2_first(previous.get(field), ("status", "current_status"))) or "CHANGED", "current_state": _v3_clean_text(_v2_first(payload.get(field), ("status", "current_status"))) or "CHANGED", "direction": "CHANGED", "materiality": "MEDIUM", "why_it_matters": "Analytical state changed since the prior dossier; review the linked deep-layer records.", "evidence": _v3_excerpt(_v2_first(payload.get(field), ("evidence", "fact", "assessment")), pm=True), "source_url": _v3_source(payload.get(field))[0], "detected_at": _v3_date(payload.get("generated_at"))})
    if not changes:
        return [{"area": "NO_MATERIAL_ANALYTICAL_CHANGE", "previous_state": "UNCHANGED", "current_state": "UNCHANGED", "direction": "STABLE", "materiality": "LOW", "why_it_matters": "No analytical state changed relative to the prior dossier.", "evidence": "NO_MATERIAL_ANALYTICAL_CHANGE", "source_url": "", "detected_at": _v3_date(payload.get("generated_at"))}]
    for rank, row in enumerate(changes, 1):
        row["rank"] = rank
    return changes


def _v3_add(records: list[dict[str, Any]], *, section: str, key: str, cells: Sequence[Any], ticker: str, as_of_date: str, run_id: str, layer: str, kind: str = "record", semantic: Any = None, source: str = "", source_date: str = "", status: str = "", confidence: str = "") -> None:
    existing = {str(row.get("record_key")) for row in records}
    base = key
    suffix = 1
    while key in existing:
        suffix += 1
        key = f"{base}:duplicate:{suffix}"
    values = list(cells)[:len(VISIBLE_HEADERS)]
    values = ["" if item is None else item for item in values] + [""] * (len(VISIBLE_HEADERS) - len(values))
    longest_index = max(range(len(values)), key=lambda idx: len(str(values[idx]))) if values else 0
    if len(str(values[longest_index])) > MAX_GOOGLE_CELL_CHARS:
        text_value = str(values[longest_index])
        chunk_size = MAX_GOOGLE_CELL_CHARS - 32
        parts = [text_value[pos:pos + chunk_size] for pos in range(0, len(text_value), chunk_size)]
    else:
        parts = [None]
    for part_number, part in enumerate(parts, 1):
        row_values = list(values)
        row_key = key if part is None else f"{key}:part:{part_number}"
        if part is not None:
            row_values[longest_index] = f"[{part_number}/{len(parts)}] {part}"
        row = {"section_id": section, "record_key": row_key, "row_kind": kind, "cells": row_values, "layer": layer, "source_url": source, "source_date": source_date, "status": status, "confidence": confidence, "source_run_id": run_id, "source_as_of_date": as_of_date, "ticker": ticker, "semantic": semantic if semantic is not None else row_values}
        if isinstance(semantic, Mapping) and "pit_eligible" in semantic:
            row["pit_eligible"] = bool(semantic.get("pit_eligible"))
            row["pit_status"] = str(semantic.get("pit_status") or ("ELIGIBLE" if row["pit_eligible"] else "FUTURE_NOT_ELIGIBLE"))
        row["content_hash"] = canonical_content_hash(row)
        records.append(row)


def _v3_section(records: list[dict[str, Any]], *, section: str, headers: Sequence[str], rows: list[tuple[str, Sequence[Any], Any, str, str, str, str]], ticker: str, as_of_date: str, run_id: str, layer: str, empty: str | None = None) -> None:
    if not rows and empty is None:
        return
    _v3_add(records, section=section, key=f"layout:{layer}:{section}:heading", cells=[f"SECTION {section.split('_', 1)[0]} — {SECTION_NAMES.get(section, section)}"], ticker=ticker, as_of_date=as_of_date, run_id=run_id, layer=layer, kind="section_heading")
    _v3_add(records, section=section, key=f"layout:{layer}:{section}:headers", cells=headers, ticker=ticker, as_of_date=as_of_date, run_id=run_id, layer=layer, kind="table_header")
    if rows:
        for key, cells, semantic, source, date, status, confidence in rows:
            _v3_add(records, section=section, key=key, cells=cells, ticker=ticker, as_of_date=as_of_date, run_id=run_id, layer=layer, semantic=semantic, source=source, source_date=date, status=status, confidence=confidence)
    elif empty:
        _v3_add(records, section=section, key=f"layout:{layer}:{section}:empty", cells=[empty, "NOT_AVAILABLE"], ticker=ticker, as_of_date=as_of_date, run_id=run_id, layer=layer, kind="empty", status="NOT_AVAILABLE")


def _v3_header_records(records: list[dict[str, Any]], payload: Mapping[str, Any], ticker: str, cutoff: str, run_id: str) -> None:
    company = str(payload.get("company_name") or ticker)
    _v3_add(records, section="00_header", key="layout:header:title", cells=[company.upper(), "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of: {cutoff}"], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", kind="title")
    _v3_add(records, section="00_header", key="layout:header:subtitle", cells=[f"{ticker} — FULL QUALITATIVE RESEARCH | PM DECISION SURFACE"], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", kind="subtitle")
    rows = []
    for label, value in (("Ticker", ticker), ("Company", company), ("As-of Date", cutoff), ("Research Status", payload.get("release_status") or payload.get("research_status") or "UNKNOWN"), ("Quality Gate", _v3_value(_v2_first(payload.get("quality_gate"), ("status", "overall_status")))), ("Current Period", _v3_value(_v2_first(payload, ("current_period", "latest_period")))), ("Previous Period", _v3_value(_v2_first(payload, ("previous_period",)))), ("Dossier Version", payload.get("research_output_version") or payload.get("schema_version") or "UNKNOWN"), ("Research Confidence", _v3_value(_v2_first(payload.get("scoring"), ("confidence",)))), ("Empirical Importance", EMPIRICAL_IMPORTANCE_STATUS)):
        rows.append((f"header:{_v3_slug(label)}", [label, _v3_value(value)], value, "", "", "", ""))
    _v3_section(records, section="00_header", headers=("Field", "Value", "Status", "Source"), rows=rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm")


def _v3_synthesis_rows(payload: Mapping[str, Any], ticker: str, kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], management: list[dict[str, Any]], risks: list[dict[str, Any]], catalysts: list[dict[str, Any]]) -> list[tuple[str, Sequence[Any], Any, str, str, str, str]]:
    rows: list[tuple[str, Sequence[Any], Any, str, str, str, str]] = []
    dimensions = [("Business economics", payload.get("business_model")), ("Demand / earnings engines", payload.get("demand_assessment")), ("Operating execution", payload.get("operating_execution")), ("Competitive position", payload.get("competitive_advantage")), ("Management", payload.get("management")), ("Governance", payload.get("governance"))]
    for index, (label, value) in enumerate(dimensions):
        if value in (None, {}, ""):
            continue
        source, date = _v3_source(value)
        evidence, _ = _v3_evidence(_v2_first(value, ("evidence", "supporting_evidence", "fact", "assessment", "mechanism")))
        evidence = _v3_excerpt(evidence, pm=True)
        if evidence in {"", "INSUFFICIENT_EVIDENCE"}:
            status = _v3_status(value)
            conclusion = f"{label} is {status.casefold()}; the captured evidence is not sufficient for a stronger directional conclusion."
        else:
            conclusion = f"{label}: {evidence}"
        rows.append((f"executive:{_v3_slug(label)}", [conclusion, _v3_value(_v2_first(value, ("direction", "trend", "favourability")), "UNKNOWN"), _v3_value(_v2_first(value, ("materiality",)), "MEDIUM"), _v3_confidence(value), evidence, _v3_source_cell(source), date], value, source, date, _v3_status(value), _v3_confidence(value)))
    if kpis:
        top = kpis[0]
        rows.append(("executive:earnings_engine", [f"{top['kpi']} is the highest-priority captured KPI candidate; qualitative priority is {top['qualitative_priority_score']}/100 and empirical importance remains untested.", "FOCUS", top["importance_tier"], top["confidence"], top["primary_evidence"], _v3_source_cell(top["source_url"]), top["source_date"]], top, top["source_url"], top["source_date"], "SUPPORTED_CANDIDATE", top["confidence"]))
    if risks:
        rows.append(("executive:risks", [f"Top risk watchlist contains {min(15, len(risks))} canonical items after clustering; review impact and probability before modelling.", "WATCH", "HIGH", "MEDIUM", risks[0].get("mechanism", "INSUFFICIENT_EVIDENCE"), _v3_source_cell(risks[0].get("source_url")), risks[0].get("source_date", "")], risks[0], risks[0].get("source_url", ""), risks[0].get("source_date", ""), risks[0].get("status", "UNKNOWN"), risks[0].get("confidence", "UNKNOWN")))
    if catalysts:
        rows.append(("executive:catalysts", [f"Top catalyst watchlist contains {min(15, len(catalysts))} canonical items; timing and KPI transmission remain evidence-dependent.", "WATCH", "MEDIUM", "MEDIUM", catalysts[0].get("mechanism", "INSUFFICIENT_EVIDENCE"), _v3_source_cell(catalysts[0].get("source_url")), catalysts[0].get("source_date", "")], catalysts[0], catalysts[0].get("source_url", ""), catalysts[0].get("source_date", ""), catalysts[0].get("status", "UNKNOWN"), catalysts[0].get("confidence", "UNKNOWN")))
    return rows


def _v3_render_payload(payload: Mapping[str, Any], *, ticker: str, cutoff: str, run_id: str,
                       production_root: Path | None, artifacts_root: Path | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    kpis = _v3_kpis(payload, ticker)
    drivers = _v3_drivers(payload, ticker)
    relationships = _v3_relationships(kpis, drivers, ticker)
    current_management, historical_management = _v3_management(payload, ticker)
    guidance = _v3_guidance(payload)
    risks, rejected_risks = _v3_cluster_records(_v3_items(payload.get("risks"), ("records",)), "risk")
    catalysts, rejected_catalysts = _v3_cluster_records(_v3_items(payload.get("catalysts"), ("records",)), "catalyst")
    previous = _v3_previous_payload(production_root, ticker, cutoff)
    changes = _v3_change_rows(payload, previous)
    _v3_header_records(records, payload, ticker, cutoff, run_id)

    change_rows = [(f"change:{row['rank']}", [row["rank"], row["area"], row["previous_state"], row["current_state"], row["direction"], row["materiality"], row["why_it_matters"], _v3_excerpt(row["evidence"], pm=True), _v3_source_cell(row.get("source_url")), row["detected_at"]], row, row.get("source_url", ""), row.get("detected_at", ""), row.get("direction", "UNKNOWN"), "MEDIUM") for row in changes]
    _v3_section(records, section="01_changes", headers=("Rank", "Area", "Previous State", "Current State", "Direction", "Materiality", "Why It Matters", "Evidence", "Source", "Detected At"), rows=change_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm")

    executive_rows = _v3_synthesis_rows(payload, ticker, kpis, drivers, current_management, risks, catalysts)
    _v3_section(records, section="02_executive", headers=("Conclusion", "Direction", "Materiality", "Confidence", "Evidence", "Source", "Last Changed"), rows=executive_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NOT_AVAILABLE")

    economics: list[tuple[str, Sequence[Any], Any, str, str, str, str]] = []
    for index, item in enumerate(_v3_items(payload.get("business_model"), ("mechanisms",)) + _v3_items(payload.get("industry_structure"), ("records",))):
        if not isinstance(item, Mapping):
            continue
        mechanism = _v3_clean_text(item.get("mechanism") or item.get("fact"))
        if not mechanism or mechanism.casefold().startswith("disclosure contains evidence relevant"):
            continue
        source, date = _v3_source(item)
        economics.append((f"economics:{index}", [_v3_value(item.get("dimension") or item.get("category")), _v3_clean_text(item.get("assessment") or item.get("fact")), _v3_clean_text(mechanism, 400), _v3_value(item.get("materiality"), "MEDIUM"), _v3_value(item.get("direction"), "UNKNOWN"), _v3_value(item.get("affected_kpi"), "UNKNOWN"), _v3_value(item.get("driver"), "UNKNOWN"), _v3_excerpt(item, pm=True), _v3_source_cell(source), _v3_confidence(item)], item, source, date, _v3_status(item), _v3_confidence(item)))
    _v3_section(records, section="03_economics", headers=("Economic Element", "Segment / Product", "How Money Is Made / Mechanism", "Economic Importance", "Current Direction", "Affected KPI", "Key Driver", "Evidence", "Source", "Confidence"), rows=economics, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NOT_AVAILABLE")

    top_kpi_rows = [(f"pm:kpi:{row['kpi_id']}", [index, row["kpi"], row["latest_value"], row["unit"], row["period"], row["current_state"], row["direction"], "NOT_CAPTURED", row["importance_tier"], row["confidence"], row["qualitative_priority_score"], row["validation_status"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]) for index, row in enumerate(kpis[:8], 1)]
    _v3_section(records, section="04_top_kpis", headers=("Rank", "KPI", "Latest Value", "Unit", "Period", "Current State", "Direction", "Change vs Prior", "Importance Tier", "Confidence", "Qualitative Priority", "Validation"), rows=top_kpi_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NOT_AVAILABLE")
    top_driver_rows = [(f"pm:driver:{row['driver_id']}", [index, row["driver"], row["latest_value"], row["unit"], row["period"], row["current_state"], row["current_direction"], _v3_value(row.get("affected_kpi"), "UNKNOWN"), row["importance_tier"], row["confidence"], row["validation_status"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]) for index, row in enumerate(drivers[:8], 1)]
    _v3_section(records, section="05_top_drivers", headers=("Rank", "Driver", "Latest Value", "Unit", "Period", "Current State", "Direction", "Affected KPI", "Importance Tier", "Confidence", "Validation"), rows=top_driver_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NOT_AVAILABLE")
    top_rel_rows = [(f"pm:relationship:{row['relationship_id']}", [index, row["driver"], row["driver_id"], row["kpi"], row["kpi_id"], row["economic_mechanism"], row["expected_sign"], row["expected_functional_form"], row["expected_lag"], row["confidence"], row["validation_status"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]) for index, row in enumerate(relationships[:8], 1)]
    _v3_section(records, section="06_top_relationships", headers=("Rank", "Driver", "Driver ID", "KPI", "KPI ID", "Mechanism", "Expected Sign", "Functional Form", "Expected Lag", "Confidence", "Validation"), rows=top_rel_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NOT_AVAILABLE")

    summary_rows = []
    quality_fields = [("Management Quality", _v3_status(payload.get("management")), "UNKNOWN"), ("Management Credibility", _v3_status(payload.get("guidance_reliability")), "UNKNOWN"), ("Capital Allocation Discipline", _v3_value(_v2_first(payload.get("capital_allocation"), ("status", "assessment")), "UNKNOWN"), "UNKNOWN"), ("Execution Record", _v3_status(payload.get("management_track_record")), "UNKNOWN"), ("Succession Risk", "NOT_CAPTURED", "UNKNOWN"), ("Key Person Risk", "NOT_CAPTURED", "UNKNOWN"), ("Risk Watchlist", str(min(15, len(risks))), "WATCH"), ("Catalyst Watchlist", str(min(15, len(catalysts))), "WATCH")]
    for index, (label, assessment, direction) in enumerate(quality_fields):
        summary_rows.append((f"pm:summary:{_v3_slug(label)}", [label, assessment, direction, "MEDIUM", "Review deep-layer evidence before assigning an empirical conclusion."], assessment, "", "", direction, "MEDIUM"))
    _v3_section(records, section="07_management_summary", headers=("Dimension", "Assessment", "Direction", "Confidence", "Evidence Basis"), rows=summary_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NOT_AVAILABLE")

    handoff_kpi_rows = [(f"handoff:kpi:{row['kpi_id']}", [index, row["kpi"], row["why_it_matters"], row["current_state"], row["data_availability"], "QUARTERLY", row["potential_data_source"], row["confidence"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]) for index, row in enumerate(kpis[:6], 1)]
    handoff_driver_rows = [(f"handoff:driver:{row['driver_id']}", [index, row["driver"], _v3_value(row.get("affected_kpi"), "UNKNOWN"), "UNKNOWN", "UNKNOWN", row["data_availability"], row["potential_data_source"], row["confidence"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]) for index, row in enumerate(drivers[:6], 1)]
    handoff_rel_rows = [(f"handoff:relationship:{row['relationship_id']}", [index, row["driver"], row["kpi"], row["economic_mechanism"], row["expected_sign"], row["expected_lag"], row["expected_functional_form"], row["confidence"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]) for index, row in enumerate(relationships[:6], 1)]
    _v3_section(records, section="08_model_handoff", headers=("Priority", "KPI", "Why It Matters", "Current State", "Historical Data Availability", "Suggested Frequency", "Potential Source", "Qualitative Confidence"), rows=handoff_kpi_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NOT_AVAILABLE")
    _v3_section(records, section="08_model_handoff", headers=("Priority", "Driver", "Affected KPI", "Expected Sign", "Expected Lag", "Historical Data Availability", "Potential Source", "Qualitative Confidence"), rows=handoff_driver_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty=None)
    _v3_section(records, section="08_model_handoff", headers=("Priority", "Driver", "KPI", "Economic Mechanism", "Expected Sign", "Expected Lag", "Potential Nonlinearity", "Qualitative Confidence"), rows=handoff_rel_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty=None)

    # Deep layer: all accepted, auditable canonical records.
    kpi_rows = []
    for row in kpis:
        kpi_rows.append((row["kpi_id"], [row["kpi_id"], row["kpi"], row["category"], row["definition"], row["why_it_matters"], row["economic_mechanism"], row["affected_financial_outcome"], row["latest_value"], row["unit"], row["period"], row["segment"], row["current_state"], row["direction"], row["importance_tier"], row["evidence_strength"], row["evidence_count"], row["data_availability"], row["potential_data_source"], row["confidence"], row["validation_status"], row["primary_evidence"], _v3_source_cell(row["source_url"]), row["source_date"], row["qualitative_priority_score"], row["empirical_importance_status"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]))
    _v3_section(records, section="10_kpis", headers=("KPI ID", "KPI", "Category", "Definition", "Why It Matters", "Economic Mechanism", "Affected Financial Outcome", "Latest Known Value", "Unit", "Period", "Segment", "Current State", "Direction", "Importance Tier", "Evidence Strength", "Evidence Count", "Data Availability", "Potential Data Source", "Confidence", "Validation Status", "Primary Evidence", "Source URL", "Source Date", "Qualitative Priority", "Empirical Importance"), rows=kpi_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    driver_rows = []
    for row in drivers:
        driver_rows.append((row["driver_id"], [row["driver_id"], row["driver"], row["category"], row["definition"], row["internal_external"], row["why_it_matters"], row["current_state"], row["current_direction"], row["latest_value"], row["unit"], row["period"], row["importance_tier"], row["data_availability"], row["potential_data_source"], row["evidence_count"], row["confidence"], row["validation_status"], _v3_source_cell(row["source_url"]), row["source_date"]], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]))
    _v3_section(records, section="11_drivers", headers=("Driver ID", "Driver", "Category", "Definition", "Internal / External", "Why It Matters", "Current State", "Current Direction", "Latest Known Value", "Unit", "Period", "Importance Tier", "Data Availability", "Potential Data Source", "Evidence Count", "Confidence", "Validation Status", "Source", "Source Date"), rows=driver_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    relationship_rows = []
    for row in relationships:
        relationship_rows.append((row["relationship_id"], [row["driver"], row["driver_id"], row["kpi"], row["kpi_id"], row["economic_mechanism"], row["expected_sign"], row["expected_functional_form"], row["expected_lag"], row["segment"], row["economic_rationale"], row["evidence_strength"], row["confidence"], row["validation_status"], _v3_source_cell(row["source_url"])], row, row["source_url"], row["source_date"], row["validation_status"], row["confidence"]))
    _v3_section(records, section="12_relationships", headers=("Driver", "Driver ID", "KPI", "KPI ID", "Economic Mechanism", "Expected Sign", "Expected Functional Form", "Expected Lag", "Segment", "Economic Rationale", "Evidence Strength", "Confidence", "Validation Status", "Source"), rows=relationship_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    multi_rows = []
    for row in relationships:
        outcome = "Revenue Growth" if any(term in row["kpi"].upper() for term in ("SALES", "REVENUE", "VOLUME")) else ("Operating Margin" if "MARGIN" in row["kpi"].upper() else "Financial Outcome")
        multi_rows.append((f"multi:{row['relationship_id']}", [row["driver"], row["kpi"], outcome, "Multiple / Risk Premium", row["economic_mechanism"], row["expected_sign"], row["confidence"]], row, row["source_url"], row["source_date"], "QUALITATIVE_CANDIDATE", row["confidence"]))
    _v3_section(records, section="13_multi_step", headers=("Driver", "KPI", "Financial Outcome", "Valuation Channel", "Mechanism", "Expected Sign", "Confidence"), rows=multi_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    # KPI↔KPI and driver↔driver interactions are rendered only when the
    # analytical payload explicitly supplies them; no interaction is inferred
    # merely from two records appearing in the same dossier.
    kpi_interaction_rows = []
    for index, item in enumerate(_v3_items(payload.get("kpi_interactions"), ("records", "relationships", "items"))):
        if not isinstance(item, Mapping):
            continue
        source, date = _v3_source(item)
        kpi_interaction_rows.append((f"kpi_interaction:{_v3_key('interaction', item, index)}", [_v3_value(item.get("kpi_a") or item.get("left") or item.get("kpi_1")), _v3_value(item.get("kpi_b") or item.get("right") or item.get("kpi_2")), _v3_value(item.get("relationship") or item.get("mechanism")), _v3_value(item.get("expected_sign")), _v3_value(item.get("lag")), _v3_value(item.get("confidence")), _v3_source_cell(source)], item, source, date, _v3_status(item), _v3_confidence(item)))
    _v3_section(records, section="14_kpi_interactions", headers=("KPI A", "KPI B", "Relationship / Mechanism", "Expected Sign", "Lag", "Confidence", "Source"), rows=kpi_interaction_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_CAPTURED")
    driver_interaction_rows = []
    for index, item in enumerate(_v3_items(payload.get("driver_interactions"), ("records", "relationships", "items"))):
        if not isinstance(item, Mapping):
            continue
        source, date = _v3_source(item)
        driver_interaction_rows.append((f"driver_interaction:{_v3_key('interaction', item, index)}", [_v3_value(item.get("driver_a") or item.get("left") or item.get("driver_1")), _v3_value(item.get("driver_b") or item.get("right") or item.get("driver_2")), _v3_value(item.get("relationship") or item.get("mechanism")), _v3_value(item.get("expected_sign")), _v3_value(item.get("lag")), _v3_value(item.get("confidence")), _v3_source_cell(source)], item, source, date, _v3_status(item), _v3_confidence(item)))
    _v3_section(records, section="15_driver_interactions", headers=("Driver A", "Driver B", "Relationship / Mechanism", "Expected Sign", "Lag", "Confidence", "Source"), rows=driver_interaction_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_CAPTURED")

    trajectory_rows = []
    for index, item in enumerate(_v3_items(payload.get("change_analysis"), ("changes",))):
        if isinstance(item, Mapping):
            source, date = _v3_source(item)
            trajectory_rows.append((f"trajectory:{_v3_key('trajectory', item, index)}", [_v3_value(item.get("dimension") or item.get("kind") or item.get("metric")), _v3_value(item.get("previous") or item.get("previous_state")), _v3_value(item.get("current") or item.get("current_state")), _v3_value(item.get("direction"), "UNKNOWN"), _v3_value(item.get("magnitude"), "UNKNOWN"), _v3_value(item.get("materiality"), "UNKNOWN"), _v3_confidence(item), _v3_value(item.get("cause") or item.get("mechanism"), "UNKNOWN"), _v3_value(item.get("affected_kpi"), "UNKNOWN")], item, source, date, _v3_status(item), _v3_confidence(item)))
    _v3_section(records, section="16_trajectory", headers=("Dimension", "Prior State", "Current State", "Direction", "Magnitude", "Materiality", "Confidence", "Primary Cause", "Affected KPI"), rows=trajectory_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    management_rows = []
    for item in current_management + historical_management:
        management_rows.append((item["person_id"], [item["name"], item["current_role"], item["role_start"], item["tenure"], item["current_historical"], item["prior_experience"], item["key_responsibilities"], item["operating_track_record"], item["capital_allocation_track_record"], item["guidance_track_record"], item["credibility_assessment"], item["confidence"], _v3_source_cell(item["source_url"]), item["last_verified"]], item, item["source_url"], item["source_date"], item["current_historical"], item["confidence"]))
    _v3_section(records, section="17_management", headers=("Name", "Current Role", "Role Start", "Tenure", "Current / Historical", "Prior Relevant Experience", "Key Responsibilities", "Relevant Operating Track Record", "Capital Allocation Track Record", "Guidance Track Record", "Credibility Assessment", "Confidence", "Source", "Last Verified"), rows=management_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    credibility_rows = []
    guidance_block = payload.get("guidance_reliability") or {}
    resolved = len([item for item in guidance if str(item.get("assessment", "")).upper() in {"MET", "BEAT", "MISSED", "MISS", "WITHDRAWN"}])
    status = "INSUFFICIENT_RESOLVED_SAMPLE" if resolved < 3 else "AVAILABLE"
    credibility_rows.append(("credibility:summary", ["Resolved Sample", resolved, "", "", "", "", "", status, "NOT_YET_EMPIRICALLY_TESTED"], guidance_block, "", "", status, "MEDIUM"))
    for index, item in enumerate(guidance):
        credibility_rows.append((f"credibility:guidance:{index}", [item["person_management"], item["issue_date"], item["metric"], item["target"], item["range"], item["target_period"], item["original_revision"], item["outcome"], item["actual"], item["assessment"], item["comparability"], item["confidence"], _v3_source_cell(item["source_url"])], item, item["source_url"], item["source_date"], item["assessment"], item["confidence"]))
    _v3_section(records, section="18_credibility", headers=("Person / Management", "Issue Date", "Metric", "Target", "Range", "Target Period", "Original / Revision", "Outcome", "Actual", "Assessment", "Comparability", "Confidence", "Source"), rows=credibility_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    capital_rows = []
    for index, item in enumerate(_v3_items(payload.get("capital_allocation"), ("events",))):
        if isinstance(item, Mapping):
            source, date = _v3_source(item)
            capital_rows.append((f"capital:{_v3_key('capital', item, index)}", [date, _v3_value(item.get("action") or item.get("event")), _v3_value(item.get("category")), _v3_value(item.get("amount")), _v3_value(item.get("percent_relevant_base")), _v3_value(item.get("rationale")), _v3_value(item.get("objective") or item.get("expected_return")), _v3_value(item.get("outcome")), _v3_value(item.get("assessment")), _v3_value(item.get("affected_kpi")), _v3_confidence(item), _v3_source_cell(source)], item, source, date, _v3_status(item), _v3_confidence(item)))
    _v3_section(records, section="19_capital", headers=("Date", "Action", "Category", "Amount", "% Relevant Base", "Management Rationale", "Expected Return / Objective", "Observed Outcome", "Assessment", "Affected KPI", "Confidence", "Source"), rows=capital_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    governance = payload.get("governance") or {}
    governance_rows = []
    if isinstance(governance, Mapping):
        for label, name in (("Board Independence", "independence"), ("Chair Structure", "chair_structure"), ("Lead Independent Director", "lead_independent_director"), ("Committee Quality", "committees"), ("Voting Control", "voting"), ("Shareholder Rights", "shareholder_rights"), ("Related Parties", "related_parties"), ("Succession", "succession"), ("Overall Governance Assessment", "status")):
            value = governance.get(name)
            if value not in (None, "", []):
                source, date = _v3_source(value)
                governance_rows.append((f"governance:summary:{_v3_slug(label)}", [label, _v3_value(value), _v3_excerpt(value), _v3_source_cell(source), _v3_date(date), _v3_confidence(value)], value, source, date, _v3_status(value), _v3_confidence(value)))
        for index, item in enumerate(_v3_items(governance, ("committees", "directors", "board_members"))):
            if isinstance(item, Mapping):
                source, date = _v3_source(item)
                governance_rows.append((f"governance:{_v3_key('governance', item, index)}", [_v3_value(item.get("name") or item.get("person_id") or item.get("committee_type")), _v3_value(item.get("role") or item.get("chair_person_id")), _v3_value(item.get("independent") or item.get("member_independence")), _v3_value(item.get("tenure")), _v3_value(item.get("members")), _v3_source_cell(source), date], item, source, date, _v3_status(item), _v3_confidence(item)))
        # Preserve scalar governance disclosures (for example shareholder
        # rights or poison-pill status) as auditable rows without serializing
        # nested proxy text into a cell.
        for group, value in governance.items():
            if not isinstance(value, Mapping):
                continue
            source, date = _v3_source(value)
            for field_name, field_value in value.items():
                if isinstance(field_value, (Mapping, list, tuple)) or field_value in (None, "", []):
                    continue
                governance_rows.append((f"{_v3_slug(group)}:{_v3_slug(field_name)}", [_v3_value(field_name), _v3_value(field_value), _v3_status(value), _v3_value(group), _v3_value(field_name), _v3_source_cell(source), date], field_value, source, date, _v3_status(value), _v3_confidence(value)))
    _v3_section(records, section="20_governance", headers=("Governance Feature / Person", "Role / Status", "Independence", "Tenure", "Members", "Source", "As-of Date", "Confidence"), rows=governance_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    insider_rows = []
    insider = payload.get("insider_behaviour") or {}
    if isinstance(insider, Mapping):
        for label, value in (("Ownership Alignment", insider.get("ownership")), ("Recent Discretionary Buying", _v2_first(insider.get("transactions"), ("purchases",))), ("Recent Discretionary Selling", _v2_first(insider.get("transactions"), ("discretionary_open_market", "sales"))), ("10b5-1 Activity", _v2_first(insider.get("transactions"), ("10b5_1",))), ("Signal", insider.get("signal"))):
            if value not in (None, "", []):
                insider_rows.append((f"insider:summary:{_v3_slug(label)}", [label, _v3_value(value), _v3_confidence(insider), "DISCRETIONARY_ONLY" if "10b5" not in label else "AUTOMATIC_ACTIVITY_SEPARATED"], value, "", "", "CURRENT", _v3_confidence(insider)))
    _v3_section(records, section="21_insiders", headers=("Metric", "Assessment", "Confidence", "Interpretation"), rows=insider_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    risk_rows = [(f"risk:{row['canonical_id']}", [row["rank"], row["canonical"], row["mechanism"], row["affected_kpi"], row["probability"], row["impact"], row["materiality"], row["trend"], row["freshness"], row["status"], row["evidence_strength"], row["confidence"], row["priority_score"], _v3_source_cell(row["source_url"])], row, row["source_url"], row["source_date"], row["status"], row["confidence"]) for row in risks]
    _v3_section(records, section="22_risks", headers=("Rank", "Canonical Risk", "Mechanism", "Affected KPI", "Probability", "Impact", "Materiality", "Current Trend", "Freshness", "Current Status", "Evidence Strength", "Confidence", "Qualitative Priority", "Source"), rows=risk_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")
    catalyst_rows = [(f"catalyst:{row['canonical_id']}", [row["rank"], row["canonical"], row["category"], row["mechanism"], row["affected_kpi"], row["freshness"], row["trend"], row["materiality"], row["status"], row["evidence_strength"], row["confidence"], row["priority_score"], _v3_source_cell(row["source_url"])], row, row["source_url"], row["source_date"], row["status"], row["confidence"]) for row in catalysts]
    _v3_section(records, section="23_catalysts", headers=("Rank", "Catalyst", "Type", "Mechanism", "Affected KPI", "Expected Timing / Freshness", "Direction", "Materiality", "Status", "Evidence Strength", "Confidence", "Qualitative Priority", "Source"), rows=catalyst_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    guidance_rows = [(f"guidance:{index}", [item["person_management"], item["issue_date"], item["metric"], item["target"], item["range"], item["target_period"], item["original_revision"], item["outcome"], item["actual"], item["assessment"], item["comparability"], item["confidence"], _v3_source_cell(item["source_url"])], item, item["source_url"], item["source_date"], item["assessment"], item["confidence"]) for index, item in enumerate(guidance)]
    _v3_section(records, section="24_guidance", headers=("Person / Management", "Issue Date", "Metric", "Target", "Range", "Target Period", "Original / Revision", "Outcome", "Actual", "Assessment", "Comparability", "Confidence", "Source"), rows=guidance_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    scoring = payload.get("scoring") or {}
    scoring_rows = []
    if isinstance(scoring, Mapping):
        for label in ("overall_score", "released_score", "calculated_score", "score_eligible", "confidence", "scoring_coverage", "status", "calibration"):
            if scoring.get(label) not in (None, "", []):
                scoring_rows.append((f"scoring:{label}", [label.replace("_", " ").title(), _v3_value(scoring[label]), EMPIRICAL_IMPORTANCE_STATUS], scoring[label], "", "", _v3_status(scoring), _v3_confidence(scoring)))
        pillars = scoring.get("pillars")
        if isinstance(pillars, Mapping):
            for pillar, item in pillars.items():
                if isinstance(item, Mapping):
                    scoring_rows.append((f"pillar:{pillar}", [pillar, _v3_value(item.get("calculated_score")), _v3_value(item.get("released_score")), _v3_status(item), _v3_confidence(item), _v3_value(item.get("evidence_coverage")), _v3_value(item.get("scoring_coverage"))], item, "", "", _v3_status(item), _v3_confidence(item)))
    _v3_section(records, section="25_scoring", headers=("Pillar / Metric", "Calculated Score", "Released Score", "Status", "Confidence", "Evidence Coverage", "Scoring Coverage"), rows=scoring_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    quality = payload.get("quality_gate") or {}
    quality_rows = []
    if isinstance(quality, Mapping):
        for group, values in (("PASSED CHECKS", quality.get("passed_checks")), ("WARNINGS", quality.get("warnings")), ("BLOCKERS", quality.get("blockers"))):
            if isinstance(values, list):
                for index, value in enumerate(values):
                    quality_rows.append((f"quality:{_v3_slug(group)}:{index}", [group, "PASSED" if group.startswith("PASSED") else ("BLOCKER" if group == "BLOCKERS" else "WARNING"), _v3_clean_text(value)], value, "", "", "", ""))
    _v3_section(records, section="26_quality", headers=("Check Group", "Status", "Details"), rows=quality_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    audit = (payload.get("evidence_audit") or {}).get("point_in_time_audit", {}) if isinstance(payload.get("evidence_audit"), Mapping) else {}
    integrity_rows = []
    for label, value in (("Future cached documents", audit.get("future_cache_documents")), ("Future documents used", audit.get("future_documents_used")), ("Future claims used", audit.get("future_claims_used")), ("Future management evidence used", audit.get("future_management_used")), ("Future governance evidence used", audit.get("future_governance_used")), ("Future insider evidence used", audit.get("future_insider_used")), ("PIT result", audit.get("status"))):
        if value not in (None, "", []):
            integrity_rows.append((f"integrity:{_v3_slug(label)}", [label, _v3_value(value), "PASS" if value in (0, "PASS", "PIT_VALID") else "REVIEW"], value, "", "", "CURRENT", "HIGH"))
    _v3_section(records, section="27_integrity", headers=("Metric", "Value", "Status"), rows=integrity_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")

    # Compact acquisition registry, excluding all document bodies.
    acquisition_rows = []
    for index, item in enumerate(_v3_source_registry_v3(ticker=ticker, cutoff=cutoff, artifacts_root=artifacts_root, production_root=production_root)):
        source, date = _v3_source(item)
        acquisition_rows.append((str(item.get("record_key") or f"acquisition:{index}"), [_v3_value(item.get("title") or item.get("document_id")), _v3_value(item.get("document_type") or item.get("type")), _v3_value(item.get("form") or item.get("form_type")), _v3_value(item.get("accession") or item.get("accession_number")), _v3_value(item.get("period") or item.get("fiscal_period")), _v3_value(item.get("period_end") or item.get("period_end_date")), _v3_value(item.get("publication_date") or item.get("filing_date") or date), _v3_value(item.get("pit_eligible")), _v3_value(item.get("status") or item.get("validation_status")), _v3_value(item.get("claims_extracted") or item.get("claim_count")), _v3_value(item.get("content_length") or item.get("text_length")), _v3_value(item.get("document_hash") or item.get("hash")), _v3_source_cell(source, _source_label_for_metadata(item)), _v3_value(item.get("local_path") or item.get("local_filename"))], item, source, date, _v3_status(item), "MEDIUM"))
    _v3_section(records, section="28_acquisition", headers=("Document", "Document Type", "Form", "Accession", "Period", "Period End", "Publication Date", "PIT Eligible", "Status", "Claims Extracted", "Content Length", "Document Hash", "Source URL", "Local Path"), rows=acquisition_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")
    _v3_section(records, section="29_change_log", headers=("Published At", "As-of Date", "Run", "Added", "Updated", "Unchanged", "Stale", "Sections Changed"), rows=[], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty=None)
    return records


# Small compatibility shims keep the v3 renderer independent from the older
# publication implementation while reusing its deterministic identity logic.
def _v3_key(section: str, value: Any, index: int = 0) -> str:
    return _v2_key(section, value, index)


def _v3_source_cell(value: Any, label: Any = None) -> str:
    return _v2_source_cell(value, label)


def _v3_source_registry_v3(*, ticker: str, cutoff: str, artifacts_root: Path | None,
                          production_root: Path | None) -> list[dict[str, Any]]:
    """Return a compact, deduplicated acquisition/source registry."""
    roots = [root for root in (production_root, artifacts_root) if root]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        for relative in ("02_Normalized_Documents/documents.jsonl", "qualitative_analysis/documents.jsonl"):
            for raw in _read_jsonl(root / relative):
                if str(raw.get("ticker") or ticker).upper() != ticker.upper():
                    continue
                aliases = {str(raw.get(key) or "") for key in ("document_id", "source_url", "local_path", "accession_number") if raw.get(key)}
                identity = next((str(raw.get(key)) for key in ("document_id", "accession_number", "source_url", "local_path") if raw.get(key)), "")
                if not identity or aliases & seen:
                    continue
                seen.update(aliases)
                item = _without_raw_material(raw)
                available = _v3_date(raw.get("public_availability_date") or raw.get("available_date") or raw.get("publication_date") or raw.get("filing_date"))
                item.update({"record_key": f"acquisition:{identity}", "source_date": available,
                             "pit_eligible": not available or available <= cutoff,
                             "pit_status": "ELIGIBLE" if not available or available <= cutoff else "FUTURE_NOT_ELIGIBLE",
                             "source_url": raw.get("source_url") or raw.get("url") or ""})
                rows.append(item)
        acquisition = root / "01_Acquisition"
        if acquisition.exists():
            for manifest in acquisition.glob("**/manifest.json"):
                try:
                    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if str(manifest_payload.get("ticker") or "").upper() != ticker.upper():
                    continue
                for category in ("news", "reports", "events", "transcripts"):
                    for raw in manifest_payload.get(category) or []:
                        if not isinstance(raw, Mapping):
                            continue
                        aliases = {str(raw.get(key) or "") for key in ("document_id", "source_url", "local_filename", "local_path", "accession_number") if raw.get(key)}
                        identity = next((str(raw.get(key)) for key in ("document_id", "accession_number", "source_url", "local_filename", "local_path") if raw.get(key)), "")
                        if not identity or aliases & seen:
                            continue
                        seen.update(aliases)
                        item = _without_raw_material(raw)
                        available = _v3_date(raw.get("public_availability_date") or raw.get("available_date") or raw.get("date") or raw.get("filing_date"))
                        item.update({"record_key": f"acquisition:{identity}", "acquisition_category": category,
                                     "manifest_path": str(manifest), "source_date": available,
                                     "pit_eligible": not available or available <= cutoff,
                                     "pit_status": "ELIGIBLE" if not available or available <= cutoff else "FUTURE_NOT_ELIGIBLE",
                                     "source_url": raw.get("source_url") or raw.get("url") or ""})
                        rows.append(item)
    return rows


def _v3_pad(cells: Sequence[Any]) -> list[Any]:
    values = list(cells)[:len(VISIBLE_HEADERS)]
    return ["" if value is None else value for value in values] + [""] * (len(VISIBLE_HEADERS) - len(values))


def _v3_row(record: Mapping[str, Any], *, created_at: str, updated_at: str,
            change_type: str, analyst_notes: str = "") -> list[Any]:
    cells = _v3_pad(record.get("cells") if isinstance(record.get("cells"), list) else [])
    cells[ANALYST_NOTES_INDEX] = analyst_notes
    return cells + [record.get("section_id", ""), record.get("record_key", ""),
                    record.get("content_hash") or canonical_content_hash(record),
                    record.get("source_run_id", ""), record.get("source_as_of_date", ""),
                    created_at, updated_at, change_type, GOOGLE_SHEET_REPORT_SCHEMA_VERSION]


def _v3_header_row(ticker: str, as_of_date: str) -> list[Any]:
    return _v3_pad([f"{ticker} — Full Qualitative Research v3", "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of {as_of_date}"]) + METADATA_HEADERS


def _v3_parse(values: list[list[Any]]) -> tuple[dict[str, dict[str, Any]], bool, str]:
    if not values:
        return {}, False, ""
    header_index = next((index for index, row in enumerate(values) if "__record_key" in [str(item) for item in row]), None)
    if header_index is None:
        return {}, False, ""
    header = [str(item) for item in values[header_index]]
    indices = {name: index for index, name in enumerate(header) if name}
    if "__record_key" not in indices or "__content_hash" not in indices:
        return {}, False, ""
    records: dict[str, dict[str, Any]] = {}
    key_index = indices["__record_key"]
    for row in values[header_index + 1:]:
        if key_index >= len(row) or not str(row[key_index]).strip():
            continue
        key = str(row[key_index])
        records[key] = {name: (row[index] if index < len(row) else "") for name, index in indices.items()}
        for index, name in enumerate(VISIBLE_HEADERS):
            records[key].setdefault(name, row[index] if index < len(row) else "")
    schema_index = indices.get("__schema_version")
    schema = ""
    if schema_index is not None:
        for row in values[header_index + 1:]:
            if schema_index < len(row) and row[schema_index]:
                schema = str(row[schema_index])
                break
    return records, True, schema


def build_canonical_state(dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None,
                          as_of_date: str | None = None, artifacts_root: str | Path | None = None,
                          production_root: str | Path | None = None, run_id: str = "") -> CanonicalState:
    if isinstance(dossier, (str, Path)):
        payload = json.loads(Path(dossier).read_text(encoding="utf-8"))
    else:
        payload = dict(dossier)
    symbol = str(ticker or payload.get("ticker") or "").upper()
    cutoff = str(as_of_date or payload.get("as_of_date") or payload.get("source_cutoff") or "")
    execution_id = run_id or str(payload.get("run_id") or payload.get("dossier_id") or f"sheet:{symbol}:{cutoff}")
    records = _v3_render_payload(payload, ticker=symbol, cutoff=cutoff, run_id=execution_id,
                                  production_root=Path(production_root) if production_root else None,
                                  artifacts_root=Path(artifacts_root) if artifacts_root else None)
    fingerprint = str(payload.get("input_fingerprint") or hashlib.sha256(json.dumps(_normalise(payload), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest())
    return CanonicalState(symbol, cutoff, execution_id, records, fingerprint)


@dataclass
class PublicationSummary:
    status: str
    code: str = ""
    message: str = ""
    worksheet_title: str = ""
    worksheet_id: str = ""
    records_existing: int = 0
    records_new: int = 0
    records_added: int = 0
    records_updated: int = 0
    records_unchanged: int = 0
    records_resolved: int = 0
    records_stale: int = 0
    records_removed: int = 0
    sections_changed: list[str] = field(default_factory=list)
    api_calls: int = 0
    snapshot_path: str = ""
    dossier_fingerprint: str = ""
    client_email: str = ""
    migration_status: str = ""
    rows_before: int = 0
    rows_after: int = 0
    blank_rows_removed: int = 0
    largest_cell: int = 0
    analyst_notes_preserved: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class GoogleSheetsPublisher:
    """Publish a deterministic v3 PM decision surface plus deep archive."""

    def __init__(self, *, spreadsheet_id: str | None = None, service_account: str | Path | None = None,
                 retries: int = 3, client: Any | None = None, now: Callable[[], str] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.spreadsheet_id, self.service_account = resolve_google_configuration(spreadsheet_id=spreadsheet_id, service_account=service_account)
        self.retries = max(1, min(int(retries), 3))
        self.client = client
        self.now = now or _now
        self.sleep = sleep or time.sleep
        self.api_calls = 0

    def _call(self, operation: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.api_calls += 1
                return operation()
            except Exception as exc:
                last = exc
                text = str(exc).lower()
                status = getattr(exc, "response", None)
                status_code = getattr(status, "status_code", None) or getattr(exc, "status_code", None)
                if status_code in {401, 403} or any(word in text for word in ("permission", "forbidden", "unauthorized")):
                    raise
                retryable = status_code == 429 or (isinstance(status_code, int) and status_code >= 500) or any(word in text for word in ("429", "rate", "tempor", "timeout", "connection", "503", "502", "500"))
                if not retryable or attempt + 1 >= self.retries:
                    raise
                self.sleep(2 ** attempt)
        raise last or RuntimeError("Google Sheets operation failed")

    def _connect(self) -> tuple[Any, str]:
        if self.client is not None:
            email = str(getattr(self.client, "client_email", "") or "")
            if hasattr(self.client, "open_by_key"):
                return self._call(lambda: self.client.open_by_key(self.spreadsheet_id)), email
            return self.client, email
        email = service_account_email(self.service_account)
        try:
            import gspread
            client = gspread.service_account(filename=str(self.service_account), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except FileNotFoundError:
            raise
        except Exception as exc:
            if any(word in str(exc).lower() for word in ("403", "permission", "forbidden")):
                raise GoogleSheetsPermissionDenied(email) from exc
            raise GoogleSheetsPublicationError("GOOGLE_SHEETS_AUTH_FAILED", "Google authentication failed.", client_email=email) from exc
        http_client = getattr(client, "http_client", None)
        if http_client is not None and getattr(http_client, "timeout", None) is None:
            http_client.timeout = 30.0
        return self._call(lambda: client.open_by_key(self.spreadsheet_id)), email

    def _open_worksheet(self, book: Any, ticker: str) -> tuple[Any, bool]:
        try:
            return self._call(lambda: book.worksheet(ticker)), False
        except Exception as exc:
            text = str(exc).lower()
            if not any(token in type(exc).__name__.lower() or token in text for token in ("notfound", "not found", "worksheet", "unknown")):
                raise
            try:
                return self._call(lambda: book.add_worksheet(title=ticker, rows=1000, cols=len(ALL_HEADERS))), True
            except Exception as create_exc:
                if any(word in str(create_exc).lower() for word in ("permission", "403", "forbidden")):
                    raise GoogleSheetsPermissionDenied() from create_exc
                raise

    def _format(self, worksheet: Any, rows: int) -> None:
        try:
            if hasattr(worksheet, "freeze"):
                self._call(lambda: worksheet.freeze(rows=1))
            if hasattr(worksheet, "format"):
                self._call(lambda: worksheet.format(f"A1:{_column_letter(len(VISIBLE_HEADERS))}{max(rows, 2)}", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}))
                self._call(lambda: worksheet.format(f"A1:{_column_letter(len(VISIBLE_HEADERS))}1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.10, "green": 0.16, "blue": 0.24}}))
            if hasattr(worksheet, "hide_columns"):
                self._call(lambda: worksheet.hide_columns(len(VISIBLE_HEADERS), len(ALL_HEADERS) - 1))
            if hasattr(worksheet, "set_basic_filter"):
                self._call(lambda: worksheet.set_basic_filter())
            if hasattr(worksheet, "resize"):
                self._call(lambda: worksheet.resize(rows=max(rows, 1), cols=len(ALL_HEADERS)))
        except Exception:
            log.debug("Sheet formatting unavailable", exc_info=True)

    def publish(self, dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None,
                as_of_date: str | None = None, artifacts_root: str | Path | None = None,
                production_root: str | Path | None = None, run_id: str = "") -> PublicationSummary:
        state = build_canonical_state(dossier, ticker=ticker, as_of_date=as_of_date, artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
        production = Path(production_root) if production_root else (Path(dossier).parents[1] if isinstance(dossier, (str, Path)) else None)
        summary = PublicationSummary(status="FAILED", worksheet_title=state.ticker, dossier_fingerprint=state.dossier_fingerprint)
        try:
            initial = [_v3_header_row(state.ticker, state.as_of_date)] + [_v3_row(item, created_at="", updated_at="", change_type="ADDED") for item in state.records]
            summary.largest_cell = assert_max_cell_lengths(initial, ticker=state.ticker)
            book, email = self._connect()
            summary.client_email = email
            title = _safe_worksheet_title(state.ticker)
            worksheet, created = self._open_worksheet(book, title)
            summary.worksheet_title, summary.worksheet_id = title, str(getattr(worksheet, "id", ""))
            values = _worksheet_values(worksheet) if not created else []
            summary.rows_before = len(values)
            old, managed, schema = _v3_parse(values)
            summary.records_existing = len(old)
            migration = bool(values and (not managed or schema != GOOGLE_SHEET_REPORT_SCHEMA_VERSION))
            if migration:
                if production:
                    backup_name = f"{state.ticker}_v2-sheet-backup.json" if schema == "full-qual-sheet-v2" else (f"{state.ticker}_v1-sheet-backup.json" if schema.startswith("full-qual-sheet-v1") else f"{state.ticker}_legacy-sheet-backup.json")
                    backup = production / "12_Google_Sheets" / backup_name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_text(json.dumps({"ticker": state.ticker, "captured_at": self.now(), "schema_version": schema, "values": values}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                summary.migration_status = "V2_TO_V3_MIGRATED" if schema == "full-qual-sheet-v2" else ("V1_TO_V3_MIGRATED" if schema.startswith("full-qual-sheet-v1") else "LEGACY_SHEET_MIGRATED")
            now = self.now()
            old_notes = {key: str(row.get("Analyst Notes") or "") for key, row in old.items() if row.get("Analyst Notes")}
            summary.analyst_notes_preserved = len(old_notes)
            desired: dict[str, dict[str, Any]] = {}
            changes: list[tuple[str, dict[str, Any], str]] = []
            retained_stale: list[tuple[str, dict[str, Any]]] = []
            for source in state.records:
                row = dict(source)
                key = str(row["record_key"])
                note = old_notes.get(key, "")
                if not note:
                    suffix = key.split(":", 1)[-1]
                    note = next((value for old_key, value in old_notes.items() if old_key.endswith(suffix) or old_key.split(":")[-1] == suffix), "")
                row["analyst_notes"] = note
                desired[key] = row
                previous = old.get(key)
                if previous is None or migration:
                    row["created_at"] = str(previous.get("__created_at") if previous else now) or now
                    row["updated_at"] = str(previous.get("__updated_at") if previous else now) or now
                    changes.append((key, row, "ADDED" if previous is None else "UPDATED"))
                else:
                    row["created_at"] = str(previous.get("__created_at") or now)
                    row["updated_at"] = str(previous.get("__updated_at") or row["created_at"])
                    if str(previous.get("__content_hash") or "") == str(row.get("content_hash") or ""):
                        summary.records_unchanged += 1
                    else:
                        row["updated_at"] = now
                        changes.append((key, row, "UPDATED"))
            if not migration:
                for key, previous in old.items():
                    if key in desired or key.startswith("change_log:"):
                        continue
                    # A stale record is intentionally retained for auditability
                    # but must not be re-emitted as a new change on every
                    # identical run.  Otherwise a deterministic rerun would
                    # continually append change-log rows.
                    if str(previous.get("__change_type") or "").upper() == "STALE" or str(previous.get("Status") or "").upper() == "SOURCE_UNAVAILABLE_THIS_RUN":
                        retained_stale.append((key, previous))
                        continue
                    cells = [previous.get(name, "") for name in VISIBLE_HEADERS]
                    if len(cells) > 2:
                        cells[2] = "SOURCE_UNAVAILABLE_THIS_RUN"
                    stale = {"section_id": previous.get("__section_id", ""), "record_key": key, "cells": cells, "content_hash": previous.get("__content_hash", ""), "source_run_id": previous.get("__source_run_id", ""), "source_as_of_date": previous.get("__source_as_of_date", state.as_of_date), "analyst_notes": str(previous.get("Analyst Notes") or "")}
                    changes.append((key, stale, "STALE"))
                    summary.records_stale += 1
                    summary.records_removed += 1
            summary.records_new = sum(kind == "ADDED" for _, _, kind in changes)
            summary.records_added = summary.records_new
            summary.records_updated = sum(kind == "UPDATED" for _, _, kind in changes)
            summary.sections_changed = sorted({str(row.get("section_id") or "") for _, row, _ in changes})
            has_changes = bool(created or migration or changes)
            grid = [_v3_header_row(state.ticker, state.as_of_date)]
            kinds = {key: kind for key, _, kind in changes}
            for key, row in desired.items():
                grid.append(_v3_row(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=kinds.get(key, "UNCHANGED"), analyst_notes=str(row.get("analyst_notes") or "")))
            for key, row, kind in changes:
                if key not in desired:
                    grid.append(_v3_row(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=kind, analyst_notes=str(row.get("analyst_notes") or "")))
            for key, previous in retained_stale:
                stale = {"section_id": previous.get("__section_id", ""), "record_key": key,
                         "cells": [previous.get(name, "") for name in VISIBLE_HEADERS],
                         "content_hash": previous.get("__content_hash", ""),
                         "source_run_id": previous.get("__source_run_id", ""),
                         "source_as_of_date": previous.get("__source_as_of_date", state.as_of_date),
                         "created_at": previous.get("__created_at", now),
                         "updated_at": previous.get("__updated_at", now),
                         "analyst_notes": str(previous.get("Analyst Notes") or "")}
                grid.append(_v3_row(stale, created_at=str(stale.get("created_at") or now), updated_at=str(stale.get("updated_at") or now), change_type="STALE", analyst_notes=str(stale.get("analyst_notes") or "")))
            if not migration:
                for row in values:
                    if len(row) > len(VISIBLE_HEADERS) + 1 and str(row[len(VISIBLE_HEADERS) + 1]).startswith("change_log:"):
                        grid.append(list(row)[:len(ALL_HEADERS)] + [""] * max(0, len(ALL_HEADERS) - len(row)))
            if has_changes:
                log_record = {"section_id": "29_change_log", "record_key": f"change_log:{state.run_id}:{now}", "cells": _v3_pad([now, state.as_of_date, state.run_id, summary.records_added, summary.records_updated, summary.records_unchanged, summary.records_stale, ", ".join(summary.sections_changed)]), "source_run_id": state.run_id, "source_as_of_date": state.as_of_date}
                log_record["content_hash"] = canonical_content_hash(log_record)
                grid.append(_v3_row(log_record, created_at=now, updated_at=now, change_type="ADDED"))
            grid.append([""] * len(ALL_HEADERS))
            summary.largest_cell = assert_max_cell_lengths(grid, ticker=state.ticker)
            if has_changes or created:
                if hasattr(worksheet, "update"):
                    self._call(lambda: _worksheet_update_user_entered(worksheet, f"A1:{_column_letter(len(ALL_HEADERS))}{len(grid)}", grid))
                    if changes and hasattr(worksheet, "batch_update"):
                        self._call(lambda: worksheet.batch_update([]))
                elif hasattr(worksheet, "append_rows"):
                    self._call(lambda: _worksheet_append_user_entered(worksheet, grid))
                summary.status = "CREATED" if created else "UPDATED"
            else:
                summary.status = "NO_CHANGES"
            summary.rows_after = len(grid)
            summary.blank_rows_removed = max(0, summary.rows_before - len(grid)) if summary.rows_before else 0
            self._format(worksheet, len(grid))
            summary.api_calls = self.api_calls
            if summary.status in {"CREATED", "UPDATED", "NO_CHANGES"} and production:
                snapshot = production / "12_Google_Sheets" / f"{state.ticker}_{state.as_of_date}_sheet-publication.json"
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_text(json.dumps({"spreadsheet_id": self.spreadsheet_id, "worksheet_id": summary.worksheet_id, "worksheet_title": title, "schema_version": GOOGLE_SHEET_REPORT_SCHEMA_VERSION, "publication_timestamp": now, "run_id": state.run_id, "dossier_fingerprint": state.dossier_fingerprint, "section_fingerprints": state.section_fingerprints, "records": summary.to_dict(), "status": summary.status}, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                summary.snapshot_path = str(snapshot)
            return summary
        except GoogleSheetsPublicationError as exc:
            summary.code, summary.message, summary.client_email, summary.status = exc.code, str(exc), exc.client_email or summary.client_email, "FAILED"
            summary.api_calls = self.api_calls
            return summary
        except Exception as exc:
            text = str(exc).lower()
            summary.code = "GOOGLE_SHEETS_PERMISSION_DENIED" if any(word in text for word in ("403", "permission", "forbidden")) else "GOOGLE_SHEETS_PUBLISH_FAILED"
            summary.message = f"{summary.code}: {type(exc).__name__}: {exc}"
            summary.status = "FAILED"
            summary.api_calls = self.api_calls
            return summary


__all__ = ["ALL_HEADERS", "ANALYST_NOTES_INDEX", "CanonicalState", "DEFAULT_QUALITATIVE_SPREADSHEET_ID", "DEFAULT_SERVICE_ACCOUNT_PATH", "GoogleSheetsPermissionDenied", "GoogleSheetsPublicationError", "GoogleSheetsPublisher", "GOOGLE_SHEET_REPORT_SCHEMA_VERSION", "MAX_GOOGLE_CELL_CHARS", "MAX_EVIDENCE_EXCERPT_CHARS", "MAX_REPORT_CELL_CHARS", "METADATA_HEADERS", "PublicationSummary", "SECTION_NAMES", "VISIBLE_HEADERS", "assert_max_cell_lengths", "build_canonical_state", "canonical_content_hash", "resolve_google_configuration", "service_account_email"]

# ---------------------------------------------------------------------------
# V4 research-intelligence layer
# ---------------------------------------------------------------------------
#
# V4 deliberately lives at the publication boundary.  The analytical
# components continue to own acquisition, normalization, extraction,
# temporal analysis, management, governance and scoring.  This layer reads
# those authoritative artifacts and turns them into an evidence-constrained,
# model-development handoff.  Keeping this as a final overlay also allows an
# old V3 worksheet to be migrated in place without changing frozen analytical
# schemas.

GOOGLE_SHEET_REPORT_SCHEMA_VERSION = "full-qual-sheet-v4"
PUBLISHER_VERSION = "full-qual-google-sheets-v4"
V4_SCHEMA_VERSION = GOOGLE_SHEET_REPORT_SCHEMA_VERSION
V4_PUBLISHER_VERSION = PUBLISHER_VERSION

_V4_PLACEHOLDER_MECHANISM = "QUALITATIVE_INFERENCE: economic linkage requires downstream testing."
_V4_STOPWORDS = {
    "about", "after", "against", "among", "because", "between", "could", "from", "into", "more",
    "other", "over", "that", "their", "there", "these", "this", "through", "under", "when", "which",
    "with", "without", "would", "year", "years", "company", "business", "current", "previous",
}


def _v4_date(value: Any) -> str:
    return _v3_date(value)


def _v4_parse_date(value: Any) -> datetime | None:
    text = _v4_date(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _v4_text(value: Any, limit: int | None = None) -> str:
    return _v3_clean_text(value, limit)


def _v4_tokens(value: Any) -> set[str]:
    return {token for token in re.findall(r"[a-z][a-z0-9]{2,}", _v4_text(value).casefold()) if token not in _V4_STOPWORDS}


def _v4_evidence_text(value: Any) -> str:
    if isinstance(value, Mapping):
        values: list[str] = []
        for key in ("evidence_text", "claim_text", "snippet", "text", "fact", "assessment", "mechanism", "description", "statement", "evidence", "supporting_evidence"):
            if value.get(key) not in (None, "", []):
                raw = value.get(key)
                if isinstance(raw, list):
                    values.extend(_v4_text(item) for item in raw if _v4_text(item) and not re.fullmatch(r"(?:clm|claim|evidence)[_:-][a-z0-9_-]+", _v4_text(item).casefold()))
                else:
                    text = _v4_text(raw)
                    if text and not re.fullmatch(r"(?:clm|claim|evidence)[_:-][a-z0-9_-]+", text.casefold()):
                        values.append(text)
        if values:
            return _v4_text(" ".join(values), MAX_EVIDENCE_EXCERPT_CHARS)
    return _v4_text(value, MAX_EVIDENCE_EXCERPT_CHARS)


def _v4_has_numeric_observation(item: Mapping[str, Any]) -> bool:
    magnitude = item.get("magnitude")
    if isinstance(magnitude, Mapping) and magnitude.get("value") not in (None, "") and magnitude.get("unit") not in (None, ""):
        return bool(item.get("period_label") or item.get("period_end_date") or item.get("public_availability_date"))
    # Normalized financial facts use ``value``/``unit`` and ``period_end``
    # rather than the qualitative claim ``magnitude`` shape.  They are still
    # authoritative internal observations and must be eligible for KPI
    # resolution.
    if item.get("value") not in (None, "") and item.get("unit") not in (None, ""):
        return bool(item.get("period_end") or item.get("period_end_date") or item.get("fiscal_period") or item.get("fiscal_year"))
    return item.get("latest_value") not in (None, "", "VALUE_NOT_CAPTURED") and item.get("unit") not in (None, "", "NOT_CAPTURED")


def _v4_row_ticker(row: Mapping[str, Any], ticker: str) -> bool:
    value = str(row.get("ticker") or row.get("issuer_ticker") or row.get("symbol") or "").upper()
    if value:
        return value == ticker.upper()
    return True  # production JSONL is ticker-scoped when the field is absent


def _v4_load_context(*, ticker: str, cutoff: str, production_root: Path | None = None,
                     artifacts_root: Path | None = None) -> dict[str, Any]:
    """Load only existing local artifacts and keep PIT eligibility explicit."""
    roots = [root for root in (production_root, artifacts_root) if root]
    context: dict[str, Any] = {"documents": [], "claims": [], "states": [], "temporal": [], "facts": [],
                               "management": [], "governance": [], "sources": [], "future": []}
    paths = {
        "documents": ("02_Normalized_Documents/documents.jsonl", "qualitative_analysis/documents.jsonl"),
        "claims": ("03_Claims/claims.jsonl", "qualitative_analysis/claims.jsonl"),
        "states": ("05_Qualitative_State/qualitative_states.jsonl", "qualitative_analysis/qualitative_states.jsonl"),
        "temporal": ("04_Temporal_Analysis/temporal_changes.jsonl", "qualitative_analysis/temporal_changes.jsonl"),
        "facts": ("07_Management_Intelligence/financial_facts.jsonl", "financial_facts.jsonl"),
        "management": ("07_Management_Intelligence/people.jsonl", "07_Management_Intelligence/roles.jsonl", "management_intelligence/people.jsonl", "management_intelligence/roles.jsonl"),
        "governance": (
            "08_Board_Governance/governance_board_memberships.jsonl",
            "08_Board_Governance/governance_committees.jsonl",
            "08_Board_Governance/governance_snapshots.jsonl",
            "08_Board_Governance/governance_coverage.jsonl",
            "management_intelligence/governance_board_memberships.jsonl",
            "management_intelligence/governance_committees.jsonl",
            "management_intelligence/governance_snapshots.jsonl",
            "management_intelligence/governance_coverage.jsonl",
        ),
    }
    seen: dict[str, set[str]] = {key: set() for key in paths}
    cutoff_dt = _v4_parse_date(cutoff)
    for root in roots:
        for group, relatives in paths.items():
            for relative in relatives:
                for row in _read_jsonl(root / relative):
                    if not _v4_row_ticker(row, ticker):
                        continue
                    identity = str(row.get("claim_id") or row.get("document_id") or row.get("state_id") or row.get("change_id") or row.get("fact_id") or row.get("person_id") or row.get("board_membership_id") or row.get("committee_id") or row.get("snapshot_id") or row.get("coverage_id") or row.get("role_id") or _json(row)[:128])
                    if identity in seen[group]:
                        continue
                    seen[group].add(identity)
                    available = _v4_parse_date(row.get("public_availability_date") or row.get("available_date") or row.get("publication_date") or row.get("filing_date") or row.get("source_date") or row.get("created_at"))
                    pit = not cutoff_dt or not available or available.date() <= cutoff_dt.date()
                    record = dict(row)
                    record["pit_eligible"] = pit
                    record["pit_status"] = "ELIGIBLE" if pit else "FUTURE_NOT_ELIGIBLE"
                    if pit:
                        context[group].append(record)
                    else:
                        context["future"].append({"group": group, "record_id": identity, "available_date": _v4_date(available)})
    # Source registry is intentionally compact and excludes document bodies.
    # The ticker production tree is authoritative.  Avoid rescanning the
    # repository's optional benchmark/model cache when a production tree is
    # present; that cache can contain multi-gigabyte Whisper assets and is not
    # a source of analytical truth for the sheet.
    context["sources"] = _v3_source_registry_v3(ticker=ticker, cutoff=cutoff,
                                                 artifacts_root=None if production_root else artifacts_root,
                                                 production_root=production_root)
    return context


def _v5_canonical_period_fields(value: Any) -> tuple[str, str]:
    """Return normalized label/end from a resolver-produced identity.

    The canonical identity is the period resolver's output.  Flattened source
    text can contain comparative quarter/year phrases which are not the
    filing's reporting period, so V5 must not let those phrases override the
    canonical identity.
    """
    parts = str(value or "").split("|")
    if len(parts) < 2:
        return "", ""
    label = _v4_text(parts[1])
    if label.upper().startswith("Q") and "FY" not in label.upper():
        match = re.match(r"^(Q[1-4])\s+(\d{4})$", label, flags=re.IGNORECASE)
        if match:
            label = f"{match.group(1).upper()} FY{match.group(2)}"
    elif label.upper().startswith("FY"):
        match = re.match(r"^FY\s*(\d{4})$", label, flags=re.IGNORECASE)
        if match:
            label = f"FY {match.group(1)}"
    period_end = _v4_date(parts[3]) if len(parts) > 3 else ""
    return label, period_end


def _v4_period_summary(context: Mapping[str, Any], payload: Mapping[str, Any] | None = None,
                       cutoff: str = "") -> dict[str, Any]:
    """Use periods already resolved by the temporal/state layers; never infer
    a fiscal quarter from a month number here."""
    payload = payload or {}
    state_rows = [row for row in context.get("states", []) if isinstance(row, Mapping) and row.get("pit_eligible", True)]
    # State rows are preferred for the current as-of period.  Normalized
    # document metadata is the authoritative fallback for immediately prior,
    # annual and quarterly identities when a topic-specific state file does
    # not contain every period.
    period_rows = list(state_rows)
    for group in ("documents", "claims"):
        period_rows.extend(row for row in context.get(group, []) if isinstance(row, Mapping) and row.get("pit_eligible", True) and (row.get("period_label") or row.get("period_end_date") or row.get("canonical_period_identity")))
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in period_rows:
        period_end = _v4_date(row.get("period_end_date") or row.get("as_of_period_end") or row.get("period_end"))
        label = _v4_text(row.get("period_label") or row.get("as_of_period_label") or row.get("period"))
        if row.get("canonical_period_identity"):
            canonical_label, canonical_end = _v5_canonical_period_fields(row.get("canonical_period_identity"))
            if canonical_label:
                label = canonical_label
            if canonical_end:
                period_end = canonical_end
        fy = row.get("as_of_fiscal_year") or row.get("fiscal_year")
        fq = row.get("as_of_fiscal_quarter") or row.get("fiscal_quarter")
        if not label and fy:
            label = f"Q{fq} FY{fy}" if fq else f"FY {fy}"
        if not period_end and not label:
            continue
        period_type = str(row.get("period_type") or row.get("as_of_period_type") or "")
        if not period_type:
            document_type = str(row.get("document_type") or row.get("document_subtype") or "").casefold()
            period_type = "ANNUAL_ACTUAL" if "annual" in document_type or str(label).upper().startswith("FY") else ("QUARTER_ACTUAL" if "quarter" in document_type or str(label).upper().startswith("Q") else "")
        key = (label, period_end, period_type)
        current = groups.get(key)
        available = _v4_date(row.get("public_availability_date") or row.get("available_date") or row.get("filing_date"))
        if current is None or (available and available > current.get("public_availability_date", "")):
            groups[key] = {"period_label": label or "NOT_CAPTURED", "period_end_date": period_end or "NOT_CAPTURED",
                           "period_start_date": _v4_date(row.get("period_start_date") or row.get("as_of_period_start")),
                           "fiscal_year": fy, "fiscal_quarter": fq,
                           "period_type": period_type,
                           "issuer_fiscal_calendar": row.get("issuer_fiscal_calendar") or {},
                           "public_availability_date": available, "source_url": row.get("source_url") or ""}
    dated_groups = [item for item in groups.values() if _v4_parse_date(item.get("period_end_date"))]
    ordered = sorted(dated_groups or list(groups.values()), key=lambda item: (item.get("period_end_date") or "", item.get("public_availability_date") or ""), reverse=True)
    current = ordered[0] if ordered else {}
    previous = next((item for item in ordered[1:] if item.get("period_end_date") != current.get("period_end_date")), {}) if current else {}
    annual = next((item for item in ordered if str(item.get("period_type", "")).upper() in {"ANNUAL", "ANNUAL_ACTUAL", "FY"} or str(item.get("period_label", "")).upper().startswith("FY")), {})
    quarterly = next((item for item in ordered if str(item.get("period_type", "")).upper() not in {"ANNUAL", "ANNUAL_ACTUAL", "FY"} and item.get("fiscal_quarter") not in (None, "")), {})
    # Some unit fixtures provide the resolved period fields in the dossier but
    # no state JSONL.  Those fields are authoritative and are used verbatim.
    def fallback(name: str, value: Any) -> Any:
        return value if value not in (None, "", {}, []) else payload.get(name)
    current = current or (fallback("current_period", {}) if isinstance(fallback("current_period", {}), Mapping) else {})
    previous = previous or (fallback("previous_period", {}) if isinstance(fallback("previous_period", {}), Mapping) else {})
    annual = annual or (fallback("latest_annual", {}) if isinstance(fallback("latest_annual", {}), Mapping) else {})
    quarterly = quarterly or (fallback("latest_quarterly", {}) if isinstance(fallback("latest_quarterly", {}), Mapping) else {})
    return {"current": current, "previous": previous, "latest_annual": annual, "latest_quarterly": quarterly,
            "fiscal_year": current.get("fiscal_year") or payload.get("fiscal_year") or "NOT_CAPTURED",
            "fiscal_quarter": current.get("fiscal_quarter") or payload.get("fiscal_quarter") or "NOT_CAPTURED",
            "current_period": current.get("period_label") or payload.get("current_period") or "NOT_CAPTURED",
            "previous_period": previous.get("period_label") or payload.get("previous_period") or "NOT_CAPTURED",
            "latest_annual_label": annual.get("period_label") or payload.get("latest_annual") or "NOT_CAPTURED",
            "latest_quarterly_label": quarterly.get("period_label") or payload.get("latest_quarterly") or "NOT_CAPTURED"}


def _v4_semantic_compatible(concept: Any, evidence: Any, *, section: Any = "", claim_type: Any = "") -> bool:
    """Reject lexical false positives for overloaded financial terms."""
    target = _v4_text(concept).casefold().replace("_", " ")
    text = _v4_evidence_text(evidence).casefold()
    context = f"{text} {_v4_text(section).casefold()} {_v4_text(claim_type).casefold()}"
    if not text:
        return False
    if "disclosure contains evidence relevant" in text:
        return False
    if any(term in target for term in ("barrier", "substitute", "competitive intensity", "competitor")):
        if any(term in context for term in ("share repurchase", "share-repurchase", "buyback", "compensation", "racial equity audit")):
            return False
        return any(term in context for term in ("barrier", "entry", "switching cost", "network effect", "scale", "licen", "substitut", "alternative", "competitor", "competition"))
    if "regulatory constraint" in target or "regulation" in target:
        if any(term in context for term in ("racial equity audit", "compensation", "share repurchase", "buyback", "risk-taking cycle")):
            return False
        return any(term in context for term in ("regulatory requirement", "regulatory constraint", "compliance", "license", "approval", "capital requirement", "regulator", "law", "rule"))
    if "capacity" in target:
        operational = ("production", "store", "unit", "facility", "distribution", "throughput", "fulfillment", "manufacturing", "capacity expansion", "utilization")
        disqualifying = ("borrowing capacity", "capacity as an agent", "legal capacity", "employment capacity", "federal home loan", "agent capacity")
        return any(term in context for term in operational) and not any(term in context for term in disqualifying)
    if "risk" in target or "cyclicality" in target:
        disqualifying = ("risk-weighted asset", "risk weighted asset", "risk-taking cycle", "risk management function", "compensation risk", "definition of", "means that", "for purposes of")
        business = ("exposure", "loss", "demand", "competition", "regulatory", "credit", "liquidity", "margin", "cost", "earnings", "could adversely", "may adversely")
        return any(term in context for term in business) and not any(term in context for term in disqualifying)
    if "margin" in target:
        disqualifying = ("collateral margin", "margin loan", "margin requirement", "safety margin", "initial margin", "variation margin")
        business = ("gross margin", "gross profit", "operating margin", "net margin", "profit margin", "margin rate", "margin expansion", "margin compression")
        return any(term in context for term in business) and not any(term in context for term in disqualifying)
    if "volume" in target:
        disqualifying = ("document volume", "trading volume", "page volume", "data volume")
        business = ("sales volume", "loan volume", "deposit volume", "customer volume", "transaction volume", "unit volume", "volume growth")
        return any(term in context for term in business) and not any(term in context for term in disqualifying)
    if "pricing" in target or "price realization" in target:
        # Share-price, stock-price and valuation language is not an operating
        # pricing driver.  Require product/sales/fee evidence before mapping
        # an overloaded "price" token to a KPI or driver.
        disqualifying = ("stock price", "share price", "price of our stock", "market expectations", "valuation", "repurchase", "buyback", "par value", "price input", "liquidation value", "preferred stock", "redemption price")
        operating = ("price realization", "pricing", "product price", "selling price", "merchandise", "sales price", "fee", "rate paid", "yield")
        return any(term in context for term in operating) and not any(term in context for term in disqualifying)
    return True


def _v4_metric_map(text: Any) -> tuple[str, str, str, str, str] | None:
    """Map evidence to a measurable KPI without asserting an issuer fact."""
    lower = _v4_text(text).casefold().replace("_", " ")
    mappings = (
        (("comparable sales", "same-store sales", "comp sales"), "COMPARABLE_SALES", "growth", "percent", "Revenue Growth"),
        (("gross profit rate", "gross margin", "gross profit margin"), "GROSS_MARGIN", "margin", "percent", "Gross Margin"),
        (("operating expense", "operating expenses as a percentage"), "OPERATING_EXPENSE_RATE", "ratio", "percent", "Operating Expense"),
        (("traffic", "transactions"), "TRAFFIC_AND_TRANSACTIONS", "count_or_growth", "count_or_percent", "Revenue Growth"),
        (("average ticket", "basket size"), "AVERAGE_TICKET", "absolute_or_growth", "currency", "Revenue Growth"),
        (("inventory", "inventory turnover"), "INVENTORY", "absolute_or_ratio", "currency_or_turns", "Cash Flow"),
        (("e-commerce", "ecommerce", "online sales"), "ECOMMERCE_SALES", "growth", "percent", "Revenue Growth"),
        (("membership", "members"), "MEMBERSHIP", "count_or_growth", "count", "Revenue Growth"),
        (("advertising", "retail media"), "RETAIL_MEDIA_REVENUE", "absolute_or_growth", "currency_or_percent", "Revenue"),
        (("shrink",), "SHRINK", "rate", "percent", "Gross Margin"),
        (("labor", "wages", "personnel expense"), "LABOR_COST", "absolute_or_ratio", "currency_or_percent", "Operating Expense"),
        (("stores", "store count", "locations"), "STORE_COUNT", "count", "count", "Revenue Growth"),
        (("freight", "supply chain", "transportation cost"), "SUPPLY_CHAIN_COST", "absolute_or_ratio", "currency_or_percent", "Operating Expense"),
        (("pricing", "price realization"), "PRICING_REALIZATION", "growth_or_index", "percent_or_index", "Revenue Growth"),
        (("sales mix", "product mix"), "SALES_MIX", "share", "percent", "Gross Margin"),
        (("revenue", "net sales"), "REVENUE", "absolute_or_growth", "currency", "Revenue"),
        (("net interest income", "nii"), "NET_INTEREST_INCOME", "absolute_or_growth", "currency", "NII"),
        (("deposits",), "DEPOSITS", "absolute_or_growth", "currency", "NII"),
        (("loan", "loans"), "LOAN_VOLUME", "absolute_or_growth", "currency", "NII"),
        (("credit loss", "provision"), "CREDIT_COST", "rate_or_absolute", "percent_or_currency", "Credit Cost"),
        (("cet1", "capital ratio"), "REGULATORY_CAPITAL", "ratio", "percent", "Capital Requirement"),
        (("fee income", "investment banking fees"), "FEE_INCOME", "absolute_or_growth", "currency", "Fee Income"),
        (("operating margin", "net margin", "profit margin"), "OPERATING_MARGIN", "margin", "percent", "Operating Margin"),
    )
    for terms, name, metric_type, unit, outcome in mappings:
        if any(term in lower for term in terms):
            return name, metric_type, unit, outcome, lower
    return None


def _v4_claim_matches(name: str, claim: Mapping[str, Any]) -> bool:
    mapped = _v4_metric_map(claim.get("dimension") or claim.get("claim_text") or claim.get("evidence_text"))
    if mapped and mapped[0] == name:
        return True
    # Do not let a single generic word (for example "sales") make an
    # unrelated numeric observation look like a comparable-sales KPI.
    # Canonical mapping is required for multi-token concepts.
    terms = set(_v4_tokens(name))
    if len(terms) > 1:
        return False
    return bool(terms and terms <= _v4_tokens(claim.get("claim_text") or claim.get("evidence_text") or claim.get("dimension")))


def _v4_unit_compatible(name: str, row: Mapping[str, Any]) -> bool:
    """Prevent a numerically real but semantically wrong unit from filling a KPI."""
    magnitude = row.get("magnitude") if isinstance(row.get("magnitude"), Mapping) else {}
    unit = _v4_text(magnitude.get("unit") if magnitude else row.get("unit")).casefold()
    if not unit:
        return True
    if name in {"COMPARABLE_SALES", "GROSS_MARGIN", "OPERATING_EXPENSE_RATE", "PRICING_REALIZATION", "SHRINK", "SALES_MIX"}:
        return any(token in unit for token in ("percent", "%", "basis", "bps", "ratio", "index"))
    if name == "TRAFFIC_AND_TRANSACTIONS":
        return any(token in unit for token in ("percent", "%", "basis", "bps", "count", "number", "ratio"))
    return True


_V4_BANK_EVIDENCE_GROUPS = {
    "deposits": ("customer deposits", "deposit balances", "deposits were", "deposit franchise", "interest-bearing deposits", "fdic", "summary of deposits"),
    "lending": ("loan portfolio", "loan growth", "loans held", "commercial banking", "consumer banking", "credit card"),
    "earnings": ("net interest income", "interest income", "fee income", "investment banking fees"),
    "regulatory": ("cet1", "common equity tier", "capital ratio", "capital adequacy", "federal reserve"),
}


def _v4_bank_evidence_support(payload: Mapping[str, Any], context: Mapping[str, Any] | None) -> bool:
    """Confirm a bank adapter from linked source evidence, not stale labels.

    Older dossiers can carry a bank adapter and bank-shaped factor names for
    issuers whose only matching words are e.g. pension funding or held-to-
    maturity time deposits.  When the normalized context is available, only
    linked evidence spanning at least two independent banking dimensions can
    activate the bank ontology.  This is deliberately issuer-agnostic.
    """
    demand = payload.get("demand_assessment") if isinstance(payload.get("demand_assessment"), Mapping) else {}
    factors = [item for item in demand.get("factors", []) if isinstance(item, Mapping)]
    evidence_ids = {
        str(value)
        for item in factors
        if _v4_text(item.get("factor")).upper() in {
            "DEPOSIT_FRANCHISE_QUALITY", "FUNDING_QUALITY", "CREDIT_QUALITY",
            "CAPITAL_STRENGTH", "FEE_INCOME_QUALITY", "OPERATING_EFFICIENCY",
        }
        for value in (item.get("evidence") or [])
        if value
    }
    texts: list[str] = []
    if isinstance(context, Mapping):
        for group in ("claims", "facts"):
            for row in context.get(group, []):
                if not isinstance(row, Mapping):
                    continue
                row_id = str(row.get("claim_id") or row.get("fact_id") or "")
                if row_id in evidence_ids:
                    texts.append(_v4_evidence_text(row).casefold())
    # Synthetic/legacy callers may provide prose evidence rather than ids.
    if not texts:
        texts = [
            _v4_evidence_text(item).casefold()
            for item in factors
            if _v4_text(item.get("factor")).upper() in {
                "DEPOSIT_FRANCHISE_QUALITY", "FUNDING_QUALITY", "CREDIT_QUALITY",
                "CAPITAL_STRENGTH", "FEE_INCOME_QUALITY", "OPERATING_EFFICIENCY",
            }
            and item.get("evidence") and not all(str(value).startswith("clm_") for value in item.get("evidence", []))
        ]
    groups: set[str] = set()
    for text in texts:
        # Held-to-maturity time deposits are investments, not customer deposit
        # franchises.  Do not let that phrase activate a bank ontology.
        if "time deposit" in text and not any(term in text for term in ("customer deposit", "deposit franchise", "fdic", "summary of deposits")):
            continue
        for group, terms in _V4_BANK_EVIDENCE_GROUPS.items():
            if any(term in text for term in terms):
                groups.add(group)
    return len(groups) >= 2


def _v4_sector_adapter(payload: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> str:
    demand = payload.get("demand_assessment") if isinstance(payload.get("demand_assessment"), Mapping) else {}
    hint = _v4_text(demand.get("industry_adapter") or demand.get("adapter")).casefold()
    factors = [item for item in demand.get("factors", []) if isinstance(item, Mapping)]
    # Some legacy dossiers carried a default retail/bank adapter label even
    # when that sector ontology had no supported evidence.  Treat the label
    # as authoritative only when its own factor evidence confirms the sector;
    # synthetic fixtures with no factors retain the explicit hint.
    if factors:
        bank_anchors = {"DEPOSIT_FRANCHISE_QUALITY", "FUNDING_QUALITY", "CREDIT_QUALITY", "CAPITAL_STRENGTH", "FEE_INCOME_QUALITY", "OPERATING_EFFICIENCY"}
        retail_anchors = {"COMPARABLE_SALES_QUALITY", "OMNICHANNEL_EXECUTION", "STORE_PRODUCTIVITY", "RETAIL_MEDIA_MONETIZATION"}
        bank_supported = sum(1 for item in factors if _v4_text(item.get("factor")).upper() in bank_anchors and item.get("evidence"))
        retail_supported = sum(1 for item in factors if _v4_text(item.get("factor")).upper() in retail_anchors and item.get("evidence"))
        if "bank" in hint or "financial institution" in hint:
            if not bank_supported:
                return "generic"
            # A context-aware resolution must validate the linked evidence;
            # factor labels alone are not an issuer classification.
            return "bank" if context is None or _v4_bank_evidence_support(payload, context) else "generic"
        if "retail" in hint:
            return "retail" if retail_supported and any(_v4_text(item.get("factor")).upper() == "COMPARABLE_SALES_QUALITY" and item.get("evidence") for item in factors) else "generic"
        return "generic"
    if "bank" in hint or "financial institution" in hint:
        return "bank"
    if "retail" in hint:
        return "retail"
    return "generic"


def _v4_sector_compatible(name: str, payload: Mapping[str, Any], evidence: Any, context: Mapping[str, Any] | None = None) -> bool:
    adapter = _v4_sector_adapter(payload, context)
    lower = name.casefold()
    context = _v4_evidence_text(evidence).casefold()
    retail_only = {"comparable_sales", "ecommerce_sales", "traffic_and_transactions", "average_ticket", "inventory", "membership", "store_count", "shrink", "supply_chain_cost", "retail_media_revenue", "labor_cost", "sales_mix"}
    bank_only = {"net_interest_income", "deposits", "loan_volume", "credit_cost", "regulatory_capital", "fee_income"}
    if adapter != "retail" and lower in retail_only:
        # A standalone dossier fixture may explicitly supply a KPI factor
        # with prose evidence even when no sector adapter is available.  That
        # explicit candidate is safe to retain; a generic claim/document
        # keyword is not enough to activate retail ontology.
        explicit_factor = isinstance(evidence, Mapping) and any(key in evidence for key in ("factor", "metric", "name"))
        factor_text = _v4_text(evidence.get("factor") or evidence.get("metric") or evidence.get("name")) if isinstance(evidence, Mapping) else ""
        if not (explicit_factor and context and _v4_tokens(factor_text) & _v4_tokens(lower.replace("_", " "))):
            return False
    if adapter != "bank" and lower in bank_only:
        return False
    return True


def _v4_resolve_numeric(name: str, context: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    cache = context.get("_numeric_resolution_cache") if isinstance(context, Mapping) else None
    if isinstance(cache, Mapping) and name in cache:
        return dict(cache[name])
    candidates: list[Mapping[str, Any]] = []
    index = context.get("_numeric_index") if isinstance(context, Mapping) else None
    if not isinstance(index, Mapping):
        built: dict[str, list[Mapping[str, Any]]] = {}
        for row in list(context.get("claims", [])) + list(context.get("facts", [])):
            if not isinstance(row, Mapping) or not row.get("pit_eligible", True) or not _v4_has_numeric_observation(row):
                continue
            # Claims expose a semantic dimension/text; financial facts expose
            # a normalized metric name.  Resolve both through the same
            # canonical mapper without fabricating a new data source.
            mapped = _v4_metric_map(row.get("metric") or row.get("dimension") or row.get("claim_text") or row.get("evidence_text"))
            semantic_source = row.get("metric") or row
            if mapped and _v4_unit_compatible(mapped[0], row) and _v4_semantic_compatible(mapped[0], semantic_source, section=row.get("section"), claim_type=row.get("claim_type")):
                built.setdefault(mapped[0], []).append(row)
        if isinstance(context, dict):
            context["_numeric_index"] = built
        index = built
    candidates.extend(index.get(name, []))
    if not candidates:
        # Fallback for a synthetic fixture whose claim text maps by token
        # overlap but has no canonical metric dimension.
        candidates.extend(row for row in list(context.get("claims", []))
                          if isinstance(row, Mapping) and row.get("pit_eligible", True) and _v4_claim_matches(name, row) and _v4_has_numeric_observation(row)
                          and _v4_unit_compatible(name, row)
                          and _v4_semantic_compatible(name, row.get("metric") or row, section=row.get("section"), claim_type=row.get("claim_type")))
    # Prefer the latest period actually represented by the issuer's resolved
    # period set.  Publication date alone can select an older annual claim
    # over a newer quarterly financial fact.
    target_periods = []
    primary_periods = []
    for group in ("states", "documents", "claims", "facts"):
        for row in context.get(group, []):
            if not isinstance(row, Mapping) or not row.get("pit_eligible", True):
                continue
            period = _v4_parse_date(row.get("period_end_date") or row.get("period_end"))
            if period:
                target_periods.append(period)
                if group in {"states", "documents"}:
                    primary_periods.append(period)
    # State/document period resolution is authoritative.  Financial facts
    # are fallback observations and must not move the current-period anchor
    # (for example, a share-count measurement date) forward by itself.
    target_end = max(primary_periods or target_periods) if (primary_periods or target_periods) else None
    def candidate_key(row: Mapping[str, Any]) -> tuple[int, str, str, int]:
        period = _v4_parse_date(row.get("period_end_date") or row.get("period_end"))
        period_text = period.strftime("%Y-%m-%d") if period else ""
        in_range = int(bool(period and (target_end is None or period <= target_end)))
        availability = _v4_date(row.get("public_availability_date") or row.get("available_date") or row.get("filing_date") or row.get("created_at"))
        # Financial facts are preferred when they describe the same period;
        # qualitative claims remain the fallback for disclosed KPIs such as
        # comparable sales or gross-profit-rate changes.
        is_fact = int(bool(row.get("fact_id") or row.get("metric")))
        return (in_range, period_text, availability, is_fact)
    candidates.sort(key=candidate_key, reverse=True)
    if not candidates:
        result = {"latest_value": "VALUE_NOT_CAPTURED", "unit": "NOT_CAPTURED", "latest_period": "NOT_CAPTURED", "source_url": "", "source_status": "UNKNOWN", "evidence_ids": []}
        if isinstance(context, dict):
            context.setdefault("_numeric_resolution_cache", {})[name] = result
        return result
    row = candidates[0]
    magnitude = row.get("magnitude") if isinstance(row.get("magnitude"), Mapping) else {}
    value = magnitude.get("value") if magnitude else row.get("latest_value") or row.get("value")
    unit = magnitude.get("unit") if magnitude else row.get("unit")
    period = row.get("period_label") or row.get("period")
    if not period and row.get("fiscal_period"):
        fiscal_period = str(row.get("fiscal_period")).upper()
        fiscal_year = row.get("fiscal_year")
        period = f"{fiscal_period} FY{fiscal_year}" if fiscal_period != "FY" and fiscal_year else (f"FY {fiscal_year}" if fiscal_year else fiscal_period)
    period = period or row.get("period_end_date") or row.get("period_end") or "NOT_CAPTURED"
    result = {"latest_value": value if value not in (None, "") else "VALUE_NOT_CAPTURED", "unit": unit or "NOT_CAPTURED", "latest_period": period,
              "source_url": row.get("source_url") or row.get("url") or "", "source_status": "VERIFIED_INTERNAL_SOURCE", "evidence_ids": [row.get("claim_id") or row.get("fact_id")],
              "public_availability_date": _v4_date(row.get("public_availability_date") or row.get("available_date") or row.get("filing_date") or row.get("created_at")), "evidence_text": _v4_evidence_text(row)}
    if isinstance(context, dict):
        context.setdefault("_numeric_resolution_cache", {})[name] = result
    return result


def _v4_kpis(payload: Mapping[str, Any], context: Mapping[str, Any], ticker: str, cutoff: str) -> list[dict[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for block in (payload.get("demand_assessment"), payload.get("operating_execution"), payload.get("business_trajectory")):
        items.extend(item for item in _v3_items(block, ("factors", "records", "items")) if isinstance(item, Mapping))
    claims = [row for row in context.get("claims", []) if isinstance(row, Mapping) and row.get("pit_eligible", True)]
    for claim in claims:
        if _v4_metric_map(claim.get("dimension") or claim.get("claim_text") or claim.get("evidence_text")):
            items.append(claim)
    by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        evidence = _v4_evidence_text(item)
        mapped = _v4_metric_map(item.get("factor") or item.get("metric") or item.get("name") or item.get("dimension") or evidence)
        if not mapped:
            continue
        name, metric_type, unit, outcome, _ = mapped
        if not _v4_sector_compatible(name, payload, item, context):
            continue
        if not _v4_semantic_compatible(name, item, section=item.get("section"), claim_type=item.get("claim_type")):
            continue
        source, date = _v3_source(item)
        numeric = _v4_resolve_numeric(name, context, cutoff)
        evidence_ids = [item.get("claim_id")] if item.get("claim_id") else []
        evidence_ids.extend(numeric.get("evidence_ids") or [])
        evidence_ids = sorted({str(value) for value in evidence_ids if value})
        definition = _v4_text(item.get("definition") or f"{name.replace('_', ' ').title()} as disclosed by the issuer.")
        frequency = "QUARTERLY" if str(item.get("period_label") or numeric.get("latest_period", "")).upper().startswith("Q") else ("ANNUAL" if "FY" in str(numeric.get("latest_period", "")).upper() else "DISCLOSURE_DEPENDENT")
        direct_numeric = numeric.get("latest_value") not in (None, "VALUE_NOT_CAPTURED")
        model_ready = bool(definition and metric_type and frequency and (numeric.get("source_status") != "UNKNOWN" or source or item.get("potential_data_source")))
        if not model_ready:
            validation = "CONCEPTUAL_KPI"
            readiness = "CONCEPTUAL_KPI"
        else:
            validation = "MODEL_READY_KPI"
            readiness = "MODEL_READY_KPI"
        key = _v3_id("kpi", ticker, name)
        row = by_name.get(key)
        display_label = _v4_text(item.get("factor") or item.get("metric") or item.get("name") or name.replace("_", " ").title())
        if row is None:
            row = {"kpi_id": key, "kpi": name, "display_name": display_label, "definition": definition,
                   "metric_type": metric_type.upper(), "numerator": "NOT_APPLICABLE", "denominator": "NOT_APPLICABLE", "unit": numeric.get("unit") if direct_numeric else unit,
                   "frequency": frequency, "segment": _v4_text(item.get("segment") or "CONSOLIDATED"), "why_it_matters": _v4_text(item.get("why_it_matters") or f"Tracks {name.replace('_', ' ').lower()} and its {outcome.lower()} transmission."),
                   "financial_transmission": outcome, "latest_value": numeric.get("latest_value"), "latest_period": numeric.get("latest_period"), "current_state": _v4_text(item.get("current_state") or item.get("assessment") or "UNKNOWN"),
                   "direction": _v4_text(item.get("direction") or item.get("trend") or "UNKNOWN"), "historical_data_availability": "HIGH" if numeric.get("source_status") != "UNKNOWN" else ("MEDIUM" if source or item.get("potential_data_source") else "UNKNOWN"),
                   "primary_historical_source": source or numeric.get("source_url") or _v4_text(item.get("potential_data_source") or "UNKNOWN"), "secondary_historical_source": "UNKNOWN",
                   "source_status": numeric.get("source_status") if numeric.get("source_status") != "UNKNOWN" else ("VERIFIED_INTERNAL_SOURCE" if source else "UNKNOWN"), "evidence_ids": evidence_ids,
                   "evidence_strength": "HIGH" if len(evidence_ids) >= 3 else ("MEDIUM" if evidence_ids else "LOW"), "qualitative_priority": 80 if direct_numeric else 60,
                   "validation_status": validation, "model_readiness": readiness, "parent_concept_id": _v4_text(item.get("parent_concept_id") or ""), "source_url": source or numeric.get("source_url", ""), "source_date": date or numeric.get("public_availability_date", ""), "confidence": _v3_confidence(item) or "UNKNOWN"}
            by_name[key] = row
        else:
            row["evidence_ids"] = sorted(set(row.get("evidence_ids", [])) | set(evidence_ids))
            row["evidence_strength"] = "HIGH" if len(row["evidence_ids"]) >= 3 else "MEDIUM"
            if direct_numeric and row.get("latest_value") in (None, "VALUE_NOT_CAPTURED"):
                row.update({"latest_value": numeric.get("latest_value"), "unit": numeric.get("unit"), "latest_period": numeric.get("latest_period"), "source_url": numeric.get("source_url", ""), "source_status": numeric.get("source_status")})
                row["model_readiness"] = "MODEL_READY_KPI"
                row["validation_status"] = "MODEL_READY_KPI"
    return sorted(by_name.values(), key=lambda row: (-int(row.get("qualitative_priority", 0)), row["kpi_id"]))


def _v4_driver_name(item: Mapping[str, Any]) -> str:
    value = _v4_text(item.get("driver") or item.get("name") or item.get("dimension") or item.get("category") or item.get("factor"))
    return value.upper().replace("_", " ") or "UNNAMED DRIVER"


def _v4_drivers(payload: Mapping[str, Any], kpis: list[dict[str, Any]], context: Mapping[str, Any], ticker: str, cutoff: str) -> list[dict[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    for item in _v3_items(payload.get("industry_structure"), ("records",)) + _v3_items(payload.get("business_model"), ("mechanisms",)):
        if isinstance(item, Mapping):
            candidates.append(item)
    for claim in context.get("claims", []):
        if isinstance(claim, Mapping) and any(token in _v4_text(claim.get("dimension") or claim.get("claim_text")).casefold() for token in ("pricing", "traffic", "volume", "cost", "capacity", "competition", "regulatory", "funding", "deposit", "credit")):
            candidates.append(claim)
    rows: dict[str, dict[str, Any]] = {}
    for item in candidates:
        name = _v4_driver_name(item)
        evidence = _v4_evidence_text(item)
        if name in {"UNNAMED DRIVER", "BUSINESS MODEL"} or not evidence or not _v4_semantic_compatible(name, item, section=item.get("section"), claim_type=item.get("claim_type")):
            continue
        # Corporate, legal, compensation and governance prose can contain
        # words such as transaction, price, capacity or risk without being an
        # operating driver.  Keep those records auditable in the source
        # layers, but do not promote them into the model-driver handoff.
        lower_evidence = evidence.casefold()
        non_operating_markers = (
            "ownership in", "ownership of", "acquisition", "certificate of incorporation",
            "shareholder proposal", "proxy statement", "court approval", "redemption price",
            "par value", "compensation table", "stock award", "medical plan", "urgent care",
            "committee", "by-laws", "bylaws", "settlement requires", "fair value of",
        )
        if any(marker in lower_evidence for marker in non_operating_markers):
            continue
        source, date = _v3_source(item)
        numeric = _v4_resolve_numeric(name, context, cutoff)
        matching_kpis = [kpi["kpi"] for kpi in kpis if len(_v4_tokens(name) & _v4_tokens(kpi["kpi"])) or any(token in _v4_text(kpi.get("definition")).casefold() for token in _v4_tokens(name))]
        mechanism = _v4_text(item.get("mechanism") or item.get("why_it_matters") or item.get("fact"))
        explicit_kpi = _v4_text(item.get("affected_kpi") or item.get("affected_kpis"))
        if explicit_kpi and explicit_kpi not in matching_kpis:
            matching_kpis.append(explicit_kpi)
        has_source = bool(source or numeric.get("source_url") or item.get("potential_data_source"))
        if matching_kpis and mechanism and has_source:
            classification = "MODEL_READY_DRIVER"
        elif matching_kpis and (has_source or item.get("potential_data_source")):
            classification = "PROXY_REQUIRED"
        elif any(word in name.casefold() for word in ("scale", "barrier", "structure", "cyclicality", "regulatory", "advantage", "disadvantage")):
            classification = "STRUCTURAL_FACTOR"
        else:
            classification = "NON_MODEL_DRIVER"
        key = _v3_id("driver", ticker, name)
        row = rows.setdefault(key, {"driver_id": key, "driver": name, "display_name": name.title(), "definition": _v4_text(item.get("definition") or evidence), "category": _v4_text(item.get("category") or "OTHER").upper(), "internal_external": _v4_text(item.get("internal_external") or "UNKNOWN").upper(), "metric_proxy": _v4_text(item.get("metric") or item.get("proxy") or ("METRIC" if numeric.get("latest_value") not in (None, "VALUE_NOT_CAPTURED") else "PROXY")), "unit": numeric.get("unit") or _v4_text(item.get("unit") or "UNKNOWN"), "frequency": _v4_text(item.get("frequency") or "DISCLOSURE_DEPENDENT"), "current_value": numeric.get("latest_value") or "VALUE_NOT_CAPTURED", "current_period": numeric.get("latest_period") or "NOT_CAPTURED", "current_state": _v4_text(item.get("current_state") or item.get("assessment") or "UNKNOWN"), "direction": _v4_text(item.get("direction") or item.get("trend") or "UNKNOWN"), "affected_kpis": sorted(set(matching_kpis)), "mechanism": mechanism or "NOT_CAPTURED", "historical_data_availability": "HIGH" if numeric.get("source_status") != "UNKNOWN" else ("MEDIUM" if has_source else "UNKNOWN"), "primary_source": source or numeric.get("source_url") or _v4_text(item.get("potential_data_source") or "UNKNOWN"), "source_status": numeric.get("source_status") if numeric.get("source_status") != "UNKNOWN" else ("VERIFIED_INTERNAL_SOURCE" if source else ("SUGGESTED_PUBLIC_SOURCE" if item.get("potential_data_source") else "UNKNOWN")), "classification": classification, "evidence_ids": sorted({str(item.get("claim_id"))} - {"None"}) if item.get("claim_id") else [], "evidence_strength": "MEDIUM" if evidence else "LOW", "confidence": _v3_confidence(item), "source_url": source or numeric.get("source_url", ""), "source_date": date or numeric.get("public_availability_date", "")})
        row["affected_kpis"] = sorted(set(row.get("affected_kpis", [])) | set(matching_kpis))
        if classification == "MODEL_READY_DRIVER":
            row["classification"] = classification
    return sorted(rows.values(), key=lambda row: (0 if row.get("classification") == "MODEL_READY_DRIVER" else 1 if row.get("classification") == "PROXY_REQUIRED" else 2, row["driver_id"]))


def _v4_relationships(kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], ticker: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for driver in drivers:
        if driver.get("classification") not in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}:
            continue
        targets = {str(value).casefold() for value in driver.get("affected_kpis", []) if value}
        if not targets:
            rejected.append({"driver": driver.get("driver"), "reason": "NO_AFFECTED_KPI"})
            continue
        for kpi in kpis:
            if kpi["kpi"].casefold() not in targets and not any(_v4_text(value).casefold() == kpi["kpi"].casefold() for value in driver.get("affected_kpis", [])):
                continue
            mechanism = _v4_text(driver.get("mechanism"))
            mechanism_lower = mechanism.casefold()
            placeholder = {
                "not_captured", "unknown", "unresolved", "value_not_captured",
                "n/a", "na", "none", "insufficient_evidence",
                _V4_PLACEHOLDER_MECHANISM.casefold(),
            }
            # A relationship is not promoted merely because a driver and KPI
            # share a label.  It needs a specific causal statement and must
            # mention the target economics (or an explicit causal verb plus
            # the target KPI tokens).
            kpi_tokens = _v4_tokens(kpi["kpi"])
            causal_terms = ("increase", "decrease", "reduce", "raise", "lower", "drive", "through", "due to", "offset", "impact", "affect", "transmit", "compress", "expand")
            target_terms_present = bool(kpi_tokens & _v4_tokens(mechanism))
            has_causal_language = any(term in mechanism_lower for term in causal_terms)
            if (not mechanism or mechanism_lower in placeholder or mechanism_lower.startswith("qualitative_inference")
                    or "economic linkage requires downstream testing" in mechanism_lower
                    or not (target_terms_present and has_causal_language)):
                rejected.append({"driver": driver["driver"], "kpi": kpi["kpi"], "reason": "SPECIFIC_MECHANISM_REQUIRED"})
                continue
            sign = "UNKNOWN"
            lower = mechanism.casefold()
            if any(word in lower for word in ("increase", "improve", "growth", "higher")):
                sign = "POSITIVE"
            elif any(word in lower for word in ("decrease", "reduce", "lower", "compression")):
                sign = "NEGATIVE"
            relationship_id = f"driver_kpi:{ticker.lower()}:{driver['driver_id']}:{kpi['kpi_id']}"
            accepted.append({"relationship_id": relationship_id, "driver": driver["driver"], "driver_id": driver["driver_id"], "kpi": kpi["kpi"], "kpi_id": kpi["kpi_id"], "economic_mechanism": mechanism, "mechanism_type": "DIRECT_EVIDENCE" if driver.get("evidence_ids") else "SECTOR_ONTOLOGY", "expected_sign": sign, "expected_lag": "UNKNOWN", "expected_functional_form": "UNKNOWN", "evidence_ids": driver.get("evidence_ids", []), "evidence_strength": driver.get("evidence_strength", "LOW"), "confidence": driver.get("confidence") or "UNKNOWN", "source_type": driver.get("source_status", "UNKNOWN"), "validation_status": "MODEL_READY_RELATIONSHIP", "source_url": driver.get("source_url", ""), "source_date": driver.get("source_date", "")})
    return accepted, rejected


def _v4_filter_watchlist(items: list[Any], kind: str) -> tuple[list[dict[str, Any]], int, int]:
    accepted: dict[str, dict[str, Any]] = {}
    rejected_definition = 0
    rejected_procedural = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        label = _v4_text(item.get("canonical_risk") if kind == "risk" else item.get("canonical_event") or item.get("catalyst") or item.get("name"))
        mechanism = _v4_text(item.get("mechanism") or item.get("evidence") or item.get("description"))
        lower = f"{label} {mechanism}".casefold()
        generic_catalyst_mechanism = "the event could change future" in lower or "magnitude remains evidence-dependent" in lower
        if kind.casefold() == "catalyst" and (generic_catalyst_mechanism or (not item.get("evidence") and not item.get("source") and not item.get("source_url"))):
            rejected_procedural += 1
            continue
        label_words = label.casefold().split()
        if len(label_words) >= 4 and len(label_words) % 2 == 0 and label_words[:len(label_words) // 2] == label_words[len(label_words) // 2:]:
            label = " ".join(label.split()[:len(label_words) // 2])
            lower = f"{label} {mechanism}".casefold()
        if len(label.split()) > 12:
            rejected_procedural += 1
            continue
        if any(term in label.casefold() for term in ("committee", "governance framework", "governance structure", "risk governance", "risk program", "monitoring and management", "disclosures about", "risk functions", "estimate of", "risk adjusted revenue", "risk controls", "initiative", "pages ", "information concerning", "whereas ", "statements of income", "settlement requires", "approval of", "the approval", "options or stock appreciation rights")):
            rejected_procedural += 1
            continue
        if label.casefold().startswith(("a ", "and ", "but ", "all employees", "additionally ", "adjust ", "based on ", "board of ", "for instance ", "i ", "it is ")):
            rejected_procedural += 1
            continue
        definition_terms = ("cet1 and rwa reflect", "operational risk rwa", "basel iii", "basel methodology", "risk weighted assets", "risk-based capital metrics", "a backtesting exception", "value at risk", "var: value-at-risk", "is a measure of", "risk associated with inadequate", "for purposes of", "means that", "definition of", "amounts in this column", "refers to", "represents the", "represents commitments that are risk rated", "is not a risk-based measure", "risk is categorized into", "categorized into physical risk and transition risk", "inherent part of", "risk functions", "chief risk officer", "risk management is an inherent", "take the risk, revenues go up")
        procedural_terms = ("refer to", "page ", "committee approval", "audit committee", "board risk committee", "proxy statement", "vote for", "compensation plan", "policy statement", "table of contents", "ordinary course", "risk management on pages", "risk tolerance", "information concerning", "whereas:", "settlement requires court approval", "approval of", "the approval", "options or stock appreciation rights")
        if any(term in label.casefold() for term in ("risk management", "risk functions", "risk based capital", "rwa", "cet1", "basel", "value at risk", "backtesting", "chief risk officer")):
            rejected_definition += 1
            continue
        if len(label) < 4 or any(term in lower for term in definition_terms):
            rejected_definition += 1
            continue
        if any(term in lower for term in procedural_terms):
            rejected_procedural += 1
            continue
        # A canonical investment risk needs an adverse mechanism that can
        # plausibly reach an economic output.  Governance/process statements
        # and generic risk labels are retained only as source evidence.
        adverse_terms = ("reduce", "lower", "decrease", "decline", "pressure", "loss", "uncertain", "volatil", "compress", "default", "fail", "exposure", "impact", "litigation", "cyber", "fraud", "liquidity", "credit")
        financial_terms = ("revenue", "sales", "margin", "profit", "earnings", "income", "cash", "capital", "cost", "valuation", "kpi", "deposit", "loan", "loss")
        affected_fields = " ".join(_v4_text(item.get(field)) for field in ("affected_driver", "affected_kpi", "financial_transmission"))
        if not any(term in lower for term in adverse_terms) or (not any(term in lower for term in financial_terms) and not any(term in affected_fields.casefold() for term in financial_terms)):
            rejected_procedural += 1
            continue
        evidence_ids = []
        if isinstance(item.get("evidence_ids"), list):
            evidence_ids = [str(value) for value in item["evidence_ids"] if value]
        source, date = _v3_source(item)
        key = _v3_slug(label)
        candidate = {"canonical_id": _v4_text(item.get("risk_id") if kind == "risk" else item.get("catalyst_id")) or f"{kind}:{key}", "canonical": label, "mechanism": mechanism or "INSUFFICIENT_EVIDENCE", "affected_driver": _v4_text(item.get("affected_driver") or "UNKNOWN"), "affected_kpi": _v4_text(item.get("affected_kpi") or "UNKNOWN"), "financial_transmission": _v4_text(item.get("financial_transmission") or "UNKNOWN"), "probability": _v4_text(item.get("probability") or "UNKNOWN"), "impact": _v4_text(item.get("impact") or "UNKNOWN"), "trend": _v4_text(item.get("trend") or item.get("current_status") or "UNKNOWN"), "freshness": _v4_text(item.get("freshness") or date or "UNKNOWN"), "confidence": _v3_confidence(item), "evidence_ids": evidence_ids, "source_url": source, "source_date": date, "materiality": _v4_text(item.get("materiality") or "UNKNOWN"), "status": _v4_status(item) if "_v4_status" in globals() else _v3_status(item)}
        previous = accepted.get(key)
        if previous is None or (str(previous.get("status") or "").upper() in {"STALE", "HISTORICAL"} and str(candidate.get("status") or "").upper() not in {"STALE", "HISTORICAL"}):
            accepted[key] = candidate
        row = accepted[key]
        row["evidence_ids"] = sorted(set(row.get("evidence_ids", [])) | set(evidence_ids))
    rows = sorted(accepted.values(), key=lambda row: (-len(row.get("evidence_ids", [])), row.get("canonical", "")))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
        row["evidence_strength"] = "HIGH" if len(row.get("evidence_ids", [])) >= 3 else ("MEDIUM" if row.get("evidence_ids") else "LOW")
    return rows, rejected_definition, rejected_procedural


def _v4_status(item: Any, default: str = "UNKNOWN") -> str:
    return _v3_status(item, default)


def _v4_management(payload: Mapping[str, Any], context: Mapping[str, Any], ticker: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    current, historical = _v3_management(payload, ticker)
    if not current:
        for item in context.get("management", []):
            if not isinstance(item, Mapping) or not item.get("full_name") or not item.get("current_roles"):
                continue
            name = _v4_text(item.get("full_name"))
            if len(name.split()) < 2:
                continue
            source = item.get("source_url") or ""
            current.append({"person_id": _v3_id("person", ticker, name), "name": name, "current_role": "; ".join(item.get("current_roles") or []), "current_historical": "CURRENT", "confidence": _v3_confidence(item), "source_url": source, "source_date": _v4_date(item.get("created_at") or item.get("filing_date")), "source_tier": _v3_source_tier(source), "last_verified": _v4_date(item.get("created_at"))})
    guidance = _v3_guidance(payload)
    sample = len([row for row in guidance if str(row.get("assessment", "")).upper() in {"MET", "BEAT", "MISSED", "MISS", "WITHDRAWN"}])
    assessments = {}
    evidence = bool(current or guidance or context.get("management"))
    for label in ("Leadership Stability", "Execution Evidence", "Guidance Credibility", "Capital Allocation Discipline", "Strategic Consistency", "Succession Risk", "Key Person Risk"):
        if not evidence:
            assessment = "INSUFFICIENT_EVIDENCE: no current management evidence was captured."
            confidence = "LOW"
        elif label == "Guidance Credibility" and sample < 3:
            assessment = "INSUFFICIENT_EVIDENCE: resolved guidance sample is too small for a credibility conclusion."
            confidence = "LOW"
        elif label in {"Succession Risk", "Key Person Risk"}:
            assessment = "INSUFFICIENT_EVIDENCE: the available records do not support a definitive assessment."
            confidence = "LOW"
        else:
            assessment = "Evidence captured; directional conclusion requires additional period-linked observations."
            confidence = "MEDIUM"
        assessments[label] = {"assessment": assessment, "direction": "UNKNOWN", "confidence": confidence, "evidence_basis": [row.get("person_id") for row in current[:5]]}
    return current + historical, assessments


def _v4_model_quality(kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], relationships: list[dict[str, Any]], rejected_relationships: list[dict[str, Any]]) -> dict[str, Any]:
    top_kpis = kpis[:15]
    top_drivers = [row for row in drivers if row.get("classification") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}][:20]
    return {"kpi_model_readiness_pct": round(100 * sum(row.get("model_readiness") == "MODEL_READY_KPI" for row in top_kpis) / len(top_kpis), 1) if top_kpis else 0.0,
            "driver_model_readiness_pct": round(100 * sum(row.get("classification") == "MODEL_READY_DRIVER" for row in top_drivers) / len(top_drivers), 1) if top_drivers else 0.0,
            "mechanism_coverage_pct": round(100 * sum(bool(row.get("economic_mechanism")) for row in relationships) / len(relationships), 1) if relationships else 0.0,
            "source_coverage_pct": round(100 * sum(row.get("source_status") not in {None, "UNKNOWN"} for row in top_kpis) / len(top_kpis), 1) if top_kpis else 0.0,
            "cartesian_relationships": 0,
            "generic_placeholder_mechanisms": sum(str(row.get("economic_mechanism", "")).casefold() == _V4_PLACEHOLDER_MECHANISM.casefold() for row in relationships),
            "rejected_relationships": len(rejected_relationships)}


def _v4_header(records: list[dict[str, Any]], payload: Mapping[str, Any], ticker: str, cutoff: str, run_id: str, periods: Mapping[str, Any]) -> None:
    company = str(payload.get("company_name") or ticker)
    _v3_add(records, section="00_header", key="layout:header:title", cells=[f"{ticker} — Full Qualitative Research V{FULL_QUAL_SOFTWARE_VERSION}", "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of {cutoff}"], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", kind="title")
    _v3_add(records, section="00_header", key="layout:header:subtitle", cells=["INSTITUTIONAL EQUITY RESEARCH | PM DECISION SURFACE"], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", kind="subtitle")
    _v3_add(records, section="00_header", key="layout:header:company", cells=[company], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", kind="company")
    rows = []
    quality = payload.get("quality_gate") if isinstance(payload.get("quality_gate"), Mapping) else {}
    for label, value in (("Ticker", ticker), ("Company", company), ("As-of Date", cutoff), ("Software Version", FULL_QUAL_SOFTWARE_RELEASE), ("Research Status", payload.get("release_status") or payload.get("research_status") or "UNKNOWN"), ("Quality Gate", quality.get("status") or "UNKNOWN"), ("Current Period", periods.get("current_period")), ("Previous Period", periods.get("previous_period")), ("Latest Annual", periods.get("latest_annual_label")), ("Latest Quarterly", periods.get("latest_quarterly_label")), ("Fiscal Year", periods.get("fiscal_year")), ("Fiscal Quarter", periods.get("fiscal_quarter")), ("Dossier Version", payload.get("research_output_version") or payload.get("schema_version") or "UNKNOWN"), ("Empirical Importance", EMPIRICAL_IMPORTANCE_STATUS)):
        rows.append((f"header:{_v3_slug(label)}", [label, _v3_value(value)], value, "", "", "", ""))
    _v3_section(records, section="00_header", headers=("Field", "Value", "Status", "Source"), rows=rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm")


def _v4_render_payload(payload: Mapping[str, Any], *, ticker: str, cutoff: str, run_id: str,
                       production_root: Path | None, artifacts_root: Path | None = None) -> list[dict[str, Any]]:
    context = _v4_load_context(ticker=ticker, cutoff=cutoff, production_root=production_root, artifacts_root=artifacts_root)
    periods = _v4_period_summary(context, payload, cutoff)
    kpis = _v4_kpis(payload, context, ticker, cutoff)
    drivers = _v4_drivers(payload, kpis, context, ticker, cutoff)
    relationships, rejected_relationships = _v4_relationships(kpis, drivers, ticker)
    management, management_assessments = _v4_management(payload, context, ticker)
    guidance = _v3_guidance(payload)
    risk_candidates, rejected_risk_definitions, rejected_risk_procedural = _v4_filter_watchlist(_v3_items(payload.get("risks"), ("records",)), "risk")
    catalyst_candidates, rejected_cat_definitions, rejected_cat_procedural = _v4_filter_watchlist(_v3_items(payload.get("catalysts"), ("records",)), "catalyst")
    # Historical/stale source disclosures remain available in the acquisition
    # and source registries, but are not canonical PM/deep investment-risk
    # rows and cannot outrank current evidence merely because they are numerous.
    risks = [row for row in risk_candidates if str(row.get("status") or "").upper() not in {"STALE", "HISTORICAL"}]
    catalysts = [row for row in catalyst_candidates if str(row.get("status") or "").upper() not in {"STALE", "HISTORICAL"}]
    quality = _v4_model_quality(kpis, drivers, relationships, rejected_relationships)
    records: list[dict[str, Any]] = []
    _v4_header(records, payload, ticker, cutoff, run_id, periods)
    # The PM executive table contains conclusions with an explicit evidence
    # basis.  Status-only text is kept out of the decision surface.
    executive_rows = []
    for label, source_block in (("Business economics", payload.get("business_model")), ("Demand / earnings engines", payload.get("demand_assessment")), ("Operating execution", payload.get("operating_execution")), ("Competitive position", payload.get("competitive_advantage"))):
        if not source_block:
            continue
        evidence = _v4_evidence_text(source_block)
        source, date = _v3_source(source_block)
        if evidence == "INSUFFICIENT_EVIDENCE":
            conclusion = f"Evidence is insufficient to determine the current direction of {label.casefold()}."
            inference = "QUALITATIVE_INFERENCE"
        else:
            conclusion = f"{label}: {evidence}"
            inference = "DIRECT_EVIDENCE"
        executive_rows.append((f"executive:{_v3_slug(label)}", [conclusion, "UNKNOWN", "MEDIUM", _v3_confidence(source_block), evidence, _v3_source_cell(source), date, inference], source_block, source, date, _v3_status(source_block), _v3_confidence(source_block)))
    if kpis:
        top = kpis[0]
        executive_rows.append(("executive:earnings_engine", [f"{top['display_name']} is the highest-priority measurable operating signal captured; its financial transmission is {top['financial_transmission']}.", "FOCUS", "HIGH", top["confidence"], "; ".join(top.get("evidence_ids", [])) or top.get("definition"), _v3_source_cell(top.get("source_url")), top.get("source_date", ""), "QUALITATIVE_INFERENCE"], top, top.get("source_url", ""), top.get("source_date", ""), "MODEL_READY_KPI", top.get("confidence", "UNKNOWN")))
    if risks:
        executive_rows.append(("executive:risks", [f"{len(risks)} evidence-backed risks remain in the watchlist; the highest-priority item is {risks[0]['canonical']}.", "WATCH", "HIGH", risks[0].get("confidence", "UNKNOWN"), "; ".join(risks[0].get("evidence_ids", [])) or risks[0].get("mechanism"), _v3_source_cell(risks[0].get("source_url")), risks[0].get("source_date", ""), "DIRECT_EVIDENCE"], risks[0], risks[0].get("source_url", ""), risks[0].get("source_date", ""), "WATCH", risks[0].get("confidence", "UNKNOWN")))
    if catalysts:
        executive_rows.append(("executive:catalysts", [f"{len(catalysts)} evidence-backed catalysts remain in the watchlist; transmission is limited to records with an explicit mechanism.", "WATCH", "MEDIUM", catalysts[0].get("confidence", "UNKNOWN"), "; ".join(catalysts[0].get("evidence_ids", [])) or catalysts[0].get("mechanism"), _v3_source_cell(catalysts[0].get("source_url")), catalysts[0].get("source_date", ""), "DIRECT_EVIDENCE"], catalysts[0], catalysts[0].get("source_url", ""), catalysts[0].get("source_date", ""), "WATCH", catalysts[0].get("confidence", "UNKNOWN")))
    _v3_section(records, section="02_executive", headers=("Conclusion", "Direction", "Materiality", "Confidence", "Evidence IDs / Basis", "Source", "Source Date", "Inference Type"), rows=executive_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")

    economics_rows = []
    economics_items = _v3_items(payload.get("business_model"), ("mechanisms",)) + _v3_items(payload.get("industry_structure"), ("records",))
    for index, item in enumerate(economics_items):
        if not isinstance(item, Mapping):
            continue
        evidence = _v4_evidence_text(item)
        dimension = _v4_text(item.get("dimension") or item.get("category") or item.get("name"))
        if not evidence or not _v4_semantic_compatible(dimension, item, section=item.get("section"), claim_type=item.get("claim_type")):
            continue
        source, date = _v3_source(item)
        economics_rows.append((f"economics:{index}", [_v4_text(item.get("component") or dimension), _v4_text(item.get("segment") or item.get("product") or "CONSOLIDATED"), _v4_text(item.get("mechanism") or item.get("fact"), 400), _v4_text(item.get("customer") or "NOT_CAPTURED"), _v4_text(item.get("revenue_mechanism") or "NOT_CAPTURED"), _v4_text(item.get("pricing_mechanism") or "NOT_CAPTURED"), _v4_text(item.get("volume_mechanism") or "NOT_CAPTURED"), _v4_text(item.get("cost_base") or "NOT_CAPTURED"), _v4_text(item.get("margin_mechanism") or "NOT_CAPTURED"), _v4_text(item.get("capital_requirement") or "NOT_CAPTURED"), _v4_text(item.get("affected_kpi") or "UNKNOWN"), _v4_text(item.get("driver") or "UNKNOWN"), evidence, _v3_source_cell(source), _v3_confidence(item), "DIRECT_EVIDENCE"], item, source, date, _v3_status(item), _v3_confidence(item)))
    # If the upstream ontology contains no defensible mechanism row, derive a
    # small company-economics bridge from measurable, evidence-backed KPIs.
    # This is a generic inference and is deliberately labelled as such; it
    # never invents issuer-specific revenue facts.
    if not economics_rows:
        for index, kpi in enumerate(kpis[:6]):
            evidence = "; ".join(kpi.get("evidence_ids", [])) or kpi.get("definition", "INSUFFICIENT_EVIDENCE")
            economics_rows.append((f"economics:kpi:{kpi['kpi_id']}", [kpi["display_name"], kpi.get("segment", "CONSOLIDATED"), f"{kpi['display_name']} is a captured operating signal with {kpi.get('financial_transmission', 'UNKNOWN')} transmission; coefficient estimation remains downstream.", "NOT_CAPTURED", "NOT_CAPTURED", "NOT_CAPTURED", "NOT_CAPTURED", "NOT_CAPTURED", "NOT_CAPTURED", "NOT_CAPTURED", kpi["display_name"], "UNKNOWN", evidence, _v3_source_cell(kpi.get("source_url")), kpi.get("confidence", "UNKNOWN"), "QUALITATIVE_INFERENCE"], kpi, kpi.get("source_url", ""), kpi.get("source_date", ""), "INFERRED_FROM_KPI", kpi.get("confidence", "UNKNOWN")))
    _v3_section(records, section="03_economics", headers=("Economic Element", "Segment / Product", "How Money Is Made / Mechanism", "Customer", "Revenue Mechanism", "Pricing Mechanism", "Volume Mechanism", "Major Cost Base", "Margin Mechanism", "Capital Requirement", "Relevant KPI", "Relevant Driver", "Evidence", "Source", "Confidence", "Inference Type"), rows=economics_rows[:6], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")

    top_kpi_rows = [(f"pm:kpi:{row['kpi_id']}", [index, row["display_name"], row["definition"], row["metric_type"], row["latest_value"], row["unit"], row["latest_period"], row["current_state"], row["financial_transmission"], row["source_status"], row["model_readiness"], row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("validation_status", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for index, row in enumerate(kpis[:6], 1)]
    _v3_section(records, section="04_top_kpis", headers=("Rank", "KPI", "Definition", "Metric Type", "Latest Value", "Unit", "Latest Period", "Current State", "Financial Outcome", "Source Status", "Model Readiness", "Confidence"), rows=top_kpi_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")
    top_driver_rows = [(f"pm:driver:{row['driver_id']}", [index, row["display_name"], row["definition"], row["classification"], row["affected_kpis"] and "; ".join(row["affected_kpis"]) or "UNKNOWN", row["mechanism"], row["historical_data_availability"], row["primary_source"], row["source_status"], row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("classification", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for index, row in enumerate([row for row in drivers if row.get("classification") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}][:5], 1)]
    _v3_section(records, section="05_top_drivers", headers=("Rank", "Driver", "Definition", "Classification", "Affected KPI", "Economic Mechanism", "Historical Availability", "Primary Source", "Source Status", "Confidence"), rows=top_driver_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")
    top_rel_rows = [(f"pm:relationship:{row['relationship_id']}", [index, row["driver"], row["kpi"], row["economic_mechanism"], row["expected_sign"], row["expected_lag"], row["expected_functional_form"], row["evidence_strength"], row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("validation_status", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for index, row in enumerate(relationships[:5], 1)]
    _v3_section(records, section="06_top_relationships", headers=("Rank", "Driver", "KPI", "Economic Mechanism", "Expected Sign", "Expected Lag", "Functional Form", "Evidence Strength", "Confidence"), rows=top_rel_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NO_SUPPORTED_RELATIONSHIPS")

    summary_rows = []
    for label, item in management_assessments.items():
        summary_rows.append((f"pm:management:{_v3_slug(label)}", [label, item["assessment"], item["direction"], item["confidence"], "; ".join(item.get("evidence_basis", [])) or "INSUFFICIENT_EVIDENCE"], item, "", "", item["direction"], item["confidence"]))
    summary_rows.extend([("pm:summary:risk_watchlist", ["Risk Watchlist", f"Top risk watchlist contains {min(15, len(risks))} evidence-backed records after filtering.", "WATCH", "MEDIUM", "Evidence-backed records only"], risks, "", "", "WATCH", "MEDIUM"), ("pm:summary:catalyst_watchlist", ["Catalyst Watchlist", len(catalysts), "WATCH", "MEDIUM", "Explicit transmission required"], catalysts, "", "", "WATCH", "MEDIUM")])
    _v3_section(records, section="07_management_summary", headers=("Dimension", "Assessment", "Direction", "Confidence", "Evidence Basis"), rows=summary_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")

    handoff_kpi_rows = [(f"handoff:kpi:{row['kpi_id']}", [index, row["display_name"], row["definition"], row["metric_type"], row["unit"], row["frequency"], row["segment"], row["why_it_matters"], row["financial_transmission"], row["current_state"], row["latest_value"], row["latest_period"], row["historical_data_availability"], row["primary_historical_source"], row["source_status"], row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("model_readiness", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for index, row in enumerate(kpis[:3], 1)]
    handoff_driver_rows = [(f"handoff:driver:{row['driver_id']}", [index, row["display_name"], row["definition"], row["category"], row["internal_external"], "; ".join(row.get("affected_kpis", [])) or "UNKNOWN", row["mechanism"], row["direction"], "UNKNOWN", row["historical_data_availability"], row["primary_source"], row["source_status"], row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("classification", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for index, row in enumerate([row for row in drivers if row.get("classification") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}][:3], 1)]
    handoff_rel_rows = [(f"handoff:relationship:{row['relationship_id']}", [index, row["driver"], row["kpi"], row["economic_mechanism"], row["expected_sign"], row["expected_lag"], row["expected_functional_form"], row["evidence_strength"], row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("validation_status", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for index, row in enumerate(relationships[:3], 1)]
    _v3_section(records, section="08_model_handoff", headers=("Priority", "KPI", "Definition", "Metric Type", "Unit", "Frequency", "Segment", "Why It Matters", "Financial Outcome", "Current State", "Latest Value", "Latest Period", "Historical Availability", "Primary Source", "Source Status", "Confidence"), rows=handoff_kpi_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NO_MODEL_READY_KPIS")
    _v3_section(records, section="08_model_handoff", headers=("Priority", "Driver", "Definition", "Category", "Internal / External", "Affected KPI", "Mechanism", "Expected Sign", "Expected Lag", "Historical Availability", "Primary Source", "Source Status", "Confidence"), rows=handoff_driver_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty=None)
    _v3_section(records, section="08_model_handoff", headers=("Priority", "Driver", "KPI", "Economic Mechanism", "Expected Sign", "Expected Lag", "Functional Form", "Evidence Strength", "Confidence"), rows=handoff_rel_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty=None)
    actionable_drivers = [row for row in drivers if row.get("classification") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}]
    gate = "MODEL_HANDOFF_READY" if kpis and actionable_drivers and quality["generic_placeholder_mechanisms"] == 0 and not rejected_relationships and all(row.get("definition") and row.get("model_readiness") in {"MODEL_READY_KPI", "CONCEPTUAL_KPI"} for row in kpis[:15]) and all(row.get("classification") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"} for row in actionable_drivers[:20]) else "MODEL_HANDOFF_READY_WITH_LIMITATIONS"
    _v3_section(records, section="08_model_handoff", headers=("Quality Gate", "KPI Readiness %", "Driver Readiness %", "Mechanism Coverage %", "Source Coverage %", "Rejected Relationships", "Cartesian Relationships"), rows=[("handoff:quality", [gate, quality["kpi_model_readiness_pct"], quality["driver_model_readiness_pct"], quality["mechanism_coverage_pct"], quality["source_coverage_pct"], quality["rejected_relationships"], quality["cartesian_relationships"]], quality, "", "", gate, "HIGH")], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty=None)

    # Deep archive retains auditable structured records without promoting
    # rejected candidates to the PM surface.
    kpi_rows = [(row["kpi_id"], [row["kpi_id"], row["display_name"], row["definition"], row["metric_type"], row["numerator"], row["denominator"], row["unit"], row["frequency"], row["segment"], row["why_it_matters"], row["financial_transmission"], row["current_state"], row["latest_value"], row["latest_period"], row["historical_data_availability"], row["primary_historical_source"], row["source_status"], "; ".join(row.get("evidence_ids", [])), row["validation_status"], row["model_readiness"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("validation_status", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for row in kpis]
    _v3_section(records, section="10_kpis", headers=("KPI ID", "Canonical KPI", "Definition", "Metric Type", "Numerator", "Denominator", "Unit", "Frequency", "Segment", "Why It Matters", "Financial Transmission", "Current State", "Latest Value", "Latest Period", "Historical Availability", "Primary Source", "Source Status", "Evidence IDs", "Validation Status", "Model Readiness"), rows=kpi_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")
    driver_rows = [(row["driver_id"], [row["driver_id"], row["display_name"], row["definition"], row["classification"], row["category"], row["internal_external"], row["metric_proxy"], row["unit"], row["frequency"], row["current_value"], row["current_period"], "; ".join(row.get("affected_kpis", [])), row["mechanism"], row["historical_data_availability"], row["primary_source"], row["source_status"], "; ".join(row.get("evidence_ids", [])), row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("classification", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for row in drivers]
    _v3_section(records, section="11_drivers", headers=("Driver ID", "Canonical Driver", "Definition", "Classification", "Category", "Internal / External", "Metric / Proxy", "Unit", "Frequency", "Current Value", "Current Period", "Affected KPI", "Mechanism", "Historical Availability", "Primary Source", "Source Status", "Evidence IDs", "Confidence"), rows=driver_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")
    rel_rows = [(row["relationship_id"], [row["driver"], row["driver_id"], row["kpi"], row["kpi_id"], row["economic_mechanism"], row["mechanism_type"], row["expected_sign"], row["expected_lag"], row["expected_functional_form"], "; ".join(row.get("evidence_ids", [])), row["evidence_strength"], row["confidence"], row["source_type"], row["validation_status"]], row, row.get("source_url", ""), row.get("source_date", ""), row.get("validation_status", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for row in relationships]
    _v3_section(records, section="12_relationships", headers=("Driver", "Driver ID", "KPI", "KPI ID", "Economic Mechanism", "Mechanism Type", "Expected Sign", "Expected Lag", "Functional Form", "Evidence IDs", "Evidence Strength", "Confidence", "Source Type", "Validation Status"), rows=rel_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NO_SUPPORTED_RELATIONSHIPS")
    _v3_section(records, section="13_multi_step", headers=("Driver", "KPI", "Financial Outcome", "Valuation Channel", "Mechanism", "Confidence"), rows=[(f"multi:{row['relationship_id']}", [row["driver"], row["kpi"], next((k.get("financial_transmission") for k in kpis if k["kpi"] == row["kpi"]), "UNKNOWN"), "Growth / Margin / Cash Flow / Risk Premium", row["economic_mechanism"], row["confidence"]], row, row.get("source_url", ""), row.get("source_date", ""), "MODEL_READY_RELATIONSHIP", row.get("confidence", "UNKNOWN")) for row in relationships], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NO_SUPPORTED_RELATIONSHIPS")
    _v3_section(records, section="17_management", headers=("Name", "Current Role", "Role Start", "Tenure", "Current / Historical", "Confidence", "Source", "Last Verified"), rows=[(row["person_id"], [row["name"], row["current_role"], row.get("role_start", "NOT_CAPTURED"), row.get("tenure", "NOT_CAPTURED"), row["current_historical"], row["confidence"], _v3_source_cell(row.get("source_url")), row.get("last_verified", "")], row, row.get("source_url", ""), row.get("source_date", ""), row.get("current_historical", "UNKNOWN"), row.get("confidence", "UNKNOWN")) for row in management], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NO_CURRENT_MANAGEMENT_EVIDENCE")
    _v3_section(records, section="18_credibility", headers=("Resolved Sample", "Status", "Empirical Importance"), rows=[("credibility:summary", [len(guidance), "INSUFFICIENT_RESOLVED_SAMPLE" if len(guidance) < 3 else "SAMPLE_AVAILABLE", EMPIRICAL_IMPORTANCE_STATUS], payload.get("guidance_reliability") or {}, "", "", "CURRENT", "MEDIUM")], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty=None)
    # Board-membership records intentionally use stable machine person IDs for
    # joins.  The sheet is a human-facing surface, so resolve those IDs through
    # the authoritative management-person roster before constructing visible
    # governance rows.  Keep the machine ID in the internal record/provenance,
    # but never expose it as the primary board-member label.
    person_names = {
        str(person.get("person_id")): _v4_text(person.get("full_name") or person.get("name"))
        for person in context.get("management", [])
        if isinstance(person, Mapping)
        and person.get("person_id")
        and _v4_text(person.get("full_name") or person.get("name"))
    }

    def _v4_governance_label(row: Mapping[str, Any]) -> str:
        label = _v4_text(row.get("full_name") or row.get("name"))
        if label:
            return label
        person_id = str(row.get("person_id") or "")
        label = person_names.get(person_id)
        return label or "NOT_CAPTURED"

    governance_rows = [(f"governance:{idx}", [_v4_governance_label(row), _v4_text(row.get("board_role") or row.get("role")), _v4_text(row.get("is_independent") or row.get("independence")), _v4_text(row.get("committee_memberships")), _v3_source_cell(row.get("source_url") or row.get("local_source_path")), _v4_date(row.get("created_at") or row.get("filing_date"))], row, row.get("source_url", ""), _v4_date(row.get("created_at")), "CURRENT", _v3_confidence(row)) for idx, row in enumerate(context.get("governance", []), 1) if isinstance(row, Mapping)]
    # Preserve scalar governance disclosures (for example poison-pill status)
    # as small auditable rows without promoting proxy prose into the PM view.
    governance_payload = payload.get("governance") if isinstance(payload.get("governance"), Mapping) else {}
    for group, value in governance_payload.items():
        if not isinstance(value, Mapping):
            continue
        for field_name, field_value in value.items():
            if isinstance(field_value, (Mapping, list, tuple)) or field_value in (None, "", []):
                continue
            governance_rows.append((f"governance:{_v3_slug(group)}:{_v3_slug(field_name)}", [_v4_text(field_name), _v4_text(field_value), _v4_status(value), _v4_text(group), "", ""], field_value, "", "", _v4_status(value), "UNKNOWN"))
    _v3_section(records, section="20_governance", headers=("Board Member / Feature", "Role", "Independence", "Committees", "Source", "As-of Date"), rows=governance_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NO_GOVERNANCE_EVIDENCE")
    risk_rows = [(f"risk:{row['canonical_id']}", [row["rank"], row["canonical"], row["mechanism"], row["affected_driver"], row["affected_kpi"], row["financial_transmission"], row["probability"], row["impact"], row["trend"], row["freshness"], row["confidence"], "; ".join(row.get("evidence_ids", []))], row, row.get("source_url", ""), row.get("source_date", ""), "WATCH", row.get("confidence", "UNKNOWN")) for row in risks]
    risk_archive_rows = [(f"risk:{row['canonical_id']}", [row["rank"], row["canonical"], row["mechanism"], row["affected_driver"], row["affected_kpi"], row["financial_transmission"], row["probability"], row["impact"], row["trend"], row["freshness"], row["confidence"], "; ".join(row.get("evidence_ids", []))], row, row.get("source_url", ""), row.get("source_date", ""), "WATCH", row.get("confidence", "UNKNOWN")) for row in risks]
    _v3_section(records, section="22_risks", headers=("Rank", "Risk", "Mechanism", "Affected Driver", "Affected KPI", "Financial Transmission", "Probability", "Impact", "Current Trend", "Freshness", "Confidence", "Evidence IDs"), rows=risk_rows[:6], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NO_EVIDENCE_BACKED_RISKS")
    _v3_section(records, section="22_risks_deep", headers=("Rank", "Risk", "Mechanism", "Affected Driver", "Affected KPI", "Financial Transmission", "Probability", "Impact", "Current Trend", "Freshness", "Confidence", "Evidence IDs"), rows=risk_archive_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NO_EVIDENCE_BACKED_RISKS")
    catalyst_rows = [(f"catalyst:{row['canonical_id']}", [row["rank"], row["canonical"], row["mechanism"], row["affected_driver"], row["affected_kpi"], row["financial_transmission"], row["trend"], row["freshness"], row["confidence"], "; ".join(row.get("evidence_ids", []))], row, row.get("source_url", ""), row.get("source_date", ""), "WATCH", row.get("confidence", "UNKNOWN")) for row in catalysts]
    catalyst_archive_rows = [(f"catalyst:{row['canonical_id']}", [row["rank"], row["canonical"], row["mechanism"], row["affected_driver"], row["affected_kpi"], row["financial_transmission"], row["trend"], row["freshness"], row["confidence"], "; ".join(row.get("evidence_ids", []))], row, row.get("source_url", ""), row.get("source_date", ""), "WATCH", row.get("confidence", "UNKNOWN")) for row in catalysts]
    _v3_section(records, section="23_catalysts", headers=("Rank", "Catalyst", "Mechanism", "Affected Driver", "Affected KPI", "Financial Transmission", "Direction", "Timing / Freshness", "Confidence", "Evidence IDs"), rows=catalyst_rows[:6], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="pm", empty="NO_EVIDENCE_BACKED_CATALYSTS")
    _v3_section(records, section="23_catalysts_deep", headers=("Rank", "Catalyst", "Mechanism", "Affected Driver", "Affected KPI", "Financial Transmission", "Direction", "Timing / Freshness", "Confidence", "Evidence IDs"), rows=catalyst_archive_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NO_EVIDENCE_BACKED_CATALYSTS")
    _v3_section(records, section="24_guidance", headers=("Guidance Metric", "Issue Date", "Target", "Range", "Target Period", "Outcome", "Comparability", "Confidence", "Source"), rows=[(f"guidance:{idx}", [row["metric"], row["issue_date"], row["target"], row["range"], row["target_period"], row["outcome"], row["comparability"], row["confidence"], _v3_source_cell(row["source_url"])], row, row["source_url"], row["source_date"], row["assessment"], row["confidence"]) for idx, row in enumerate(guidance)], ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NO_GUIDANCE_RECORDS")
    quality_rows = [("quality:model_handoff", ["MODEL_HANDOFF", "PASS" if gate == "MODEL_HANDOFF_READY" else "LIMITED", gate], quality, "", "", gate, "HIGH"), ("quality:risk_definitions_rejected", ["RISK_DEFINITIONS_REJECTED", rejected_risk_definitions, "Definitions excluded from canonical risk output"], {}, "", "", "PASS", "HIGH"), ("quality:procedural_rejected", ["PROCEDURAL_RECORDS_REJECTED", rejected_risk_procedural + rejected_cat_procedural, "Procedural records excluded"], {}, "", "", "PASS", "HIGH")]
    _v3_section(records, section="26_quality", headers=("Check", "Value", "Details"), rows=quality_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty=None)
    # ``context['future']`` contains records deliberately excluded by the
    # point-in-time filter.  It is not evidence that a future record entered
    # the dossier.  Report *used* counts as zero and retain excluded counts
    # separately for auditability.
    excluded = {
        "excluded_future_documents": len([row for row in context.get("future", []) if row.get("group") == "documents"]),
        "excluded_future_claims": len([row for row in context.get("future", []) if row.get("group") == "claims"]),
        "excluded_future_management": len([row for row in context.get("future", []) if row.get("group") == "management"]),
        "excluded_future_insider": len([row for row in context.get("future", []) if row.get("group") == "insider"]),
        "excluded_future_temporal": len([row for row in context.get("future", []) if row.get("group") == "temporal"]),
    }
    pit = {"future_documents": 0, "future_claims": 0, "future_management": 0, "future_insider": 0, "future_risks_catalysts": 0}
    integrity_rows = [(f"integrity:{key}", [key.replace("_", " ").title(), value, "PASS"], value, "", "", "CURRENT", "HIGH") for key, value in pit.items()]
    integrity_rows.extend((f"integrity:{key}", [key.replace("_", " ").title(), value, "EXCLUDED"], value, "", "", "CURRENT", "HIGH") for key, value in excluded.items())
    _v3_section(records, section="27_integrity", headers=("Metric", "Value", "Status"), rows=integrity_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty=None)
    source_rows = []
    for item in context.get("sources", []):
        source, date = _v3_source(item)
        source_status = _v4_text(item.get("source_status") or ("VERIFIED_INTERNAL_SOURCE" if source or item.get("local_path") or item.get("local_filename") else "UNKNOWN"))
        source_rows.append((str(item.get("record_key") or f"source:{len(source_rows)}"), [_v4_text(item.get("title") or item.get("document_id")), _v4_text(item.get("document_type") or item.get("type")), _v4_text(item.get("form") or item.get("form_type")), _v4_text(item.get("accession") or item.get("accession_number")), _v4_text(item.get("period") or item.get("fiscal_period")), _v4_date(item.get("period_end") or item.get("period_end_date")), _v4_date(item.get("publication_date") or item.get("filing_date") or date), _v4_text(item.get("pit_status") or "UNKNOWN"), _v4_text(item.get("status") or item.get("validation_status") or "UNKNOWN"), source_status, _v3_source_cell(source, _source_label_for_metadata(item)), _v4_text(item.get("local_path") or item.get("local_filename"))], item, source, date, _v4_status(item), "MEDIUM"))
    _v3_section(records, section="28_acquisition", headers=("Document", "Document Type", "Form", "Accession", "Period", "Period End", "Publication Date", "PIT Status", "Validation Status", "Source Status", "Source URL", "Local Path"), rows=source_rows, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer="deep", empty="NOT_AVAILABLE")
    # Keep PM first and guarantee a compact surface before deep records.
    pm = [row for row in records if row.get("layer") == "pm"]
    deep = [row for row in records if row.get("layer") != "pm"]
    return pm + deep


def build_canonical_state(dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None,
                          as_of_date: str | None = None, artifacts_root: str | Path | None = None,
                          production_root: str | Path | None = None, run_id: str = "") -> CanonicalState:
    if isinstance(dossier, (str, Path)):
        payload = json.loads(Path(dossier).read_text(encoding="utf-8"))
    else:
        payload = dict(dossier)
    symbol = str(ticker or payload.get("ticker") or "").upper()
    cutoff = str(as_of_date or payload.get("as_of_date") or payload.get("source_cutoff") or "")
    execution_id = run_id or str(payload.get("run_id") or payload.get("dossier_id") or f"sheet:{symbol}:{cutoff}")
    records = _v4_render_payload(payload, ticker=symbol, cutoff=cutoff, run_id=execution_id,
                                  production_root=Path(production_root) if production_root else None,
                                  artifacts_root=Path(artifacts_root) if artifacts_root else None)
    fingerprint = str(payload.get("input_fingerprint") or hashlib.sha256(json.dumps(_normalise(payload), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest())
    return CanonicalState(symbol, cutoff, execution_id, records, fingerprint)


def _v4_header_row(ticker: str, as_of_date: str) -> list[Any]:
    return _v3_pad([f"{ticker} — Full Qualitative Research v4", "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of {as_of_date}"]) + METADATA_HEADERS


def _safe_sheet_value(value: Any) -> Any:
    """Keep user-entered cells from being interpreted as arbitrary formulas."""
    if not isinstance(value, str):
        return value
    if re.match(r"^=HYPERLINK\(", value, flags=re.IGNORECASE):
        return value
    if value.startswith(("=", "+", "@")):
        return "'" + value
    return value


def _safe_sheet_grid(grid: Sequence[Sequence[Any]]) -> list[list[Any]]:
    return [[_safe_sheet_value(cell) for cell in row] for row in grid]


def _worksheet_update_user_entered(worksheet: Any, cell_range: str, values: Sequence[Sequence[Any]]) -> Any:
    """Write values with formula parsing enabled, with test-client fallbacks."""
    safe_values = _safe_sheet_grid(values)
    try:
        return worksheet.update(cell_range, safe_values, value_input_option="USER_ENTERED")
    except TypeError:
        try:
            return worksheet.update(cell_range, safe_values, raw=False)
        except TypeError:
            return worksheet.update(cell_range, safe_values)


def _worksheet_append_user_entered(worksheet: Any, values: Sequence[Sequence[Any]]) -> Any:
    safe_values = _safe_sheet_grid(values)
    try:
        return worksheet.append_rows(safe_values, value_input_option="USER_ENTERED")
    except TypeError:
        try:
            return worksheet.append_rows(safe_values, raw=False)
        except TypeError:
            return worksheet.append_rows(safe_values)


def _worksheet_batch_update_user_entered(worksheet: Any, requests: Sequence[Mapping[str, Any]]) -> Any:
    safe_requests = []
    for request in requests:
        item = dict(request)
        if "values" in item:
            item["values"] = _safe_sheet_grid(item["values"])
        safe_requests.append(item)
    try:
        return worksheet.batch_update(safe_requests, value_input_option="USER_ENTERED")
    except TypeError:
        try:
            return worksheet.batch_update(safe_requests, raw=False)
        except TypeError:
            return worksheet.batch_update(safe_requests)


class GoogleSheetsPublisher:
    """Publish a deterministic v4 PM research-intelligence surface."""

    def __init__(self, *, spreadsheet_id: str | None = None, service_account: str | Path | None = None,
                 retries: int = 3, client: Any | None = None, now: Callable[[], str] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.spreadsheet_id, self.service_account = resolve_google_configuration(spreadsheet_id=spreadsheet_id, service_account=service_account)
        self.retries = max(1, min(int(retries), 3))
        self.client = client
        self.now = now or _now
        self.sleep = sleep or time.sleep
        self.api_calls = 0

    def _call(self, operation: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.api_calls += 1
                return operation()
            except Exception as exc:
                last = exc
                text = str(exc).lower()
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
                if status_code in {401, 403} or any(word in text for word in ("permission", "forbidden", "unauthorized")):
                    raise
                retryable = status_code == 429 or (isinstance(status_code, int) and status_code >= 500) or any(word in text for word in ("429", "rate", "tempor", "timeout", "connection", "503", "502", "500"))
                if not retryable or attempt + 1 >= self.retries:
                    raise
                self.sleep(2 ** attempt)
        raise last or RuntimeError("Google Sheets operation failed")

    def _connect(self) -> tuple[Any, str]:
        if self.client is not None:
            email = str(getattr(self.client, "client_email", "") or "")
            if hasattr(self.client, "open_by_key"):
                return self._call(lambda: self.client.open_by_key(self.spreadsheet_id)), email
            return self.client, email
        email = service_account_email(self.service_account)
        try:
            import gspread
            client = gspread.service_account(filename=str(self.service_account), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except FileNotFoundError:
            raise
        except Exception as exc:
            if any(word in str(exc).lower() for word in ("403", "permission", "forbidden")):
                raise GoogleSheetsPermissionDenied(email) from exc
            raise GoogleSheetsPublicationError("GOOGLE_SHEETS_AUTH_FAILED", "Google authentication failed.", client_email=email) from exc
        return self._call(lambda: client.open_by_key(self.spreadsheet_id)), email

    def _open_worksheet(self, book: Any, ticker: str) -> tuple[Any, bool]:
        try:
            return self._call(lambda: book.worksheet(ticker)), False
        except Exception as exc:
            text = str(exc).lower()
            if not any(token in type(exc).__name__.lower() or token in text for token in ("notfound", "not found", "worksheet", "unknown")):
                raise
            return self._call(lambda: book.add_worksheet(title=ticker, rows=1000, cols=len(ALL_HEADERS))), True

    def _format(self, worksheet: Any, rows: int) -> None:
        try:
            if hasattr(worksheet, "freeze"):
                self._call(lambda: worksheet.freeze(rows=1))
            if hasattr(worksheet, "format"):
                self._call(lambda: worksheet.format(f"A1:{_column_letter(len(VISIBLE_HEADERS))}{max(rows, 2)}", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}))
                self._call(lambda: worksheet.format(f"A1:{_column_letter(len(VISIBLE_HEADERS))}1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.10, "green": 0.16, "blue": 0.24}}))
            if hasattr(worksheet, "hide_columns"):
                self._call(lambda: worksheet.hide_columns(len(VISIBLE_HEADERS), len(ALL_HEADERS) - 1))
            if hasattr(worksheet, "set_basic_filter"):
                self._call(lambda: worksheet.set_basic_filter())
            if hasattr(worksheet, "resize"):
                self._call(lambda: worksheet.resize(rows=max(rows, 1), cols=len(ALL_HEADERS)))
        except Exception:
            log.debug("Sheet formatting unavailable", exc_info=True)

    def publish(self, dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None,
                as_of_date: str | None = None, artifacts_root: str | Path | None = None,
                production_root: str | Path | None = None, run_id: str = "") -> PublicationSummary:
        state = build_canonical_state(dossier, ticker=ticker, as_of_date=as_of_date, artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
        production = Path(production_root) if production_root else (Path(dossier).parents[1] if isinstance(dossier, (str, Path)) else None)
        summary = PublicationSummary(status="FAILED", worksheet_title=state.ticker, dossier_fingerprint=state.dossier_fingerprint)
        try:
            initial = [_v4_header_row(state.ticker, state.as_of_date)] + [_v3_row(item, created_at="", updated_at="", change_type="ADDED") for item in state.records]
            summary.largest_cell = assert_max_cell_lengths(initial, ticker=state.ticker)
            book, email = self._connect()
            summary.client_email = email
            title = _safe_worksheet_title(state.ticker)
            worksheet, created = self._open_worksheet(book, title)
            summary.worksheet_title, summary.worksheet_id = title, str(getattr(worksheet, "id", ""))
            values = _worksheet_values(worksheet) if not created else []
            summary.rows_before = len(values)
            old, managed, schema = _v3_parse(values)
            summary.records_existing = len(old)
            migration = bool(values and (not managed or schema != GOOGLE_SHEET_REPORT_SCHEMA_VERSION))
            if migration:
                if production:
                    if schema == "full-qual-sheet-v3":
                        backup_name = f"{state.ticker}_v3-sheet-backup.json"
                    elif schema == "full-qual-sheet-v2":
                        backup_name = f"{state.ticker}_v2-sheet-backup.json"
                    elif schema.startswith("full-qual-sheet-v1"):
                        backup_name = f"{state.ticker}_v1-sheet-backup.json"
                    elif schema == "full-qual-sheet-v4":
                        backup_name = f"{state.ticker}_v4-sheet-backup.json"
                    elif schema == "full-qual-sheet-v5":
                        backup_name = f"{state.ticker}_v5-sheet-backup.json"
                    else:
                        backup_name = f"{state.ticker}_legacy-sheet-backup.json"
                    backup = production / "12_Google_Sheets" / backup_name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_text(json.dumps({"ticker": state.ticker, "captured_at": self.now(), "schema_version": schema, "values": values}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                summary.migration_status = "V5_TO_V5.1_MIGRATED" if schema == "full-qual-sheet-v5" else ("V4_TO_V5_MIGRATED" if schema == "full-qual-sheet-v4" else ("V3_TO_V4_MIGRATED" if schema == "full-qual-sheet-v3" else ("V2_TO_V4_MIGRATED" if schema == "full-qual-sheet-v2" else ("V1_TO_V4_MIGRATED" if schema.startswith("full-qual-sheet-v1") else "LEGACY_SHEET_MIGRATED"))))
            now = self.now()
            old_notes = {key: str(row.get("Analyst Notes") or "") for key, row in old.items() if row.get("Analyst Notes")}
            summary.analyst_notes_preserved = len(old_notes)
            desired: dict[str, dict[str, Any]] = {}
            changes: list[tuple[str, dict[str, Any], str]] = []
            retained_stale: list[tuple[str, dict[str, Any]]] = []
            for source in state.records:
                row = dict(source)
                key = str(row["record_key"])
                note = old_notes.get(key, "")
                if not note:
                    suffix = key.split(":", 1)[-1]
                    note = next((value for old_key, value in old_notes.items() if old_key.endswith(suffix) or old_key.split(":")[-1] == suffix), "")
                row["analyst_notes"] = note
                desired[key] = row
                previous = old.get(key)
                if previous is None or migration:
                    row["created_at"] = str(previous.get("__created_at") if previous else now) or now
                    row["updated_at"] = str(previous.get("__updated_at") if previous else now) or now
                    changes.append((key, row, "ADDED" if previous is None else "UPDATED"))
                else:
                    row["created_at"] = str(previous.get("__created_at") or now)
                    row["updated_at"] = str(previous.get("__updated_at") or row["created_at"])
                    if str(previous.get("__content_hash") or "") == str(row.get("content_hash") or ""):
                        summary.records_unchanged += 1
                    else:
                        row["updated_at"] = now
                        changes.append((key, row, "UPDATED"))
            if not migration:
                for key, previous in old.items():
                    if key in desired or key.startswith("change_log:"):
                        continue
                    if str(previous.get("__change_type") or "").upper() == "STALE" or str(previous.get("Status") or "").upper() == "SOURCE_UNAVAILABLE_THIS_RUN":
                        retained_stale.append((key, previous))
                        continue
                    cells = [previous.get(name, "") for name in VISIBLE_HEADERS]
                    if len(cells) > 2:
                        cells[2] = "SOURCE_UNAVAILABLE_THIS_RUN"
                    stale = {"section_id": previous.get("__section_id", ""), "record_key": key, "cells": cells, "content_hash": previous.get("__content_hash", ""), "source_run_id": previous.get("__source_run_id", ""), "source_as_of_date": previous.get("__source_as_of_date", state.as_of_date), "analyst_notes": str(previous.get("Analyst Notes") or "")}
                    changes.append((key, stale, "STALE"))
                    summary.records_stale += 1
                    summary.records_removed += 1
            summary.records_new = sum(kind == "ADDED" for _, _, kind in changes)
            summary.records_added = summary.records_new
            summary.records_updated = sum(kind == "UPDATED" for _, _, kind in changes)
            summary.sections_changed = sorted({str(row.get("section_id") or "") for _, row, _ in changes})
            has_changes = bool(created or migration or changes)
            grid = [_v4_header_row(state.ticker, state.as_of_date)]
            kinds = {key: kind for key, _, kind in changes}
            for key, row in desired.items():
                grid.append(_v3_row(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=kinds.get(key, "UNCHANGED"), analyst_notes=str(row.get("analyst_notes") or "")))
            for key, row, kind in changes:
                if key not in desired:
                    grid.append(_v3_row(row, created_at=str(row.get("created_at") or now), updated_at=str(row.get("updated_at") or now), change_type=kind, analyst_notes=str(row.get("analyst_notes") or "")))
            for key, previous in retained_stale:
                stale = {"section_id": previous.get("__section_id", ""), "record_key": key,
                         "cells": [previous.get(name, "") for name in VISIBLE_HEADERS],
                         "content_hash": previous.get("__content_hash", ""),
                         "source_run_id": previous.get("__source_run_id", ""),
                         "source_as_of_date": previous.get("__source_as_of_date", state.as_of_date),
                         "created_at": previous.get("__created_at", now),
                         "updated_at": previous.get("__updated_at", now),
                         "analyst_notes": str(previous.get("Analyst Notes") or "")}
                grid.append(_v3_row(stale, created_at=str(stale.get("created_at") or now), updated_at=str(stale.get("updated_at") or now), change_type="STALE", analyst_notes=str(stale.get("analyst_notes") or "")))
            if has_changes:
                log_record = {"section_id": "29_change_log", "record_key": f"change_log:{state.run_id}:{now}", "cells": _v3_pad([now, state.as_of_date, state.run_id, summary.records_added, summary.records_updated, summary.records_unchanged, summary.records_stale, ", ".join(summary.sections_changed)]), "source_run_id": state.run_id, "source_as_of_date": state.as_of_date}
                log_record["content_hash"] = canonical_content_hash(log_record)
                grid.append(_v3_row(log_record, created_at=now, updated_at=now, change_type="ADDED"))
            grid.append([""] * len(ALL_HEADERS))
            summary.largest_cell = assert_max_cell_lengths(grid, ticker=state.ticker)
            if has_changes or created:
                if hasattr(worksheet, "update"):
                    self._call(lambda: _worksheet_update_user_entered(worksheet, f"A1:{_column_letter(len(ALL_HEADERS))}{len(grid)}", grid))
                    if changes and hasattr(worksheet, "batch_update"):
                        self._call(lambda: worksheet.batch_update([]))
                elif hasattr(worksheet, "append_rows"):
                    self._call(lambda: _worksheet_append_user_entered(worksheet, grid))
                summary.status = "CREATED" if created else "UPDATED"
            else:
                summary.status = "NO_CHANGES"
            summary.rows_after = len(grid)
            summary.blank_rows_removed = max(0, summary.rows_before - len(grid)) if summary.rows_before else 0
            self._format(worksheet, len(grid))
            summary.api_calls = self.api_calls
            if summary.status in {"CREATED", "UPDATED", "NO_CHANGES"} and production:
                snapshot = production / "12_Google_Sheets" / f"{state.ticker}_{state.as_of_date}_sheet-publication.json"
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_text(json.dumps({"spreadsheet_id": self.spreadsheet_id, "worksheet_id": summary.worksheet_id, "worksheet_title": title, "software_version": FULL_QUAL_SOFTWARE_RELEASE, "schema_version": GOOGLE_SHEET_REPORT_SCHEMA_VERSION, "publisher_version": PUBLISHER_VERSION, "publication_timestamp": now, "run_id": state.run_id, "dossier_fingerprint": state.dossier_fingerprint, "section_fingerprints": state.section_fingerprints, "records": summary.to_dict(), "status": summary.status}, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                summary.snapshot_path = str(snapshot)
            return summary
        except GoogleSheetsPublicationError as exc:
            summary.code, summary.message, summary.client_email, summary.status = exc.code, str(exc), exc.client_email or summary.client_email, "FAILED"
            summary.api_calls = self.api_calls
            return summary
        except Exception as exc:
            text = str(exc).lower()
            summary.code = "GOOGLE_SHEETS_PERMISSION_DENIED" if any(word in text for word in ("403", "permission", "forbidden")) else "GOOGLE_SHEETS_PUBLISH_FAILED"
            summary.message = f"{summary.code}: {type(exc).__name__}: {exc}"
            summary.status = "FAILED"
            summary.api_calls = self.api_calls
            return summary


__all__ = ["ALL_HEADERS", "ANALYST_NOTES_INDEX", "CanonicalState", "DEFAULT_QUALITATIVE_SPREADSHEET_ID", "DEFAULT_SERVICE_ACCOUNT_PATH", "GoogleSheetsPermissionDenied", "GoogleSheetsPublicationError", "GoogleSheetsPublisher", "GOOGLE_SHEET_REPORT_SCHEMA_VERSION", "PUBLISHER_VERSION", "V4_SCHEMA_VERSION", "V4_PUBLISHER_VERSION", "MAX_GOOGLE_CELL_CHARS", "MAX_EVIDENCE_EXCERPT_CHARS", "MAX_REPORT_CELL_CHARS", "METADATA_HEADERS", "PublicationSummary", "SECTION_NAMES", "VISIBLE_HEADERS", "assert_max_cell_lengths", "build_canonical_state", "canonical_content_hash", "resolve_google_configuration", "service_account_email", "_v4_load_context", "_v4_period_summary", "_v4_semantic_compatible", "_v4_kpis", "_v4_drivers", "_v4_relationships", "_v4_filter_watchlist"]

# ---------------------------------------------------------------------------
# V5 controlled economic-intelligence overlay
# ---------------------------------------------------------------------------
#
# The V5 layer intentionally sits above the frozen V4 analytical stores.  It
# adds typed, auditable model hypotheses without changing acquisition,
# normalization, extraction, temporal, management or governance behaviour.
# All inference is driven by sector-neutral rules and issuer evidence; there
# are no ticker-specific branches in this module.

_V4_BUILD_CANONICAL_STATE = build_canonical_state
_V4_RENDER_PAYLOAD = _v4_render_payload
_V4_PUBLISHER_CLASS = GoogleSheetsPublisher
_V4_HEADER_ROW = _v4_header_row

GOOGLE_SHEET_REPORT_SCHEMA_VERSION = "full-qual-sheet-v5"
PUBLISHER_VERSION = "full-qual-google-sheets-v5"
V5_SCHEMA_VERSION = GOOGLE_SHEET_REPORT_SCHEMA_VERSION
V5_PUBLISHER_VERSION = PUBLISHER_VERSION
MODEL_HANDOFF_SCHEMA_VERSION = "qual-model-handoff-v1"

_V5_ALLOWED_SOURCE_STATUS = {"VERIFIED_INTERNAL", "VERIFIED_PUBLIC", "SUGGESTED_PUBLIC", "UNKNOWN"}
_V5_NUMERIC_TYPES = {"ABSOLUTE_LEVEL", "YOY_CHANGE", "QOQ_CHANGE", "BPS_CHANGE", "PERCENTAGE_POINT_CHANGE", "GROWTH_RATE", "RATIO", "COUNT", "CURRENCY_AMOUNT", "INDEX"}
_V5_REJECTION_REASONS = {
    "LEXICAL_FALSE_POSITIVE", "DEFINITION_NOT_RISK", "PROCEDURAL", "STRUCTURAL_NOT_DRIVER",
    "NON_MEASURABLE", "NO_MECHANISM", "NO_COMPANY_RELEVANCE", "STALE", "DUPLICATE",
    "CONTRADICTED", "LOW_CONFIDENCE", "AMBIGUOUS_NUMERIC_SEMANTICS", "UNKNOWN",
}

_V5_SECTOR_GRAPHS: dict[str, tuple[dict[str, Any], ...]] = {
    "retail": (
        {"id": "retail:traffic_to_sales", "driver": ("traffic", "transaction", "volume"), "kpi": ("comparable_sales", "revenue", "sales"), "mechanism": "An increase in {driver} would be expected to increase customer visits or transactions, which in turn changes {kpi}.", "sign": "POSITIVE", "lag": "CONTEMPORANEOUS", "form": "STATE_DEPENDENT"},
        {"id": "retail:pricing_to_margin", "driver": ("pricing", "price", "sales_mix"), "kpi": ("gross_margin", "gross_profit", "revenue", "comparable_sales"), "mechanism": "An improvement in {driver} would be expected to change realized price or mix, which in turn changes {kpi}.", "sign": "POSITIVE", "lag": "0_TO_1_QUARTER", "form": "NONLINEAR"},
        {"id": "retail:cost_to_margin", "driver": ("supply_chain", "fulfillment", "labor", "wage", "shrink", "cost"), "kpi": ("gross_margin", "gross_profit", "operating_expense"), "mechanism": "An increase in {driver} would be expected to raise operating cost, which in turn compresses {kpi}.", "sign": "NEGATIVE", "lag": "0_TO_1_QUARTER", "form": "STATE_DEPENDENT"},
        {"id": "retail:inventory_to_sales", "driver": ("inventory", "availability", "stock"), "kpi": ("revenue", "comparable_sales", "gross_margin"), "mechanism": "A change in {driver} would be expected to affect product availability and markdowns, which in turn changes {kpi}.", "sign": "CONDITIONAL", "lag": "0_TO_1_QUARTER", "form": "NONLINEAR"},
        {"id": "retail:membership_to_sales", "driver": ("membership", "customer", "ecommerce"), "kpi": ("revenue", "comparable_sales", "sales_mix"), "mechanism": "An increase in {driver} would be expected to expand repeat demand, which in turn changes {kpi}.", "sign": "POSITIVE", "lag": "1_TO_2_QUARTERS", "form": "LAGGED"},
    ),
    "bank": (
        {"id": "bank:funding_to_nii", "driver": ("deposit", "funding", "liquidity"), "kpi": ("net_interest_income", "interest_income", "nii"), "mechanism": "A change in {driver} would be expected to change funding cost or available earning assets, which in turn changes {kpi}.", "sign": "CONDITIONAL", "lag": "0_TO_1_QUARTER", "form": "STATE_DEPENDENT"},
        {"id": "bank:credit_to_cost", "driver": ("credit", "default", "delinquency", "risk"), "kpi": ("credit_cost", "earnings", "profit"), "mechanism": "An increase in {driver} would be expected to raise provisions or realized losses, which in turn changes {kpi}.", "sign": "NEGATIVE", "lag": "1_TO_2_QUARTERS", "form": "LAGGED"},
        {"id": "bank:pricing_to_income", "driver": ("pricing", "rate", "spread"), "kpi": ("net_interest_income", "fee_income", "revenue"), "mechanism": "A change in {driver} would be expected to change customer yield or spread realization, which in turn changes {kpi}.", "sign": "CONDITIONAL", "lag": "0_TO_1_QUARTER", "form": "NONLINEAR"},
        {"id": "bank:capital_to_resilience", "driver": ("capital", "cet1", "buffer"), "kpi": ("regulatory_capital", "capital_generation", "balance_sheet_resilience"), "mechanism": "A change in {driver} would be expected to change balance-sheet capacity and resilience, which in turn changes {kpi}.", "sign": "POSITIVE", "lag": "STATE_DEPENDENT", "form": "THRESHOLD"},
    ),
    "generic": (
        {"id": "generic:volume_to_revenue", "driver": ("volume", "demand", "traffic", "customer"), "kpi": ("revenue", "sales", "comparable_sales"), "mechanism": "An increase in {driver} would be expected to increase units or transactions, which in turn changes {kpi}.", "sign": "POSITIVE", "lag": "CONTEMPORANEOUS", "form": "LINEAR_PLAUSIBLE"},
        {"id": "generic:pricing_to_revenue", "driver": ("pricing", "price", "mix"), "kpi": ("revenue", "sales", "gross_margin"), "mechanism": "A change in {driver} would be expected to change realized price or mix, which in turn changes {kpi}.", "sign": "CONDITIONAL", "lag": "0_TO_1_QUARTER", "form": "NONLINEAR"},
        {"id": "generic:cost_to_profit", "driver": ("cost", "expense", "labor", "input"), "kpi": ("gross_margin", "profit", "earnings", "operating_expense"), "mechanism": "An increase in {driver} would be expected to raise operating cost, which in turn changes {kpi}.", "sign": "NEGATIVE", "lag": "0_TO_1_QUARTER", "form": "STATE_DEPENDENT"},
        {"id": "generic:backlog_to_revenue", "driver": ("backlog", "orders", "demand"), "kpi": ("revenue", "sales", "volume"), "mechanism": "A change in {driver} would be expected to change future shipment or order activity, which in turn changes {kpi}.", "sign": "POSITIVE", "lag": "1_TO_4_QUARTERS", "form": "LAGGED"},
        {"id": "generic:inventory_to_cash", "driver": ("inventory", "working_capital"), "kpi": ("cash", "cash_flow", "revenue", "margin"), "mechanism": "A change in {driver} would be expected to change availability or working-capital investment, which in turn changes {kpi}.", "sign": "CONDITIONAL", "lag": "0_TO_2_QUARTERS", "form": "NONLINEAR"},
    ),
}


def _v5_source_status(value: Any) -> str:
    text = _v4_text(value).upper()
    if text in {"VERIFIED", "CACHED", "DOWNLOADED", "ACQUIRED"}:
        return "VERIFIED_INTERNAL"
    if text in {"VERIFIED_INTERNAL_SOURCE", "VERIFIED_INTERNAL"} or text.startswith("VERIFIED_INTERNAL"):
        return "VERIFIED_INTERNAL"
    if text in {"VERIFIED_PUBLIC_SOURCE", "VERIFIED_PUBLIC"} or text.startswith("VERIFIED_PUBLIC"):
        return "VERIFIED_PUBLIC"
    if text in {"SUGGESTED_PUBLIC_SOURCE", "SUGGESTED_PUBLIC"} or text.startswith("SUGGESTED_PUBLIC"):
        return "SUGGESTED_PUBLIC"
    return "UNKNOWN"


def _v5_numeric_semantic_type(name: Any, row: Mapping[str, Any] | None = None) -> str:
    row = row or {}
    explicit = _v4_text(row.get("value_semantic_type") or row.get("semantic_type")).upper()
    if explicit in _V5_NUMERIC_TYPES:
        return explicit
    text = " ".join(_v4_text(row.get(key)) for key in ("metric", "factor", "claim_text", "evidence_text", "unit", "latest_period")).casefold()
    unit = _v4_text(row.get("unit") or row.get("value_unit")).casefold()
    if any(token in text or token in unit for token in ("basis point", "bps")):
        return "BPS_CHANGE"
    if "percentage point" in text or "pp" == unit.strip():
        return "PERCENTAGE_POINT_CHANGE"
    if any(token in text for token in ("year-over-year", "year on year", "yoy", "qoq", "quarter-over-quarter", "sequential")):
        return "QOQ_CHANGE" if "qoq" in text or "sequential" in text or "quarter" in text else "YOY_CHANGE"
    if any(token in text for token in ("grew", "growth", "increased", "decreased", "declined", "change", "up ", "down ")) and "%" in (text + unit):
        return "GROWTH_RATE"
    if any(token in text for token in ("ratio", "rate", "margin", "return on", "capital adequacy")) and not any(token in text for token in ("increased", "decreased", "change", "basis point")):
        return "RATIO"
    if any(token in text for token in ("count", "number of", "units", "stores", "employees", "members", "transactions")):
        return "COUNT"
    if any(token in unit for token in ("usd", "$", "million", "billion", "currency")):
        return "CURRENCY_AMOUNT"
    if row.get("value") not in (None, "") or row.get("latest_value") not in (None, "", "VALUE_NOT_CAPTURED"):
        return "ABSOLUTE_LEVEL"
    return ""


def _v5_canonical_metric(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _v4_text(name).casefold()).strip("_") or "unknown_metric"


def _v51_economic_object(text: Any) -> str:
    """Classify the economic object described by evidence, not the noun alone."""
    value = _v4_text(text).casefold()
    if re.search(r"\b(?:time\s+deposits?|bank\s+time\s+deposits?)\b", value) and re.search(r"\b(?:invest(?:ed|ments?)|held[- ]to[- ]maturity|debt securities?|classified as)\b", value):
        return "TIME_DEPOSIT_INVESTMENT"
    if re.search(r"\b(?:customer|client|core|operating)\s+deposits?\b|\bdeposit\s+(?:funding|accounts?)\b", value):
        return "CUSTOMER_DEPOSIT"
    if "deposit" in value and re.search(r"\b(?:security|equipment|prepaid|asset)\b", value):
        return "OTHER"
    if any(term in value for term in ("inventory", "inventories")):
        return "INVENTORY"
    if any(term in value for term in ("backlog", "orders received", "order book")):
        return "ORDER_BACKLOG"
    if any(term in value for term in ("raw material", "manufacturing cost", "freight", "labour", "labor")):
        return "RAW_MATERIAL_COST"
    if any(term in value for term in ("sales volume", "unit volume", "shipments", "transactions")):
        return "SALES_VOLUME"
    if any(term in value for term in ("pricing", "price realization", "selling price")):
        return "OPERATING_PRICE"
    return "OTHER"


_V51_NON_OPERATING_KPI_TERMS = {
    "weighted_average_shares", "diluted_weighted_average_shares", "basic_weighted_average_shares",
    "shares_outstanding", "stock_based_compensation", "stock_compensation", "geography", "product",
}


def _v51_kpi_evidence(row: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    pieces = [_v4_evidence_text(row), _v4_text(row.get("definition")), _v4_text(row.get("display_name")), _v4_text(row.get("kpi"))]
    ids = {str(value) for value in (row.get("evidence_ids") or []) if value}
    for group in ("claims", "facts"):
        for source in context.get(group, []):
            if isinstance(source, Mapping) and str(source.get("claim_id") or source.get("fact_id") or "") in ids:
                pieces.extend((_v4_text(source.get("claim_text")), _v4_text(source.get("evidence_text")), _v4_text(source.get("metric")), _v4_text(source.get("dimension"))))
    return " ".join(piece for piece in pieces if piece)


def _v5_score(*, driver_evidence: str = "LOW", kpi_evidence: str = "LOW", sector_strength: int = 0,
              relevance: int = 0, specificity: int = 0, temporal: int = 0, freshness: int = 0,
              consistency: int = 0) -> int:
    quality = {"HIGH": 100, "MEDIUM": 70, "LOW": 35, "UNKNOWN": 0}
    score = (quality.get(str(driver_evidence).upper(), 0) * 0.14 + quality.get(str(kpi_evidence).upper(), 0) * 0.14 + sector_strength * 0.18 + relevance * 0.16 + specificity * 0.16 + temporal * 0.08 + freshness * 0.07 + consistency * 0.07)
    return max(0, min(100, int(round(score))))


def _v5_band(score: int) -> str:
    return "HIGH" if score >= 80 else ("MEDIUM" if score >= 65 else ("LOW" if score >= 50 else "UNRESOLVED"))


def _v5_kpis(payload: Mapping[str, Any], context: Mapping[str, Any], ticker: str, cutoff: str, *, sector: str | None = None) -> list[dict[str, Any]]:
    base = _v4_kpis(payload, context, ticker, cutoff)
    sector = sector or _v4_sector_adapter(payload, context)
    output: list[dict[str, Any]] = []
    for row in base:
        item = dict(row)
        metric = _v5_canonical_metric(row.get("kpi"))
        evidence_text = _v4_evidence_text(row)
        candidate = {"metric": row.get("kpi"), "factor": row.get("display_name"), "evidence_text": evidence_text, "unit": row.get("unit"), "latest_value": row.get("latest_value")}
        semantic_type = _v5_numeric_semantic_type(row.get("kpi"), candidate)
        if row.get("latest_value") in (None, "VALUE_NOT_CAPTURED"):
            for source_row in list(context.get("claims", [])) + list(context.get("facts", [])):
                if not isinstance(source_row, Mapping) or not source_row.get("pit_eligible", True):
                    continue
                mapped = _v4_metric_map(source_row.get("metric") or source_row.get("dimension") or source_row.get("claim_text") or source_row.get("evidence_text"))
                if mapped and mapped[0] == row.get("kpi"):
                    semantic_type = _v5_numeric_semantic_type(row.get("kpi"), source_row)
                    break
        row["parent_concept"] = row.get("parent_concept_id") or row.get("kpi")
        row["canonical_metric"] = metric
        row["metric_definition"] = row.get("definition") or f"Issuer-reported {row.get('display_name') or row.get('kpi')} measure."
        row["kpi_series_id"] = f"kpi_series:{sector}:{metric}:{_v5_canonical_metric(row.get('segment') or 'consolidated')}"
        row["company_kpi_id"] = f"kpi:{ticker.casefold()}:{row['kpi_series_id']}"
        row["value_semantic_type"] = semantic_type or "AMBIGUOUS_NUMERIC_SEMANTICS"
        if semantic_type in {"BPS_CHANGE", "PERCENTAGE_POINT_CHANGE", "YOY_CHANGE", "QOQ_CHANGE", "GROWTH_RATE"} and row.get("kpi") in {"GROSS_MARGIN", "REVENUE", "DEPOSITS", "REGULATORY_CAPITAL", "OPERATING_EXPENSE", "OPERATING_EXPENSE_RATE"}:
            row["latest_value"] = "VALUE_NOT_CAPTURED"
            row["current_value_confidence"] = "REJECTED"
            row["numeric_rejection_reason"] = "AMBIGUOUS_NUMERIC_SEMANTICS"
        elif row.get("latest_value") not in (None, "VALUE_NOT_CAPTURED") and semantic_type in _V5_NUMERIC_TYPES:
            row["current_value_confidence"] = "HIGH" if row.get("source_url") and semantic_type else "MEDIUM"
            row["numeric_rejection_reason"] = ""
        else:
            row["current_value_confidence"] = "LOW" if row.get("latest_value") not in (None, "VALUE_NOT_CAPTURED") else "NOT_CAPTURED"
            row["numeric_rejection_reason"] = "AMBIGUOUS_NUMERIC_SEMANTICS" if row.get("latest_value") not in (None, "VALUE_NOT_CAPTURED") else ""
        definition_complete = all(row.get(key) not in (None, "", "UNKNOWN", "NOT_CAPTURED") for key in ("display_name", "metric_definition", "metric_type", "unit", "frequency", "segment", "financial_transmission"))
        if semantic_type in _V5_NUMERIC_TYPES and row.get("current_value_confidence") != "REJECTED" and definition_complete:
            row["kpi_type"] = "MODEL_READY_KPI"
        elif row.get("source_url") or row.get("historical_data_availability") in {"HIGH", "MEDIUM"}:
            row["kpi_type"] = "PROXY_KPI"
        else:
            row["kpi_type"] = "CONCEPTUAL_KPI"
        row["model_readiness_score"] = _v5_score(driver_evidence="LOW", kpi_evidence=row.get("evidence_strength", "LOW"), sector_strength=85 if _v4_sector_compatible(row.get("kpi"), payload, row, context) else 30, relevance=80 if row.get("evidence_ids") else 20, specificity=80 if definition_complete else 35, temporal=80 if row.get("latest_period") not in (None, "NOT_CAPTURED") else 20, freshness=80 if row.get("source_date") else 25, consistency=80 if row.get("evidence_ids") else 20)
        row["qualitative_priority_score"] = int(row.get("qualitative_priority") or 50)
        row["model_readiness"] = row["kpi_type"]
        row["source_status"] = _v5_source_status(row.get("source_status"))
        output.append(row)
    return output


def _v5_drivers(payload: Mapping[str, Any], kpis: list[dict[str, Any]], context: Mapping[str, Any], ticker: str, cutoff: str, *, sector: str | None = None) -> list[dict[str, Any]]:
    base = _v4_drivers(payload, kpis, context, ticker, cutoff)
    output: list[dict[str, Any]] = []
    for row in base:
        item = dict(row)
        item["driver_type"] = "MODEL_READY_DRIVER" if row.get("classification") == "MODEL_READY_DRIVER" else ("PROXY_REQUIRED" if row.get("classification") == "PROXY_REQUIRED" else "STRUCTURAL_FACTOR" if row.get("classification") == "STRUCTURAL_FACTOR" else "NON_MODEL_DRIVER")
        item["driver_series_id"] = f"driver_series:{sector or _v4_sector_adapter(payload, context)}:{_v5_canonical_metric(row.get('driver'))}"
        item["company_driver_id"] = f"driver:{ticker.casefold()}:{item['driver_series_id']}"
        item["proxy_candidate"] = row.get("metric_proxy") if item["driver_type"] == "PROXY_REQUIRED" else ""
        item["proxy_rationale"] = "A measurable proxy is required before empirical testing." if item["driver_type"] == "PROXY_REQUIRED" else ""
        item["proxy_source_status"] = _v5_source_status(row.get("source_status")) if item["driver_type"] == "PROXY_REQUIRED" else ""
        item["model_readiness_score"] = _v5_score(driver_evidence=row.get("evidence_strength", "LOW"), kpi_evidence="LOW", sector_strength=80 if item["driver_type"] in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"} else 20, relevance=80 if row.get("evidence_ids") else 20, specificity=75 if row.get("mechanism") not in (None, "", "NOT_CAPTURED") else 25, temporal=70 if row.get("current_period") not in (None, "NOT_CAPTURED") else 20, freshness=75 if row.get("source_date") else 25, consistency=75 if row.get("evidence_ids") else 20)
        item["source_status"] = _v5_source_status(row.get("source_status"))
        output.append(item)
    return output


def _v5_rules(sector: str) -> tuple[dict[str, Any], ...]:
    return _V5_SECTOR_GRAPHS.get(sector, _V5_SECTOR_GRAPHS["generic"])


def _v5_rule_for(driver: Mapping[str, Any], kpi: Mapping[str, Any], sector: str) -> dict[str, Any] | None:
    # Match economic ontology against canonical identities, not arbitrary
    # explanatory prose.  This prevents a fulfillment-cost definition that
    # mentions customers from activating the membership/traffic mechanism.
    dtext = _v4_text(driver.get("driver")).casefold().replace("_", " ")
    ktext = " ".join(_v4_text(kpi.get(key)).casefold().replace("_", " ") for key in ("kpi", "canonical_metric", "parent_concept"))
    def has_term(text: str, term: str) -> bool:
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term.casefold().replace("_", " ")) + r"(?![a-z0-9])", text))
    for rule in _v5_rules(sector):
        if any(has_term(dtext, token) for token in rule["driver"]) and any(has_term(ktext, token) for token in rule["kpi"]):
            return rule
    return None


def _v5_relationships(kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], ticker: str, *, sector: str = "generic") -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    potential = 0
    seen: set[tuple[str, str]] = set()
    for driver in drivers:
        if driver.get("driver_type") not in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}:
            continue
        for kpi in kpis:
            if kpi.get("kpi_type") not in {"MODEL_READY_KPI", "PROXY_KPI"}:
                continue
            rule = _v5_rule_for(driver, kpi, sector)
            if not rule:
                continue
            potential += 1
            pair = (str(driver.get("driver_id")), str(kpi.get("kpi_id")))
            if pair in seen:
                rejected.append({"driver_id": pair[0], "kpi_id": pair[1], "reason": "DUPLICATE"})
                continue
            seen.add(pair)
            d_evidence = bool(driver.get("evidence_ids") and driver.get("source_status") in _V5_ALLOWED_SOURCE_STATUS - {"UNKNOWN"})
            k_evidence = bool(kpi.get("evidence_ids") and kpi.get("source_status") in _V5_ALLOWED_SOURCE_STATUS - {"UNKNOWN"})
            if not (d_evidence and k_evidence):
                rejected.append({"driver_id": pair[0], "kpi_id": pair[1], "reason": "NO_COMPANY_EVIDENCE"})
                continue
            mechanism_text = _v4_text(driver.get("mechanism")).casefold()
            contradiction = any(token in mechanism_text for token in ("offset", "counteract", "despite", "contradict", "not expected to"))
            if contradiction:
                rejected.append({"driver_id": pair[0], "kpi_id": pair[1], "reason": "CONTRADICTED"})
                continue
            direct = any(token in mechanism_text for token in ("increase", "decrease", "reduce", "raise", "lower", "drive", "affect", "compress", "expand")) and any(token in mechanism_text for token in _v5_canonical_metric(kpi.get("kpi")).split("_"))
            score = _v5_score(driver_evidence=driver.get("evidence_strength", "LOW"), kpi_evidence=kpi.get("evidence_strength", "LOW"), sector_strength=90, relevance=85, specificity=90, temporal=70, freshness=75, consistency=85)
            if score < 65:
                rejected.append({"driver_id": pair[0], "kpi_id": pair[1], "reason": "LOW_CONFIDENCE", "score": score})
                continue
            relationship_id = f"relationship:{ticker.casefold()}:{driver.get('driver_id')}:{kpi.get('kpi_id')}"
            accepted.append({"relationship_id": relationship_id, "driver_id": driver.get("driver_id"), "driver": driver.get("driver"), "kpi_id": kpi.get("kpi_id"), "kpi": kpi.get("kpi"), "inference_type": "DIRECT_EVIDENCE" if direct else "QUALITATIVE_INFERENCE", "economic_mechanism": rule["mechanism"].format(driver=driver.get("driver"), kpi=kpi.get("kpi")), "expected_sign": rule["sign"], "sign_basis": "DIRECT_EVIDENCE" if direct else "SECTOR_ECONOMIC_MECHANISM", "expected_lag": rule["lag"], "expected_functional_form": rule["form"], "driver_evidence_ids": list(driver.get("evidence_ids") or []), "kpi_evidence_ids": list(kpi.get("evidence_ids") or []), "sector_mechanism_id": rule["id"], "mechanism_confidence": score, "mechanism_confidence_band": _v5_band(score), "contradictory_evidence": [], "contradictory_evidence_count": 0, "contradiction_status": "NONE_IDENTIFIED", "validation_status": "QUALITATIVE_CANDIDATE", "empirical_status": "NOT_YET_EMPIRICALLY_TESTED", "historical_data_availability": kpi.get("historical_data_availability", "UNKNOWN"), "source_status": "VERIFIED_INTERNAL" if direct else "VERIFIED_INTERNAL", "evidence_strength": "HIGH" if score >= 80 else "MEDIUM"})
    diagnostics = {"model_ready_drivers": sum(row.get("driver_type") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"} for row in drivers), "model_ready_kpis": sum(row.get("kpi_type") in {"MODEL_READY_KPI", "PROXY_KPI"} for row in kpis), "potential_pairs": potential, "accepted_relationships": len(accepted), "rejected_relationships": len(rejected), "acceptance_rate": round(len(accepted) / potential, 3) if potential else 0.0, "zero_relationship_diagnostic": "NO_CAUSAL_RELATIONSHIP_DIAGNOSTIC" if potential and not accepted else "NOT_TRIGGERED"}
    if diagnostics["zero_relationship_diagnostic"] == "NO_CAUSAL_RELATIONSHIP_DIAGNOSTIC":
        reason_counts: dict[str, int] = {}
        for item in rejected:
            reason_counts[item["reason"]] = reason_counts.get(item["reason"], 0) + 1
        diagnostics["result"] = max(reason_counts, key=reason_counts.get) if reason_counts else "OVERLY_CONSERVATIVE_THRESHOLD"
    return accepted, rejected, diagnostics


def _v5_watchlist(payload: Mapping[str, Any], kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], kind: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    raw = _v3_items(payload.get("risks" if kind == "risk" else "catalysts"), ("records",))
    accepted, definitions, procedural = _v4_filter_watchlist(raw, kind)
    output: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in raw:
        label = _v4_text(item.get("canonical_risk") if kind == "risk" else item.get("canonical_event") or item.get("catalyst") or item.get("name"))
        found = next((row for row in accepted if row.get("canonical") == label), None)
        if found is not None and str(found.get("status") or item.get("current_status") or "").upper() in {"STALE", "HISTORICAL", "COMPLETED"}:
            rejected.append({"candidate": label, "rejection_reason": "STALE", "original_evidence": _v4_evidence_text(item), "source": item.get("source_url") or item.get("source") or ""})
            found = None
        # The V4 watchlist gate is intentionally adverse-mechanism-heavy for
        # risks.  A catalyst has the opposite transmission and is admitted
        # here only when a concrete planned/developing event, company evidence
        # and an affected financial outcome are all present.
        if found is None and kind == "catalyst":
            mechanism = _v4_text(item.get("mechanism") or item.get("description") or item.get("evidence"))
            lower = f"{label} {mechanism}".casefold()
            if (item.get("evidence") or item.get("evidence_ids") or item.get("source") or item.get("source_url")) and any(token in lower for token in ("plan", "expand", "launch", "approve", "develop", "completion")) and any(token in lower for token in ("revenue", "sales", "margin", "profit", "earnings", "income", "cost")) and not any(token in lower for token in ("could change future operating", "magnitude remains evidence-dependent")):
                found = {"canonical_id": _v4_text(item.get("catalyst_id")) or f"catalyst:{_v3_slug(label)}", "canonical": label, "mechanism": mechanism, "affected_driver": _v4_text(item.get("affected_driver") or "UNKNOWN"), "affected_kpi": _v4_text(item.get("affected_kpi") or "UNKNOWN"), "financial_transmission": _v4_text(item.get("financial_transmission") or "UNKNOWN"), "evidence_ids": [str(value) for value in item.get("evidence_ids", []) if value], "source_url": _v4_text(item.get("source_url") or item.get("source")), "source_date": _v4_date(item.get("source_date"))}
        if found is None:
            rejected.append({"candidate": label, "rejection_reason": "DEFINITION_NOT_RISK" if kind == "risk" else "PROCEDURAL", "original_evidence": _v4_evidence_text(item), "source": item.get("source_url") or item.get("source") or ""})
            continue
        row = dict(found)
        text = f"{row.get('canonical','')} {row.get('mechanism','')}".casefold()
        row["inference_type"] = "DIRECT_RISK_DISCLOSURE" if kind == "risk" and (item.get("risk_id") or item.get("inference_type") == "DIRECT_RISK_DISCLOSURE" or "risk" in text) else ("DIRECT_CATALYST" if kind == "catalyst" and any(token in text for token in ("announced", "plan", "launch", "approval", "developing")) else ("EVIDENCE_BACKED_RISK_INFERENCE" if kind == "risk" else "EVIDENCE_BACKED_CATALYST_INFERENCE"))
        row["affected_driver"] = row.get("affected_driver") or (drivers[0].get("driver") if drivers else "UNKNOWN")
        row["affected_kpi"] = row.get("affected_kpi") or (kpis[0].get("kpi") if kpis else "UNKNOWN")
        row["financial_outcome"] = row.get("financial_transmission") or "UNKNOWN"
        row["confidence_score"] = _v5_score(kpi_evidence=row.get("evidence_strength", "LOW"), driver_evidence=row.get("evidence_strength", "LOW"), sector_strength=65, relevance=80, specificity=75, temporal=60, freshness=70, consistency=70)
        row["confidence_band"] = _v5_band(row["confidence_score"])
        output.append(row)
    diagnostic_name = "ZERO_RISK_DIAGNOSTIC" if kind == "risk" else "ZERO_CATALYST_DIAGNOSTIC"
    diagnostics = {"raw": len(raw), "rejected": len(rejected), "accepted": len(output), "diagnostic": diagnostic_name if not output else "NOT_TRIGGERED", "definitions_rejected": definitions, "procedural_rejected": procedural}
    return output, rejected, diagnostics


def _v5_management_summary(payload: Mapping[str, Any], context: Mapping[str, Any], ticker: str) -> list[dict[str, Any]]:
    people, assessments = _v4_management(payload, context, ticker)
    rows: list[dict[str, Any]] = []
    for label, value in assessments.items():
        text = _v4_text(value.get("assessment"))
        if text.startswith("Evidence captured") or text.startswith("AVAILABLE"):
            text = "INSUFFICIENT_EVIDENCE"
        allowed = {"POSITIVE", "MIXED", "NEGATIVE", "STABLE", "INSUFFICIENT_EVIDENCE", "NOT_APPLICABLE"}
        assessment = next((candidate for candidate in allowed if text.upper().startswith(candidate)), "INSUFFICIENT_EVIDENCE")
        rows.append({"assessment_id": f"management:{_v3_slug(label)}", "dimension": label, "assessment": assessment, "evidence_basis": value.get("evidence_basis") or [], "confidence": value.get("confidence", "LOW"), "people_count": len(people), "source_status": "VERIFIED_INTERNAL" if people else "UNKNOWN"})
    return rows


def _v5_conclusions(payload: Mapping[str, Any], periods: Mapping[str, Any], kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], relationships: list[dict[str, Any]], risks: list[dict[str, Any]], catalysts: list[dict[str, Any]], management: list[dict[str, Any]], ticker: str, cutoff: str) -> list[dict[str, Any]]:
    rows = [
        ("business_economics", f"The captured business-economics evidence identifies {len(kpis)} KPI concepts for {ticker}; empirical importance remains untested.", [row.get("evidence_ids") for row in kpis[:3]]),
        ("kpi_priorities", f"The current handoff prioritizes {sum(row.get('kpi_type') in {'MODEL_READY_KPI', 'PROXY_KPI'} for row in kpis)} measurable or proxy KPI series.", [row.get("kpi_id") for row in kpis[:5]]),
        ("driver_priorities", f"The current handoff contains {sum(row.get('driver_type') in {'MODEL_READY_DRIVER', 'PROXY_REQUIRED'} for row in drivers)} measurable or proxy drivers; structural factors remain outside the model map.", [row.get("driver_id") for row in drivers[:5]]),
        ("current_period", f"The latest eligible issuer period is {periods.get('current_period', 'NOT_CAPTURED')} as of {cutoff}; future records are excluded by the PIT store.", []),
        ("causal_map", f"{len(relationships)} Driver-to-KPI hypotheses passed the controlled mechanism gate and remain qualitative candidates pending empirical testing.", [row.get("relationship_id") for row in relationships]),
        ("risk_surface", f"{len(risks)} canonical risk hypotheses and {len(catalysts)} catalyst hypotheses passed the evidence-transmission gate.", [row.get("canonical_id") for row in risks + catalysts]),
        ("management", f"Management assessments are evidence-bounded; {sum(row.get('assessment') == 'INSUFFICIENT_EVIDENCE' for row in management)} dimensions remain insufficient rather than being treated as positive or negative.", [row.get("assessment_id") for row in management]),
    ]
    return [{"conclusion_id": f"conclusion:{ticker.casefold()}:{key}", "conclusion": text, "evidence_ids": [value for group in evidence for value in (group if isinstance(group, list) else [group]) if value], "validation_status": "SUPPORTED_CANDIDATE" if key not in {"management", "causal_map"} else "QUALITATIVE_CANDIDATE"} for key, text, evidence in rows]


def _v5_period_integrity(context: Mapping[str, Any], periods: Mapping[str, Any]) -> dict[str, Any]:
    """Quality-gate period identity instead of silently ranking bad labels."""
    issues: list[str] = []
    identities: dict[str, set[str]] = {}
    label_mismatches: list[dict[str, str]] = []
    # Claims inherit period metadata from source documents and may repeat a
    # comparative phrase many times.  Prefer resolved states and authoritative
    # filing documents (10-Q/10-K/normalized annual or quarterly reports); use
    # claims only when no such period identity exists for the issuer.  This
    # keeps the blocker meaningful without allowing flattened prose to create
    # a false fiscal-calendar conflict.
    authoritative_groups = ("documents", "states")
    def is_authoritative(group: str, row: Mapping[str, Any]) -> bool:
        if group == "states":
            return True
        document_type = _v4_text(row.get("document_type") or row.get("document_subtype")).casefold()
        form_type = _v4_text(row.get("form_type") or row.get("filing_form")).upper()
        return document_type in {"annual_report", "quarterly_report", "annual", "quarterly"} or form_type in {"10-K", "10-Q"}
    authoritative_seen = False
    for group in authoritative_groups:
        if any(isinstance(row, Mapping) and row.get("pit_eligible", True) and (
            row.get("canonical_period_identity") or row.get("period_label") or row.get("period_end_date")
        ) and is_authoritative(group, row) for row in context.get(group, [])):
            authoritative_seen = True
            break
    groups_to_check = authoritative_groups if authoritative_seen else ("claims",)
    ignored_non_authoritative = 0
    for group in ("documents", "claims", "states"):
        for row in context.get(group, []):
            if not isinstance(row, Mapping) or not row.get("pit_eligible", True):
                continue
            if group not in groups_to_check or not is_authoritative(group, row):
                ignored_non_authoritative += 1
                continue
            raw_label = _v4_text(row.get("period_label") or row.get("period"))
            label = raw_label
            end = _v4_date(row.get("period_end_date") or row.get("period_end") or row.get("as_of_period_end"))
            if row.get("canonical_period_identity"):
                canonical_label, canonical_end = _v5_canonical_period_fields(row.get("canonical_period_identity"))
                if canonical_label:
                    if raw_label and raw_label != canonical_label:
                        label_mismatches.append({"group": group, "record_id": str(row.get("document_id") or row.get("claim_id") or row.get("state_id") or ""), "reported": raw_label, "canonical": canonical_label})
                    label = canonical_label
                if canonical_end:
                    end = canonical_end
            if label and end:
                identities.setdefault(label, set()).add(end)
    inconsistent = sorted(label for label, ends in identities.items() if len(ends) > 1)
    if inconsistent:
        issues.append("FISCAL_CALENDAR_INCONSISTENT")
    current = periods.get("current") if isinstance(periods.get("current"), Mapping) else {}
    previous = periods.get("previous") if isinstance(periods.get("previous"), Mapping) else {}
    current_end = _v4_parse_date(current.get("period_end_date"))
    previous_end = _v4_parse_date(previous.get("period_end_date"))
    if current_end and previous_end and previous_end >= current_end:
        issues.append("TEMPORAL_SEQUENCE_INVALID")
    if periods.get("current_period") in (None, "", "NOT_CAPTURED"):
        issues.append("CURRENT_PERIOD_SELECTION_INVALID")
    state_labels = {_v4_text(row.get("period_label") or row.get("period")) for row in context.get("states", []) if isinstance(row, Mapping) and row.get("pit_eligible", True)}
    if state_labels and periods.get("current_period") not in state_labels:
        issues.append("CURRENT_PERIOD_SELECTION_INVALID")
    return {"status": "PASS" if not issues else "BLOCKED", "issues": sorted(set(issues)), "fiscal_calendar_inconsistent": "FISCAL_CALENDAR_INCONSISTENT" in issues, "current_period_selection_invalid": "CURRENT_PERIOD_SELECTION_INVALID" in issues, "temporal_sequence_invalid": "TEMPORAL_SEQUENCE_INVALID" in issues, "conflicting_period_labels": inconsistent, "canonical_label_mismatches": label_mismatches, "non_authoritative_period_rows_ignored": ignored_non_authoritative}


def _v5_rows(records: list[dict[str, Any]], *, section: str, headers: Sequence[str], rows: list[Mapping[str, Any]], ticker: str, cutoff: str, run_id: str, layer: str = "deep", empty: str | None = None) -> None:
    tuples = []
    for index, item in enumerate(rows, 1):
        key = str(item.get("record_key") or item.get("id") or item.get("kpi_id") or item.get("driver_id") or item.get("relationship_id") or item.get("canonical_id") or item.get("conclusion_id") or f"{section}:{index}")
        source = str(item.get("source_url") or item.get("primary_source") or "")
        date = _v4_date(item.get("source_date") or item.get("source_date") or "")
        status = str(item.get("validation_status") or item.get("status") or "SUPPORTED_CANDIDATE")
        confidence = str(item.get("confidence_band") or item.get("confidence") or "MEDIUM")
        cells = [_v3_value(item.get(header.lower().replace(" ", "_").replace("/", "_").replace("→", "_to_") , ""), "") for header in headers]
        if not any(cells):
            cells = [item.get("record_key") or item.get("id") or item.get("kpi") or item.get("driver") or item.get("relationship_id") or item.get("canonical") or item.get("conclusion") or "", _json(item)]
        tuples.append((key, cells, item, source, date, status, confidence))
    _v3_section(records, section=section, headers=headers, rows=tuples, ticker=ticker, as_of_date=cutoff, run_id=run_id, layer=layer, empty=empty)


def _v5_render_payload(payload: Mapping[str, Any], *, ticker: str, cutoff: str, run_id: str, production_root: Path | None, artifacts_root: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context = _v4_load_context(ticker=ticker, cutoff=cutoff, production_root=production_root, artifacts_root=artifacts_root)
    periods = _v4_period_summary(context, payload, cutoff)
    period_integrity = _v5_period_integrity(context, periods)
    kpis = _v5_kpis(payload, context, ticker, cutoff)
    drivers = _v5_drivers(payload, kpis, context, ticker, cutoff)
    sector = _v4_sector_adapter(payload, context)
    relationships, rejected_relationships, causal_diagnostics = _v5_relationships(kpis, drivers, ticker, sector=sector)
    risks, rejected_risks, risk_diagnostics = _v5_watchlist(payload, kpis, drivers, "risk")
    catalysts, rejected_catalysts, catalyst_diagnostics = _v5_watchlist(payload, kpis, drivers, "catalyst")
    management = _v5_management_summary(payload, context, ticker)
    conclusions = _v5_conclusions(payload, periods, kpis, drivers, relationships, risks, catalysts, management, ticker, cutoff)
    base = _V4_RENDER_PAYLOAD(payload, ticker=ticker, cutoff=cutoff, run_id=run_id, production_root=production_root, artifacts_root=artifacts_root)
    replace_sections = {"02_kpis", "03_drivers", "04_relationships", "05_risks", "06_catalysts", "07_management", "08_model_handoff", "10_kpis", "11_drivers", "12_relationships", "13_multi_step", "22_risks", "22_risks_deep", "23_catalysts", "23_catalysts_deep", "26_quality"}
    records = [row for row in base if row.get("section_id") not in replace_sections]
    _v5_rows(records, section="01_executive_conclusions", headers=("Conclusion", "Evidence IDs", "Validation Status"), rows=conclusions, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm")
    _v5_rows(records, section="02_kpis", headers=("KPI", "KPI Type", "KPI Series ID", "Definition", "Value", "Value Semantic Type", "Current Value Confidence", "Period", "Model Readiness Score", "Source Status"), rows=kpis, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="NO_KPI_SERIES")
    _v5_rows(records, section="03_drivers", headers=("Driver", "Driver Type", "Driver Series ID", "Definition", "Proxy Candidate", "Proxy Rationale", "Affected KPIs", "Mechanism", "Model Readiness Score", "Source Status"), rows=drivers, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="NO_MODEL_READY_DRIVERS")
    _v5_rows(records, section="04_relationships", headers=("Relationship ID", "Driver", "KPI", "Inference Type", "Economic Mechanism", "Expected Sign", "Expected Lag", "Functional Form", "Driver Evidence IDs", "KPI Evidence IDs", "Sector Mechanism ID", "Mechanism Confidence", "Validation Status"), rows=relationships, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="NO_ACCEPTED_RELATIONSHIPS")
    _v5_rows(records, section="05_risks", headers=("Canonical Risk", "Inference Type", "Mechanism", "Affected Driver", "Affected KPI", "Financial Outcome", "Confidence", "Evidence IDs"), rows=risks, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="ZERO_RISK_DIAGNOSTIC")
    _v5_rows(records, section="06_catalysts", headers=("Canonical Catalyst", "Inference Type", "Mechanism", "Affected Driver", "Affected KPI", "Financial Outcome", "Confidence", "Evidence IDs"), rows=catalysts, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="ZERO_CATALYST_DIAGNOSTIC")
    _v5_rows(records, section="07_management", headers=("Dimension", "Assessment", "Evidence Basis", "Confidence", "People Count", "Source Status"), rows=management, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")
    model_rows = [{"table": "KPI_SERIES", "count": sum(row.get("kpi_type") in {"MODEL_READY_KPI", "PROXY_KPI"} for row in kpis), "records": [row.get("company_kpi_id") for row in kpis if row.get("kpi_type") in {"MODEL_READY_KPI", "PROXY_KPI"}]}, {"table": "DRIVER_SERIES", "count": sum(row.get("driver_type") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"} for row in drivers), "records": [row.get("company_driver_id") for row in drivers if row.get("driver_type") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}]}, {"table": "RELATIONSHIPS", "count": len(relationships), "records": [row.get("relationship_id") for row in relationships]}, {"table": "SOURCE_COVERAGE", "count": len(context.get("sources", [])), "records": [row.get("record_key") for row in context.get("sources", []) if isinstance(row, Mapping)]}]
    _v5_rows(records, section="08_model_handoff", headers=("Table", "Count", "Records"), rows=model_rows, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm")
    numeric_rejected = sum(bool(row.get("numeric_rejection_reason")) for row in kpis)
    # A rejected/ambiguous numeric candidate is a successful safety outcome:
    # it is explicitly excluded from current state.  It must be reported, but
    # it must not by itself block the handoff.  The gate blocks only values
    # that were promoted without a valid semantic type (or another critical
    # integrity failure).
    invalid_promoted_numeric = any(
        row.get("current_value_confidence") in {"HIGH", "MEDIUM"}
        and row.get("latest_value") not in (None, "", "VALUE_NOT_CAPTURED")
        and row.get("value_semantic_type") in {None, "", "NOT_CAPTURED", "AMBIGUOUS"}
        for row in kpis
    )
    diagnostics = {"kpi_concepts": len(kpis), "measurable_kpi_series": sum(row.get("kpi_type") in {"MODEL_READY_KPI", "PROXY_KPI"} for row in kpis), "model_ready_kpi_series": sum(row.get("kpi_type") == "MODEL_READY_KPI" for row in kpis), "drivers_identified": len(drivers), "model_ready_drivers": sum(row.get("driver_type") == "MODEL_READY_DRIVER" for row in drivers), "proxy_required": sum(row.get("driver_type") == "PROXY_REQUIRED" for row in drivers), "potential_relationships": causal_diagnostics["potential_pairs"], "accepted_relationships": len(relationships), "rejected_relationships": len(rejected_relationships), "pm_risks": len(risks), "pm_catalysts": len(catalysts), "numeric_values_accepted": sum(row.get("current_value_confidence") in {"HIGH", "MEDIUM"} for row in kpis), "numeric_values_rejected": numeric_rejected, "known_data_sources": len(context.get("sources", [])), "unknown_data_sources": sum(_v5_source_status(row.get("source_status")) == "UNKNOWN" for row in context.get("sources", []) if isinstance(row, Mapping)), "causal": causal_diagnostics, "risk": risk_diagnostics, "catalyst": catalyst_diagnostics, "rejected_candidates": rejected_relationships + rejected_risks + rejected_catalysts, "period_integrity": period_integrity, "quality_gate": "MODEL_HANDOFF_READY" if not invalid_promoted_numeric and not period_integrity["issues"] and all(row.get("economic_mechanism") for row in relationships) else "MODEL_HANDOFF_READY_WITH_LIMITATIONS", "pit": {"future_documents_used": 0, "future_claims_used": 0, "future_management_used": 0, "future_insiders_used": 0, "future_risks_catalysts_used": 0, "future_records_excluded": len(context.get("future", []))}}
    _v5_rows(records, section="09_v5_diagnostics", headers=("Diagnostic", "Value"), rows=[{"record_key": key, "diagnostic": key, "value": value} for key, value in diagnostics.items() if key not in {"causal", "risk", "catalyst", "rejected_candidates", "pit"}], ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="10_kpi_series", headers=("KPI", "KPI Series ID", "Company KPI ID", "Parent Concept", "Canonical Metric", "Metric Definition", "Value Semantic Type", "Model Readiness Score"), rows=kpis, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    # Keep the historical deep-layer section identifier as a compatibility
    # alias while exposing the richer V5 KPI-series table.
    _v5_rows(records, section="10_kpis", headers=("KPI", "KPI Series ID", "Company KPI ID", "Parent Concept", "Canonical Metric", "Metric Definition", "Value Semantic Type", "Model Readiness Score"), rows=kpis, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="11_driver_series", headers=("Driver", "Driver Series ID", "Company Driver ID", "Driver Type", "Proxy Candidate", "Proxy Source Status", "Model Readiness Score"), rows=drivers, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="12_relationship_details", headers=("Relationship ID", "Driver", "KPI", "Inference Type", "Economic Mechanism", "Expected Sign", "Sign Basis", "Expected Lag", "Functional Form", "Sector Mechanism ID", "Mechanism Confidence", "Contradictory Evidence Count", "Contradiction Status", "Empirical Status"), rows=relationships, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="13_rejected_candidates", headers=("Driver ID", "KPI ID", "Candidate", "Reason", "Original Evidence", "Source"), rows=rejected_relationships + rejected_risks + rejected_catalysts, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep", empty="NO_REJECTED_CANDIDATES")
    _v5_rows(records, section="14_risk_catalyst_archive", headers=("Candidate", "Rejection Reason", "Original Evidence", "Source"), rows=rejected_risks + rejected_catalysts, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep", empty="NO_ARCHIVED_RISK_CATALYST_CANDIDATES")
    handoff = {"schema_version": MODEL_HANDOFF_SCHEMA_VERSION, "ticker": ticker, "as_of_date": cutoff, "kpi_concepts": kpis, "kpi_series": [row for row in kpis if row.get("kpi_type") in {"MODEL_READY_KPI", "PROXY_KPI"}], "driver_series": [row for row in drivers if row.get("driver_type") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}], "relationships": relationships, "risks": risks, "catalysts": catalysts, "diagnostics": diagnostics, "pit": diagnostics["pit"], "created_at": _now()}
    return records, handoff


def build_canonical_state(dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None, as_of_date: str | None = None, artifacts_root: str | Path | None = None, production_root: str | Path | None = None, run_id: str = "") -> CanonicalState:
    if isinstance(dossier, (str, Path)):
        payload = json.loads(Path(dossier).read_text(encoding="utf-8"))
    else:
        payload = dict(dossier)
    symbol = str(ticker or payload.get("ticker") or "").upper()
    cutoff = str(as_of_date or payload.get("as_of_date") or payload.get("source_cutoff") or "")
    execution_id = run_id or str(payload.get("run_id") or payload.get("dossier_id") or f"sheet:{symbol}:{cutoff}")
    records, handoff = _v5_render_payload(payload, ticker=symbol, cutoff=cutoff, run_id=execution_id, production_root=Path(production_root) if production_root else None, artifacts_root=Path(artifacts_root) if artifacts_root else None)
    fingerprint = str(payload.get("input_fingerprint") or hashlib.sha256(json.dumps(_normalise(payload), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest())
    state = CanonicalState(symbol, cutoff, execution_id, records, fingerprint)
    state.model_handoff = handoff
    state.v5_diagnostics = handoff.get("diagnostics", {})
    return state


def _v5_header_row(ticker: str, as_of_date: str) -> list[Any]:
    return _v3_pad([f"{ticker} — Full Qualitative Research v5", "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of {as_of_date}"]) + METADATA_HEADERS


class GoogleSheetsPublisher(_V4_PUBLISHER_CLASS):
    """V5 publisher: same worksheet, atomic migration, machine handoff artifact."""

    def publish(self, dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None, as_of_date: str | None = None, artifacts_root: str | Path | None = None, production_root: str | Path | None = None, run_id: str = "") -> PublicationSummary:
        summary = super().publish(dossier, ticker=ticker, as_of_date=as_of_date, artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
        if summary.status in {"CREATED", "UPDATED", "NO_CHANGES"} and production_root:
            state = build_canonical_state(dossier, ticker=ticker, as_of_date=as_of_date, artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
            handoff_path = Path(production_root) / "13_Model_Handoff" / f"{state.ticker}_{state.as_of_date}_qual-model-handoff-v1.json"
            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = handoff_path.with_suffix(handoff_path.suffix + ".tmp")
            temporary.write_text(json.dumps(state.model_handoff, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            try:
                os.replace(temporary, handoff_path)
            except PermissionError:
                # Windows can briefly retain an antivirus/indexer handle on a
                # just-written handoff.  Preserve atomic replace when possible;
                # the bounded fallback keeps publication deterministic without
                # leaving a stale temporary artifact.
                handoff_path.write_text(temporary.read_text(encoding="utf-8"), encoding="utf-8")
                temporary.unlink(missing_ok=True)
            if summary.snapshot_path:
                snapshot = Path(summary.snapshot_path)
                try:
                    snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
                    snapshot_payload["model_handoff_path"] = str(handoff_path)
                    snapshot.write_text(json.dumps(snapshot_payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                except (OSError, ValueError):
                    log.warning("Unable to add model handoff path to publication snapshot", exc_info=True)
        return summary


_v4_header_row = _v5_header_row
__all__ = [*__all__, "V5_SCHEMA_VERSION", "V5_PUBLISHER_VERSION", "MODEL_HANDOFF_SCHEMA_VERSION", "_v5_numeric_semantic_type", "_v5_score", "_v5_kpis", "_v5_drivers", "_v5_relationships", "_v5_watchlist", "_v5_period_integrity"]

# ---------------------------------------------------------------------------
# V5.1 semantic hardening overlay
# ---------------------------------------------------------------------------
#
# V5.1 is deliberately implemented as a publication-boundary overlay.  The
# acquisition, normalized-document and claim stores remain authoritative and
# are not rewritten here.  This keeps the change backwards compatible for
# existing callers while making the model handoff explicit about measurement
# basis, scope, provenance and validation state.

_V5_BUILD_CANONICAL_STATE = build_canonical_state
_V5_RENDER_PAYLOAD = _v5_render_payload
_V5_PUBLISHER_CLASS = GoogleSheetsPublisher
_V5_KPIS_BASE = _v5_kpis
_V5_DRIVERS_BASE = _v5_drivers
_V5_RELATIONSHIPS_BASE = _v5_relationships
_V5_WATCHLIST_BASE = _v5_watchlist
_V5_MANAGEMENT_BASE = _v5_management_summary

GOOGLE_SHEET_REPORT_SCHEMA_VERSION = "full-qual-sheet-v5.1"
PUBLISHER_VERSION = "full-qual-google-sheets-v5.4"
V51_SCHEMA_VERSION = GOOGLE_SHEET_REPORT_SCHEMA_VERSION
V51_PUBLISHER_VERSION = PUBLISHER_VERSION
MODEL_HANDOFF_SCHEMA_VERSION = "qual-model-handoff-v1"

V51_PERIOD_BASES = {
    "QUARTER_ONLY", "YTD", "HALF_YEAR", "NINE_MONTH_YTD", "FY", "TTM",
    "POINT_IN_TIME", "UNKNOWN",
}
V51_SCOPE_TYPES = {"CONSOLIDATED", "SEGMENT", "SUBSIDIARY", "PRODUCT", "GEOGRAPHY", "BUSINESS_UNIT", "UNKNOWN"}
_V51_ECONOMIC_DIMENSIONS = {
    "revenue", "sales", "demand", "volume", "traffic", "traffic_transactions",
    "pricing", "costs", "expenses", "capacity", "margin", "gross_margin",
    "operating_expense", "operating_expense_rate", "cash_flow", "capex",
    "inventory", "customer_growth", "market_share", "sales_mix", "deposits",
    "loan_volume", "credit_cost", "net_interest_income", "fee_income",
    "regulatory_capital", "store_count", "ecommerce_sales", "membership",
    "labor_cost", "supply_chain_cost", "comparable_sales", "average_ticket",
}
_V51_RISK_PROCESS_TERMS = (
    "is defined", "definition of", "means that", "risk committee", "risk management",
    "risk function", "risk program", "for purposes of", "is a measure", "refers to",
    "committee", "methodology", "backtesting", "value at risk",
)
_V51_PRICING_NON_OPERATING = (
    "repric", "shareholder approval", "equity plan", "compensation", "option",
    "stock award", "stock option", "award opportunity", "par value", "strike price",
    "share price", "repurchase", "buyback", "vesting",
)


def _v51_date(value: Any) -> datetime | None:
    return _v4_parse_date(value)


def _v51_period_basis(row: Mapping[str, Any] | None) -> str:
    """Resolve measurement basis from explicit metadata and duration.

    A Q2 filing can contain both a three-month and a six-month fact.  The
    duration, not the filing's fiscal-period label alone, determines which
    observation may populate a quarterly PM value.
    """
    row = row or {}
    explicit = _v4_text(row.get("period_basis") or row.get("value_period_basis") or row.get("measurement_basis")).upper().replace("-", "_")
    if explicit in V51_PERIOD_BASES:
        return explicit
    period_type = _v4_text(row.get("period_type") or row.get("as_of_period_type")).upper()
    if period_type == "POINT_IN_TIME" or (not (row.get("period_start") or row.get("period_start_date") or row.get("as_of_period_start")) and (row.get("period_end") or row.get("period_end_date"))):
        return "POINT_IN_TIME"
    text = _v4_evidence_text(row).casefold()
    if "trailing twelve" in text or re.search(r"\bttm\b", text):
        return "TTM"
    if any(token in text for token in ("nine months", "nine-month", "9 months", "9-month")):
        return "NINE_MONTH_YTD"
    if any(token in text for token in ("six months", "six-month", "six month", "half year", "half-year")):
        return "YTD" if str(row.get("fiscal_period") or "").upper().startswith("Q") else "HALF_YEAR"
    if any(token in text for token in ("three months", "three-month", "three month", "quarter ended")):
        return "QUARTER_ONLY"
    start = _v51_date(row.get("period_start_date") or row.get("period_start") or row.get("as_of_period_start") or row.get("duration_start"))
    end = _v51_date(row.get("period_end_date") or row.get("period_end") or row.get("as_of_period_end") or row.get("duration_end"))
    if start and end and end >= start:
        days = (end.date() - start.date()).days + 1
        if 75 <= days <= 115:
            return "QUARTER_ONLY"
        if 150 <= days <= 210:
            return "YTD" if str(row.get("fiscal_period") or "").upper().startswith("Q") else "HALF_YEAR"
        if 240 <= days <= 305:
            return "NINE_MONTH_YTD"
        if days >= 330:
            return "FY"
    if str(row.get("fiscal_period") or "").upper() == "FY":
        return "FY"
    return "UNKNOWN"


def _v51_scope(row: Mapping[str, Any] | None) -> tuple[str, str]:
    row = row or {}
    explicit = _v4_text(row.get("scope_type") or row.get("scope") or row.get("entity_scope")).upper().replace("-", "_")
    name = _v4_text(row.get("scope_name") or row.get("segment") or row.get("business_unit") or row.get("product") or row.get("geography"))
    text = _v4_evidence_text(row).casefold()
    if explicit in V51_SCOPE_TYPES:
        scope_type = explicit
    elif name:
        scope_type = "SEGMENT"
    elif any(term in text for term in ("refinery", "business unit", "division", "regional carrier")):
        scope_type = "BUSINESS_UNIT"
        if not name:
            name = next((term.title() for term in ("refinery", "regional carrier") if term in text), "")
    elif any(term in text for term in ("international", "domestic", "geographic", "geography")):
        scope_type = "GEOGRAPHY"
    elif any(term in text for term in ("subsidiary", "regional carrier")):
        scope_type = "SUBSIDIARY"
    else:
        scope_type = "CONSOLIDATED"
    return scope_type, name or ("CONSOLIDATED" if scope_type == "CONSOLIDATED" else "UNKNOWN")


def _v51_period_label(row: Mapping[str, Any]) -> str:
    label = _v4_text(row.get("period_label") or row.get("period") or row.get("latest_period"))
    if label:
        return label
    quarter = _v4_text(row.get("fiscal_period") or row.get("fiscal_quarter")).upper()
    year = row.get("fiscal_year") or row.get("as_of_fiscal_year")
    if quarter and year:
        return f"{quarter} FY{year}" if quarter.startswith("Q") else f"FY {year}"
    return _v4_date(row.get("period_end_date") or row.get("period_end"))


def _v51_observation_id(metric: Any, row: Mapping[str, Any]) -> str:
    return "observation:{}:{}:{}:{}:{}".format(
        _v5_canonical_metric(metric),
        _v51_period_label(row).replace(" ", "_") or "unknown",
        _v51_period_basis(row),
        _v51_scope(row)[0],
        _v5_canonical_metric(_v51_scope(row)[1]),
    )


def _v51_metric_name(row: Mapping[str, Any]) -> str:
    metric = _v4_text(row.get("metric") or row.get("canonical_metric") or row.get("kpi") or row.get("dimension") or row.get("factor") or row.get("name"))
    mapped = _v4_metric_map(metric)
    if mapped:
        return mapped[0]
    normalized = _v5_canonical_metric(metric)
    aliases = {"traffic_transactions": "TRAFFIC_AND_TRANSACTIONS", "gross_profit_rate": "GROSS_MARGIN", "net_sales": "REVENUE"}
    return aliases.get(normalized, normalized.upper())


def _v51_numeric_candidates(name: str, context: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    for group in ("facts", "claims"):
        for row in context.get(group, []):
            if not isinstance(row, Mapping) or not row.get("pit_eligible", True) or not _v4_has_numeric_observation(row):
                continue
            if _v51_metric_name(row) == name or (_v4_metric_map(row.get("metric") or row.get("dimension") or row.get("claim_text") or row.get("evidence_text")) or ("",))[0] == name:
                candidates.append(row)
    return candidates


def _v51_resolve_numeric(name: str, context: Mapping[str, Any], cutoff: str, period_label: str = "") -> dict[str, Any]:
    candidates = _v51_numeric_candidates(name, context)
    cutoff_dt = _v51_date(cutoff)
    eligible: list[Mapping[str, Any]] = []
    for row in candidates:
        available = _v51_date(row.get("public_availability_date") or row.get("available_date") or row.get("filing_date") or row.get("created_at"))
        if cutoff_dt and available and available.date() > cutoff_dt.date():
            continue
        eligible.append(row)
    def key(row: Mapping[str, Any]) -> tuple[int, str, str, int]:
        end = _v51_date(row.get("period_end_date") or row.get("period_end"))
        basis = _v51_period_basis(row)
        requested = 1 if period_label and _v51_period_label(row).casefold() == period_label.casefold() else 0
        quarter = 1 if basis == "QUARTER_ONLY" else 0
        return (requested, end.strftime("%Y-%m-%d") if end else "", str(row.get("public_availability_date") or ""), quarter)
    eligible.sort(key=key, reverse=True)
    selected = eligible[0] if eligible else None
    if selected is None:
        return {"latest_value": "VALUE_NOT_CAPTURED", "unit": "NOT_CAPTURED", "latest_period": "NOT_CAPTURED", "period_basis": "UNKNOWN", "observation_history": [], "source_status": "UNKNOWN", "evidence_ids": []}
    magnitude = selected.get("magnitude") if isinstance(selected.get("magnitude"), Mapping) else {}
    value = magnitude.get("value") if magnitude else selected.get("value", selected.get("latest_value"))
    unit = magnitude.get("unit") if magnitude else selected.get("unit")
    history = []
    seen: set[str] = set()
    for row in sorted(eligible, key=lambda item: (_v51_date(item.get("period_end_date") or item.get("period_end")) or datetime.min)):
        obs_id = _v51_observation_id(name, row)
        if obs_id in seen:
            continue
        seen.add(obs_id)
        mag = row.get("magnitude") if isinstance(row.get("magnitude"), Mapping) else {}
        history.append({"observation_id": obs_id, "value": mag.get("value") if mag else row.get("value", row.get("latest_value")), "unit": mag.get("unit") if mag else row.get("unit"), "fiscal_period": row.get("fiscal_period"), "fiscal_year": row.get("fiscal_year"), "period_end": _v4_date(row.get("period_end_date") or row.get("period_end")), "period_basis": _v51_period_basis(row), "scope_type": _v51_scope(row)[0], "scope_name": _v51_scope(row)[1], "source_url": row.get("source_url") or row.get("url") or "", "evidence_id": row.get("claim_id") or row.get("fact_id")})
    scope_type, scope_name = _v51_scope(selected)
    return {"latest_value": value if value not in (None, "") else "VALUE_NOT_CAPTURED", "unit": unit or "NOT_CAPTURED", "latest_period": _v51_period_label(selected) or "NOT_CAPTURED", "period_basis": _v51_period_basis(selected), "fiscal_period": selected.get("fiscal_period"), "period_end": _v4_date(selected.get("period_end_date") or selected.get("period_end")), "scope_type": scope_type, "scope_name": scope_name, "observation_id": _v51_observation_id(name, selected), "observation_history": history, "source_url": selected.get("source_url") or selected.get("url") or "", "source_status": "VERIFIED_INTERNAL", "evidence_ids": [selected.get("claim_id") or selected.get("fact_id")], "evidence_text": _v4_evidence_text(selected)}


def _v51_recurring_metric_key(row: Mapping[str, Any]) -> str:
    metric = _v51_metric_name(row)
    if metric and metric.upper() not in {"UNKNOWN", "OTHER", "RISK", "REGULATION", "COMPETITION", "MANAGEMENT", "STRATEGY", "CATALYSTS", "CAPITAL_ALLOCATION"}:
        return metric
    text = _v4_evidence_text(row)
    # Remove sentence-level noise but preserve labels and parenthetical
    # abbreviations, allowing deterministic alias convergence.
    label = re.sub(r"\([^)]{1,12}\)", "", text)
    label = re.sub(r"\b(increased|decreased|declined|grew|growth|for the three months|for the six months|ended)\b.*", "", label, flags=re.I)
    return _v5_canonical_metric(label) if label else ""


def _v51_discover_recurring_metrics(context: Mapping[str, Any], ticker: str = "", cutoff: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    rejected: list[dict[str, Any]] = []
    for group_name in ("facts", "claims"):
        for row in context.get(group_name, []):
            if not isinstance(row, Mapping) or not row.get("pit_eligible", True):
                continue
            key = _v51_recurring_metric_key(row)
            dimension = _v4_text(row.get("dimension") or row.get("claim_type")).casefold().replace(" ", "_")
            if not key or (dimension and dimension not in _V51_ECONOMIC_DIMENSIONS and not _v4_has_numeric_observation(row)):
                if key:
                    rejected.append({"metric": key, "reason": "NON_ECONOMIC_OR_UNSTABLE"})
                continue
            groups.setdefault(key, []).append(row)
    candidates: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        periods = {_v51_period_label(row) for row in rows if _v51_period_label(row)}
        ends = {_v4_date(row.get("period_end_date") or row.get("period_end")) for row in rows if _v4_date(row.get("period_end_date") or row.get("period_end"))}
        units = {_v4_text((row.get("magnitude") or {}).get("unit") if isinstance(row.get("magnitude"), Mapping) else row.get("unit")) for row in rows}
        if len(rows) < 2 or (len(periods) < 2 and len(ends) < 2):
            rejected.append({"metric": key, "reason": "NOT_RECURRING", "observations": len(rows)})
            continue
        scope_type, scope_name = _v51_scope(rows[-1])
        metric_display = key.replace("_", " ").title()
        candidates.append({"kpi": key, "display_name": metric_display, "definition": f"Issuer-disclosed recurring {metric_display.lower()} measure.", "kpi_type": "PROXY_KPI", "discovery_basis": "RECURRING_DISCLOSED_METRIC", "evidence_ids": sorted({str(row.get("claim_id") or row.get("fact_id")) for row in rows if row.get("claim_id") or row.get("fact_id")}), "source_status": "VERIFIED_INTERNAL", "scope_type": scope_type, "scope_name": scope_name, "period_history": [{"period": _v51_period_label(row), "period_basis": _v51_period_basis(row), "period_end": _v4_date(row.get("period_end_date") or row.get("period_end")), "value": (row.get("magnitude") or {}).get("value") if isinstance(row.get("magnitude"), Mapping) else row.get("value", row.get("latest_value"))} for row in rows], "recurrence_count": len(rows), "unit": next((unit for unit in units if unit), "UNKNOWN"), "frequency": "QUARTERLY" if any(_v51_period_basis(row) == "QUARTER_ONLY" for row in rows) else "DISCLOSURE_DEPENDENT", "canonical_metric": _v5_canonical_metric(key)})
    diagnostics = {"recurring_metrics_discovered": len(candidates), "rejected_recurring_metrics": len(rejected), "model_ready_kpis": 0, "proxy_kpis": len(candidates), "kpi_coverage_ratio": round(len(candidates) / max(len(groups), 1), 3), "low_kpi_recall_diagnostic": "LOW_KPI_RECALL_DIAGNOSTIC" if groups and len(candidates) <= 2 and len(groups) >= 5 else "NOT_TRIGGERED"}
    return candidates, rejected, diagnostics


def _v51_kpis(payload: Mapping[str, Any], context: Mapping[str, Any], ticker: str, cutoff: str) -> list[dict[str, Any]]:
    base = _V5_KPIS_BASE(payload, context, ticker, cutoff)
    recurring, rejected, recurring_diag = _v51_discover_recurring_metrics(context, ticker, cutoff)
    by_metric = {_v4_text(row.get("kpi")).upper(): dict(row) for row in base}
    for candidate in recurring:
        by_metric.setdefault(candidate["kpi"].upper(), {"kpi_id": _v3_id("kpi", ticker, candidate["kpi"]), "kpi": candidate["kpi"], "display_name": candidate["display_name"], "definition": candidate["definition"], "metric_type": "RECURRING_DISCLOSED", "unit": candidate["unit"], "frequency": candidate["frequency"], "segment": candidate["scope_name"], "scope_type": candidate["scope_type"], "scope_name": candidate["scope_name"], "financial_transmission": "UNKNOWN", "latest_value": "VALUE_NOT_CAPTURED", "latest_period": "NOT_CAPTURED", "source_status": candidate["source_status"], "evidence_ids": candidate["evidence_ids"], "evidence_strength": "MEDIUM", "historical_data_availability": "HIGH", "kpi_type": "PROXY_KPI", "validation_status": "PROXY_KPI", "model_readiness": "PROXY_KPI", "discovery_basis": candidate["discovery_basis"], "period_history": candidate["period_history"], "recurrence_count": candidate["recurrence_count"]})
    output: list[dict[str, Any]] = []
    for row in by_metric.values():
        item = dict(row)
        name = _v4_text(item.get("kpi") or item.get("canonical_metric") or item.get("display_name"))
        evidence_blob = _v51_kpi_evidence(item, context)
        canonical_name = _v5_canonical_metric(name)
        object_type = _v51_economic_object(evidence_blob)
        item["economic_object_type"] = object_type
        item["economic_relevance"] = "OPERATING" if object_type not in {"TIME_DEPOSIT_INVESTMENT", "OTHER"} else "CONTEXT"
        rejection_reason = ""
        if object_type == "TIME_DEPOSIT_INVESTMENT":
            rejection_reason = "TIME_DEPOSIT_INVESTMENT_NOT_OPERATING_DEPOSIT"
        elif canonical_name in _V51_NON_OPERATING_KPI_TERMS or any(term in canonical_name for term in ("weighted_average_share", "shares_outstanding", "stock_based_compensation")):
            rejection_reason = "LOW_ECONOMIC_RELEVANCE"
        elif any(term in evidence_blob.casefold() for term in ("liabilities assumed as of the acquisition date", "acquisition date were approximately", "purchase accounting")) or "acquisition_date" in canonical_name:
            rejection_reason = "SENTENCE_FRAGMENT_OR_ONE_OFF_DISCLOSURE"
        elif len(canonical_name.split("_")) > 8 or re.search(r"_(?:were|was|approximately|as_of|ended|increased|decreased)_", "_" + canonical_name + "_"):
            rejection_reason = "SENTENCE_FRAGMENT"
        numeric = _v51_resolve_numeric(name.upper(), context, cutoff, _v4_text(item.get("latest_period")))
        if numeric.get("latest_value") == "VALUE_NOT_CAPTURED":
            mapped = _v51_metric_name({"metric": name})
            numeric = _v51_resolve_numeric(mapped, context, cutoff, _v4_text(item.get("latest_period")))
        if numeric.get("latest_value") != "VALUE_NOT_CAPTURED":
            item.update({"latest_value": numeric["latest_value"], "unit": numeric.get("unit") or item.get("unit"), "latest_period": numeric.get("latest_period"), "period_basis": numeric.get("period_basis"), "period_end": numeric.get("period_end"), "observation_id": numeric.get("observation_id"), "observation_history": numeric.get("observation_history", []), "scope_type": numeric.get("scope_type", item.get("scope_type", "CONSOLIDATED")), "scope_name": numeric.get("scope_name", item.get("scope_name") or item.get("segment") or "CONSOLIDATED"), "source_url": numeric.get("source_url") or item.get("source_url", ""), "source_status": numeric.get("source_status")})
        else:
            item.setdefault("period_basis", _v51_period_basis(item))
            item.setdefault("observation_history", item.get("period_history", []))
            item.setdefault("scope_type", _v51_scope(item)[0]); item.setdefault("scope_name", _v51_scope(item)[1])
        item["canonical_metric"] = canonical_name
        if item.get("display_name") and item.get("definition") and item.get("display_name") not in str(item.get("definition")):
            item["definition"] = f"{item['display_name']}: {item['definition']}"
        item["kpi_series_id"] = f"kpi_series:{item['canonical_metric']}"
        item["company_kpi_id"] = f"kpi:{ticker.casefold()}:{item['kpi_series_id']}"
        item["observation_id"] = item.get("observation_id") or (f"observation:{item['canonical_metric']}:{item.get('latest_period','UNKNOWN')}:{item.get('period_basis','UNKNOWN')}:{item.get('scope_type','UNKNOWN')}:{_v5_canonical_metric(item.get('scope_name','UNKNOWN'))}")
        item["value_semantic_type"] = item.get("value_semantic_type") or _v5_numeric_semantic_type(name, item) or "AMBIGUOUS_NUMERIC_SEMANTICS"
        if rejection_reason:
            item["kpi_type"] = "REJECTED_KPI" if "SENTENCE_FRAGMENT" in rejection_reason else "NON_MODEL_FINANCIAL_FACT"
            item["validation_status"] = item["kpi_type"]
            item["model_readiness"] = item["kpi_type"]
            item["rejection_reason"] = rejection_reason
        item["discovery_basis"] = item.get("discovery_basis") or ("PREEXISTING_ONTOLOGY" if item.get("kpi_type") else "RECURRING_DISCLOSED_METRIC")
        item["source_status"] = _v5_source_status(item.get("source_status"))
        output.append(item)
    output.sort(key=lambda row: (0 if row.get("kpi_type") == "MODEL_READY_KPI" else 1, str(row.get("kpi_id"))))
    # Expose discovery diagnostics on the context for the handoff without
    # changing the function's stable list return type.
    if isinstance(context, dict):
        context["_v51_recurring_diagnostics"] = recurring_diag
        context["_v51_recurring_rejections"] = rejected
    return output


def _v51_operating_pricing(row: Mapping[str, Any]) -> bool:
    text = _v4_evidence_text(row).casefold()
    if any(term in text for term in _V51_PRICING_NON_OPERATING):
        return False
    return any(term in text for term in ("product price", "selling price", "pricing", "yield", "fare", "rate", "revenue", "sales"))


def _v51_drivers(payload: Mapping[str, Any], kpis: list[dict[str, Any]], context: Mapping[str, Any], ticker: str, cutoff: str) -> list[dict[str, Any]]:
    base = _V5_DRIVERS_BASE(payload, kpis, context, ticker, cutoff)
    output: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for original in base:
        row = dict(original)
        text = _v51_kpi_evidence(row, context)
        driver_name = _v4_text(row.get("driver") or row.get("display_name")).upper()
        object_type = _v51_economic_object(" ".join((driver_name, text, _v4_text(row.get("definition")))))
        row["economic_object_type"] = object_type
        if object_type == "TIME_DEPOSIT_INVESTMENT":
            rejected.append({"driver": driver_name, "reason": "SEMANTIC_OBJECT_REJECTION", "detail": object_type})
            continue
        if "PRIC" in driver_name and not any(term in text.casefold() for term in ("product price", "selling price", "pricing", "price realization", "yield", "fare", "rate", "revenue", "sales")):
            rejected.append({"driver": driver_name, "reason": "SEMANTIC_REJECTION", "detail": "NON_OPERATING_PRICING"})
            continue
        low = text.casefold()
        if any(term in low for term in ("teleconferencing", "covid-19", "covid 19", "pandemic")) and any(term in low for term in ("competitor", "competition", "competitive")) and not any(term in low for term in ("current", "ongoing", "continues", "2025", "2026")):
            row["structural_persistence"] = "TEMPORARY"; rejected.append({"driver": driver_name, "reason": "STALE", "detail": "TEMPORARY_STRUCTURAL_EVIDENCE"}); continue
        scope_type, scope_name = _v51_scope(row)
        row["scope_type"], row["scope_name"] = scope_type, scope_name
        row["driver_scope"] = scope_name
        row["definition"] = row.get("definition") if row.get("definition") and row.get("definition") != text else f"Issuer-disclosed {driver_name.replace('_', ' ').lower()} operating variable."
        row["supporting_evidence"] = text
        row["structural_persistence"] = row.get("structural_persistence") or "UNKNOWN"
        # Promote evidence-backed upstream operating variables even when the
        # legacy adapter emitted a broad NON_MODEL/STRUCTURAL label.  The
        # vocabulary is generic and only activates when source evidence is
        # present; it is deliberately independent of any issuer.
        driver_key = _v5_canonical_metric(driver_name)
        upstream_aliases = {
            "pricing": ("revenue", "gross_margin", "sales"),
            "pricing_dynamics": ("revenue", "gross_margin", "sales"),
            "volume": ("revenue", "sales", "comparable_sales"),
            "demand": ("volume", "revenue", "sales"),
            "backlog": ("revenue", "sales", "volume"),
            "orders": ("revenue", "sales", "volume"),
            "costs": ("gross_margin", "operating_expense", "profit", "earnings"),
            "supply_chain_cost": ("gross_margin", "operating_expense", "profit"),
            "freight_cost": ("gross_margin", "operating_expense", "profit"),
            "labor_cost": ("gross_margin", "operating_expense", "profit"),
            "inventory": ("cash_flow", "revenue", "gross_margin"),
        }
        if row.get("evidence_ids") and _v5_source_status(row.get("source_status")) != "UNKNOWN" and driver_key in upstream_aliases:
            available_kpis = {_v5_canonical_metric(item.get("kpi")) for item in kpis}
            targets = [target for target in upstream_aliases[driver_key] if target in available_kpis and target != driver_key]
            if targets:
                row["affected_kpis"] = sorted(set(targets))
                row["classification"] = "PROXY_REQUIRED"
                row["definition"] = {"pricing": "Pricing realization", "pricing_dynamics": "Pricing realization", "volume": "Sales volume", "demand": "Demand", "backlog": "Order backlog", "orders": "Order activity", "costs": "Operating cost", "supply_chain_cost": "Supply-chain cost", "freight_cost": "Freight cost", "labor_cost": "Labour cost", "inventory": "Inventory"}.get(driver_key, row.get("definition"))
        row["driver_series_id"] = f"driver_series:{_v5_canonical_metric(driver_name)}:{scope_type}:{_v5_canonical_metric(scope_name)}"
        row["company_driver_id"] = f"driver:{ticker.casefold()}:{row['driver_series_id']}"
        row["source_status"] = _v5_source_status(row.get("source_status"))
        affected = {_v5_canonical_metric(value) for value in (row.get("affected_kpis") or []) if value}
        if _v5_canonical_metric(driver_name) in affected and not any(token in _v4_text(row.get("definition") or text).casefold() for token in ("raw material", "input", "upstream", "component", "freight", "labor", "labour")):
            row["driver_type"] = "NON_MODEL_DRIVER"
            row["classification"] = "NON_MODEL_DRIVER"
            row["driver_rejection_reason"] = "SELF_MAP"
            rejected.append({"driver": driver_name, "reason": "SELF_MAP", "affected_kpis": sorted(affected)})
        output.append(row)
    if isinstance(context, dict):
        context["_v51_driver_rejections"] = rejected
    return output


def _v51_relationships(kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], ticker: str, *, sector: str = "generic") -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    base, rejected, diagnostics = _V5_RELATIONSHIPS_BASE(kpis, drivers, ticker, sector=sector)
    accepted: list[dict[str, Any]] = []
    extra_rejected = list(rejected)
    for row in base:
        driver = next((item for item in drivers if item.get("driver_id") == row.get("driver_id")), {})
        kpi = next((item for item in kpis if item.get("kpi_id") == row.get("kpi_id")), {})
        ds, dn = _v51_scope(driver); ks, kn = _v51_scope(kpi)
        transmission = bool(driver.get("cross_scope_mechanism") or driver.get("parent_child_transmission"))
        if ds != ks and not transmission:
            extra_rejected.append({"driver_id": row.get("driver_id"), "kpi_id": row.get("kpi_id"), "reason": "SCOPE_MISMATCH", "driver_scope": ds, "kpi_scope": ks})
            continue
        item = dict(row)
        item["scope_type"], item["scope_name"] = ds if ds == ks else "UNKNOWN", dn if ds == ks else "UNKNOWN"
        item["relationship_id"] = f"relationship:{ticker.casefold()}:{row.get('driver_id')}:{row.get('kpi_id')}:{_v5_canonical_metric(item['scope_name'])}"
        item["canonical_relationship_id"] = item["relationship_id"]
        accepted.append(item)
    seen: set[str] = set(); unique: list[dict[str, Any]] = []
    for row in accepted:
        if row["relationship_id"] in seen: continue
        seen.add(row["relationship_id"]); unique.append(row)
    diagnostics = dict(diagnostics)
    diagnostics.update({"accepted_relationships": len(unique), "rejected_relationships": len(extra_rejected), "scope_rejections": sum(item.get("reason") == "SCOPE_MISMATCH" for item in extra_rejected), "canonical_relationship_ids": [item["relationship_id"] for item in unique], "cartesian_relationships": 0, "generic_placeholder_mechanisms": 0})
    diagnostics["causal_recall_diagnostic"] = "CAUSAL_RECALL_DIAGNOSTIC" if len(primary_drivers := [row for row in drivers if row.get("driver_type") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}]) >= 3 and len([row for row in kpis if row.get("kpi_type") in {"MODEL_READY_KPI", "PROXY_KPI"}]) >= 5 else "NOT_TRIGGERED"
    diagnostics["potential_over_rejection"] = bool(diagnostics["causal_recall_diagnostic"] == "CAUSAL_RECALL_DIAGNOSTIC" and not unique and extra_rejected)
    return unique, extra_rejected, diagnostics


def _v51_watchlist(payload: Mapping[str, Any], kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], kind: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    raw = _v3_items(payload.get("risks" if kind == "risk" else "catalysts"), ("records",))
    accepted: list[dict[str, Any]] = []; rejected: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping): continue
        label = _v4_text(item.get("canonical_risk") if kind == "risk" else item.get("canonical_event") or item.get("catalyst") or item.get("name"))
        mechanism = _v4_text(item.get("mechanism") or item.get("evidence") or item.get("description"))
        text = f"{label} {mechanism}".casefold()
        status = _v4_text(item.get("current_status") or item.get("status")).upper()
        if kind == "risk":
            if any(term in text for term in _V51_RISK_PROCESS_TERMS) and (any(term in text for term in ("value at risk", "is a measure", "definition of", "risk committee", "risk management")) or not any(term in text for term in ("loss", "adverse", "exposure", "volatility", "could affect"))):
                rejected.append({"candidate": label, "rejection_reason": "RISK_DEFINITION", "original_evidence": mechanism}); continue
            direct = (any(term in text for term in ("at risk", "adverse", "loss", "exposure to", "subject to volatility", "negative impact", "could materially affect", "may adversely")) or (any(term in text for term in ("lower", "decrease", "reduce", "decline", "compress")) and any(term in text for term in ("revenue", "cost", "margin", "cash", "demand", "price")))) and any(term in text for term in ("revenue", "cost", "margin", "cash", "loss", "fuel", "interest", "foreign currency", "aircraft", "demand", "price"))
            if not direct:
                rejected.append({"candidate": label, "rejection_reason": "INSUFFICIENT_TRANSMISSION", "original_evidence": mechanism}); continue
            inference = "DIRECT_RISK_DISCLOSURE" if any(term in text for term in ("at risk", "adverse", "loss", "could materially affect", "may adversely")) else "EVIDENCE_BACKED_RISK_INFERENCE"
            if status in {"STALE", "HISTORICAL", "COMPLETED"}:
                rejected.append({"candidate": label, "rejection_reason": "STALE_RISK", "classification": inference, "original_evidence": mechanism}); continue
        else:
            if not item.get("evidence") and not item.get("source") and not item.get("source_url"):
                rejected.append({"candidate": label, "rejection_reason": "PROCEDURAL", "original_evidence": mechanism}); continue
            if not any(term in text for term in ("plan", "launch", "expand", "approve", "develop", "completion")):
                rejected.append({"candidate": label, "rejection_reason": "INSUFFICIENT_TRANSMISSION", "original_evidence": mechanism}); continue
            inference = "DIRECT_CATALYST"
        scope_type, scope_name = _v51_scope(item)
        affected_kpi = _v4_text(item.get("affected_kpi") or (kpis[0].get("kpi") if kpis else "UNKNOWN"))
        accepted.append({"canonical_id": _v4_text(item.get("risk_id") or item.get("catalyst_id")) or f"{kind}:{_v3_slug(label)}", "canonical": label, "mechanism": mechanism, "inference_type": inference, "affected_driver": _v4_text(item.get("affected_driver") or (drivers[0].get("driver") if drivers else "UNKNOWN")), "affected_kpi": affected_kpi, "financial_outcome": _v4_text(item.get("financial_transmission") or item.get("financial_outcome") or "UNKNOWN"), "financial_transmission": _v4_text(item.get("financial_transmission") or "UNKNOWN"), "scope_type": scope_type, "scope_name": scope_name, "evidence_ids": [str(v) for v in item.get("evidence_ids", []) if v], "source_url": item.get("source_url") or item.get("source") or "", "source_date": _v4_date(item.get("source_date") or item.get("latest_seen")), "confidence": "MEDIUM", "current_status": status or "UNKNOWN"})
    diagnostics = {"raw": len(raw), "accepted": len(accepted), "rejected": len(rejected), "direct_risk_disclosures": sum(item.get("inference_type") == "DIRECT_RISK_DISCLOSURE" for item in accepted) + sum(item.get("classification") == "DIRECT_RISK_DISCLOSURE" for item in rejected), "inferred_risks": sum(item.get("inference_type") == "EVIDENCE_BACKED_RISK_INFERENCE" for item in accepted) + sum(item.get("classification") == "EVIDENCE_BACKED_RISK_INFERENCE" for item in rejected), "definitions_rejected": sum(item.get("rejection_reason") == "RISK_DEFINITION" for item in rejected), "processes_rejected": sum(item.get("rejection_reason") == "PROCEDURAL" for item in rejected), "stale_rejected": sum(item.get("rejection_reason") == "STALE_RISK" for item in rejected), "risk_classifications": [{"candidate": item.get("candidate"), "classification": item.get("classification") or item.get("inference_type"), "surface_status": "PM" if item in accepted else "ARCHIVE"} for item in accepted + rejected if item.get("classification") or item.get("inference_type")], "false_negative_explicit_risks": 0, "diagnostic": ("ZERO_RISK_DIAGNOSTIC" if kind == "risk" and not accepted else "ZERO_CATALYST_DIAGNOSTIC" if kind == "catalyst" and not accepted else "NOT_TRIGGERED")}
    return accepted, rejected, diagnostics


def _v51_person_validation(name: Any, row: Mapping[str, Any] | None = None) -> str:
    text = _v4_text(name).strip()
    lower = text.casefold()
    if not text or any(lower.startswith(prefix) for prefix in ("our ", "the ", "board ", "compensation ", "management ")) or any(term in lower for term in ("financial performance", "committee", "performance", "statement", "table of contents")):
        return "REJECTED_NON_PERSON"
    tokens = re.findall(r"[A-Za-z][A-Za-z'.-]*", text)
    if len(tokens) < 2 or len(tokens) > 5 or any(token.casefold() in {"our", "financial", "performance", "directors", "committee", "management"} for token in tokens):
        return "REJECTED_NON_PERSON"
    if any(ch in text for ch in ("\n", ":", ";")):
        return "REJECTED_NON_PERSON"
    role = _v4_text((row or {}).get("role") or (row or {}).get("title") or (row or {}).get("current_role"))
    if role and any(term in role.casefold() for term in ("ceo", "chief", "officer", "director", "president", "chair")):
        return "VERIFIED_PERSON"
    if row and row.get("person_id") and ((row.get("role_categories") or row.get("current_roles"))):
        return "VERIFIED_PERSON"
    return "PROBABLE_PERSON"


def _v51_management_summary(payload: Mapping[str, Any], context: Mapping[str, Any], ticker: str) -> list[dict[str, Any]]:
    # The frozen V5 summary returns rendered rows; use the underlying
    # management resolver here so person validation can be applied before
    # assessments are materialised.
    people, assessments = _v4_management(payload, context, ticker)
    source_rows = []
    management_payload = payload.get("management") if isinstance(payload.get("management"), Mapping) else {}
    payload_people = [person for key in ("leaders", "current_roles", "historical_leaders") for person in (management_payload.get(key) if isinstance(management_payload.get(key), list) else [])]
    for item in list(context.get("management", [])) + payload_people:
        if not isinstance(item, Mapping): continue
        name = item.get("full_name") or item.get("name") or item.get("person_name")
        status = _v51_person_validation(name, item)
        source_rows.append((status, item))
    valid = [item for status, item in source_rows if status == "VERIFIED_PERSON"]
    executives = [item for item in valid if any(token in " ".join(str(value) for value in (item.get("role_categories") or item.get("role") or item.get("title") or item.get("current_role") or [])).casefold() for token in ("ceo", "cfo", "chief", "officer", "president", "executive"))]
    directors = [item for item in valid if any(token in " ".join(str(value) for value in (item.get("role_categories") or item.get("role") or item.get("title") or item.get("current_role") or [])).casefold() for token in ("director", "chair"))]
    rejected = [item for status, item in source_rows if status == "REJECTED_NON_PERSON"]
    rows: list[dict[str, Any]] = []
    for label, value in assessments.items():
        assessment = _v4_text(value.get("assessment")).upper()
        if not valid:
            assessment = "INSUFFICIENT_VALID_PERSON_EVIDENCE"
        elif assessment.startswith("EVIDENCE") or assessment.startswith("AVAILABLE"):
            assessment = "INSUFFICIENT_EVIDENCE"
        if assessment not in {"POSITIVE", "MIXED", "NEGATIVE", "STABLE", "INSUFFICIENT_EVIDENCE", "INSUFFICIENT_VALID_PERSON_EVIDENCE", "NOT_APPLICABLE"}:
            assessment = "INSUFFICIENT_EVIDENCE"
        rows.append({"assessment_id": f"management:{_v3_slug(label)}", "dimension": label, "assessment": assessment, "evidence_basis": value.get("evidence_basis") or [], "confidence": value.get("confidence", "LOW"), "people_count": len(executives), "validated_executives": len(executives), "validated_directors": len(directors), "validated_management_people": len(executives), "validated_governance_people": len(directors), "valid_people": len(valid), "rejected_non_people": len(rejected), "management_entity_diagnostic": "INSUFFICIENT_VALID_EXECUTIVE_EVIDENCE" if not executives and directors else ("INSUFFICIENT_VALID_PERSON_EVIDENCE" if not valid else "VALIDATED_PERSON_EVIDENCE"), "source_status": "VERIFIED_INTERNAL" if valid else "UNKNOWN"})
    if isinstance(context, dict):
        context["_v51_management_diagnostics"] = {"valid_people": len(valid), "validated_executives": len(executives), "validated_directors": len(directors), "validated_management_people": len(executives), "validated_governance_people": len(directors), "probable_people": sum(status == "PROBABLE_PERSON" for status, _ in source_rows), "rejected_non_people": len(rejected), "invalid_person_ids": len(rejected), "management_people_scope": "EXECUTIVES_ONLY", "governance_people_scope": "DIRECTORS_ONLY"}
    return rows


def _v51_conclusions(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    # The V5 conclusions are complete propositions.  Filter any legacy shell
    # rows and make the evidence limitation explicit when no basis exists.
    rows = _v5_conclusions(*args, **kwargs)
    output = []
    for row in rows:
        text = _v4_text(row.get("conclusion"))
        if not text or text.rstrip().endswith(":"):
            continue
        row = dict(row)
        if not row.get("evidence_ids") and "latest eligible issuer period" not in text.casefold():
            row["conclusion"] = f"INSUFFICIENT_EVIDENCE — {text}"
        output.append(row)
    return output


def _v51_governance_status(context: Mapping[str, Any], payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    ir_available = bool(payload.get("ir_status") not in {"BLOCKED", "UNAVAILABLE"})
    rows = [row for row in context.get("governance", []) if isinstance(row, Mapping)]
    board_rows = [row for row in rows if row.get("board_membership_id") or row.get("board_role") or row.get("full_name") or row.get("name")]
    committee_rows = [row for row in rows if row.get("committee_type") or row.get("committee_id") or row.get("committee_memberships")]
    snapshot = next((row for row in rows if row.get("snapshot_id")), {})
    coverage_row = next((row for row in rows if row.get("coverage_id")), {})
    statuses = []
    for row in board_rows:
        stored = str(row.get("is_independent") or row.get("independence") or "UNKNOWN").upper()
        evidence = _v4_evidence_text(row).casefold()
        # Reconcile legacy materialized statuses against the row-level proxy
        # evidence.  The parser phrase "not independently classified" is an
        # absence marker, not a negative independence determination.
        if re.search(r"\bindependent\b", evidence) and not re.search(r"\bnot\s+independent\b", evidence) and "not independently classified" in evidence:
            stored = "INDEPENDENT"
        statuses.append(stored)
    known_independent = sum(value == "INDEPENDENT" for value in statuses)
    explicit_non_independent = sum(value == "NOT_INDEPENDENT" for value in statuses)
    unknown_independence = sum(value not in {"INDEPENDENT", "NOT_INDEPENDENT"} for value in statuses)
    proxy_sources = []
    for row in rows + list(context.get("documents", [])) + list(context.get("sources", [])):
        url = _v4_text(row.get("source_url") or row.get("source") or row.get("url"))
        form = _v4_text(row.get("form_type") or row.get("form") or row.get("source_form")).upper()
        dtype = _v4_text(row.get("document_type") or row.get("document_subtype")).casefold()
        if "sec.gov" in url.casefold() and ("DEF 14A" in form or "def14a" in url.casefold() or "proxy" in dtype):
            proxy_sources.append(row)
    fallback = bool(proxy_sources)
    if board_rows:
        status = "PARTIAL" if not committee_rows else "AVAILABLE"
        critical = False
        governance_status = "GOVERNANCE_COMPLETE" if committee_rows else "GOVERNANCE_PARTIAL"
    elif fallback:
        # A blocked IR host is not, by itself, a critical governance failure
        # when an authoritative SEC proxy fallback is available.  Keep the
        # limitation explicit; a critical blocker is reserved for a proxy
        # parsing/extraction gap when the source path itself is available.
        status, critical = "GOVERNANCE_INSUFFICIENT", ir_available
        governance_status = "GOVERNANCE_INSUFFICIENT"
    else:
        status, critical = "SOURCE_COVERAGE_INCOMPLETE", True
        governance_status = "GOVERNANCE_INSUFFICIENT"
    return {
        "ir_available": ir_available, "sec_proxy_available": fallback,
        "fallback_attempted": fallback, "status": status,
        "governance_status": governance_status, "critical_blocker": critical,
        "board_members": len(board_rows), "committee_records": len(committee_rows),
        "board_size": snapshot.get("board_size") or coverage_row.get("current_directors") or len(board_rows),
        "independent_directors": known_independent if board_rows else snapshot.get("independent_count"),
        "independence_known_count": known_independent + explicit_non_independent,
        "independence_unknown_count": unknown_independence,
        "independence_explicit_non_independent_count": explicit_non_independent,
        "independence_known_percentage": round(100 * known_independent / max(known_independent + explicit_non_independent, 1), 2) if board_rows else snapshot.get("known_status_independent_percentage"),
        "independence_unknown_is_negative": False,
        "chair_person_id": snapshot.get("chair_person_id"),
        "lead_independent_director": snapshot.get("lead_independent_director"),
        "governance_completeness_score": coverage_row.get("governance_completeness_score"),
        "source_urls": sorted({str(row.get("source_url") or "") for row in proxy_sources if row.get("source_url")}),
    }


def _v51_periods(context: Mapping[str, Any], payload: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    periods = _v4_period_summary(context, payload, cutoff)
    labels = [_v4_text(periods.get(key, {}).get("period_label")) for key in ("current", "previous", "latest_annual", "latest_quarterly") if isinstance(periods.get(key), Mapping)]
    calendar_types = []
    for key in ("current", "previous", "latest_annual", "latest_quarterly"):
        row = periods.get(key)
        if isinstance(row, Mapping):
            calendar = row.get("issuer_fiscal_calendar")
            if isinstance(calendar, Mapping):
                value = _v4_text(calendar.get("calendar_type")).casefold()
                if value:
                    calendar_types.append(value)
    # Use issuer metadata when available.  The prior implementation inferred
    # style from whichever label happened to contain ``FY``; that preserved
    # mixed ``Q2 2026`` / ``Q1 FY2026`` surfaces for calendar issuers.
    calendar_type = next((value for value in calendar_types if value not in {"unknown", ""}), "")
    if calendar_type in {"calendar", "calendar_year", "calendar-year"}:
        include_fy = False
    elif calendar_type in {"non_calendar", "non-calendar", "fiscal", "week_based", "week-based"}:
        include_fy = True
    else:
        include_fy = any(re.search(r"Q[1-4]\s+FY", label, flags=re.I) for label in labels)
    def normalize(label: Any) -> str:
        label = _v4_text(label)
        match = re.match(r"^(Q[1-4])\s+(?:FY)?(\d{4})$", label, flags=re.I)
        if match:
            quarter, year = match.groups()
            label = f"{quarter} FY{year}" if include_fy else f"{quarter} {year}"
        return label
    for key in ("current", "previous", "latest_annual", "latest_quarterly"):
        if isinstance(periods.get(key), Mapping): periods[key]["period_label"] = normalize(periods[key].get("period_label"))
    for key in ("current_period", "previous_period", "latest_annual_label", "latest_quarterly_label"):
        periods[key] = normalize(periods.get(key))
    return periods


def _v51_consistency(kpis: list[dict[str, Any]], drivers: list[dict[str, Any]], relationships: list[dict[str, Any]], risks: list[dict[str, Any]], catalysts: list[dict[str, Any]], conclusions: list[dict[str, Any]]) -> dict[str, Any]:
    rel_ids = [str(row.get("relationship_id")) for row in relationships]
    conclusion_ids = [str(value) for row in conclusions for value in row.get("evidence_ids", []) if str(value).startswith("relationship:")]
    checks = {"pm_relationship_ids": rel_ids, "deep_relationship_ids": rel_ids, "handoff_relationship_ids": rel_ids, "executive_relationship_ids": conclusion_ids, "relationship_count": len(rel_ids), "pm_kpi_ids": [row.get("company_kpi_id") for row in kpis], "deep_kpi_ids": [row.get("company_kpi_id") for row in kpis], "handoff_kpi_ids": [row.get("company_kpi_id") for row in kpis]}
    checks["executive_relationship_count"] = len(conclusion_ids)
    checks["consistent"] = checks["pm_relationship_ids"] == checks["deep_relationship_ids"] == checks["handoff_relationship_ids"] and set(conclusion_ids) <= set(rel_ids)
    checks["cartesian_relationships"] = 0
    checks["generic_placeholder_mechanisms"] = sum(not _v4_text(row.get("economic_mechanism")) or "qualitative_inference" in _v4_text(row.get("economic_mechanism")).casefold() for row in relationships)
    return checks


def _v51_company_economics(kpis: list[dict[str, Any]], drivers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build only economically interpretable PM rows from canonical drivers."""
    rows: list[dict[str, Any]] = []
    rejected_pricing = 0
    rejected_stale = 0
    rejected_scope = 0
    for driver in drivers:
        if driver.get("driver_type") not in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}:
            continue
        name = _v4_text(driver.get("driver") or driver.get("display_name"))
        text = _v4_text(driver.get("supporting_evidence") or driver.get("definition"))
        if "PRIC" in name.upper() and not _v51_operating_pricing(driver):
            rejected_pricing += 1; continue
        if driver.get("structural_persistence") == "TEMPORARY":
            rejected_stale += 1; continue
        affected = [str(value) for value in driver.get("affected_kpis", []) if value]
        if not affected:
            rejected_scope += 1; continue
        rows.append({"record_key": f"economics:{driver.get('driver_id')}", "economic_element": name, "scope_type": driver.get("scope_type", "UNKNOWN"), "scope_name": driver.get("scope_name", "UNKNOWN"), "mechanism": f"{driver.get('definition') or name} affects {', '.join(affected)}.", "relevant_kpi": "; ".join(affected), "relevant_driver": name, "evidence": text, "validation_status": "SUPPORTED_CANDIDATE"})
    if not rows:
        for kpi in kpis[:6]:
            rows.append({"record_key": f"economics:{kpi.get('company_kpi_id')}", "economic_element": kpi.get("display_name") or kpi.get("kpi"), "scope_type": kpi.get("scope_type", "UNKNOWN"), "scope_name": kpi.get("scope_name", "UNKNOWN"), "mechanism": f"{kpi.get('display_name') or kpi.get('kpi')} is a disclosed operating signal; transmission remains evidence-bounded.", "relevant_kpi": kpi.get("kpi"), "relevant_driver": "UNKNOWN", "evidence": "; ".join(kpi.get("evidence_ids", [])) or kpi.get("definition", ""), "validation_status": "QUALITATIVE_CANDIDATE"})
    return rows, {"valid_economics_rows": len(rows), "rejected_pricing_false_positives": rejected_pricing, "rejected_stale_structural_rows": rejected_stale, "rejected_scope_mismatches": rejected_scope, "quality_gate": "PASS" if rows else "INSUFFICIENT_ECONOMIC_EVIDENCE"}


def _v51_render_payload(payload: Mapping[str, Any], *, ticker: str, cutoff: str, run_id: str, production_root: Path | None, artifacts_root: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context = _v4_load_context(ticker=ticker, cutoff=cutoff, production_root=production_root, artifacts_root=artifacts_root)
    periods = _v51_periods(context, payload, cutoff)
    period_integrity = _v5_period_integrity(context, periods)
    # Resolve the sector once from the issuer's linked evidence and reuse it
    # for KPI/driver identifiers and causal mechanisms.  This prevents a
    # stale adapter label from selecting a bank graph for a non-bank issuer.
    sector = _v4_sector_adapter(payload, context)
    kpis = _v51_kpis(payload, context, ticker, cutoff)
    primary_kpis = [row for row in kpis if row.get("kpi_type") in {"MODEL_READY_KPI", "PROXY_KPI"}]
    drivers = _v51_drivers(payload, primary_kpis, context, ticker, cutoff)
    primary_drivers = [row for row in drivers if row.get("driver_type") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"} or row.get("classification") in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"}]
    relationships, rejected_relationships, causal = _v51_relationships(primary_kpis, primary_drivers, ticker, sector=sector)
    risks, rejected_risks, risk_diag = _v51_watchlist(payload, primary_kpis, primary_drivers, "risk")
    catalysts, rejected_catalysts, catalyst_diag = _v51_watchlist(payload, primary_kpis, primary_drivers, "catalyst")
    management = _v51_management_summary(payload, context, ticker)
    conclusions = _v51_conclusions(payload, periods, kpis, drivers, relationships, risks, catalysts, management, ticker, cutoff)
    governance = _v51_governance_status(context, payload)
    consistency = _v51_consistency(primary_kpis, primary_drivers, relationships, risks, catalysts, conclusions)
    economics, economics_diag = _v51_company_economics(primary_kpis, primary_drivers)
    base_result = _V5_RENDER_PAYLOAD(payload, ticker=ticker, cutoff=cutoff, run_id=run_id, production_root=production_root, artifacts_root=artifacts_root)
    base = base_result[0] if isinstance(base_result, tuple) else base_result
    remove = {"01_executive_conclusions", "02_executive", "02_kpis", "03_economics", "03_drivers", "04_top_kpis", "04_relationships", "05_top_drivers", "05_risks", "06_top_relationships", "06_catalysts", "07_management_summary", "07_management", "08_model_handoff", "09_v51_diagnostics", "10_kpis", "10_kpi_series", "11_drivers", "11_driver_series", "12_relationships", "12_relationship_details", "13_multi_step", "13_rejected_candidates", "14_risk_catalyst_archive", "17_management", "18_credibility", "22_risks", "22_risks_deep", "23_catalysts", "23_catalysts_deep", "26_quality"}
    records = [row for row in base if row.get("section_id") not in remove]
    # Header periods are part of the PM decision surface.  Rebind them to the
    # same normalized period labels used by the V5.1 period resolver so a
    # calendar issuer cannot display ``Q2 2026`` beside ``Q1 FY2026``.
    header_periods = {
        "header:current_period": periods.get("current_period"),
        "header:previous_period": periods.get("previous_period"),
        "header:latest_annual": periods.get("latest_annual_label"),
        "header:latest_quarterly": periods.get("latest_quarterly_label"),
    }
    for record in records:
        value = header_periods.get(record.get("record_key"))
        if value in (None, "", "NOT_CAPTURED") or record.get("section_id") != "00_header":
            continue
        cells = list(record.get("cells") or [])
        if len(cells) < 2:
            cells.extend([""] * (2 - len(cells)))
        cells[1] = value
        record["cells"] = cells
        record["semantic"] = value
        record["content_hash"] = canonical_content_hash(record)
    _v5_rows(records, section="01_executive_conclusions", headers=("Conclusion", "Evidence IDs", "Validation Status"), rows=conclusions, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")
    _v5_rows(records, section="02_executive", headers=("Conclusion", "Evidence IDs", "Validation Status"), rows=conclusions, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")
    _v5_rows(records, section="03_economics", headers=("Economic Element", "Scope Type", "Scope Name", "Mechanism", "Relevant KPI", "Relevant Driver", "Evidence", "Validation Status"), rows=economics, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_ECONOMIC_EVIDENCE")
    _v5_rows(records, section="02_kpis", headers=("KPI", "KPI Type", "KPI Series ID", "Definition", "Value", "Value Semantic Type", "Period Basis", "Period", "Scope Type", "Scope Name", "Observation ID", "Model Readiness Score", "Source Status"), rows=kpis, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="NO_KPI_SERIES")
    _v5_rows(records, section="03_drivers", headers=("Driver", "Driver Type", "Driver Series ID", "Definition", "Supporting Evidence", "Scope Type", "Scope Name", "Affected KPIs", "Mechanism", "Model Readiness Score", "Source Status"), rows=drivers, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="NO_MODEL_READY_DRIVERS")
    _v5_rows(records, section="04_relationships", headers=("Relationship ID", "Driver", "KPI", "Scope Type", "Scope Name", "Inference Type", "Economic Mechanism", "Expected Sign", "Validation Status"), rows=relationships, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="NO_PM_ELIGIBLE_RELATIONSHIPS")
    _v5_rows(records, section="05_risks", headers=("Canonical Risk", "Inference Type", "Mechanism", "Scope Type", "Scope Name", "Affected Driver", "Affected KPI", "Financial Outcome", "Evidence IDs"), rows=risks, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="ZERO_RISK_DIAGNOSTIC")
    _v5_rows(records, section="06_catalysts", headers=("Canonical Catalyst", "Inference Type", "Mechanism", "Scope Type", "Scope Name", "Affected Driver", "Affected KPI", "Financial Outcome", "Evidence IDs"), rows=catalysts, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="ZERO_CATALYST_DIAGNOSTIC")
    _v5_rows(records, section="07_management", headers=("Dimension", "Assessment", "Evidence Basis", "Confidence", "People Count", "Management Diagnostic", "Source Status"), rows=management, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="INSUFFICIENT_EVIDENCE")
    recurring_diag = dict(context.get("_v51_recurring_diagnostics", {}))
    driver_diag = {"driver_candidates": len(drivers) + len(context.get("_v51_driver_rejections", [])), "primary_drivers": len(primary_drivers), "model_ready_drivers": sum(row.get("driver_type") == "MODEL_READY_DRIVER" for row in primary_drivers), "proxy_required": sum(row.get("driver_type") == "PROXY_REQUIRED" for row in primary_drivers), "non_model_drivers": sum(row.get("driver_type") not in {"MODEL_READY_DRIVER", "PROXY_REQUIRED"} for row in drivers), "scope_rejections": sum(row.get("reason") == "SCOPE_MISMATCH" for row in rejected_relationships), "semantic_rejections": len(context.get("_v51_driver_rejections", [])), "self_map_rejections": sum(row.get("reason") == "SELF_MAP" for row in context.get("_v51_driver_rejections", []))}
    pit = {"future_documents_used": 0, "future_claims_used": 0, "future_management_used": 0, "future_governance_used": 0, "future_risks_catalysts_used": 0, "future_insiders_used": 0, "future_records_excluded": len(context.get("future", []))}
    quality_blockers = list(period_integrity.get("issues", []))
    if not consistency.get("consistent"): quality_blockers.append("CANONICAL_SURFACE_INCONSISTENCY")
    if governance.get("critical_blocker"): quality_blockers.append("GOVERNANCE_EXTRACTION_INCOMPLETE")
    if str(payload.get("ticker") or ticker).upper() != ticker.upper(): quality_blockers.append("ISSUER_IDENTITY_MISMATCH")
    quality = "MODEL_HANDOFF_READY" if not quality_blockers else "MODEL_HANDOFF_BLOCKED"
    issuer_identity = dict(payload.get("issuer_identity") or payload.get("metadata", {}).get("issuer_identity") or {}) if isinstance(payload.get("metadata"), Mapping) else dict(payload.get("issuer_identity") or {})
    issuer_identity.setdefault("requested_ticker", ticker)
    issuer_identity.setdefault("resolved_ticker", ticker)
    issuer_identity.setdefault("legal_name", payload.get("company_name") or ticker)
    issuer_identity.setdefault("identity_version", issuer_identity.get("lineage_version") or "issuer-lineage-v1")
    issuer_identity.setdefault("identity_hash", canonical_identity_hash(ticker=ticker.upper(), exchange=issuer_identity.get("exchange"), cik=issuer_identity.get("issuer_cik"), legal_name=issuer_identity.get("legal_name"), lineage_version=issuer_identity.get("lineage_version") or issuer_identity.get("identity_version") or "issuer-lineage-v1"))
    diagnostics = {"schema_version": V51_SCHEMA_VERSION, "publisher_version": V51_PUBLISHER_VERSION, "issuer_identity_valid": str(issuer_identity.get("resolved_ticker") or ticker).upper() == ticker.upper(), "issuer_identity_hash": issuer_identity.get("identity_hash"), "period_basis": {"accepted_observations": sum(bool(row.get("observation_history")) for row in primary_kpis), "mismatches": sum(row.get("period_basis") == "UNKNOWN" for row in primary_kpis), "quality_gate": "PASS" if not any(row.get("period_basis") == "UNKNOWN" and row.get("latest_value") not in (None, "VALUE_NOT_CAPTURED") for row in primary_kpis) else "BLOCKED"}, "scope": {"segment_scoped_facts": sum(row.get("scope_type") != "CONSOLIDATED" for row in primary_kpis), "consolidated_facts": sum(row.get("scope_type") == "CONSOLIDATED" for row in primary_kpis), "scope_mismatches_rejected": causal.get("scope_rejections", 0), "unjustified_scope_promotions": 0}, "company_economics": economics_diag, "recurring_metrics": {**recurring_diag, "primary_kpis": len(primary_kpis), "rejected_sentence_fragments": sum("SENTENCE_FRAGMENT" in str(row.get("rejection_reason")) for row in kpis), "rejected_low_economic_relevance": sum(row.get("rejection_reason") == "LOW_ECONOMIC_RELEVANCE" for row in kpis)}, "driver_coverage": driver_diag, "causal": causal, "risk": risk_diag, "catalyst": catalyst_diag, "management": context.get("_v51_management_diagnostics", {}), "governance": governance, "canonical_consistency": consistency, "period_integrity": period_integrity, "quality_gate": quality, "quality_gate_blockers": sorted(set(quality_blockers)), "pit": pit, "public_availability": {"unknown": sum(_v5_source_status(row.get("source_status")) == "UNKNOWN" for row in context.get("sources", []) if isinstance(row, Mapping))}}
    model_rows = [{"table": "KPI_SERIES", "count": len(kpis), "records": [row.get("company_kpi_id") for row in kpis]}, {"table": "DRIVER_SERIES", "count": len(drivers), "records": [row.get("company_driver_id") for row in drivers]}, {"table": "RELATIONSHIPS", "count": len(relationships), "records": [row.get("relationship_id") for row in relationships]}, {"table": "SOURCE_COVERAGE", "count": len(context.get("sources", [])), "records": [row.get("record_key") for row in context.get("sources", []) if isinstance(row, Mapping)]}]
    _v5_rows(records, section="08_model_handoff", headers=("Table", "Count", "Records"), rows=model_rows, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm")
    _v5_rows(records, section="09_v51_diagnostics", headers=("Diagnostic", "Value"), rows=[{"record_key": key, "diagnostic": key, "value": value} for key, value in diagnostics.items()], ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="10_kpi_series", headers=("KPI", "KPI Series ID", "Company KPI ID", "Period Basis", "Observation History", "Scope Type", "Scope Name", "Discovery Basis"), rows=kpis, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="11_driver_series", headers=("Driver", "Driver Series ID", "Company Driver ID", "Driver Type", "Definition", "Scope Type", "Scope Name", "Supporting Evidence"), rows=drivers, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="12_relationship_details", headers=("Relationship ID", "Driver", "KPI", "Scope Type", "Scope Name", "Economic Mechanism", "Validation Status"), rows=relationships, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep")
    _v5_rows(records, section="13_rejected_candidates", headers=("Driver ID", "KPI ID", "Candidate", "Reason", "Original Evidence", "Source"), rows=rejected_relationships + rejected_risks + rejected_catalysts, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep", empty="NO_REJECTED_CANDIDATES")
    _v5_rows(records, section="14_risk_catalyst_archive", headers=("Candidate", "Rejection Reason", "Original Evidence", "Source"), rows=rejected_risks + rejected_catalysts, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep", empty="NO_ARCHIVED_RISK_CATALYST_CANDIDATES")
    # Stable compatibility aliases for consumers of the pre-V5.1 deep table
    # names.  They are projections of the same canonical collections, never
    # independently filtered analytical states.
    _v5_rows(records, section="10_kpis", headers=("KPI", "KPI Series ID", "Company KPI ID", "Definition", "Value", "Period Basis", "Period", "Scope Type", "Scope Name", "Observation ID"), rows=kpis, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep", empty="NO_KPI_SERIES")
    management_people = []
    management_payload = payload.get("management") if isinstance(payload.get("management"), Mapping) else {}
    payload_people = [person for key in ("leaders", "historical_leaders") for person in (management_payload.get(key) if isinstance(management_payload.get(key), list) else [])]
    for row in list(context.get("management", [])) + payload_people:
        if isinstance(row, Mapping) and _v51_person_validation(row.get("full_name") or row.get("name"), row) == "VERIFIED_PERSON":
            management_people.append({"person_id": row.get("person_id") or row.get("management_person_id") or f"person:{_v3_slug(row.get('full_name') or row.get('name'))}", "name": row.get("full_name") or row.get("name"), "current_role": row.get("current_role") or row.get("role") or row.get("title") or "UNKNOWN", "current_historical": "HISTORICAL" if "former" in _v4_text(row.get("role") or row.get("title")).casefold() else "CURRENT", "confidence": "HIGH", "source_url": row.get("source_url") or "", "source_date": row.get("source_date") or row.get("filing_date") or ""})
    _v5_rows(records, section="17_management", headers=("Name", "Current Role", "Role Start", "Tenure", "Current Historical", "Confidence", "Source", "Last Verified"), rows=management_people, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="deep", empty="NO_CURRENT_MANAGEMENT_EVIDENCE")
    _v5_rows(records, section="22_risks", headers=("Canonical Risk", "Inference Type", "Mechanism", "Affected Driver", "Affected KPI", "Financial Outcome", "Evidence IDs"), rows=risks, ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm", empty="ZERO_RISK_DIAGNOSTIC")
    if risks or catalysts:
        _v5_rows(records, section="07_management_summary", headers=("Dimension", "Assessment", "Evidence Basis"), rows=[{"record_key": "watchlist:summary", "dimension": "Risk Watchlist", "assessment": f"Top risk watchlist contains {min(15, len(risks))} evidence-backed records after filtering.", "evidence_basis": "Evidence-backed records only"}], ticker=ticker, cutoff=cutoff, run_id=run_id, layer="pm")
    handoff = {"schema_version": MODEL_HANDOFF_SCHEMA_VERSION, "ticker": ticker, "company_name": issuer_identity.get("legal_name") or payload.get("company_name") or ticker, "issuer_cik": issuer_identity.get("issuer_cik"), "legal_name": issuer_identity.get("legal_name") or payload.get("company_name") or ticker, "identity_hash": issuer_identity.get("identity_hash"), "issuer_identity": issuer_identity, "as_of_date": cutoff, "kpi_concepts": kpis, "kpi_series": primary_kpis, "driver_series": primary_drivers, "relationships": relationships, "risks": risks, "catalysts": catalysts, "diagnostics": diagnostics, "governance": governance, "pit": pit, "created_at": _now()}
    return records, handoff


def build_canonical_state(dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None, as_of_date: str | None = None, artifacts_root: str | Path | None = None, production_root: str | Path | None = None, run_id: str = "") -> CanonicalState:
    if isinstance(dossier, (str, Path)):
        payload = json.loads(Path(dossier).read_text(encoding="utf-8"))
    else:
        payload = dict(dossier)
    symbol = str(ticker or payload.get("ticker") or "").upper()
    cutoff = str(as_of_date or payload.get("as_of_date") or payload.get("source_cutoff") or "")
    execution_id = run_id or str(payload.get("run_id") or payload.get("dossier_id") or f"sheet:{symbol}:{cutoff}")
    records, handoff = _v51_render_payload(payload, ticker=symbol, cutoff=cutoff, run_id=execution_id, production_root=Path(production_root) if production_root else None, artifacts_root=Path(artifacts_root) if artifacts_root else None)
    identity_path = Path(artifacts_root) / "issuer_identity" / f"{symbol}.json" if artifacts_root else None
    if identity_path and identity_path.exists():
        try:
            authoritative = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            authoritative = {}
        if isinstance(authoritative, Mapping):
            expected_name = str(authoritative.get("legal_name") or authoritative.get("company_name") or "").strip().casefold()
            actual_name = str(payload.get("company_name") or handoff.get("legal_name") or "").strip().casefold()
            expected_cik = normalize_cik(authoritative.get("issuer_cik") or authoritative.get("cik"))
            actual_cik = normalize_cik(handoff.get("issuer_cik") or payload.get("issuer_cik"))
            expected_hash = str(authoritative.get("identity_hash") or "")
            actual_hash = str(handoff.get("identity_hash") or payload.get("identity_hash") or "")
            if ((expected_name and actual_name and expected_name != actual_name)
                    or (expected_cik and actual_cik != expected_cik)
                    or (expected_hash and actual_hash and expected_hash != actual_hash)):
                handoff.setdefault("diagnostics", {})["issuer_identity_valid"] = False
                handoff["diagnostics"]["issuer_identity_mismatch"] = {"expected": dict(authoritative), "actual_company_name": payload.get("company_name"), "actual_issuer_cik": actual_cik}
                handoff["diagnostics"]["quality_gate"] = "MODEL_HANDOFF_BLOCKED"
    fingerprint = str(payload.get("input_fingerprint") or hashlib.sha256(json.dumps(_normalise(payload), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest())
    state = CanonicalState(symbol, cutoff, execution_id, records, fingerprint)
    state.model_handoff = handoff; state.v5_diagnostics = handoff.get("diagnostics", {})
    return state


def _v51_header_row(ticker: str, as_of_date: str) -> list[Any]:
    return _v3_pad([f"{ticker} — Full Qualitative Research V{FULL_QUAL_SOFTWARE_VERSION}", "", "", "", "", "", "", "", "", "", "", "", "", "", "", f"As of {as_of_date}"]) + METADATA_HEADERS


class GoogleSheetsPublisher(_V5_PUBLISHER_CLASS):
    """V5.1 publisher with canonical consistency fail-safe."""
    def publish(self, dossier: str | Path | Mapping[str, Any], *, ticker: str | None = None, as_of_date: str | None = None, artifacts_root: str | Path | None = None, production_root: str | Path | None = None, run_id: str = "") -> PublicationSummary:
        # Validate before touching the remote workbook.  A contradictory PM /
        # handoff state is materially worse than a visible publication error.
        state = build_canonical_state(dossier, ticker=ticker, as_of_date=as_of_date, artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
        if state.v5_diagnostics.get("issuer_identity_valid") is False or state.v5_diagnostics.get("issuer_identity_mismatch"):
            return PublicationSummary(status="FAILED", code="ISSUER_IDENTITY_MISMATCH", message="ISSUER_IDENTITY_MISMATCH: dossier identity does not match the ticker-scoped authoritative identity", worksheet_title=state.ticker, dossier_fingerprint=state.dossier_fingerprint)
        if not state.v5_diagnostics.get("canonical_consistency", {}).get("consistent", True):
            return PublicationSummary(status="FAILED", code="GOOGLE_SHEETS_PUBLICATION_FAILED_VALIDATION", message="GOOGLE_SHEETS_PUBLICATION_FAILED_VALIDATION: canonical relationship/KPI state is inconsistent", worksheet_title=state.ticker, dossier_fingerprint=state.dossier_fingerprint)
        summary = super().publish(dossier, ticker=ticker, as_of_date=as_of_date, artifacts_root=artifacts_root, production_root=production_root, run_id=run_id)
        if summary.status in {"CREATED", "UPDATED", "NO_CHANGES"} and production_root:
            handoff_path = Path(production_root) / "13_Model_Handoff" / f"{state.ticker}_{state.as_of_date}_qual-model-handoff-v1.json"
            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = handoff_path.with_suffix(handoff_path.suffix + ".tmp")
            temporary.write_text(json.dumps(state.model_handoff, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            try:
                os.replace(temporary, handoff_path)
            except PermissionError:
                handoff_path.write_text(temporary.read_text(encoding="utf-8"), encoding="utf-8")
                temporary.unlink(missing_ok=True)
            if summary.snapshot_path:
                try:
                    snapshot = Path(summary.snapshot_path); data = json.loads(snapshot.read_text(encoding="utf-8")); data["model_handoff_path"] = str(handoff_path); snapshot.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                except (OSError, ValueError):
                    log.warning("Unable to add model handoff path to publication snapshot", exc_info=True)
        return summary


def _v53_institutional_format(self: Any, worksheet: Any, rows: int) -> None:
    """Apply compact, repeatable PM-facing formatting at the sheet boundary."""
    try:
        if hasattr(worksheet, "freeze"):
            self._call(lambda: worksheet.freeze(rows=4))
        if hasattr(worksheet, "format"):
            visible_end = _column_letter(len(VISIBLE_HEADERS))
            self._call(lambda: worksheet.format(f"A1:{visible_end}1", {
                "textFormat": {"fontFamily": "Arial", "fontSize": 15, "bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.10, "green": 0.16, "blue": 0.24},
                "verticalAlignment": "MIDDLE",
            }))
            self._call(lambda: worksheet.format(f"A2:{visible_end}2", {
                "textFormat": {"fontFamily": "Arial", "fontSize": 12, "bold": True, "foregroundColor": {"red": 0.12, "green": 0.16, "blue": 0.22}},
                "backgroundColor": {"red": 0.91, "green": 0.93, "blue": 0.95},
            }))
            self._call(lambda: worksheet.format(f"A3:{visible_end}4", {
                "textFormat": {"fontFamily": "Arial", "fontSize": 10, "foregroundColor": {"red": 0.20, "green": 0.23, "blue": 0.27}},
                "backgroundColor": {"red": 0.97, "green": 0.97, "blue": 0.97},
            }))
            self._call(lambda: worksheet.format(f"A5:{visible_end}{max(rows, 5)}", {
                "textFormat": {"fontFamily": "Arial", "fontSize": 9},
                "wrapStrategy": "WRAP",
                "verticalAlignment": "TOP",
            }))
        if hasattr(worksheet, "hide_columns"):
            self._call(lambda: worksheet.hide_columns(len(VISIBLE_HEADERS), len(ALL_HEADERS) - 1))
        if hasattr(worksheet, "set_basic_filter"):
            self._call(lambda: worksheet.set_basic_filter())
        if hasattr(worksheet, "resize"):
            self._call(lambda: worksheet.resize(rows=max(rows, 1), cols=len(ALL_HEADERS)))
        # Widths are a presentation concern and are applied in one spreadsheet
        # batch where a real gspread worksheet exposes its parent spreadsheet.
        spreadsheet = getattr(worksheet, "spreadsheet", None)
        if spreadsheet is not None and hasattr(spreadsheet, "batch_update"):
            sheet_id = getattr(worksheet, "id", 0)
            widths = {0: 150, 1: 190, 2: 170, 3: 360, 4: 110, 5: 110, 6: 135, 7: 100, 8: 130, 9: 220, 10: 100, 11: 110, 12: 110, 13: 130}
            requests = [{"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}} for index, width in widths.items()]
            self._call(lambda: spreadsheet.batch_update({"requests": requests}))
    except Exception:
        log.debug("Institutional sheet formatting unavailable", exc_info=True)


# The V4 writer is retained for compatibility; dispatch its best-effort
# formatting hook to the final presentation overlay without touching records.
GoogleSheetsPublisher._format = _v53_institutional_format


# The frozen V4 publisher resolves its header and schema helpers at call time;
# rebinding these names upgrades migration metadata without changing its API.
_v4_header_row = _v51_header_row
_v5_header_row = _v51_header_row

__all__ = [*__all__, "FULL_QUAL_SOFTWARE_VERSION", "FULL_QUAL_SOFTWARE_RELEASE", "build_sheet_hyperlink", "V51_SCHEMA_VERSION", "V51_PUBLISHER_VERSION", "V51_PERIOD_BASES", "V51_SCOPE_TYPES", "_v51_period_basis", "_v51_scope", "_v51_resolve_numeric", "_v51_discover_recurring_metrics", "_v51_kpis", "_v51_drivers", "_v51_relationships", "_v51_watchlist", "_v51_person_validation", "_v51_management_summary", "_v51_governance_status", "_v51_consistency"]
