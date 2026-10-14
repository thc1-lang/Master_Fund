"""Incrementally synchronise historical CFTC COT data with Google Sheets.

The pipeline downloads and validates complete annual Disaggregated Futures Only
reports from 2010 through the current UTC year, reduces each one to a canonical
six-column raw staging table, reconciles ``COT YYYY`` by stable record key, builds
56 contract-code-matched histories using each tab's G1 setting, and updates the
Summary date/Flip matrix. Identical data causes no Google Sheets writes.

The default command also runs a separate Traders in Financial Futures (TFF)
Futures Only branch for three independently configured workbooks. The TFF branch
uses the same authentication/session, but its annual source frames, eight-column
raw schema, instrument histories and summaries remain isolated from commodities.

Use ``--dry-run`` to download, validate, read the workbook and report the exact diff
without mutating it. Temporary downloads always live in TemporaryDirectory objects.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CFTC_INDEX_URL = (
    "https://www.cftc.gov/MarketReports/CommitmentsofTraders/"
    "HistoricalCompressed/index.htm"
)
DEFAULT_SERVICE_ACCOUNT_PATH = r'C:\Code\service_account.json'
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SPREADSHEET_ID = "14PJjSpPU731GZPU_BEe98Je0AJiM7m2GZfm0vZZqWEY"
TFF_SPREADSHEET_IDS = (
    "1zPe35IBmgdPJdh-aUGMz_UF_7sAHAhIIvjzcTZFEzYA",
    "1l4S4rhV06FyDhPz0zuFNnBLSn6JFUd1bwESQemxOd2Q",
    "19mg01oVPQlHjYC6jk72glxMNtoD6Q7pay7-S4GUARac",
)
START_YEAR = 2010
TFF_START_YEAR = 2010
SUMMARY_SHEET_NAME = "Summary"
GOOGLE_SHEETS_SAFE_CELL_LIMIT = 10_000_000
DISAGG_SECTION_LABEL = "Disaggregated Futures Only Reports"
TFF_SECTION_LABEL = "Traders in Financial Futures ; Futures Only Reports"

COMMODITY_SHEETS = [
    "WTI Crude", "Brent Crude", "Henry Hub Natural Gas", "RBOB Gasoline",
    "Heating Oil", "ICE Gasoil", "European TTF Natural Gas", "JKM LNG",
    "Oman Crude", "Naphtha", "Fuel Oil", "Singapore marine fuel", "Propane",
    "Gasoline Crack", "Diesel Crack", "Gold", "Silver", "Platinum",
    "Palladium", "Copper", "Aluminium", "Zinc", "Nickel", "Lead", "Tin",
    "Iron Ore 62% Fe", "Iron Ore 58% Fe", "Coking Coal", "Thermal Coal",
    "HRC Steel", "Corn", "Chicago SRW Wheat", "Kansas HRW Wheat",
    "Soybeans", "Soybean Meal", "Soybean Oil", "Oats", "Rough Rice",
    "Canola", "European Wheat", "Rapeseed", "Palm Oil", "Coffee Arabica",
    "Coffee Robusta", "Cocoa", "Sugar #11", "Cotton #2",
    "Frozen Orange Juice", "Rubber", "Live Cattle", "Feeder Cattle",
    "Lean Hogs", "Lithium Carbonate", "Lithium Hydroxide", "Cobalt", "Uranium",
]

COMMODITY_HEADERS = ["Date", "Open Interest", "Manager Long", "Manager Short", "Flip"]

# Canonical raw staging schema. CFTC annual files contain roughly 191 fields,
# but downstream calculations and reconciliation need only these six. Keeping
# the order fixed makes the Google Sheets footprint predictable and lets future
# source-header changes be handled before storage.
RAW_MARKET_HEADER = "Market_and_Exchange_Names"
RAW_CONTRACT_HEADER = "CFTC_Contract_Market_Code"
RAW_DATE_HEADER = "Report_Date_as_YYYY-MM-DD"
RAW_OPEN_INTEREST_HEADER = "Open_Interest_All"
RAW_MANAGER_LONG_HEADER = "M_Money_Positions_Long_All"
RAW_MANAGER_SHORT_HEADER = "M_Money_Positions_Short_All"
COMPACT_RAW_HEADERS = [
    RAW_MARKET_HEADER,
    RAW_CONTRACT_HEADER,
    RAW_DATE_HEADER,
    RAW_OPEN_INTEREST_HEADER,
    RAW_MANAGER_LONG_HEADER,
    RAW_MANAGER_SHORT_HEADER,
]

# TFF Futures Only uses different participant classifications from the
# Disaggregated report. Keep this schema separate so a source-family mix-up is
# visible in both code review and the grid-size preflight.
TFF_RAW_ASSET_LONG_HEADER = "Asset_Mgr_Positions_Long_All"
TFF_RAW_ASSET_SHORT_HEADER = "Asset_Mgr_Positions_Short_All"
TFF_RAW_LEV_LONG_HEADER = "Lev_Money_Positions_Long_All"
TFF_RAW_LEV_SHORT_HEADER = "Lev_Money_Positions_Short_All"
TFF_COMPACT_RAW_HEADERS = [
    RAW_MARKET_HEADER,
    RAW_CONTRACT_HEADER,
    RAW_DATE_HEADER,
    RAW_OPEN_INTEREST_HEADER,
    TFF_RAW_ASSET_LONG_HEADER,
    TFF_RAW_ASSET_SHORT_HEADER,
    TFF_RAW_LEV_LONG_HEADER,
    TFF_RAW_LEV_SHORT_HEADER,
]

TFF_FULL_HEADERS = [
    "Date", "Open Interest", "Asset Manager Long", "Asset Manager Short",
    "Asset Manager Flip", "Leveraged Fund Long", "Leveraged Fund Short",
    "Leveraged Fund Flip",
]
# Existing prepared TFF tabs use the commodity-era five-column labels. Preserve
# those labels and their A:E layout, while sourcing the values from TFF Asset
# Manager fields. New/prepared tabs with explicit headers use TFF_FULL_HEADERS.
TFF_LEGACY_HEADERS = ["Date", "Open Interest", "Manager Long", "Manager Short", "Flip"]


@dataclass
class ExcelLink:
    year: int
    url: str


@dataclass
class SheetInfo:
    title: str
    sheet_id: int
    row_count: int
    column_count: int


@dataclass
class YearDataset:
    year: int
    url: str
    dataframe: pd.DataFrame
    market_col: str
    date_col: str
    contract_col: str | None
    open_interest_col: str
    manager_long_col: str
    manager_short_col: str
    dates: pd.Series
    source_column_count: int = 0


@dataclass
class TFFYearDataset:
    year: int
    url: str
    dataframe: pd.DataFrame
    market_col: str
    date_col: str
    contract_col: str
    open_interest_col: str
    asset_long_col: str
    asset_short_col: str
    lev_long_col: str
    lev_short_col: str
    dates: pd.Series
    source_column_count: int = 0


@dataclass
class ContractCodeConfig:
    """Read-only G1 configuration for one commodity sheet."""
    commodity: str
    raw_value: str
    code: str
    valid: bool


@dataclass
class SyncResult:
    name: str
    existing: int = 0
    target: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    removal_candidates_preserved: int = 0
    writes: int = 0
    schema_migrated: bool = False
    skipped: bool = False
    reason: str = ""
    # Summary-specific reconciliation diagnostics.  These remain optional so
    # the same result type can continue to be used by raw and instrument syncs.
    backfilled: int = 0
    corrected: int = 0
    cleared: int = 0
    unchanged_cells: int = 0
    coverage: Dict[str, Dict[str, Any]] | None = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed or self.writes
                    or self.schema_migrated)


@dataclass
class RunSummary:
    years_checked: int = 0
    raw_unchanged: int = 0
    raw_updated: int = 0
    raw_new: int = 0
    raw_corrected: int = 0
    raw_removed: int = 0
    commodities_checked: int = 0
    commodities_unchanged: int = 0
    commodities_updated: int = 0
    commodities_skipped: int = 0
    commodity_new: int = 0
    commodity_corrected: int = 0
    commodity_removed: int = 0
    commodity_removals_blocked: int = 0
    summary_new_dates: int = 0
    summary_cells_updated: int = 0
    summary_unchanged_cells: int = 0
    summary_backfilled: int = 0
    summary_corrected: int = 0
    summary_cleared: int = 0


def _http_session() -> requests.Session:
    """Retry transient GET failures; never treat permission/network errors as 404."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


HTTP = _http_session()


def _extract_year_candidates(text: str, href: str) -> List[int]:
    """Use digit boundaries: underscores in filenames are word characters."""
    return sorted({int(y) for y in re.findall(r"(?<!\d)([12]\d{3})(?!\d)", f"{text} {href}")})


def _report_stem(section_label: str) -> str:
    label = " ".join(str(section_label).lower().split())
    if label == DISAGG_SECTION_LABEL.lower():
        return "fut_disagg"
    if label == TFF_SECTION_LABEL.lower():
        return "fut_fin"
    raise ValueError(
        f"Unknown or unsupported CFTC report section: {section_label!r}; "
        "only Disaggregated Futures Only Reports and TFF Futures Only Reports are allowed"
    )


def _fallback_urls(section_label: str, target_year: int) -> List[str]:
    stem = _report_stem(section_label)
    return [f"https://www.cftc.gov/files/dea/history/{stem}_{kind}_{target_year}.zip"
            for kind in ("txt", "xls")]


def _url_exists(url: str) -> bool:
    """Probe with GET (some servers reject HEAD), checking the ZIP signature.

    Only 404/410 mean unavailable. Fail visibly on outages/access denial instead
    of silently selecting an older year. HTML error pages with HTTP 200 are errors.
    """
    with HTTP.get(url, timeout=(15, 45), stream=True) as response:
        if response.status_code in (404, 410):
            return False
        response.raise_for_status()
        prefix = next(response.iter_content(chunk_size=8), b"")
        if not prefix.startswith(b"PK\x03\x04"):
            raise RuntimeError(f"Expected a nonempty ZIP, received a different response: {url}")
        return True


def fetch_section_report_link(section_label: str, year: int | None = None) -> ExcelLink:
    """Check the requested/current year before the index, then newest older year.

    Index links must identify the exact futures-only family. Never substitute a
    combined/options/legacy report when section matching fails. Explicit --year
    never falls back to a different year. Canonical paths also work if the index
    is down. Current-year files are probed even when absent from the index.
    """
    current_year = pd.Timestamp.now(tz="UTC").year
    target_year = current_year if year is None else year
    if not 2006 <= target_year <= current_year:
        raise ValueError(f"Year must be between 2006 and {current_year}.")
    checked = set()

    def probe(y, urls):
        for url in urls:
            if url in checked:
                continue
            checked.add(url)
            if _url_exists(url):
                return ExcelLink(year=y, url=url)
        return None

    hit = probe(target_year, _fallback_urls(section_label, target_year))
    if hit:
        return hit

    indexed = {}
    try:
        with HTTP.get(CFTC_INDEX_URL, timeout=(15, 45)) as response:
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        stem = _report_stem(section_label)
        for anchor in soup.find_all("a", href=True):
            url = urljoin(CFTC_INDEX_URL, anchor["href"])
            parsed = urlparse(url)
            filename = Path(parsed.path).name.lower()
            # Restrict to annual ZIPs for this exact report family.
            match = re.fullmatch(rf"{stem}_(txt|xls)_([12]\d{{3}})\.zip", filename)
            if parsed.hostname not in {"www.cftc.gov", "cftc.gov"} or not match:
                continue
            indexed.setdefault(int(match[2]), []).append(url)
    except requests.RequestException as exc:
        print(f"WARNING: CFTC index unavailable ({exc}); using canonical annual paths.")

    years = [target_year] if year is not None else range(target_year, 2005, -1)
    for y in years:
        urls = indexed.get(y, []) + _fallback_urls(section_label, y)
        urls = sorted(set(urls), key=lambda u: ("_txt_" not in u.lower(), u))
        hit = probe(y, urls)
        if hit:
            if y != target_year:
                print(f"WARNING: {section_label}: {target_year} unavailable; using {y}.")
            return hit
    raise RuntimeError(f"No annual futures-only file available for {section_label}, year={year}.")


def _read_delimited_text(path: Path) -> pd.DataFrame:
    """Read UTF-8/Windows text without losing leading zeros in contract codes."""
    errors = []
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                sample = handle.read(16384)
        except UnicodeError as exc:
            errors.append(str(exc))
            continue
        try:
            first_sep = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            first_sep = ","
        for sep in dict.fromkeys([first_sep, ",", "\t", ";", "|"]):
            try:
                data = pd.read_csv(path, sep=sep, encoding=encoding, dtype=object,
                                   keep_default_na=False, low_memory=False)
                _audit_columns(data)  # Reject one-column parses, README and HTML.
                return data
            except (ValueError, RuntimeError, UnicodeError, pd.errors.ParserError) as exc:
                errors.append(str(exc))
    raise RuntimeError(f"Unable to parse CFTC text file {path.name}: {errors[-1:]}")


def download_report_to_dataframe(report_url: str) -> pd.DataFrame:
    """Read a verified report, preferring text members over legacy Excel.

    Try all supported members (e.g. skip README.txt). Ambiguous ZIPs containing
    multiple valid tables of the preferred format fail rather than lose data.
    Members are copied to a fixed temporary filename, never extracted by path.
    """
    suffix = Path(urlparse(report_url).path).suffix.lower() or ".bin"
    with tempfile.TemporaryDirectory(prefix="cot_download_") as temp_dir:
        local = Path(temp_dir) / f"report{suffix}"
        with HTTP.get(report_url, timeout=(15, 90)) as response:
            response.raise_for_status()
            local.write_bytes(response.content)

        def read(path):
            if path.suffix.lower() in {".xls", ".xlsx", ".xlsm"}:
                frame = pd.read_excel(path, sheet_name=0, dtype=object)
                _audit_columns(frame)
                return frame
            return _read_delimited_text(path)

        if suffix == ".zip":
            errors = []
            with zipfile.ZipFile(local) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()
                           and not m.filename.startswith("__MACOSX/")]
                for extensions in ((".txt", ".csv"), (".xlsx", ".xls", ".xlsm")):
                    valid = []
                    for member in members:
                        ext = Path(member.filename).suffix.lower()
                        if ext not in extensions:
                            continue
                        extracted = Path(temp_dir) / f"member{ext}"
                        extracted.write_bytes(archive.read(member))
                        try:
                            frame = read(extracted)
                            if frame.empty:
                                raise RuntimeError("Empty report table")
                            valid.append(frame)
                        except (ValueError, RuntimeError, ImportError, UnicodeError) as exc:
                            errors.append(f"{member.filename}: {exc}")
                    if len(valid) > 1:
                        raise RuntimeError("ZIP contains multiple report tables; cannot safely select one.")
                    if valid:
                        return valid[0].fillna("")
            raise RuntimeError(f"ZIP contains no readable CFTC report: {errors}")
        data = read(local)
        if data.empty:
            raise RuntimeError("Downloaded report is empty.")
        return data.fillna("")



def _normalize_column_name(name: object) -> str:
    """Ignore case, whitespace, punctuation and underscores in field names."""
    return ''.join(ch.lower() for ch in str(name) if ch.isalnum())


def _find_column(df: pd.DataFrame, candidates: List[str]) -> str | None:
    for candidate in candidates:
        hits = [c for c in df.columns if _normalize_column_name(c) == _normalize_column_name(candidate)]
        if len(hits) > 1:
            raise RuntimeError(f'Ambiguous CFTC field: {candidate}')
        if hits:
            return hits[0]
    return None


def _audit_columns(df: pd.DataFrame):
    """Identify fields conservatively; ambiguous/unknown schemas fail clearly."""
    asset = _find_column(df, ['Market_and_Exchange_Names', 'Market_and_Exchange_Name', 'Market_Name'])
    code = _find_column(df, ['CFTC_Contract_Market_Code', 'Contract_Market_Code'])
    date_cols = []
    for c in df.columns:
        name = _normalize_column_name(c)
        if name.startswith(('reportdate', 'asofdate')):
            date_cols.append(c)
    # Prefer full dates, using compact dates to recover blank/unparseable cells.
    date_cols.sort(key=lambda c: ('yymmdd' in _normalize_column_name(c), str(c)))
    if asset is None or not date_cols:
        raise RuntimeError(f'Cannot identify market/report-date fields. Columns: {list(df.columns)}')
    return asset, code, date_cols


def _parse_cot_dates(series: pd.Series) -> pd.Series:
    """Parse mixed CFTC dates explicitly, avoiding integer-as-nanosecond errors.

    Six-digit YYMMDD is interpreted as 20YYMMDD for these post-2006 datasets,
    avoiding Python's 1969 pivot. Full four-digit-year fields are preferred.
    """
    raw = series.fillna('').astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    result = pd.Series(pd.NaT, index=series.index, dtype='datetime64[ns]')
    compact = raw.str.fullmatch(r'\d{6}')
    result.loc[compact] = pd.to_datetime('20' + raw.loc[compact], format='%Y%m%d', errors='coerce')
    for fmt in ('%Y%m%d', '%Y-%m-%d', '%m/%d/%Y', '%Y/%m/%d', '%m-%d-%Y', '%Y-%m-%d %H:%M:%S'):
        mask = result.isna()
        result.loc[mask] = pd.to_datetime(raw.loc[mask], format=fmt, errors='coerce')
    # Excel serial dates only within a plausible range for COT's modern era.
    serial = result.isna() & raw.str.fullmatch(r'\d{5}')
    if serial.any():
        numbers = pd.to_numeric(raw.loc[serial], errors='coerce')
        numbers = numbers.where(numbers.between(38718, 100000))
        result.loc[serial] = pd.to_datetime(numbers, unit='D', origin='1899-12-30', errors='coerce')
    return result.dt.normalize()


def _report_dates(df: pd.DataFrame) -> pd.Series:
    _, _, columns = _audit_columns(df)
    result = pd.Series(pd.NaT, index=df.index, dtype='datetime64[ns]')
    for col in columns:
        result = result.fillna(_parse_cot_dates(df[col]))
    return result


def _find_long_gaps(dates, threshold_days: int = 10) -> List[str]:
    """Report observed intervals strictly greater than the threshold, not guessed dates."""
    ordered = sorted(set(dates))
    return [f'{a.date()} -> {b.date()} ({(b-a).days} days)'
            for a, b in zip(ordered, ordered[1:]) if (b-a).days > threshold_days]


def build_data_quality_audit(df: pd.DataFrame, stale_after_days: int = 10,
                             inactive_after_reports: int = 3,
                             today=None) -> tuple[pd.DataFrame, dict]:
    """Audit without modifying raw rows; retain invalid rows in counts and flags.

    Stable contract codes group renamed markets together; missing codes fall back
    to market names. Duplicate_Date_Rows counts excess valid rows per date (N-1).
    Coverage uses unique valid dates / source dates between the contract's first
    and last observations. Later source reports are measured as lag instead of
    being mislabeled as missing dates. A lag of ``inactive_after_reports`` or more
    is marked POSSIBLY_INACTIVE_OR_DISCONTINUED because annual CFTC files retain
    markets that may legitimately cease reporting. Source_Status independently
    exposes stale source data even when a contract also lags. Negative ages are
    avoided by excluding and flagging future observations. A contract with no valid
    dates remains visible as NO_VALID_DATES with unknown coverage/lag.
    """
    if df.empty:
        raise RuntimeError('Cannot audit an empty dataset.')
    if stale_after_days < 0:
        raise ValueError('stale_after_days must be nonnegative')
    if inactive_after_reports < 1:
        raise ValueError('inactive_after_reports must be at least 1')
    now = pd.Timestamp.now(tz='UTC') if today is None else pd.Timestamp(today)
    if now.tzinfo is not None:
        now = now.tz_convert('UTC').tz_localize(None)
    now = now.normalize()
    asset_col, code_col, _ = _audit_columns(df)
    work = pd.DataFrame(index=range(len(df)))
    work['asset'] = df[asset_col].fillna('').astype(str).str.strip().to_numpy()
    work['code'] = (df[code_col].fillna('').astype(str).str.strip().str.strip('"').str.replace(r'\.0$', '', regex=True).to_numpy()
                    if code_col is not None else '')
    work['date'] = _report_dates(df).to_numpy()
    work['future'] = work['date'] > now
    work['invalid'] = work['date'].isna()
    work.loc[work['future'], 'date'] = pd.NaT
    work['key'] = ['code:' + c if c else 'name:' + a for c, a in zip(work['code'], work['asset'])]
    source_dates = sorted(work['date'].dropna().unique())
    source_dates = [pd.Timestamp(d) for d in source_dates]
    if not source_dates:
        raise RuntimeError('No valid non-future report dates in source; refusing upload.')
    source_latest = source_dates[-1]
    source_age = (now - source_latest).days
    source_status = 'SOURCE_DELAYED' if source_age > stale_after_days else 'CURRENT'
    source_gaps = _find_long_gaps(source_dates)
    rows = []
    iso = lambda d: pd.Timestamp(d).date().isoformat() if pd.notna(d) else ''
    for _, group in work.groupby('key', sort=True, dropna=False):
        ordered = group.sort_values('date', na_position='first')
        names = ordered.loc[ordered['asset'].ne(''), 'asset']
        asset = names.iloc[-1] if len(names) else '(missing market name)'
        code = group['code'].iloc[0]
        dates = sorted(pd.Timestamp(d) for d in group['date'].dropna().unique())
        earliest = dates[0] if dates else pd.NaT
        latest = dates[-1] if dates else pd.NaT
        # Only dates inside the observed lifespan are expected coverage. Source
        # dates after ``latest`` are lag, not proof of a gap: many CFTC markets
        # legitimately stop reporting during an annual file.
        expected = [d for d in source_dates if earliest <= d <= latest] if dates else []
        missing = sorted(set(expected) - set(dates))
        lag = (source_latest - latest).days if dates else ''
        behind = sum(d > latest for d in source_dates) if dates else ''
        gaps = _find_long_gaps(dates)
        duplicate_dates = group['date'].dropna().value_counts()
        duplicate_dates = duplicate_dates[duplicate_dates > 1].sort_index()
        duplicates = int((duplicate_dates - 1).sum())
        invalid = int(group['invalid'].sum())
        future = int(group['future'].sum())
        freshness = ('ASSET_LAGGING' if lag > 0 else source_status) if dates else 'NO_VALID_DATES'
        if not dates:
            reporting_status = 'UNKNOWN_NO_VALID_DATES'
        elif behind == 0:
            reporting_status = 'REPORTED_IN_LATEST_SOURCE'
        elif behind < inactive_after_reports:
            reporting_status = 'RECENTLY_NOT_REPORTED'
        else:
            reporting_status = 'POSSIBLY_INACTIVE_OR_DISCONTINUED'
        flags = []
        for condition, flag in [(bool(missing), 'MISSING_SOURCE_DATES'), (bool(gaps), 'LONG_CALENDAR_GAPS'),
                                (duplicates > 0, 'DUPLICATE_DATES'), (invalid > 0, 'INVALID_DATES'),
                                (future > 0, 'FUTURE_DATES'),
                                (group['asset'].eq('').any(), 'MISSING_ASSET_NAME'),
                                (group['code'].eq('').any(), 'MISSING_CONTRACT_CODE')]:
            if condition:
                flags.append(flag)
        status_parts = [freshness]
        if reporting_status not in {'REPORTED_IN_LATEST_SOURCE', 'UNKNOWN_NO_VALID_DATES'}:
            status_parts.append(reporting_status)
        status_parts.extend(flags)
        rows.append({
            'Asset': asset, 'CFTC_Contract_Market_Code': code,
            'Status': ' | '.join(status_parts), 'Freshness': freshness,
            'Reporting_Status': reporting_status, 'Flags': ' | '.join(flags),
            'Source_Status': source_status,
            'Audit_Date_UTC': iso(now), 'Stale_After_Days': stale_after_days,
            'Earliest_Report_Date': iso(earliest), 'Latest_Report_Date': iso(latest),
            'Source_Earliest_Report_Date': iso(source_dates[0]), 'Source_Latest_Report_Date': iso(source_latest),
            'Age_Days_vs_Today': (now-latest).days if dates else '',
            'Source_Age_Days': source_age, 'Lag_Days_vs_Source': lag, 'Reports_Behind_Source': behind,
            'Rows': len(group), 'Valid_Date_Rows': int(group['date'].notna().sum()),
            'Invalid_Date_Rows': invalid, 'Future_Date_Rows': future,
            'Unique_Report_Dates': len(dates), 'Expected_Source_Dates': len(expected) if dates else '',
            'Coverage_Pct': round(100 * len(dates) / len(expected), 2) if expected else '',
            'Missing_Source_Date_Count': len(missing) if dates else '',
            'Missing_Source_Dates': ', '.join(iso(d) for d in missing),
            'Long_Calendar_Gap_Count': len(gaps), 'Long_Calendar_Gaps': ' | '.join(gaps),
            'Duplicate_Date_Rows': duplicates,
            'Duplicate_Dates': ' | '.join(f'{iso(d)} ({count} rows)' for d, count in duplicate_dates.items()),
            'Source_Report_Date_Count': len(source_dates),
            'Source_Long_Calendar_Gap_Count': len(source_gaps), 'Source_Long_Calendar_Gaps': ' | '.join(source_gaps),
            'Observed_Market_Names': ' | '.join(sorted(set(group['asset']) - {''})),
            'Coverage_Basis': 'Observed source dates between first and last contract observations; selected annual file only',
        })
    audit = pd.DataFrame(rows)
    # Put current/recent markets with integrity problems ahead of likely inactive
    # markets. Invalid/future/duplicate rows always remain highest priority.
    numeric_issues = audit[['Invalid_Date_Rows', 'Future_Date_Rows',
                            'Duplicate_Date_Rows', 'Missing_Source_Date_Count',
                            'Long_Calendar_Gap_Count']].apply(pd.to_numeric, errors='coerce').fillna(0)
    serious_problem = numeric_issues[['Invalid_Date_Rows', 'Future_Date_Rows',
                                      'Duplicate_Date_Rows']].gt(0).any(axis=1)
    gap_problem = numeric_issues[['Missing_Source_Date_Count',
                                  'Long_Calendar_Gap_Count']].gt(0).any(axis=1)
    inactive = audit['Reporting_Status'].eq('POSSIBLY_INACTIVE_OR_DISCONTINUED')
    recent = audit['Reporting_Status'].eq('RECENTLY_NOT_REPORTED')
    priority = pd.Series(5, index=audit.index)
    priority.loc[inactive] = 4
    priority.loc[recent] = 2
    priority.loc[gap_problem & ~inactive] = 1
    priority.loc[serious_problem] = 0
    audit['_priority'] = priority
    audit['_lag_sort'] = pd.to_numeric(audit['Lag_Days_vs_Source'], errors='coerce').fillna(-1)
    audit = audit.sort_values(['_priority', '_lag_sort', 'Asset'], ascending=[True, False, True],
                              kind='stable').drop(columns=['_priority', '_lag_sort']).reset_index(drop=True)
    summary = {
        'source_earliest': iso(source_dates[0]), 'source_latest': iso(source_latest),
        'source_age_days': source_age, 'source_status': source_status,
        'source_report_dates': len(source_dates), 'source_calendar_gaps': source_gaps,
        'asset_count': len(audit), 'row_count': len(df),
        'current_count': int(audit['Freshness'].eq('CURRENT').sum()),
        'source_delayed_count': int(audit['Freshness'].eq('SOURCE_DELAYED').sum()),
        'lagging_count': int(audit['Freshness'].eq('ASSET_LAGGING').sum()),
        'no_valid_dates_count': int(audit['Freshness'].eq('NO_VALID_DATES').sum()),
        'recently_not_reported_count': int(audit['Reporting_Status'].eq('RECENTLY_NOT_REPORTED').sum()),
        'possibly_inactive_count': int(audit['Reporting_Status'].eq('POSSIBLY_INACTIVE_OR_DISCONTINUED').sum()),
        'reported_latest_count': int(audit['Reporting_Status'].eq('REPORTED_IN_LATEST_SOURCE').sum()),
        'clean_count': int(audit['Status'].eq('CURRENT').sum()),
        'assets_with_integrity_flags': int(audit['Flags'].ne('').sum()),
        'assets_with_missing_dates': int(audit['Flags'].str.contains('MISSING_SOURCE_DATES').sum()),
        'assets_with_long_gaps': int(audit['Long_Calendar_Gap_Count'].gt(0).sum()),
        'assets_with_duplicates': int(audit['Duplicate_Date_Rows'].gt(0).sum()),
        'invalid_date_rows': int(work['invalid'].sum()), 'future_date_rows': int(work['future'].sum()),
    }
    return audit, summary


def print_audit_summary(sheet_name: str, audit_df: pd.DataFrame, summary: dict) -> None:
    """Print compact totals and at most ten problem contracts; full detail is in the tab."""
    print(f"\n{sheet_name} audit: {summary['asset_count']} contracts, {summary['row_count']} rows")
    print(f"Source: {summary['source_earliest']} -> {summary['source_latest']}; "
          f"{summary['source_report_dates']} dates; age {summary['source_age_days']} days; {summary['source_status']}")
    print(f"Freshness: current {summary['current_count']} | Source delayed {summary['source_delayed_count']} | "
          f"Lagging {summary['lagging_count']} | No valid dates {summary['no_valid_dates_count']}")
    print(f"Reporting presence: latest source {summary['reported_latest_count']} | recent absence {summary['recently_not_reported_count']} | "
          f"possibly inactive/discontinued {summary['possibly_inactive_count']}")
    print(f"Data quality: fully clean {summary['clean_count']} | flagged {summary['assets_with_integrity_flags']} | "
          f"internal missing dates {summary['assets_with_missing_dates']} | "
          f"Long gaps {summary['assets_with_long_gaps']} | Duplicates {summary['assets_with_duplicates']} | "
          f"Invalid/future rows {summary['invalid_date_rows']}/{summary['future_date_rows']}")
    print(f"Source calendar gaps >10 days: {len(summary['source_calendar_gaps'])}")
    for gap in summary['source_calendar_gaps'][:5]:
        print('  ' + gap)
    problems = audit_df.loc[audit_df['Status'].ne('CURRENT')]
    if not problems.empty:
        print(problems[['Asset', 'Status', 'Latest_Report_Date', 'Reports_Behind_Source']].head(10).to_string(index=False))
        print(f"{len(problems)} contracts flagged; full details in {sheet_name}_Audit.")
    print('Coverage/gaps: between each contract\'s first and last observations in the annual file.')
    print('Later source reports are lag; 3+ missed reports may indicate an inactive/discontinued market.')


def to_sheet_values(df: pd.DataFrame) -> List[List[str]]:
    """Convert DataFrame into Google Sheets row matrix (header + data rows)."""
    header = [str(col) if col is not None else "" for col in df.columns.tolist()]

    rows: List[List[str]] = [header]
    for row in df.itertuples(index=False, name=None):
        rows.append(["" if val is None else str(val) for val in row])

    return rows


def chunk_rows(rows: List[List[str]], chunk_size: int = 1000) -> Iterable[List[List[str]]]:
    """Yield row chunks to keep API payload sizes manageable."""
    for i in range(0, len(rows), chunk_size):
        yield rows[i : i + chunk_size]


def _build_google_sheets_service(service_account_path: str):
    """Authenticate once so one run can reuse the same Sheets API connection."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(service_account_path, scopes=SHEETS_SCOPE)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_current_year() -> int:
    """Return the UTC calendar year so rollover never requires a code edit."""
    return datetime.now(timezone.utc).year


def _execute(request):
    """Execute a Google API request with bounded client-library retries."""
    return request.execute(num_retries=3)


def _quote_sheet(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def _column_letter(number: int) -> str:
    """Convert a one-based column number to A1 notation."""
    if number < 1:
        raise ValueError("Column number must be positive")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_metadata(service, spreadsheet_id: str) -> Dict[str, SheetInfo]:
    metadata = _execute(
        service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))",
        )
    )
    result = {}
    for sheet in metadata.get("sheets", []):
        props = sheet["properties"]
        grid = props.get("gridProperties", {})
        result[props["title"]] = SheetInfo(
            title=props["title"],
            sheet_id=props["sheetId"],
            row_count=grid.get("rowCount", 0),
            column_count=grid.get("columnCount", 0),
        )
    return result


def _projected_grid_cells(sheets: Dict[str, SheetInfo],
                          datasets: Dict[int, YearDataset]) -> Tuple[int, int]:
    """Return current/projected cells after compact raw-tab reconciliation.

    Raw tabs are deliberately resized to their authoritative row count and the
    six-column staging schema. This includes the cells released by replacing
    legacy 191-column raw grids.
    """
    current = sum(sheet.row_count * sheet.column_count for sheet in sheets.values())
    projected = current
    for year, dataset in datasets.items():
        name = f"COT {year}"
        if name not in sheets:
            continue
        sheet = sheets[name]
        existing = sheet.row_count * sheet.column_count
        if list(dataset.dataframe.columns) != COMPACT_RAW_HEADERS:
            raise RuntimeError(f"{name}: preflight received a non-compact raw dataset")
        required = max(1, len(dataset.dataframe) + 1) * len(COMPACT_RAW_HEADERS)
        projected += required - existing
    return current, projected


def _raw_sheet_name(sheets: Dict[str, SheetInfo], year: int) -> str | None:
    """Find an explicitly prepared annual raw tab without inventing one."""
    for candidate in (f"COT {year}", f"COT{year}"):
        if candidate in sheets:
            return candidate
    return None


def _projected_tff_grid_cells(sheets: Dict[str, SheetInfo],
                              datasets: Dict[int, TFFYearDataset]
                              ) -> Tuple[int, int]:
    """Project one TFF workbook using the compact eight-column raw schema."""
    current = sum(sheet.row_count * sheet.column_count for sheet in sheets.values())
    projected = current
    for year, dataset in datasets.items():
        name = _raw_sheet_name(sheets, year)
        if name is None:
            continue
        sheet = sheets[name]
        required = max(1, len(dataset.dataframe) + 1) * len(TFF_COMPACT_RAW_HEADERS)
        projected += required - sheet.row_count * sheet.column_count
    return current, projected


def _get_values(service, spreadsheet_id: str, a1_range: str) -> List[List[Any]]:
    response = _execute(
        service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=a1_range,
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER",
        )
    )
    return response.get("values", [])


def _batch_get_values(service, spreadsheet_id: str,
                      ranges: Sequence[str]) -> Dict[str, List[List[Any]]]:
    if not ranges:
        return {}
    response = _execute(
        service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=list(ranges),
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER",
        )
    )
    return {
        requested: returned.get("values", [])
        for requested, returned in zip(ranges, response.get("valueRanges", []))
    }


def _batch_update_values(service, spreadsheet_id: str,
                         updates: Sequence[Dict[str, Any]], dry_run: bool) -> int:
    """Write value ranges in bounded batches; return the number of API mutations."""
    if dry_run or not updates:
        return 0
    calls = 0
    for batch in chunk_rows(list(updates), chunk_size=200):
        _execute(
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": batch},
            )
        )
        calls += 1
    return calls


def _batch_clear_values(service, spreadsheet_id: str,
                        ranges: Sequence[str], dry_run: bool) -> int:
    if dry_run or not ranges:
        return 0
    calls = 0
    for batch in chunk_rows(list(ranges), chunk_size=500):
        _execute(
            service.spreadsheets().values().batchClear(
                spreadsheetId=spreadsheet_id,
                body={"ranges": batch},
            )
        )
        calls += 1
    return calls


def _ensure_grid_size(service, spreadsheet_id: str, sheet: SheetInfo,
                      rows: int, columns: int, dry_run: bool) -> int:
    if rows <= sheet.row_count and columns <= sheet.column_count:
        return 0
    new_rows = max(rows, sheet.row_count, 1000)
    new_cols = max(columns, sheet.column_count, 26)
    if not dry_run:
        _execute(
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet.sheet_id,
                            "gridProperties": {
                                "rowCount": new_rows,
                                "columnCount": new_cols,
                            },
                        },
                        "fields": "gridProperties(rowCount,columnCount)",
                    }
                }]},
            )
        )
    sheet.row_count = new_rows
    sheet.column_count = new_cols
    return 0 if dry_run else 1


def _resize_compact_raw_grid(service, spreadsheet_id: str, sheet: SheetInfo,
                             rows: int, dry_run: bool) -> int:
    """Set a raw staging tab to its used row count and exactly six columns.

    Call this only after compact values have been written successfully. A failed
    resize then leaves a recoverable wide tab whose first six columns already
    contain the canonical dataset.
    """
    rows = max(1, rows)
    columns = len(COMPACT_RAW_HEADERS)
    if rows == sheet.row_count and columns == sheet.column_count:
        return 0
    if not dry_run:
        _execute(
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet.sheet_id,
                            "gridProperties": {
                                "rowCount": rows,
                                "columnCount": columns,
                            },
                        },
                        "fields": "gridProperties(rowCount,columnCount)",
                    }
                }]},
            )
        )
        sheet.row_count = rows
        sheet.column_count = columns
    return 0 if dry_run else 1


def _format_raw_code_column_as_text(service, spreadsheet_id: str, sheet: SheetInfo,
                                    headers: Sequence[str], rows: int,
                                    dry_run: bool) -> int:
    """Protect canonical contract codes such as 067651 from numeric coercion."""
    lookup = {_normalize_column_name(header): index for index, header in enumerate(headers)}
    code_index = lookup.get(_normalize_column_name(RAW_CONTRACT_HEADER))
    if code_index is None:
        raise RuntimeError(f"{sheet.title}: compact raw header lacks contract code")
    if dry_run:
        return 0
    _execute(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": sheet.sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": max(1, rows),
                        "startColumnIndex": code_index,
                        "endColumnIndex": code_index + 1,
                    },
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "TEXT", "pattern": "@"}
                    }},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }]},
        )
    )
    return 1


def _format_data_rows(service, spreadsheet_id: str, sheet: SheetInfo,
                      start_row: int, end_row: int, summary_width: int | None,
                      dry_run: bool) -> int:
    """Format only newly written/rebuilt rows; row numbers are one-based inclusive."""
    if dry_run or end_row < start_row:
        return 0
    requests_body = [{
        "repeatCell": {
            "range": {
                "sheetId": sheet.sheet_id,
                "startRowIndex": start_row - 1,
                "endRowIndex": end_row,
                "startColumnIndex": 0,
                "endColumnIndex": 1,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "DATE", "pattern": "yyyy-mm-dd"
            }}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }]
    if summary_width is None:
        requests_body.extend([
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet.sheet_id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": 1,
                        "endColumnIndex": 4,
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {
                        "type": "NUMBER", "pattern": "#,##0"
                    }}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet.sheet_id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": 4,
                        "endColumnIndex": 5,
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {
                        "type": "PERCENT", "pattern": "0.00%"
                    }}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
        ])
    elif summary_width > 1:
        requests_body.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet.sheet_id,
                    "startRowIndex": start_row - 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": 1,
                    "endColumnIndex": summary_width,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {
                    "type": "PERCENT", "pattern": "0.00%"
                }}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    _execute(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests_body},
        )
    )
    return 1


def _normalise_header(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def _required_disagg_columns(df: pd.DataFrame) -> Dict[str, str | None]:
    """Find required Disaggregated fields by resilient, exact-normalised headers."""
    fields = {
        "market": _find_column(df, [
            "Market_and_Exchange_Names", "Market and Exchange Names", "Market_Name"
        ]),
        "date": _find_column(df, [
            "Report_Date_as_YYYY-MM-DD", "Report_Date_as_MM_DD_YYYY",
            "As_of_Date_In_Form_MM/DD/YYYY", "As_of_Date_In_Form_YYMMDD",
        ]),
        "contract": _find_column(df, [
            "CFTC_Contract_Market_Code", "CFTC Contract Market Code"
        ]),
        "open_interest": _find_column(df, ["Open_Interest_All", "Open Interest All"]),
        "manager_long": _find_column(df, [
            "M_Money_Positions_Long_All", "Managed_Money_Positions_Long_All"
        ]),
        "manager_short": _find_column(df, [
            "M_Money_Positions_Short_All", "Managed_Money_Positions_Short_All"
        ]),
    }
    missing = [name for name in ("market", "date", "contract", "open_interest",
                                 "manager_long", "manager_short")
               if fields[name] is None]
    if missing:
        raise RuntimeError(
            f"Missing required Disaggregated header(s): {', '.join(missing)}. "
            f"Available columns: {list(df.columns)}"
        )
    return fields


def _numeric_series(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str).str.strip().str.replace(",", "", regex=False)
    return pd.to_numeric(text, errors="coerce")


def _compact_disagg_dataframe(df: pd.DataFrame, fields: Dict[str, str | None],
                              dates: pd.Series) -> pd.DataFrame:
    """Reduce a validated full CFTC table to the canonical six raw fields."""
    market = df[fields["market"]].fillna("").astype(str).str.strip()
    if fields["contract"] is None:
        contract = pd.Series("", index=df.index, dtype="object")
    else:
        # Canonical strings preserve/restore leading zeroes and letters.
        contract = df[fields["contract"]].map(normalize_cftc_contract_code)

    numeric = {}
    for field_name, output_header in (
        ("open_interest", RAW_OPEN_INTEREST_HEADER),
        ("manager_long", RAW_MANAGER_LONG_HEADER),
        ("manager_short", RAW_MANAGER_SHORT_HEADER),
    ):
        values = _numeric_series(df[fields[field_name]])
        if values.isna().any():
            raise RuntimeError(f"Cannot compact nonnumeric {fields[field_name]} values")
        # Position fields are integer counts in the authoritative report. Keep
        # a float only if CFTC ever publishes a genuine fractional value.
        numeric[output_header] = values.map(
            lambda value: int(value) if float(value).is_integer() else float(value)
        )

    compact = pd.DataFrame({
        RAW_MARKET_HEADER: market,
        RAW_CONTRACT_HEADER: contract,
        RAW_DATE_HEADER: dates.dt.strftime("%Y-%m-%d"),
        RAW_OPEN_INTEREST_HEADER: numeric[RAW_OPEN_INTEREST_HEADER],
        RAW_MANAGER_LONG_HEADER: numeric[RAW_MANAGER_LONG_HEADER],
        RAW_MANAGER_SHORT_HEADER: numeric[RAW_MANAGER_SHORT_HEADER],
    })
    return compact[COMPACT_RAW_HEADERS].reset_index(drop=True)


def validate_source_dataset(df: pd.DataFrame, year: int, url: str) -> YearDataset:
    """Validate the complete CFTC table, then retain six required raw fields."""
    if len(df) < 50:
        raise RuntimeError(f"{year}: implausibly small source ({len(df)} rows): {url}")
    clean = df.copy()
    clean.columns = [_normalise_header(column) for column in clean.columns]
    normalised_headers = [_normalize_column_name(column) for column in clean.columns]
    if len(set(normalised_headers)) != len(normalised_headers):
        raise RuntimeError(f"{year}: duplicate normalised headers in {url}")
    fields = _required_disagg_columns(clean)
    dates = _parse_cot_dates(clean[fields["date"]])
    invalid_dates = int(dates.isna().sum())
    wrong_year = int((dates.dropna().dt.year != year).sum())
    if invalid_dates or wrong_year:
        raise RuntimeError(
            f"{year}: date validation failed in {url}; "
            f"invalid={invalid_dates}, wrong_year={wrong_year}"
        )
    minimum_dates = 1 if year == get_current_year() else 2
    if dates.nunique() < minimum_dates:
        raise RuntimeError(f"{year}: fewer than {minimum_dates} report dates in {url}")
    blank_markets = clean[fields["market"]].fillna("").astype(str).str.strip().eq("")
    if blank_markets.any():
        raise RuntimeError(f"{year}: {int(blank_markets.sum())} blank market names in {url}")
    for label in ("open_interest", "manager_long", "manager_short"):
        invalid = _numeric_series(clean[fields[label]]).isna()
        if invalid.any():
            raise RuntimeError(
                f"{year}: {int(invalid.sum())} nonnumeric {fields[label]} values in {url}"
            )
    compact = _compact_disagg_dataframe(clean, fields, dates)
    return YearDataset(
        year=year,
        url=url,
        dataframe=compact,
        market_col=RAW_MARKET_HEADER,
        date_col=RAW_DATE_HEADER,
        contract_col=RAW_CONTRACT_HEADER,
        open_interest_col=RAW_OPEN_INTEREST_HEADER,
        manager_long_col=RAW_MANAGER_LONG_HEADER,
        manager_short_col=RAW_MANAGER_SHORT_HEADER,
        dates=pd.to_datetime(compact[RAW_DATE_HEADER]),
        source_column_count=len(clean.columns),
    )


def download_cot_year(year: int) -> YearDataset:
    link = fetch_section_report_link(DISAGG_SECTION_LABEL, year)
    dataframe = download_report_to_dataframe(link.url)
    return validate_source_dataset(dataframe, year, link.url)


def build_tff_futures_only_url(year: int) -> str:
    """Return the canonical annual TFF Futures Only text ZIP URL."""
    if not isinstance(year, int) or year < TFF_START_YEAR:
        raise ValueError(f"TFF year must be an integer >= {TFF_START_YEAR}: {year!r}")
    return f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"


def _required_tff_columns(df: pd.DataFrame) -> Dict[str, str]:
    """Locate TFF fields by normalized header name, never physical position."""
    fields = {
        "market": _find_column(df, [
            "Market_and_Exchange_Names", "Market and Exchange Names", "Market_Name"
        ]),
        "date": _find_column(df, [
            "Report_Date_as_YYYY-MM-DD", "Report_Date_as_MM_DD_YYYY",
            "As_of_Date_In_Form_MM/DD/YYYY", "As_of_Date_In_Form_YYMMDD",
        ]),
        "contract": _find_column(df, [
            "CFTC_Contract_Market_Code", "CFTC Contract Market Code"
        ]),
        "open_interest": _find_column(df, ["Open_Interest_All", "Open Interest All"]),
        "asset_long": _find_column(df, [
            "Asset_Mgr_Positions_Long_All", "Asset Manager Positions Long All"
        ]),
        "asset_short": _find_column(df, [
            "Asset_Mgr_Positions_Short_All", "Asset Manager Positions Short All"
        ]),
        "lev_long": _find_column(df, [
            "Lev_Money_Positions_Long_All", "Leveraged Funds Positions Long All",
            "Leveraged_Funds_Positions_Long_All"
        ]),
        "lev_short": _find_column(df, [
            "Lev_Money_Positions_Short_All", "Leveraged Funds Positions Short All",
            "Leveraged_Funds_Positions_Short_All"
        ]),
    }
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        raise RuntimeError(
            f"Missing required TFF header(s): {', '.join(missing)}. "
            f"Available columns: {list(df.columns)}"
        )
    return fields  # type: ignore[return-value]


def _compact_tff_dataframe(df: pd.DataFrame, fields: Dict[str, str],
                           dates: pd.Series) -> pd.DataFrame:
    """Reduce a complete TFF source frame to the compact eight-field schema."""
    compact = pd.DataFrame({
        RAW_MARKET_HEADER: df[fields["market"]].fillna("").astype(str).str.strip(),
        RAW_CONTRACT_HEADER: df[fields["contract"]].map(normalize_cftc_contract_code),
        RAW_DATE_HEADER: dates.dt.strftime("%Y-%m-%d"),
    })
    for source_key, output_header in (
        ("open_interest", RAW_OPEN_INTEREST_HEADER),
        ("asset_long", TFF_RAW_ASSET_LONG_HEADER),
        ("asset_short", TFF_RAW_ASSET_SHORT_HEADER),
        ("lev_long", TFF_RAW_LEV_LONG_HEADER),
        ("lev_short", TFF_RAW_LEV_SHORT_HEADER),
    ):
        values = _numeric_series(df[fields[source_key]])
        if values.isna().any():
            raise RuntimeError(f"Cannot compact nonnumeric TFF field {fields[source_key]}")
        compact[output_header] = values.map(
            lambda value: int(value) if float(value).is_integer() else float(value)
        )
    return compact[TFF_COMPACT_RAW_HEADERS].reset_index(drop=True)


def validate_tff_source_dataset(df: pd.DataFrame, year: int, url: str) -> TFFYearDataset:
    """Validate a complete TFF annual table and retain only eight TFF fields."""
    if len(df) < 50:
        raise RuntimeError(f"TFF {year}: implausibly small source ({len(df)} rows): {url}")
    clean = df.copy()
    clean.columns = [_normalise_header(column) for column in clean.columns]
    normalized_headers = [_normalize_column_name(column) for column in clean.columns]
    if len(set(normalized_headers)) != len(normalized_headers):
        raise RuntimeError(f"TFF {year}: duplicate normalised headers in {url}")
    fields = _required_tff_columns(clean)
    dates = _parse_cot_dates(clean[fields["date"]])
    invalid_dates = int(dates.isna().sum())
    wrong_year = int((dates.dropna().dt.year != year).sum())
    if invalid_dates or wrong_year:
        raise RuntimeError(
            f"TFF {year}: date validation failed in {url}; "
            f"invalid={invalid_dates}, wrong_year={wrong_year}"
        )
    minimum_dates = 1 if year in (2010, get_current_year()) else 2
    if dates.nunique() < minimum_dates:
        raise RuntimeError(f"TFF {year}: fewer than {minimum_dates} report dates in {url}")
    if clean[fields["market"]].fillna("").astype(str).str.strip().eq("").any():
        raise RuntimeError(f"TFF {year}: blank market names in {url}")
    for key in ("open_interest", "asset_long", "asset_short", "lev_long", "lev_short"):
        if _numeric_series(clean[fields[key]]).isna().any():
            raise RuntimeError(f"TFF {year}: nonnumeric {fields[key]} values in {url}")
    compact = _compact_tff_dataframe(clean, fields, dates)
    return TFFYearDataset(
        year=year, url=url, dataframe=compact,
        market_col=RAW_MARKET_HEADER, date_col=RAW_DATE_HEADER,
        contract_col=RAW_CONTRACT_HEADER, open_interest_col=RAW_OPEN_INTEREST_HEADER,
        asset_long_col=TFF_RAW_ASSET_LONG_HEADER, asset_short_col=TFF_RAW_ASSET_SHORT_HEADER,
        lev_long_col=TFF_RAW_LEV_LONG_HEADER, lev_short_col=TFF_RAW_LEV_SHORT_HEADER,
        dates=pd.to_datetime(compact[RAW_DATE_HEADER]),
        source_column_count=len(clean.columns),
    )


def download_tff_year(year: int) -> TFFYearDataset:
    """Download one exact TFF Futures Only annual source, never another family."""
    url = build_tff_futures_only_url(year)
    # Use the same bounded HTTP/session and ZIP parser as commodities. The
    # canonical URL is authoritative; the index is only a diagnostic check and
    # must never redirect to combined/options/legacy reports.
    dataframe = download_report_to_dataframe(url)
    return validate_tff_source_dataset(dataframe, year, url)


def _sheet_scalar(value: Any) -> Any:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if hasattr(value, "item") and not isinstance(value, str):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    return value.strip() if isinstance(value, str) else value


def normalize_market_name(value: Any) -> str:
    """Normalise market-name whitespace for diagnostics only, never matching."""
    scalar = _sheet_scalar(value)
    if scalar == "":
        return ""
    return " ".join(str(scalar).replace("\xa0", " ").split())


def normalize_cftc_contract_code(value: Any) -> str:
    """Return a string-safe canonical CFTC Contract Market Code.

    Numeric-looking values returned by Sheets/Excel are restored to six digits;
    alphanumeric identifiers are uppercased but never numerically coerced.
    """
    scalar = _sheet_scalar(value)
    if scalar == "":
        return ""
    if isinstance(scalar, bool):
        return str(scalar).upper()
    if isinstance(scalar, int):
        text = str(scalar)
    elif isinstance(scalar, float):
        text = str(int(scalar)) if scalar.is_integer() else str(scalar)
    else:
        text = str(scalar).strip().upper().strip('"')
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


def parse_contract_code_config(commodity: str,
                               values: Sequence[Sequence[Any]]) -> ContractCodeConfig:
    """Read G1 only; G2 and lower cells are intentionally ignored."""
    value = values[0][0] if values and values[0] else ""
    raw_value = "" if _sheet_scalar(value) == "" else str(_sheet_scalar(value)).strip()
    code = normalize_cftc_contract_code(value)
    valid = not code or bool(re.fullmatch(r"[A-Z0-9]{6}", code))
    return ContractCodeConfig(commodity, raw_value, code, valid)


def parse_tff_contract_code_config(instrument: str,
                                   values: Sequence[Sequence[Any]]) -> ContractCodeConfig:
    """Read TFF G1 as text, allowing legitimate symbols such as ``13874+``."""
    value = values[0][0] if values and values[0] else ""
    raw_value = "" if _sheet_scalar(value) == "" else str(_sheet_scalar(value)).strip()
    code = normalize_cftc_contract_code(value)
    # CFTC TFF identifiers are strings. Permit letters, digits and the symbols
    # observed in the archive, while rejecting names, whitespace and formulas.
    valid = not code or bool(re.fullmatch(r"[A-Z0-9][A-Z0-9+._-]*", code))
    return ContractCodeConfig(instrument, raw_value, code, valid)


def contract_code_preflight(configs: Dict[str, ContractCodeConfig]):
    """Return invalid G1 configurations and codes shared by multiple sheets."""
    invalid = {name: config for name, config in configs.items()
               if config.code and not config.valid}
    owners: Dict[str, List[str]] = {}
    for commodity, config in configs.items():
        if config.code and config.valid:
            owners.setdefault(config.code, []).append(commodity)
    collisions = {code: commodities for code, commodities in owners.items()
                  if len(commodities) > 1}
    return invalid, collisions


def _canonical_scalar(value: Any) -> str:
    value = _sheet_scalar(value)
    if value == "":
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except InvalidOperation:
            return str(value)
    text = str(value).strip()
    numeric = text.replace(",", "")
    try:
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", numeric):
            return format(Decimal(numeric).normalize(), "f")
    except InvalidOperation:
        pass
    return text


def _normalised_row(row: Sequence[Any], width: int) -> Tuple[str, ...]:
    padded = list(row) + [""] * max(0, width - len(row))
    return tuple(_canonical_scalar(value) for value in padded[:width])


def _parse_any_date(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 30000 <= float(value) <= 100000:
            parsed = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(value), unit="D")
            return parsed.normalize()
    text = str(value).strip()
    # Raw staging values are canonical ISO dates. Avoid constructing a Series
    # and trying every historical CFTC format for each of ~170,000 rows.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return pd.Timestamp(datetime.strptime(text, "%Y-%m-%d")).normalize()
        except ValueError:
            return None
    parsed = _parse_cot_dates(pd.Series([value])).iloc[0]
    return None if pd.isna(parsed) else pd.Timestamp(parsed).normalize()


def _date_serial(value: Any) -> int:
    date = pd.Timestamp(value).normalize()
    return int((date - pd.Timestamp("1899-12-30")).days)


def _raw_key_columns(headers: Sequence[str]) -> Tuple[int, int, int | None]:
    lookup = {_normalize_column_name(header): index for index, header in enumerate(headers)}

    def find(names: Sequence[str]) -> int | None:
        for name in names:
            if _normalize_column_name(name) in lookup:
                return lookup[_normalize_column_name(name)]
        return None

    market = find(["Market_and_Exchange_Names", "Market and Exchange Names", "Market_Name"])
    date = find(["Report_Date_as_YYYY-MM-DD", "Report_Date_as_MM_DD_YYYY",
                 "As_of_Date_In_Form_MM/DD/YYYY", "As_of_Date_In_Form_YYMMDD"])
    contract = find(["CFTC_Contract_Market_Code", "CFTC Contract Market Code"])
    if market is None or date is None:
        raise RuntimeError("Raw sheet lacks market/date key headers")
    return market, date, contract


def _normalised_market_key(value: Any) -> str:
    """Normalise only surrounding/repeated whitespace for the name fallback."""
    return normalize_market_name(value)


def _normalised_contract_key(value: Any) -> str:
    """Normalise raw-record contract keys without losing leading zeroes."""
    return normalize_cftc_contract_code(value)


def _keyed_rows(headers: Sequence[str], rows: Sequence[Sequence[Any]],
                label: str) -> Tuple[Dict[Tuple[str, str], Tuple[int, List[Any], Tuple[str, ...]]], List[int]]:
    """Index raw rows by contract code/date, with a market/date fallback.

    The exact market name remains in the canonical row and therefore still
    participates in correction detection even though a present contract code is
    the preferred stable identifier.
    """
    market_col, date_col, contract_col = _raw_key_columns(headers)
    width = len(headers)
    records = {}
    duplicate_rows = []
    for offset, source_row in enumerate(rows, start=2):
        padded = list(source_row) + [""] * max(0, width - len(source_row))
        padded = [_sheet_scalar(value) for value in padded[:width]]
        if not any(_canonical_scalar(value) for value in padded):
            continue
        date = _parse_any_date(padded[date_col])
        if date is None:
            raise RuntimeError(f"{label}: invalid report date at row {offset}")
        market = _normalised_market_key(padded[market_col])
        contract = _normalised_contract_key(padded[contract_col]) if contract_col is not None else ""
        identifier = f"code:{contract}" if contract else f"market:{market}"
        key = (identifier, date.date().isoformat())
        canonical_values = list(_normalised_row(padded, width))
        if contract_col is not None:
            # Preserve the exact stored identifier representation for correction
            # detection: numeric 67651 must be rewritten once as text 067651.
            canonical_values[contract_col] = str(_sheet_scalar(padded[contract_col])).strip().upper()
        canonical = tuple(canonical_values)
        if key in records:
            if records[key][2] != canonical:
                raise RuntimeError(f"{label}: conflicting duplicate stable key {key}")
            duplicate_rows.append(offset)
            continue
        records[key] = (offset, padded, canonical)
    return records, duplicate_rows


def _dataset_signature(records: Dict[Tuple[str, str], Tuple[int, List[Any], Tuple[str, ...]]]) -> str:
    payload = [[list(key), list(value[2])] for key, value in sorted(records.items())]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact_existing_raw_values(values: Sequence[Sequence[Any]],
                                 label: str) -> Tuple[List[List[Any]], bool]:
    """Read either a legacy wide raw tab or an already compact raw tab.

    A prior run may have written canonical A:F values but failed before shrinking
    legacy columns G onward. Recognising the canonical first six fields makes
    that state safely recoverable on the next run.
    """
    raw_headers = [_normalise_header(value) for value in values[0]]
    while raw_headers and raw_headers[-1] == "":
        raw_headers.pop()
    canonical_normalised = [_normalize_column_name(value) for value in COMPACT_RAW_HEADERS]
    first_six = [_normalize_column_name(value)
                 for value in raw_headers[:len(COMPACT_RAW_HEADERS)]]
    schema_exact = raw_headers == COMPACT_RAW_HEADERS
    if first_six == canonical_normalised:
        return [list(row[:len(COMPACT_RAW_HEADERS)]) for row in values[1:]], schema_exact

    width = len(raw_headers)
    rows = []
    for row in values[1:]:
        padded = list(row) + [""] * max(0, width - len(row))
        padded = padded[:width]
        if any(_canonical_scalar(value) for value in padded):
            rows.append(padded)
    if not rows:
        return [], False
    normalised = [_normalize_column_name(header) for header in raw_headers]
    if len(set(normalised)) != len(normalised):
        raise RuntimeError(f"{label}: legacy raw headers are ambiguous")
    frame = pd.DataFrame(rows, columns=raw_headers)
    fields = _required_disagg_columns(frame)
    dates = _parse_cot_dates(frame[fields["date"]])
    if dates.isna().any():
        raise RuntimeError(f"{label}: legacy raw tab contains invalid report dates")
    compact = _compact_disagg_dataframe(frame, fields, dates)
    return [[_sheet_scalar(value) for value in row]
            for row in compact.itertuples(index=False, name=None)], False


def _full_raw_updates(sheet_name: str, rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Build bounded A:F writes for a complete compact raw table."""
    updates = []
    for offset in range(0, len(rows), 250):
        updates.append({
            "range": f"{_quote_sheet(sheet_name)}!A{offset + 1}",
            "majorDimension": "ROWS",
            "values": [list(row) for row in rows[offset:offset + 250]],
        })
    return updates


def _changed_segments(old_row: Sequence[Any], new_row: Sequence[Any],
                      text_columns: set[int] | None = None) -> List[Tuple[int, int]]:
    """Return inclusive zero-based runs of changed columns."""
    text_columns = text_columns or set()
    changed = []
    for index, (old, new) in enumerate(zip(old_row, new_row)):
        if index in text_columns:
            unequal = str(_sheet_scalar(old)).strip().upper() != str(_sheet_scalar(new)).strip().upper()
        else:
            unequal = _canonical_scalar(old) != _canonical_scalar(new)
        if unequal:
            changed.append(index)
    if not changed:
        return []
    segments = []
    start = previous = changed[0]
    for index in changed[1:]:
        if index != previous + 1:
            segments.append((start, previous))
            start = index
        previous = index
    segments.append((start, previous))
    return segments


def _source_comparison_safe(source_records, existing_records, year: int) -> Tuple[bool, str]:
    """Protect all writes when the new source looks truncated relative to stored data."""
    if not existing_records:
        return True, ""
    source_count = len(source_records)
    existing_count = len(existing_records)
    if source_count < max(50, math.floor(existing_count * 0.80)):
        return False, f"source has {source_count} rows versus {existing_count} stored rows"
    source_dates = [pd.Timestamp(key[1]) for key in source_records]
    existing_dates = [pd.Timestamp(key[1]) for key in existing_records]
    if max(source_dates) < max(existing_dates):
        return False, "source ends before the newest stored report date"
    if year < get_current_year() and min(source_dates) > min(existing_dates):
        return False, "completed-year source begins after stored history"
    return True, ""


def _deletions_safe(source_records, existing_records, year: int) -> Tuple[bool, str]:
    safe, reason = _source_comparison_safe(source_records, existing_records, year)
    if not safe:
        return safe, reason
    if len(source_records) < math.floor(len(existing_records) * 0.95):
        return False, "more than 5% of stored rows would disappear"
    if year == get_current_year():
        latest = max(pd.Timestamp(key[1]) for key in source_records)
        if (pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - latest).days > 28:
            return False, "current-year source is more than 28 days old"
    return True, ""


def sync_raw_cot_sheet(service, spreadsheet_id: str, sheet: SheetInfo,
                       dataset: YearDataset, dry_run: bool,
                       existing_values: Sequence[Sequence[Any]] | None = None) -> SyncResult:
    """Reconcile one annual raw tab using the compact six-column schema.

    Existing legacy wide tabs are compared through a compact in-memory view,
    safety-checked, rewritten once, and then resized. Already compact tabs keep
    the normal record-level incremental update path.
    """
    name = sheet.title
    result = SyncResult(name=name, target=len(dataset.dataframe))
    if existing_values is None:
        existing_values = _get_values(service, spreadsheet_id, _quote_sheet(name))
    source_headers = [_normalise_header(column) for column in dataset.dataframe.columns]
    if source_headers != COMPACT_RAW_HEADERS:
        raise RuntimeError(f"{name}: authoritative dataset is not in the compact raw schema")
    source_rows = [[_sheet_scalar(value) for value in row]
                   for row in dataset.dataframe.itertuples(index=False, name=None)]
    source_records, source_duplicates = _keyed_rows(
        source_headers, source_rows, f"{name} source"
    )
    if source_duplicates:
        print(f"WARNING: [{name}] ignored {len(source_duplicates)} identical duplicate source rows")
    result.target = len(source_records)

    if not existing_values or not any(str(value).strip() for value in existing_values[0]):
        unique_source_rows = [record[1] for record in sorted(
            source_records.values(), key=lambda record: record[0]
        )]
        result.added = len(unique_source_rows)
        all_rows = [source_headers] + unique_source_rows
        result.writes += _ensure_grid_size(
            service, spreadsheet_id, sheet, len(all_rows), len(source_headers), dry_run
        )
        result.writes += _format_raw_code_column_as_text(
            service, spreadsheet_id, sheet, source_headers, len(all_rows), dry_run
        )
        result.writes += _batch_update_values(
            service, spreadsheet_id, _full_raw_updates(name, all_rows), dry_run
        )
        result.writes += _resize_compact_raw_grid(
            service, spreadsheet_id, sheet, len(all_rows), dry_run
        )
        print(f"[{name}] Initial load: {result.added} rows" + (" (dry run)" if dry_run else ""))
        return result

    existing_rows, schema_exact = _compact_existing_raw_values(existing_values, name)
    existing_records, existing_duplicate_rows = _keyed_rows(
        COMPACT_RAW_HEADERS, existing_rows, f"{name} existing"
    )
    result.existing = len(existing_records) + len(existing_duplicate_rows)

    if (_dataset_signature(source_records) == _dataset_signature(existing_records)
            and not existing_duplicate_rows and schema_exact):
        result.unchanged = len(source_records)
        expected_rows = len(existing_rows) + 1
        if sheet.row_count != expected_rows or sheet.column_count != len(COMPACT_RAW_HEADERS):
            result.schema_migrated = True
            result.writes += _resize_compact_raw_grid(
                service, spreadsheet_id, sheet, expected_rows, dry_run
            )
            print(f"[{name}] Data unchanged; compact grid resize required"
                  + (" (dry run)" if dry_run else ""))
        else:
            print(f"[{name}] No changes detected - sheet left untouched")
        return result

    safe, reason = _source_comparison_safe(source_records, existing_records, dataset.year)
    if not safe:
        result.skipped = True
        result.reason = f"suspicious/incomplete source: {reason}"
        print(f"WARNING: [{name}] {result.reason}; sheet preserved")
        return result

    source_keys = set(source_records)
    existing_keys = set(existing_records)
    new_keys = sorted(source_keys - existing_keys)
    common_keys = source_keys & existing_keys
    changed_keys = sorted(key for key in common_keys
                          if source_records[key][2] != existing_records[key][2])
    unchanged_keys = common_keys - set(changed_keys)
    removal_keys = sorted(existing_keys - source_keys)
    removal_rows = [existing_records[key][0] for key in removal_keys] + existing_duplicate_rows

    result.added = len(new_keys)
    result.updated = len(changed_keys)
    result.unchanged = len(unchanged_keys)

    delete_safe = True
    delete_reason = ""
    if removal_rows:
        delete_safe, delete_reason = _deletions_safe(
            source_records, existing_records, dataset.year
        )
        if delete_safe:
            result.removed = len(removal_rows)
        else:
            result.removal_candidates_preserved = len(removal_rows)
            print(f"WARNING: [{name}] preserved {len(removal_rows)} removal candidates: "
                  f"{delete_reason}")

    # A legacy/wide header requires one controlled full rewrite so A:F has the
    # canonical order. Guarded removal candidates remain in the compact output.
    if not schema_exact:
        result.schema_migrated = True
        final_rows = [record[1] for record in sorted(
            source_records.values(), key=lambda record: record[0]
        )]
        if removal_rows and not delete_safe:
            existing_by_row = {index + 2: row for index, row in enumerate(existing_rows)}
            final_rows.extend(existing_by_row[row_number] for row_number in sorted(removal_rows))
        all_rows = [COMPACT_RAW_HEADERS] + final_rows
        result.writes += _ensure_grid_size(
            service, spreadsheet_id, sheet, len(all_rows), len(COMPACT_RAW_HEADERS), dry_run
        )
        result.writes += _format_raw_code_column_as_text(
            service, spreadsheet_id, sheet, COMPACT_RAW_HEADERS, len(all_rows), dry_run
        )
        result.writes += _batch_update_values(
            service, spreadsheet_id, _full_raw_updates(name, all_rows), dry_run
        )
        result.writes += _resize_compact_raw_grid(
            service, spreadsheet_id, sheet, len(all_rows), dry_run
        )
        print(f"[{name}] Compact-schema migration; Source rows: {result.target}; "
              f"Existing rows: {result.existing}; New: {result.added}; "
              f"Changed: {result.updated}; Removed: {result.removed}; "
              f"Unchanged: {result.unchanged}"
              + (" (dry run)" if dry_run else ""))
        return result

    updates = []
    _, _, contract_column = _raw_key_columns(COMPACT_RAW_HEADERS)
    raw_text_columns = {contract_column} if contract_column is not None else set()
    code_representation_changed = False
    for key in changed_keys:
        row_number = existing_records[key][0]
        old_row = existing_records[key][1]
        new_row = source_records[key][1]
        if contract_column is not None:
            code_representation_changed = code_representation_changed or (
                str(_sheet_scalar(old_row[contract_column])).strip().upper()
                != str(_sheet_scalar(new_row[contract_column])).strip().upper()
            )
        for start_col, end_col in _changed_segments(
            old_row, new_row, text_columns=raw_text_columns
        ):
            updates.append({
                "range": (
                    f"{_quote_sheet(name)}!{_column_letter(start_col + 1)}{row_number}:"
                    f"{_column_letter(end_col + 1)}{row_number}"
                ),
                "majorDimension": "ROWS",
                "values": [new_row[start_col:end_col + 1]],
            })

    next_row = len(existing_values) + 1
    for offset in range(0, len(new_keys), 250):
        batch_keys = new_keys[offset:offset + 250]
        updates.append({
            "range": f"{_quote_sheet(name)}!A{next_row + offset}",
            "majorDimension": "ROWS",
            "values": [source_records[key][1] for key in batch_keys],
        })
    required_rows = next_row + len(new_keys) - 1
    result.writes += _ensure_grid_size(
        service, spreadsheet_id, sheet, required_rows, len(COMPACT_RAW_HEADERS), dry_run
    )
    if code_representation_changed:
        result.writes += _format_raw_code_column_as_text(
            service, spreadsheet_id, sheet, COMPACT_RAW_HEADERS, required_rows, dry_run
        )
    result.writes += _batch_update_values(service, spreadsheet_id, updates, dry_run)

    if removal_rows:
        if delete_safe:
            if not dry_run:
                requests_body = [{
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet.sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        }
                    }
                } for row_number in sorted(removal_rows, reverse=True)]
                for batch in chunk_rows(requests_body, chunk_size=200):
                    _execute(service.spreadsheets().batchUpdate(
                        spreadsheetId=spreadsheet_id, body={"requests": batch}
                    ))
                    result.writes += 1

    final_used_rows = 1 + len(source_records) + result.removal_candidates_preserved
    result.writes += _resize_compact_raw_grid(
        service, spreadsheet_id, sheet, final_used_rows, dry_run
    )

    print(f"[{name}] Source rows: {result.target}; Existing rows: {result.existing}; "
          f"New: {result.added}; Changed: {result.updated}; Removed: {result.removed}; "
          f"Unchanged: {result.unchanged}" + (" (dry run)" if dry_run else ""))
    return result


def _tff_existing_raw_values(values: Sequence[Sequence[Any]], label: str
                             ) -> Tuple[List[List[Any]], bool]:
    """Return canonical TFF rows and whether the stored schema is exact.

    Existing six-column commodity staging tabs in the new TFF workbooks are
    deliberately treated as a one-time family migration. They are not allowed
    to masquerade as Asset Manager/Leveraged Fund data.
    """
    if not values or not values[0]:
        return [], False
    headers = [_normalise_header(value) for value in values[0]]
    while headers and headers[-1] == "":
        headers.pop()
    normalized = [_normalize_column_name(value) for value in headers]
    required = [_normalize_column_name(value) for value in TFF_COMPACT_RAW_HEADERS]
    if len(headers) < len(required) or normalized[:len(required)] != required:
        return [], False
    rows = []
    for row in values[1:]:
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        padded = [_sheet_scalar(value) for value in padded[:len(headers)]]
        if any(_canonical_scalar(value) for value in padded):
            rows.append(padded[:len(required)])
    return rows, headers == TFF_COMPACT_RAW_HEADERS


def _full_tff_raw_updates(sheet_name: str, rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    updates = []
    for offset in range(0, len(rows), 250):
        updates.append({
            "range": f"{_quote_sheet(sheet_name)}!A{offset + 1}",
            "majorDimension": "ROWS",
            "values": [list(row) for row in rows[offset:offset + 250]],
        })
    return updates


def _resize_tff_raw_grid(service, spreadsheet_id: str, sheet: SheetInfo,
                         rows: int, dry_run: bool) -> int:
    rows = max(1, rows)
    columns = len(TFF_COMPACT_RAW_HEADERS)
    if rows == sheet.row_count and columns == sheet.column_count:
        return 0
    if not dry_run:
        _execute(service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": sheet.sheet_id,
                               "gridProperties": {"rowCount": rows,
                                                   "columnCount": columns}},
                "fields": "gridProperties(rowCount,columnCount)"}}]}
        ))
        sheet.row_count, sheet.column_count = rows, columns
    return 0 if dry_run else 1


def _format_tff_raw_code_as_text(service, spreadsheet_id: str, sheet: SheetInfo,
                                 rows: int, dry_run: bool) -> int:
    if dry_run:
        return 0
    code_index = TFF_COMPACT_RAW_HEADERS.index(RAW_CONTRACT_HEADER)
    _execute(service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"repeatCell": {
            "range": {"sheetId": sheet.sheet_id, "startRowIndex": 1,
                      "endRowIndex": max(1, rows), "startColumnIndex": code_index,
                      "endColumnIndex": code_index + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT", "pattern": "@"}}},
            "fields": "userEnteredFormat.numberFormat"}}]}
    ))
    return 1


def sync_tff_raw_sheet(service, spreadsheet_id: str, sheet: SheetInfo,
                       dataset: TFFYearDataset, dry_run: bool,
                       existing_values: Sequence[Sequence[Any]] | None = None) -> SyncResult:
    """Incrementally reconcile one annual TFF raw tab using eight columns."""
    name = sheet.title
    result = SyncResult(name=name, target=len(dataset.dataframe))
    if existing_values is None:
        existing_values = _get_values(service, spreadsheet_id, _quote_sheet(name))
    source_headers = [_normalise_header(column) for column in dataset.dataframe.columns]
    if source_headers != TFF_COMPACT_RAW_HEADERS:
        raise RuntimeError(f"{name}: authoritative dataset is not the TFF compact schema")
    source_rows = [[_sheet_scalar(value) for value in row]
                   for row in dataset.dataframe.itertuples(index=False, name=None)]
    source_records, source_duplicates = _keyed_rows(
        source_headers, source_rows, f"{name} TFF source"
    )
    if source_duplicates:
        print(f"WARNING: [{name}] ignored {len(source_duplicates)} identical duplicate TFF rows")
    result.target = len(source_records)
    existing_rows, schema_exact = _tff_existing_raw_values(existing_values, name)
    existing_records, existing_duplicate_rows = _keyed_rows(
        TFF_COMPACT_RAW_HEADERS, existing_rows, f"{name} TFF existing"
    ) if existing_rows else ({}, [])
    result.existing = len(existing_records) + len(existing_duplicate_rows)

    # A six-column commodity tab or unknown/wide layout is migrated from the
    # authoritative TFF source in one controlled rewrite.
    if not schema_exact:
        result.schema_migrated = True
        result.added = len(source_records)
        rows = [record[1] for record in sorted(source_records.values(), key=lambda record: record[0])]
        all_rows = [TFF_COMPACT_RAW_HEADERS] + rows
        result.writes += _ensure_grid_size(
            service, spreadsheet_id, sheet, len(all_rows), len(TFF_COMPACT_RAW_HEADERS), dry_run
        )
        result.writes += _format_tff_raw_code_as_text(
            service, spreadsheet_id, sheet, len(all_rows), dry_run
        )
        result.writes += _batch_update_values(
            service, spreadsheet_id, _full_tff_raw_updates(name, all_rows), dry_run
        )
        result.writes += _resize_tff_raw_grid(service, spreadsheet_id, sheet, len(all_rows), dry_run)
        print(f"[{name}] TFF compact-schema migration; Source rows: {result.target}; "
              f"Existing rows: {result.existing}; New: {result.added}"
              + (" (dry run)" if dry_run else ""))
        return result

    if (_dataset_signature(source_records) == _dataset_signature(existing_records)
            and not existing_duplicate_rows):
        result.unchanged = len(source_records)
        expected_rows = len(existing_rows) + 1
        if sheet.row_count != expected_rows or sheet.column_count != len(TFF_COMPACT_RAW_HEADERS):
            result.schema_migrated = True
            result.writes += _resize_tff_raw_grid(service, spreadsheet_id, sheet, expected_rows, dry_run)
            print(f"[{name}] TFF data unchanged; compact grid resize required"
                  + (" (dry run)" if dry_run else ""))
        else:
            print(f"[{name}] No TFF changes detected - sheet left untouched")
        return result

    safe, reason = _source_comparison_safe(source_records, existing_records, dataset.year)
    if not safe:
        result.skipped = True
        result.reason = f"suspicious/incomplete TFF source: {reason}"
        print(f"WARNING: [{name}] {result.reason}; sheet preserved")
        return result

    source_keys, existing_keys = set(source_records), set(existing_records)
    new_keys = sorted(source_keys - existing_keys)
    common_keys = source_keys & existing_keys
    changed_keys = sorted(key for key in common_keys
                          if source_records[key][2] != existing_records[key][2])
    result.added = len(new_keys)
    result.updated = len(changed_keys)
    result.unchanged = len(common_keys) - result.updated
    removal_rows = [existing_records[key][0] for key in sorted(existing_keys - source_keys)]
    removal_rows += existing_duplicate_rows
    delete_safe, delete_reason = True, ""
    if removal_rows:
        delete_safe, delete_reason = _deletions_safe(source_records, existing_records, dataset.year)
        if delete_safe:
            result.removed = len(removal_rows)
        else:
            result.removal_candidates_preserved = len(removal_rows)
            print(f"WARNING: [{name}] preserved {len(removal_rows)} TFF removal candidates: {delete_reason}")

    updates = []
    code_column = TFF_COMPACT_RAW_HEADERS.index(RAW_CONTRACT_HEADER)
    for key in changed_keys:
        old_row, new_row = existing_records[key][1], source_records[key][1]
        row_number = existing_records[key][0]
        for start_col, end_col in _changed_segments(old_row, new_row, text_columns={code_column}):
            updates.append({
                "range": f"{_quote_sheet(name)}!{_column_letter(start_col + 1)}{row_number}:"
                         f"{_column_letter(end_col + 1)}{row_number}",
                "majorDimension": "ROWS", "values": [new_row[start_col:end_col + 1]],
            })
    next_row = len(existing_values) + 1
    for offset in range(0, len(new_keys), 250):
        keys = new_keys[offset:offset + 250]
        updates.append({"range": f"{_quote_sheet(name)}!A{next_row + offset}",
                        "majorDimension": "ROWS",
                        "values": [source_records[key][1] for key in keys]})
    required_rows = next_row + len(new_keys) - 1
    result.writes += _ensure_grid_size(
        service, spreadsheet_id, sheet, max(1, required_rows), len(TFF_COMPACT_RAW_HEADERS), dry_run
    )
    result.writes += _batch_update_values(service, spreadsheet_id, updates, dry_run)
    if removal_rows and delete_safe and not dry_run:
        requests_body = [{"deleteDimension": {"range": {"sheetId": sheet.sheet_id,
            "dimension": "ROWS", "startIndex": row - 1, "endIndex": row}}}
            for row in sorted(removal_rows, reverse=True)]
        for batch in chunk_rows(requests_body, chunk_size=200):
            _execute(service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": batch}
            ))
            result.writes += 1
    final_rows = 1 + len(source_records) + result.removal_candidates_preserved
    result.writes += _resize_tff_raw_grid(service, spreadsheet_id, sheet, final_rows, dry_run)
    print(f"[{name}] TFF source rows: {result.target}; Existing rows: {result.existing}; "
          f"New: {result.added}; Changed: {result.updated}; Removed: {result.removed}; "
          f"Unchanged: {result.unchanged}" + (" (dry run)" if dry_run else ""))
    return result


def _to_number(value: Any, allow_blank: bool = False) -> float | int | None:
    if value is None or value == "":
        return None if allow_blank else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.endswith("%"):
            try:
                return float(text[:-1]) / 100.0
            except ValueError:
                return None
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
    else:
        text = value
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _format_year_set(years: Iterable[int]) -> str:
    """Format a set of years as compact consecutive ranges."""
    ordered = sorted(set(int(year) for year in years))
    if not ordered:
        return "none"
    ranges = []
    start = previous = ordered[0]
    for year in ordered[1:] + [None]:
        if year is None or year != previous + 1:
            ranges.append(str(start) if start == previous else f"{start}-{previous}")
            if year is None:
                break
            start = year
        previous = year
    return ", ".join(ranges)


def build_contract_code_history(year_datasets: Dict[int, YearDataset],
                                configured_code: str,
                                commodity: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Build one commodity history using Contract Market Code exclusively."""
    code = normalize_cftc_contract_code(configured_code)
    diagnostics: Dict[str, Any] = {
        "code": code,
        "rows_found": 0,
        "first_date": None,
        "last_date": None,
        "years": set(),
        "missing_years": [],
        "name_infos": {},
        "duplicate_dates": [],
        "conflicts": [],
        "observations": 0,
    }
    if not code:
        return pd.DataFrame(columns=COMMODITY_HEADERS), diagnostics

    pieces = []
    for year in sorted(year_datasets):
        dataset = year_datasets[year]
        if not dataset.contract_col:
            raise RuntimeError(f"COT {year}: contract-code header is unavailable")
        source_codes = dataset.dataframe[dataset.contract_col].map(
            normalize_cftc_contract_code
        )
        matches = dataset.dataframe.loc[source_codes.eq(code)].copy()
        if matches.empty:
            continue
        dates = _parse_cot_dates(matches[dataset.date_col])
        open_interest = _numeric_series(matches[dataset.open_interest_col])
        manager_long = _numeric_series(matches[dataset.manager_long_col])
        manager_short = _numeric_series(matches[dataset.manager_short_col])
        invalid = dates.isna() | open_interest.isna() | manager_long.isna() | manager_short.isna()
        if invalid.any():
            raise RuntimeError(
                f"{commodity}: {int(invalid.sum())} invalid code-matched observations in COT {year}"
            )
        source_names = matches[dataset.market_col].fillna("").astype(str).str.strip()
        diagnostics["rows_found"] += len(matches)
        diagnostics["years"].add(year)
        for index in matches.index:
            display_name = source_names.loc[index]
            name_key = normalize_market_name(display_name).casefold()
            info = diagnostics["name_infos"].setdefault(name_key, {
                "name": display_name,
                "rows": 0,
                "years": set(),
                "first_date": None,
                "last_date": None,
            })
            date = pd.Timestamp(dates.loc[index]).normalize()
            info["rows"] += 1
            info["years"].add(year)
            info["first_date"] = date if info["first_date"] is None else min(info["first_date"], date)
            info["last_date"] = date if info["last_date"] is None else max(info["last_date"], date)

        pieces.append(pd.DataFrame({
            "Date": dates.to_numpy(),
            "Open Interest": open_interest.to_numpy(),
            "Manager Long": manager_long.to_numpy(),
            "Manager Short": manager_short.to_numpy(),
            "_Market": source_names.to_numpy(),
        }))

    diagnostics["missing_years"] = sorted(set(year_datasets) - diagnostics["years"])
    if not pieces:
        return pd.DataFrame(columns=COMMODITY_HEADERS), diagnostics

    combined = pd.concat(pieces, ignore_index=True)
    diagnostics["first_date"] = pd.Timestamp(combined["Date"].min()).normalize()
    diagnostics["last_date"] = pd.Timestamp(combined["Date"].max()).normalize()
    output_rows = []
    for date, group in combined.groupby("Date", sort=True):
        observation_columns = ["Open Interest", "Manager Long", "Manager Short"]
        unique = group[observation_columns].drop_duplicates()
        if len(unique) > 1:
            diagnostics["conflicts"].append({
                "date": pd.Timestamp(date).normalize(),
                "records": [{
                    "market": row["_Market"],
                    "open_interest": _to_number(row["Open Interest"]),
                    "manager_long": _to_number(row["Manager Long"]),
                    "manager_short": _to_number(row["Manager Short"]),
                } for _, row in group.iterrows()],
            })
            continue
        if len(group) > 1:
            diagnostics["duplicate_dates"].append(pd.Timestamp(date).normalize())
        chosen = group.sort_values("_Market", kind="stable").iloc[0]
        oi = _to_number(chosen["Open Interest"])
        long = _to_number(chosen["Manager Long"])
        short = _to_number(chosen["Manager Short"])
        output_rows.append({
            "Date": pd.Timestamp(date).normalize(),
            "Open Interest": oi,
            "Manager Long": long,
            "Manager Short": short,
            "Flip": None if oi == 0 else (long - short) / oi,
        })

    if diagnostics["conflicts"]:
        return pd.DataFrame(columns=COMMODITY_HEADERS), diagnostics
    history = (pd.DataFrame(output_rows, columns=COMMODITY_HEADERS)
               .sort_values("Date").reset_index(drop=True))
    validate_commodity_history(history, commodity)
    diagnostics["observations"] = len(history)
    return history, diagnostics


def print_contract_code_diagnostics(commodity: str,
                                    diagnostics: Dict[str, Any]) -> None:
    """Print code coverage and historical source names for one commodity."""
    code = diagnostics["code"]
    print(f"\n[{commodity}] Configured CFTC code: {code}")
    print(f"[{commodity}] Rows found: {diagnostics['rows_found']}")
    if not diagnostics["rows_found"]:
        print(f"[{commodity}] WARNING: configured CFTC Contract Market Code {code} "
              f"produced zero COT observations from 2010-{get_current_year()}.")
        return
    print(f"[{commodity}] First date: {diagnostics['first_date'].date()}")
    print(f"[{commodity}] Latest date: {diagnostics['last_date'].date()}")
    print(f"[{commodity}] Years represented: {_format_year_set(diagnostics['years'])}")
    print(f"[{commodity}] Missing loaded years: "
          f"{_format_year_set(diagnostics['missing_years'])}")
    print(f"[{commodity}] Distinct historical market names: "
          f"{len(diagnostics['name_infos'])}")
    for info in sorted(diagnostics["name_infos"].values(),
                       key=lambda item: (item["first_date"], item["name"])):
        print(f"[{commodity}]   {_format_year_set(info['years'])}: {info['name']} "
              f"({info['first_date'].date()} -> {info['last_date'].date()}, "
              f"{info['rows']} rows)")
    if diagnostics["duplicate_dates"]:
        print(f"[{commodity}] Equivalent duplicate code/date rows: "
              f"{len(diagnostics['duplicate_dates'])}")
    for conflict in diagnostics["conflicts"]:
        print(f"[{commodity}] CONFLICT: code {code} produced different observations "
              f"for {conflict['date'].date()}; commodity update blocked.")
        for record in conflict["records"]:
            print(f"[{commodity}]   {record['market']}; Open Interest: "
                  f"{record['open_interest']}; Long: {record['manager_long']}; "
                  f"Short: {record['manager_short']}")
    print(f"[{commodity}] Final unique observations: {diagnostics['observations']}")


def extract_commodity_history(year_datasets: Dict[int, YearDataset],
                              configured_code: str, commodity: str) -> pd.DataFrame:
    """Build a code-matched history for focused callers and tests."""
    history, diagnostics = build_contract_code_history(
        year_datasets, configured_code, commodity
    )
    print_contract_code_diagnostics(commodity, diagnostics)
    if diagnostics["conflicts"]:
        raise RuntimeError(f"{commodity}: conflicting same-code/date observations")
    return history


def validate_commodity_history(history: pd.DataFrame, commodity: str) -> None:
    if history.empty:
        return
    if history["Date"].isna().any():
        raise RuntimeError(f"{commodity}: target contains invalid dates")
    if history["Date"].duplicated().any():
        raise RuntimeError(f"{commodity}: target contains duplicate dates")
    if not history["Date"].is_monotonic_increasing:
        raise RuntimeError(f"{commodity}: target is not oldest-to-newest")
    for _, row in history.iterrows():
        expected = None if row["Open Interest"] == 0 else (
            row["Manager Long"] - row["Manager Short"]
        ) / row["Open Interest"]
        actual = row["Flip"]
        if expected is None:
            if pd.notna(actual):
                raise RuntimeError(f"{commodity}: zero Open Interest produced a Flip")
        elif not math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12):
            raise RuntimeError(f"{commodity}: Flip validation failed at {row['Date']}")


def parse_existing_history(values: Sequence[Sequence[Any]]) -> Tuple[pd.DataFrame, List[int]]:
    rows = []
    invalid_rows = []
    for row_number, source_row in enumerate(values[1:], start=2):
        padded = list(source_row) + [""] * max(0, 5 - len(source_row))
        padded = padded[:5]
        if not any(value not in (None, "") for value in padded):
            continue
        date = _parse_any_date(padded[0])
        if date is None:
            invalid_rows.append(row_number)
            continue
        oi = _to_number(padded[1], allow_blank=True)
        long = _to_number(padded[2], allow_blank=True)
        short = _to_number(padded[3], allow_blank=True)
        flip = _to_number(padded[4], allow_blank=True)
        if oi is None or long is None or short is None:
            invalid_rows.append(row_number)
            continue
        rows.append({
            "Date": date, "Open Interest": oi, "Manager Long": long,
            "Manager Short": short, "Flip": flip, "_Row": row_number,
        })
    return pd.DataFrame(rows, columns=COMMODITY_HEADERS + ["_Row"]), invalid_rows


def _history_records_equal(left: pd.Series, right: pd.Series) -> bool:
    if any(_to_number(left[column]) != _to_number(right[column])
           for column in ("Open Interest", "Manager Long", "Manager Short")):
        return False
    left_flip = _to_number(left["Flip"], allow_blank=True)
    right_flip = _to_number(right["Flip"], allow_blank=True)
    if left_flip is None or right_flip is None:
        return left_flip is None and right_flip is None
    return math.isclose(float(left_flip), float(right_flip), rel_tol=1e-10, abs_tol=1e-12)


def compare_histories(existing: pd.DataFrame, target: pd.DataFrame) -> Dict[str, Any]:
    """Pure date-key diff used by commodity sync and idempotency tests."""
    existing_map = {pd.Timestamp(row["Date"]): row for _, row in existing.iterrows()}
    target_map = {pd.Timestamp(row["Date"]): row for _, row in target.iterrows()}
    existing_dates = set(existing_map)
    target_dates = set(target_map)
    common = existing_dates & target_dates
    changed = sorted(date for date in common
                     if not _history_records_equal(existing_map[date], target_map[date]))
    return {
        "added": sorted(target_dates - existing_dates),
        "updated": changed,
        "removed": sorted(existing_dates - target_dates),
        "unchanged": sorted(common - set(changed)),
    }


def _commodity_deletions_safe(existing: pd.DataFrame, target: pd.DataFrame,
                              removed_dates: Sequence[pd.Timestamp]) -> Tuple[bool, str]:
    """Conservatively gate deletion of stored commodity observations."""
    if not removed_dates or existing.empty:
        return True, ""
    if target.empty:
        return False, "target history is empty"
    if target["Date"].min() > existing["Date"].min():
        return False, "target begins after existing history (possible G1/source change)"
    if target["Date"].max() < existing["Date"].max():
        return False, "target ends before existing history"
    maximum_removals = max(3, math.floor(len(existing) * 0.05))
    if len(removed_dates) > maximum_removals:
        return False, f"{len(removed_dates)} removals exceed the guarded 5% threshold"
    return True, ""


def _history_values(history: pd.DataFrame) -> List[List[Any]]:
    values = []
    for _, row in history.iterrows():
        values.append([
            _date_serial(row["Date"]),
            _sheet_scalar(row["Open Interest"]),
            _sheet_scalar(row["Manager Long"]),
            _sheet_scalar(row["Manager Short"]),
            "" if pd.isna(row["Flip"]) else float(row["Flip"]),
        ])
    return values


def _merge_unavailable_years(target: pd.DataFrame, existing: pd.DataFrame,
                             available_years: set[int], start_year: int,
                             current_year: int) -> pd.DataFrame:
    """Preserve stored observations for years whose source could not be validated."""
    if existing.empty:
        return target
    preserve = existing.loc[
        ~existing["Date"].dt.year.isin(available_years)
        | (existing["Date"].dt.year < start_year)
        | (existing["Date"].dt.year > current_year),
        COMMODITY_HEADERS,
    ]
    if preserve.empty:
        return target
    combined = pd.concat([preserve, target], ignore_index=True)
    combined = combined.sort_values("Date", kind="stable").drop_duplicates("Date", keep="last")
    return combined.reset_index(drop=True)


def _header_updates(sheet_name: str, existing_header: Sequence[Any]) -> List[Dict[str, Any]]:
    padded = list(existing_header) + [""] * max(0, 5 - len(existing_header))
    updates = []
    for start, end in _changed_segments(padded[:5], COMMODITY_HEADERS):
        updates.append({
            "range": (f"{_quote_sheet(sheet_name)}!{_column_letter(start + 1)}1:"
                      f"{_column_letter(end + 1)}1"),
            "majorDimension": "ROWS",
            "values": [COMMODITY_HEADERS[start:end + 1]],
        })
    return updates


def sync_commodity_sheet(service, spreadsheet_id: str, sheet: SheetInfo,
                         existing_values: List[List[Any]], target: pd.DataFrame,
                         configured_code_blank: bool,
                         dry_run: bool) -> Tuple[SyncResult, pd.DataFrame]:
    """Synchronise only A:E, preserving G1, column G and all unrelated cells."""
    name = sheet.title
    existing, invalid_rows = parse_existing_history(existing_values)
    result = SyncResult(name=name, existing=len(existing), target=len(target))
    header = existing_values[0][:5] if existing_values else []
    header_changes = _header_updates(name, header)

    if target.empty:
        if existing.empty and not invalid_rows:
            a2 = ""
            if len(existing_values) > 1 and existing_values[1]:
                a2 = str(existing_values[1][0]).strip()
            updates = list(header_changes)
            if a2 != "No COT report available":
                updates.append({
                    "range": f"{_quote_sheet(name)}!A2",
                    "majorDimension": "ROWS", "values": [["No COT report available"]],
                })
            result.writes += _batch_update_values(service, spreadsheet_id, updates, dry_run)
            result.skipped = True
            result.reason = ("blank G1" if configured_code_blank
                             else "configured code produced zero source matches")
        else:
            result.skipped = True
            result.reason = ("blank G1; existing A:E preserved" if configured_code_blank
                             else "configured code produced zero source matches; existing A:E preserved")
            result.writes += _batch_update_values(
                service, spreadsheet_id, header_changes, dry_run
            )
        print(f"[{name}] Skipped: {result.reason}" + (" (dry run)" if dry_run else ""))
        return result, existing[COMMODITY_HEADERS].copy() if not existing.empty else target

    validate_commodity_history(target, name)
    diff = compare_histories(existing, target)
    if diff["removed"]:
        deletion_safe, deletion_reason = _commodity_deletions_safe(
            existing, target, diff["removed"]
        )
        if not deletion_safe:
            result.removal_candidates_preserved = len(diff["removed"])
            preserved = existing.loc[
                existing["Date"].isin(diff["removed"]), COMMODITY_HEADERS
            ]
            target = (pd.concat([target, preserved], ignore_index=True)
                      .sort_values("Date", kind="stable")
                      .drop_duplicates("Date", keep="first")
                      .reset_index(drop=True))
            result.target = len(target)
            print(f"[{name}] WARNING: configured G1/source change would remove "
                  f"{result.removal_candidates_preserved} historical observations. "
                  f"Destructive removal blocked: {deletion_reason}")
            diff = compare_histories(existing, target)
    result.added = len(diff["added"])
    result.updated = len(diff["updated"])
    result.removed = len(diff["removed"])
    result.unchanged = len(diff["unchanged"])

    existing_dates = existing["Date"].tolist() if not existing.empty else []
    target_dates = target["Date"].tolist()
    updates = list(header_changes)
    clears = []
    format_start = None
    format_end = None

    if invalid_rows or existing_dates != target_dates:
        first_difference = 0
        for first_difference, (left, right) in enumerate(zip(existing_dates, target_dates)):
            if left != right:
                break
        else:
            first_difference = min(len(existing_dates), len(target_dates))
        target_suffix = target.iloc[first_difference:].reset_index(drop=True)
        start_row = first_difference + 2
        suffix_values = _history_values(target_suffix)
        for offset in range(0, len(suffix_values), 500):
            updates.append({
                "range": f"{_quote_sheet(name)}!A{start_row + offset}",
                "majorDimension": "ROWS", "values": suffix_values[offset:offset + 500],
            })
        old_last_row = max([len(existing_values)] + invalid_rows if invalid_rows else [len(existing_values)])
        new_last_row = len(target) + 1
        if old_last_row > new_last_row:
            clears.append(f"{_quote_sheet(name)}!A{new_last_row + 1}:E{old_last_row}")
        if suffix_values:
            format_start, format_end = start_row, new_last_row
    else:
        existing_by_date = {pd.Timestamp(row["Date"]): row for _, row in existing.iterrows()}
        target_by_date = {pd.Timestamp(row["Date"]): row for _, row in target.iterrows()}
        for date in diff["updated"]:
            old_row = existing_by_date[date]
            new_row = target_by_date[date]
            row_number = int(old_row["_Row"])
            old_values = [old_row[column] for column in COMMODITY_HEADERS]
            new_values = _history_values(pd.DataFrame([new_row]))[0]
            # The date key is unchanged; only B:E require comparison.
            for start, end in _changed_segments(old_values[1:], new_values[1:]):
                actual_start, actual_end = start + 1, end + 1
                updates.append({
                    "range": (f"{_quote_sheet(name)}!{_column_letter(actual_start + 1)}{row_number}:"
                              f"{_column_letter(actual_end + 1)}{row_number}"),
                    "majorDimension": "ROWS",
                    "values": [new_values[actual_start:actual_end + 1]],
                })

    if not updates and not clears:
        print(f"[{name}] No changes detected - sheet left untouched")
        return result, target

    result.writes += _ensure_grid_size(
        service, spreadsheet_id, sheet, len(target) + 1, 7, dry_run
    )
    result.writes += _batch_update_values(service, spreadsheet_id, updates, dry_run)
    result.writes += _batch_clear_values(service, spreadsheet_id, clears, dry_run)
    if format_start is not None:
        result.writes += _format_data_rows(
            service, spreadsheet_id, sheet, format_start, format_end, None, dry_run
        )
    print(f"[{name}] Existing rows: {result.existing}; Target rows: {result.target}; "
          f"New: {result.added}; Changed: {result.updated}; Removed: {result.removed}; "
          f"Unchanged: {result.unchanged}" + (" (dry run)" if dry_run else ""))
    return result, target


def build_tff_code_history(year_datasets: Dict[int, TFFYearDataset],
                           code: str, instrument: str
                           ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Build one continuous TFF history by Contract Market Code only."""
    target_code = normalize_cftc_contract_code(code)
    matches = []
    names = set()
    years = set()
    for year, dataset in sorted(year_datasets.items()):
        frame = dataset.dataframe.copy()
        frame[RAW_CONTRACT_HEADER] = frame[RAW_CONTRACT_HEADER].map(normalize_cftc_contract_code)
        selected = frame.loc[frame[RAW_CONTRACT_HEADER] == target_code].copy()
        if selected.empty:
            continue
        years.add(year)
        matches.append(selected)
        names.update(normalize_market_name(value) for value in selected[RAW_MARKET_HEADER])
    diagnostics = {"instrument": instrument, "code": target_code,
                   "rows_found": sum(len(frame) for frame in matches),
                   "years": years, "name_infos": names, "conflicts": []}
    if not matches:
        print(f"[{instrument}] Configured CFTC code: {target_code}; Rows found: 0")
        return pd.DataFrame(columns=TFF_FULL_HEADERS), diagnostics
    combined = pd.concat(matches, ignore_index=True)
    rows = []
    for date, group in combined.groupby(RAW_DATE_HEADER, sort=True):
        records = group[TFF_COMPACT_RAW_HEADERS].to_dict("records")
        comparable = [tuple(_canonical_scalar(row[column]) for column in TFF_COMPACT_RAW_HEADERS[2:])
                      for row in records]
        if len(set(comparable)) > 1:
            details = [{
                "code": target_code, "date": str(date),
                "market_names": sorted({normalize_market_name(row[RAW_MARKET_HEADER]) for row in records}),
                "open_interest": [row[RAW_OPEN_INTEREST_HEADER] for row in records],
                "asset_manager": [(row[TFF_RAW_ASSET_LONG_HEADER], row[TFF_RAW_ASSET_SHORT_HEADER]) for row in records],
                "leveraged_funds": [(row[TFF_RAW_LEV_LONG_HEADER], row[TFF_RAW_LEV_SHORT_HEADER]) for row in records],
            }]
            diagnostics["conflicts"].extend(details)
            raise RuntimeError(f"[{instrument}] conflicting TFF code/date observation: {details[0]}")
        rows.append(group.iloc[0])
    combined = pd.DataFrame(rows).sort_values(RAW_DATE_HEADER, kind="stable")
    dates = pd.to_datetime(combined[RAW_DATE_HEADER])
    oi = _numeric_series(combined[RAW_OPEN_INTEREST_HEADER])
    asset_long = _numeric_series(combined[TFF_RAW_ASSET_LONG_HEADER])
    asset_short = _numeric_series(combined[TFF_RAW_ASSET_SHORT_HEADER])
    lev_long = _numeric_series(combined[TFF_RAW_LEV_LONG_HEADER])
    lev_short = _numeric_series(combined[TFF_RAW_LEV_SHORT_HEADER])
    asset_flip = (asset_long - asset_short).div(oi.where(oi != 0))
    lev_flip = (lev_long - lev_short).div(oi.where(oi != 0))
    history = pd.DataFrame({
        "Date": dates.dt.normalize(), "Open Interest": oi,
        "Asset Manager Long": asset_long, "Asset Manager Short": asset_short,
        "Asset Manager Flip": asset_flip,
        "Leveraged Fund Long": lev_long, "Leveraged Fund Short": lev_short,
        "Leveraged Fund Flip": lev_flip,
    }).reset_index(drop=True)
    diagnostics.update({
        "first_date": dates.min(), "last_date": dates.max(),
        "final_observations": len(history), "name_infos": sorted(names),
    })
    print(f"[{instrument}] Configured CFTC code: {target_code}; Rows found: {len(combined)}; "
          f"First date: {dates.min().date()}; Latest date: {dates.max().date()}; "
          f"Years represented: {_format_year_set(years)}; "
          f"Distinct historical market names: {len(names)}; Final unique observations: {len(history)}")
    return history, diagnostics


def _tff_layout(values: Sequence[Sequence[Any]]) -> Tuple[str, List[str]]:
    """Choose the prepared TFF output layout from its existing headers."""
    headers = [str(value).strip() for value in (values[0] if values else [])]
    normalized = {_normalize_column_name(value) for value in headers}
    full_required = {_normalize_column_name(value) for value in TFF_FULL_HEADERS}
    if full_required.issubset(normalized):
        return "full", TFF_FULL_HEADERS
    # Preserve the current prepared A:E tabs exactly. Their generic Manager
    # fields are interpreted as Asset Manager/Institutional for TFF.
    # A partial/full header row that also contains a G1 code is intentionally
    # left in this safe legacy mode so no output write can ever touch column G.
    return "asset_legacy", TFF_LEGACY_HEADERS


def _tff_history_values(history: pd.DataFrame, layout: str) -> List[List[Any]]:
    values = []
    for _, row in history.iterrows():
        if layout == "full":
            values.append([
                _date_serial(row["Date"]), _sheet_scalar(row["Open Interest"]),
                _sheet_scalar(row["Asset Manager Long"]), _sheet_scalar(row["Asset Manager Short"]),
                "" if pd.isna(row["Asset Manager Flip"]) else float(row["Asset Manager Flip"]),
                _sheet_scalar(row["Leveraged Fund Long"]), _sheet_scalar(row["Leveraged Fund Short"]),
                "" if pd.isna(row["Leveraged Fund Flip"]) else float(row["Leveraged Fund Flip"]),
            ])
        else:
            values.append([
                _date_serial(row["Date"]), _sheet_scalar(row["Open Interest"]),
                _sheet_scalar(row["Asset Manager Long"]), _sheet_scalar(row["Asset Manager Short"]),
                "" if pd.isna(row["Asset Manager Flip"]) else float(row["Asset Manager Flip"]),
            ])
    return values


def _parse_tff_history(values: Sequence[Sequence[Any]], width: int,
                       headers: Sequence[str]) -> Tuple[pd.DataFrame, List[int]]:
    rows, invalid = [], []
    for row_number, source_row in enumerate(values[1:], start=2):
        padded = list(source_row) + [""] * max(0, width - len(source_row))
        padded = padded[:width]
        if not any(value not in (None, "") for value in padded):
            continue
        date = _parse_any_date(padded[0])
        if date is None:
            invalid.append(row_number)
            continue
        numbers = [_to_number(value, allow_blank=True) for value in padded[1:]]
        if any(value is None for value in numbers):
            invalid.append(row_number)
            continue
        item = {"Date": date, "_Row": row_number}
        for column, value in zip(headers[1:], numbers):
            item[column] = value
        rows.append(item)
    return pd.DataFrame(rows, columns=list(headers) + ["_Row"]), invalid


def _format_tff_history_rows(service, spreadsheet_id: str, sheet: SheetInfo,
                             start_row: int, end_row: int, width: int,
                             dry_run: bool) -> int:
    if dry_run or end_row < start_row:
        return 0
    requests_body = [{"repeatCell": {
        "range": {"sheetId": sheet.sheet_id, "startRowIndex": start_row - 1,
                  "endRowIndex": end_row, "startColumnIndex": 0, "endColumnIndex": 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
        "fields": "userEnteredFormat.numberFormat"}}]
    if width > 1:
        requests_body.append({"repeatCell": {
            "range": {"sheetId": sheet.sheet_id, "startRowIndex": start_row - 1,
                      "endRowIndex": end_row, "startColumnIndex": 1, "endColumnIndex": width},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}})
    for flip_column in ([4] if width == 5 else [4, 7]):
        requests_body.append({"repeatCell": {
            "range": {"sheetId": sheet.sheet_id, "startRowIndex": start_row - 1,
                      "endRowIndex": end_row, "startColumnIndex": flip_column,
                      "endColumnIndex": flip_column + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}},
            "fields": "userEnteredFormat.numberFormat"}})
    _execute(service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests_body}
    ))
    return 1


def sync_tff_instrument_sheet(service, spreadsheet_id: str, sheet: SheetInfo,
                              existing_values: Sequence[Sequence[Any]],
                              target: pd.DataFrame, dry_run: bool
                              ) -> Tuple[SyncResult, pd.DataFrame, str]:
    """Incrementally sync one TFF instrument while preserving its prepared layout."""
    layout, headers = _tff_layout(existing_values)
    if layout == "asset_legacy":
        print(f"[{sheet.title}] Existing Manager A:E layout retained; TFF values use "
              "Asset Manager / Institutional fields")
    width = len(headers)
    existing, invalid_rows = _parse_tff_history(existing_values, width, headers)
    result = SyncResult(name=sheet.title, existing=len(existing), target=len(target))
    if target.empty:
        result.skipped = True
        result.reason = "configured code produced zero source matches; existing history preserved"
        print(f"[{sheet.title}] TFF skipped: {result.reason}" + (" (dry run)" if dry_run else ""))
        return result, existing, layout
    target = target.sort_values("Date", kind="stable").reset_index(drop=True)
    output = pd.DataFrame({"Date": target["Date"], "Open Interest": target["Open Interest"],
                           "Asset Manager Long": target["Asset Manager Long"],
                           "Asset Manager Short": target["Asset Manager Short"],
                           "Asset Manager Flip": target["Asset Manager Flip"],
                           "Leveraged Fund Long": target["Leveraged Fund Long"],
                           "Leveraged Fund Short": target["Leveraged Fund Short"],
                           "Leveraged Fund Flip": target["Leveraged Fund Flip"]})
    if layout == "asset_legacy":
        target_rows = pd.DataFrame({
            "Date": output["Date"], "Open Interest": output["Open Interest"],
            "Manager Long": output["Asset Manager Long"], "Manager Short": output["Asset Manager Short"],
            "Flip": output["Asset Manager Flip"],
        })
    else:
        target_rows = output[TFF_FULL_HEADERS].copy()
    target_rows["Date"] = pd.to_datetime(target_rows["Date"])
    existing_map = {pd.Timestamp(row["Date"]): row for _, row in existing.iterrows()}
    target_map = {pd.Timestamp(row["Date"]): row for _, row in target_rows.iterrows()}
    common = set(existing_map) & set(target_map)
    changed = sorted(date for date in common
                     if any(_to_number(existing_map[date][column], allow_blank=True)
                            != _to_number(target_map[date][column], allow_blank=True)
                            for column in headers[1:]))
    added = sorted(set(target_map) - set(existing_map))
    removed = sorted(set(existing_map) - set(target_map))
    if removed:
        safe, reason = _commodity_deletions_safe(existing.rename(columns={"Manager Long": "Manager Long"}),
                                                  target_rows.rename(columns={"Manager Long": "Manager Long"}), removed)
        if not safe:
            result.removal_candidates_preserved = len(removed)
            target_rows = pd.concat([target_rows,
                                     existing[existing["Date"].isin(removed)][headers]], ignore_index=True)
            target_rows = target_rows.sort_values("Date", kind="stable").reset_index(drop=True)
            target_map = {pd.Timestamp(row["Date"]): row for _, row in target_rows.iterrows()}
            removed = []
            print(f"[{sheet.title}] WARNING: guarded TFF removals preserved: {reason}")
    result.added, result.updated = len(added), len(changed)
    result.removed = len(removed)
    result.unchanged = len(common) - result.updated
    target_dates = target_rows["Date"].tolist()
    existing_dates = existing["Date"].tolist() if not existing.empty else []
    updates, clears = [], []
    format_start = format_end = None
    if invalid_rows or existing_dates != target_dates:
        first = 0
        for first, pair in enumerate(zip(existing_dates, target_dates)):
            if pair[0] != pair[1]:
                break
        else:
            first = min(len(existing_dates), len(target_dates))
        suffix = _tff_history_values(target_rows.iloc[first:], layout) if layout == "full" else _tff_history_values(
            output.iloc[first:], layout
        )
        start_row = first + 2
        for offset in range(0, len(suffix), 500):
            updates.append({"range": f"{_quote_sheet(sheet.title)}!A{start_row + offset}",
                            "majorDimension": "ROWS", "values": suffix[offset:offset + 500]})
        old_last = max([len(existing_values)] + invalid_rows if invalid_rows else [len(existing_values)])
        new_last = len(target_rows) + 1
        if old_last > new_last:
            clears.append(f"{_quote_sheet(sheet.title)}!A{new_last + 1}:{_column_letter(width)}{old_last}")
        if suffix:
            format_start, format_end = start_row, new_last
    else:
        for date in changed:
            old, new = existing_map[date], target_map[date]
            row_number = int(old["_Row"])
            old_vals = [old[column] for column in headers]
            new_vals = _tff_history_values(pd.DataFrame([new]), layout)[0] if layout == "full" else [
                _date_serial(new["Date"]), _sheet_scalar(new["Open Interest"]),
                _sheet_scalar(new["Manager Long"]), _sheet_scalar(new["Manager Short"]),
                "" if pd.isna(new["Flip"]) else float(new["Flip"])]
            for start, end in _changed_segments(old_vals[1:], new_vals[1:]):
                updates.append({"range": f"{_quote_sheet(sheet.title)}!{_column_letter(start + 2)}{row_number}:"
                                         f"{_column_letter(end + 2)}{row_number}",
                                "majorDimension": "ROWS", "values": [new_vals[start + 1:end + 2]]})
    if not updates and not clears:
        print(f"[{sheet.title}] No TFF changes detected - sheet left untouched")
        return result, output, layout
    result.writes += _ensure_grid_size(service, spreadsheet_id, sheet, len(target_rows) + 1, width, dry_run)
    if existing_values and str(existing_values[0][:width]) != str(headers):
        updates.insert(0, {"range": f"{_quote_sheet(sheet.title)}!A1",
                           "majorDimension": "ROWS", "values": [headers]})
    result.writes += _batch_update_values(service, spreadsheet_id, updates, dry_run)
    result.writes += _batch_clear_values(service, spreadsheet_id, clears, dry_run)
    if format_start is not None:
        result.writes += _format_tff_history_rows(service, spreadsheet_id, sheet,
                                                  format_start, format_end, width, dry_run)
    print(f"[{sheet.title}] Existing rows: {result.existing}; Target rows: {result.target}; "
          f"New: {result.added}; Changed: {result.updated}; Removed: {result.removed}; "
          f"Unchanged: {result.unchanged}" + (" (dry run)" if dry_run else ""))
    return result, output, layout


def _summary_existing(values: Sequence[Sequence[Any]], width: int):
    rows = []
    invalid_rows = []
    seen = set()
    duplicate_rows = []
    for row_number, source_row in enumerate(values[1:], start=2):
        padded = list(source_row) + [""] * max(0, width - len(source_row))
        padded = padded[:width]
        if not any(value not in (None, "") for value in padded):
            continue
        date = _parse_any_date(padded[0])
        if date is None:
            invalid_rows.append(row_number)
            continue
        if date in seen:
            duplicate_rows.append(row_number)
        else:
            seen.add(date)
            rows.append((date, padded, row_number))
    return rows, invalid_rows, duplicate_rows


def sync_summary(service, spreadsheet_id: str, sheet: SheetInfo,
                 existing_values: List[List[Any]],
                 histories: Dict[str, pd.DataFrame], dry_run: bool,
                 authoritative_complete: bool = True) -> SyncResult:
    """Reconcile every managed Summary cell against the authoritative histories.

    Summary dates are a union across instruments.  The old implementation only
    reliably wrote the suffix when a new date was appended, so an instrument
    whose dates already existed (notably TFF 2Y) could remain populated only at
    the newest row.  This implementation compares every managed instrument/date
    cell while retaining the existing suffix optimisation for structural date
    changes.  Failed or unconfigured instruments are absent from ``histories``
    and therefore cannot trigger destructive clears.
    """
    name = SUMMARY_SHEET_NAME
    headers = list(existing_values[0]) if existing_values else ["Date"]
    while headers and headers[-1] in (None, ""):
        headers.pop()
    if not headers:
        headers = ["Date"]
    width = len(headers)

    # Match headers by their trimmed displayed name; no positional assumptions.
    history_names = {str(instrument).strip(): instrument for instrument in histories}
    managed_columns: Dict[int, str] = {}
    header_columns: Dict[str, List[int]] = {}
    for column, header in enumerate(headers[1:], start=1):
        key = str(_sheet_scalar(header)).strip()
        if key in history_names:
            managed_columns[column] = history_names[key]
            header_columns.setdefault(key, []).append(column)
    missing_headers = [key for key in history_names if key not in header_columns]
    for key in missing_headers:
        print(f"[Summary] WARNING: populated instrument {key!r} has no matching Summary header")
    duplicate_headers = {key: cols for key, cols in header_columns.items() if len(cols) > 1}
    if duplicate_headers:
        print("[Summary] WARNING: duplicate managed headers; reconciling every matching column: "
              + "; ".join(f"{key}={cols}" for key, cols in duplicate_headers.items()))

    existing_rows, invalid_rows, duplicate_rows = _summary_existing(existing_values, width)
    existing_by_date = {date: row for date, row, _ in existing_rows}
    existing_dates = [date for date, _, _ in existing_rows]

    # Canonical source maps retain every valid report date, including dates for
    # which Flip is intentionally blank (for example zero open interest).
    flip_maps: Dict[str, Dict[pd.Timestamp, Any]] = {}
    source_observations: Dict[str, int] = {}
    source_valid_flips: Dict[str, int] = {}
    target_dates_set = set()
    for display_name, history in histories.items():
        key = str(display_name).strip()
        mapping: Dict[pd.Timestamp, Any] = {}
        if history is not None and not history.empty:
            for _, source_row in history.iterrows():
                date = _parse_any_date(source_row.get("Date"))
                if date is None:
                    continue
                flip = _to_number(source_row.get("Flip"), allow_blank=True)
                mapping[date] = "" if flip is None else float(flip)
        flip_maps[key] = mapping
        source_observations[key] = len(mapping)
        source_valid_flips[key] = sum(value not in (None, "") for value in mapping.values())
        target_dates_set.update(mapping)

    if not authoritative_complete:
        print("[Summary] WARNING: source history is incomplete; stale managed cells "
              "will be preserved until a complete authoritative run is available")

    unmanaged_columns = [index for index in range(1, width) if index not in managed_columns]
    # Preserve dates carrying values in Summary columns this run does not own.
    for date, row, _ in existing_rows:
        if any(row[index] not in (None, "") for index in unmanaged_columns):
            target_dates_set.add(date)
        if (not authoritative_complete
                and any(row[index] not in (None, "") for index in managed_columns)):
            target_dates_set.add(date)
    target_dates = sorted(target_dates_set)
    result = SyncResult(name=name, existing=len(existing_rows), target=len(target_dates),
                        coverage={})
    result.added = len(set(target_dates) - set(existing_dates))
    result.removed = len(set(existing_dates) - set(target_dates))

    target_rows: List[List[Any]] = []
    for date in target_dates:
        existing_row = existing_by_date.get(date, [""] * width)
        row = [_date_serial(date)] + [""] * (width - 1)
        for column in range(1, width):
            if column in managed_columns:
                instrument = str(managed_columns[column]).strip()
                mapping = flip_maps.get(instrument, {})
                if date in mapping:
                    row[column] = mapping[date]
                elif not authoritative_complete:
                    row[column] = existing_row[column] if column < len(existing_row) else ""
                else:
                    row[column] = ""
            else:
                row[column] = existing_row[column] if column < len(existing_row) else ""
        target_rows.append(row)

    def values_equal(old: Any, new: Any) -> bool:
        old_number = _to_number(old, allow_blank=True)
        new_number = _to_number(new, allow_blank=True)
        if old_number is None or new_number is None:
            return old_number is None and new_number is None
        return math.isclose(float(old_number), float(new_number), rel_tol=1e-10, abs_tol=1e-12)

    # Compare every managed cell, including rows that already existed before a
    # newly appended date.  This is the key historical backfill/correction path.
    changed_cells: List[Tuple[int, int, Any, Any, str]] = []
    unchanged_populated = 0
    changed_by_column: Dict[int, List[int]] = {}
    for row_index, date in enumerate(target_dates):
        existing_row = existing_by_date.get(date, [""] * width)
        target_row = target_rows[row_index]
        for column, instrument_value in managed_columns.items():
            old = existing_row[column] if column < len(existing_row) else ""
            new = target_row[column]
            if values_equal(old, new):
                if _to_number(new, allow_blank=True) is not None:
                    unchanged_populated += 1
                continue
            old_number = _to_number(old, allow_blank=True)
            new_number = _to_number(new, allow_blank=True)
            if old_number is None and new_number is not None:
                kind = "backfilled"
                result.backfilled += 1
            elif old_number is not None and new_number is None:
                kind = "cleared"
                result.cleared += 1
            else:
                kind = "corrected"
                result.corrected += 1
            changed_cells.append((row_index, column, old, new, kind))
            changed_by_column.setdefault(column, []).append(row_index)

    result.updated = len(changed_cells)
    # Count cells that were already correct, including correct blanks, while the
    # user-facing diagnostic focuses on populated unchanged values.
    result.unchanged = max(0, len(target_dates) * len(managed_columns) - result.updated)
    result.unchanged_cells = unchanged_populated

    for column, instrument in managed_columns.items():
        key = str(instrument).strip()
        mapping = flip_maps.get(key, {})
        valid_dates = {date for date, value in mapping.items() if value not in (None, "")}
        existing_populated = 0
        final_populated = 0
        for date in valid_dates:
            old_row = existing_by_date.get(date, [""] * width)
            if _to_number(old_row[column] if column < len(old_row) else "", allow_blank=True) is not None:
                existing_populated += 1
            target_index = target_dates.index(date) if date in target_dates_set else None
            if target_index is not None and _to_number(target_rows[target_index][column], allow_blank=True) is not None:
                final_populated += 1
        observations = source_observations.get(key, 0)
        coverage = (100.0 * final_populated / len(valid_dates)) if valid_dates else 100.0
        result.coverage[key] = {
            "observations": observations,
            "valid_flips": len(valid_dates),
            "existing_populated": existing_populated,
            "backfilled": sum(1 for row, col, _, _, kind in changed_cells
                               if col == column and kind == "backfilled"),
            "corrected": sum(1 for row, col, _, _, kind in changed_cells
                              if col == column and kind == "corrected"),
            "cleared": sum(1 for row, col, _, _, kind in changed_cells
                            if col == column and kind == "cleared"),
            "final_populated": final_populated,
            "coverage": coverage,
        }

    updates: List[Dict[str, Any]] = []
    clears: List[str] = []
    format_start = format_end = None
    if str(_sheet_scalar(headers[0])).strip() != "Date":
        updates.append({"range": f"{_quote_sheet(name)}!A1", "majorDimension": "ROWS",
                        "values": [["Date"]]})

    structural_change = bool(invalid_rows or duplicate_rows or existing_dates != target_dates)
    first_difference = 0
    if structural_change:
        for first_difference, (left, right) in enumerate(zip(existing_dates, target_dates)):
            if left != right:
                break
        else:
            first_difference = min(len(existing_dates), len(target_dates))
        suffix = target_rows[first_difference:]
        start_row = first_difference + 2
        for offset in range(0, len(suffix), 500):
            updates.append({"range": f"{_quote_sheet(name)}!A{start_row + offset}",
                            "majorDimension": "ROWS",
                            "values": suffix[offset:offset + 500]})
        old_last = len(existing_values)
        new_last = len(target_rows) + 1
        if old_last > new_last:
            clears.append(f"{_quote_sheet(name)}!A{new_last + 1}:{_column_letter(width)}{old_last}")
        if suffix:
            format_start, format_end = start_row, new_last

    # If the date sequence changed, the suffix write above already covers its
    # managed cells.  Historical cells in the unchanged prefix still need
    # individual reconciliation (the common TFF 2Y case).
    for column, row_indices in changed_by_column.items():
        eligible = [index for index in row_indices if not structural_change or index < first_difference]
        if not eligible:
            continue
        start = previous = eligible[0]
        for row_index in eligible[1:] + [None]:
            if row_index is None or row_index != previous + 1:
                first_row = start + 2
                last_row = previous + 2
                if first_row == last_row:
                    cell_range = f"{_quote_sheet(name)}!{_column_letter(column + 1)}{first_row}"
                else:
                    cell_range = (f"{_quote_sheet(name)}!{_column_letter(column + 1)}{first_row}:"
                                  f"{_column_letter(column + 1)}{last_row}")
                updates.append({"range": cell_range, "majorDimension": "COLUMNS",
                                "values": [[target_rows[index][column] for index in range(start, previous + 1)]]})
                if row_index is None:
                    break
                start = row_index
            previous = row_index

    # Compact per-instrument diagnostics and a zero-discrepancy validation.
    print(f"[Summary] Dates existing: {len(existing_dates)}; Dates added: {result.added}; "
          f"Instrument columns checked: {len(managed_columns)}; "
          f"Missing historical cells backfilled: {result.backfilled}; "
          f"Existing cells corrected: {result.corrected}; Stale cells cleared: {result.cleared}; "
          f"Unchanged populated cells: {result.unchanged_cells}"
          + (" (dry run)" if dry_run else ""))
    for instrument in sorted(result.coverage):
        stats = result.coverage[instrument]
        print(f"[Summary] {instrument}: observations={stats['observations']}; "
              f"existing populated={stats['existing_populated']}; "
              f"backfilled={stats['backfilled']}; corrected={stats['corrected']}; "
              f"cleared={stats['cleared']}; projected coverage={stats['coverage']:.2f}%")
    validation_errors = 0
    for column, instrument in managed_columns.items():
        mapping = flip_maps.get(str(instrument).strip(), {})
        for date, expected in mapping.items():
            if date not in target_dates_set:
                validation_errors += 1
                continue
            actual = target_rows[target_dates.index(date)][column]
            if not values_equal(actual, expected):
                validation_errors += 1
    if validation_errors:
        raise RuntimeError(f"Summary validation found {validation_errors} unreconciled managed cells")
    print(f"[Summary] Validation: {len(managed_columns)} managed columns, 0 discrepancies")

    if not updates and not clears:
        print("[Summary] No changes detected - sheet left untouched")
        return result
    result.writes += _ensure_grid_size(
        service, spreadsheet_id, sheet, len(target_rows) + 1, width, dry_run
    )
    result.writes += _batch_update_values(service, spreadsheet_id, updates, dry_run)
    result.writes += _batch_clear_values(service, spreadsheet_id, clears, dry_run)
    if format_start is not None:
        result.writes += _format_data_rows(
            service, spreadsheet_id, sheet, format_start, format_end, width, dry_run
        )
    print(f"[Summary] New dates: {result.added}; Updated cells/rows: {result.updated}; "
          f"Removed dates: {result.removed}; Unchanged populated cells: {result.unchanged_cells}"
          + (" (dry run)" if dry_run else ""))
    return result


def _print_run_summary(summary: RunSummary, histories: Dict[str, pd.DataFrame],
                       failed_years: Sequence[int], zero_matches: Sequence[str],
                       changed_raw: Sequence[str], changed_commodities: Sequence[str],
                       dry_run: bool) -> None:
    all_dates = sorted({pd.Timestamp(date) for history in histories.values()
                        for date in history.get("Date", [])})
    print("\n========== COT SYNC SUMMARY ==========")
    print(f"Mode: {'DRY RUN - no Google mutations' if dry_run else 'LIVE'}")
    print(f"Years checked: {summary.years_checked}")
    print(f"Raw COT sheets unchanged: {summary.raw_unchanged}")
    print(f"Raw COT sheets updated: {summary.raw_updated}")
    print(f"New/corrected/removed raw records: {summary.raw_new}/{summary.raw_corrected}/{summary.raw_removed}")
    print(f"Commodity sheets checked: {summary.commodities_checked}")
    print(f"Commodity sheets unchanged: {summary.commodities_unchanged}")
    print(f"Commodity sheets updated: {summary.commodities_updated}")
    print(f"Commodity sheets skipped: {summary.commodities_skipped}")
    print(f"New/corrected/removed commodity observations: "
          f"{summary.commodity_new}/{summary.commodity_corrected}/{summary.commodity_removed}")
    print(f"Guarded commodity removals blocked: {summary.commodity_removals_blocked}")
    print(f"Summary new dates: {summary.summary_new_dates}")
    print(f"Summary cells updated: {summary.summary_cells_updated}")
    print(f"Summary unchanged cells: {summary.summary_unchanged_cells}")
    print(f"Summary historical cells backfilled: {summary.summary_backfilled}")
    print(f"Summary existing cells corrected: {summary.summary_corrected}")
    print(f"Summary stale cells cleared: {summary.summary_cleared}")
    print(f"Earliest date: {all_dates[0].date().isoformat() if all_dates else 'none'}")
    print(f"Latest date: {all_dates[-1].date().isoformat() if all_dates else 'none'}")
    print(f"Failed/unavailable years: {', '.join(map(str, failed_years)) if failed_years else 'none'}")
    print(f"G1 codes with zero matches: {', '.join(zero_matches) if zero_matches else 'none'}")
    print(f"Raw sheets changed: {', '.join(changed_raw) if changed_raw else 'none'}")
    print(f"Commodity sheets changed: {', '.join(changed_commodities) if changed_commodities else 'none'}")


def _print_contract_code_summary(configs: Dict[str, ContractCodeConfig],
                                 diagnostics: Dict[str, Dict[str, Any]],
                                 collisions: Dict[str, List[str]],
                                 histories: Dict[str, pd.DataFrame]) -> None:
    """Print final G1 code, source-name and observation coverage."""
    configured = {name: config for name, config in configs.items() if config.code}
    blank = {name: config for name, config in configs.items() if not config.code}
    invalid = {name: config for name, config in configured.items() if not config.valid}
    zero_match = [name for name, item in diagnostics.items() if not item["rows_found"]]
    renamed = [name for name, item in diagnostics.items() if len(item["name_infos"]) > 1]
    dated = [item for item in diagnostics.values() if item["first_date"] is not None]
    earliest = min((item["first_date"] for item in dated), default=None)
    latest = max((item["last_date"] for item in dated), default=None)

    print("\n========== CFTC CONTRACT CODE SUMMARY ==========")
    print(f"Commodity sheets checked: {len(configs)}")
    print(f"Configured CFTC codes: {len(configured)}")
    print(f"Blank G1 sheets: {len(blank)}")
    print(f"Unique configured codes: {len(set(config.code for config in configured.values()))}")
    print(f"Duplicate/colliding codes: {len(collisions)}")
    print(f"Invalid configured codes: {len(invalid)}")
    print(f"Configured codes with zero matches: {len(zero_match)}")
    print(f"Historical market-name changes detected: {len(renamed)}")
    print(f"Earliest observation: {earliest.date() if earliest is not None else 'none'}")
    print(f"Latest observation: {latest.date() if latest is not None else 'none'}")
    print("G1 codes detected:")
    for commodity in COMMODITY_SHEETS:
        if commodity in configs:
            config = configs[commodity]
            suffix = " INVALID" if config.code and not config.valid else ""
            print(f"  {commodity}: {config.code or '(blank)'}{suffix}")
    print("Duplicate/colliding codes:")
    if collisions:
        for code, commodities in collisions.items():
            print(f"  {code}: {', '.join(commodities)}")
    else:
        print("  none")
    print("Invalid codes:")
    if invalid:
        for commodity, config in invalid.items():
            print(f"  {commodity}: {config.raw_value} -> {config.code}")
    else:
        print("  none")
    print("Zero-match codes:")
    if zero_match:
        for commodity in zero_match:
            print(f"  {commodity}: {diagnostics[commodity]['code']}")
    else:
        print("  none")
    print("Historical names and final observations:")
    for commodity in COMMODITY_SHEETS:
        if commodity not in diagnostics:
            continue
        item = diagnostics[commodity]
        names = " | ".join(info["name"] for info in item["name_infos"].values()) or "none"
        first = item["first_date"].date() if item["first_date"] is not None else "none"
        last = item["last_date"].date() if item["last_date"] is not None else "none"
        final_count = len(histories.get(commodity, pd.DataFrame()))
        print(f"  {commodity} [{item['code']}]: {final_count} observations; "
              f"{first} -> {last}; names: {names}")


def _tff_instrument_sheets(sheets: Dict[str, SheetInfo]) -> List[str]:
    """Return workbook-local managed instrument candidates.

    TFF workbooks have no universal sheet-name list. Summary, annual raw tabs,
    and obvious internal tabs are excluded; G1 is still the authoritative
    configuration gate for every remaining candidate.
    """
    return [name for name in sheets
            if name != SUMMARY_SHEET_NAME and _raw_sheet_name(sheets, _year_from_raw_name(name)) is None
            and not name.lower().startswith(("config", "readme", "audit"))]


def _year_from_raw_name(name: str) -> int:
    match = re.fullmatch(r"COT\s*(\d{4})", name.strip(), flags=re.IGNORECASE)
    return int(match.group(1)) if match else -1


def _tff_summary_history(history: pd.DataFrame) -> pd.DataFrame:
    """Use Asset Manager Flip for the existing Summary layout by default."""
    if history.empty:
        return pd.DataFrame(columns=["Date", "Flip"])
    return history[["Date", "Asset Manager Flip"]].rename(
        columns={"Asset Manager Flip": "Flip"}
    )


def _print_tff_code_summary(configs: Dict[str, ContractCodeConfig],
                            diagnostics: Dict[str, Dict[str, Any]],
                            collisions: Dict[str, List[str]],
                            histories: Dict[str, pd.DataFrame]) -> None:
    configured = {name: config for name, config in configs.items() if config.code}
    invalid = {name: config for name, config in configured.items() if not config.valid}
    zero = [name for name, item in diagnostics.items() if item.get("rows_found", 0) == 0]
    renamed = [name for name, item in diagnostics.items()
               if len(item.get("name_infos", [])) > 1]
    dated = [item for item in diagnostics.values() if item.get("first_date") is not None]
    earliest = min((item["first_date"] for item in dated), default=None)
    latest = max((item["last_date"] for item in dated), default=None)
    print("\n========== TFF CONTRACT CODE SUMMARY ==========")
    print(f"Configured CFTC codes: {len(configured)}")
    print(f"Unique codes: {len(set(config.code for config in configured.values()))}")
    print(f"Duplicate/colliding codes: {len(collisions)}")
    print(f"Invalid codes: {len(invalid)}")
    print(f"Codes with zero matches: {len(zero)}")
    print(f"Historical name changes detected: {len(renamed)}")
    print(f"Earliest observation: {earliest.date() if earliest is not None else 'none'}")
    print(f"Latest observation: {latest.date() if latest is not None else 'none'}")
    print("Instrument -> code:")
    for name, config in configs.items():
        print(f"  {name}: {config.code or '(blank)'}")
    print("TFF instrument observations:")
    for name, item in diagnostics.items():
        first = item.get("first_date")
        last = item.get("last_date")
        print(f"  {name} [{item.get('code', '')}]: {len(histories.get(name, []))} observations; "
              f"{first.date() if first is not None else 'none'} -> "
              f"{last.date() if last is not None else 'none'}; "
              f"names: {' | '.join(item.get('name_infos', [])) or 'none'}")


def run_tff_workbook(service, spreadsheet_id: str, workbook_number: int,
                     datasets: Dict[int, TFFYearDataset], args: argparse.Namespace,
                     authoritative_complete: bool = True
                     ) -> Dict[str, Any]:
    """Process one TFF workbook in an isolated read/compare/write boundary."""
    print(f"\n========== TFF WORKBOOK {workbook_number} ==========")
    print(f"Spreadsheet ID: {spreadsheet_id}")
    sheets = _sheet_metadata(service, spreadsheet_id)
    years = sorted(datasets)
    raw_names = {year: _raw_sheet_name(sheets, year) for year in years}
    missing_raw = [year for year, name in raw_names.items() if name is None]
    if missing_raw:
        message = (f"TFF workbook {workbook_number} is missing prepared raw sheet(s): "
                   + ", ".join(f"COT {year}" for year in missing_raw))
        print(f"ERROR: {message}")
        if not args.dry_run:
            raise RuntimeError(message)
    instruments = _tff_instrument_sheets(sheets)
    # Read through I so a workbook that keeps G1 as configuration can expose
    # optional Leveraged Fund columns in H:I without ever writing column G.
    history_ranges = [f"{_quote_sheet(name)}!A:I" for name in instruments]
    code_ranges = [f"{_quote_sheet(name)}!G1" for name in instruments]
    raw_ranges = [f"{_quote_sheet(name)}" for name in raw_names.values() if name is not None]
    requested = history_ranges + code_ranges + raw_ranges
    values = _batch_get_values(service, spreadsheet_id, requested)
    missing_reads = [a1 for a1 in requested if a1 not in values]
    if missing_reads:
        raise RuntimeError("TFF bulk read omitted range(s); refusing workbook mutations: "
                           + ", ".join(missing_reads))

    configs = {
        name: parse_tff_contract_code_config(name, values.get(f"{_quote_sheet(name)}!G1", []))
        for name in instruments
    }
    invalid, collisions = contract_code_preflight(configs)
    for name, config in invalid.items():
        print(f"ERROR: [TFF {name}] invalid CFTC Contract Market Code in G1: "
              f"{config.raw_value!r}")
    for code, owners in collisions.items():
        print(f"ERROR: TFF CFTC Contract Market Code collision in workbook {workbook_number}: "
              f"{code} -> {', '.join(owners)}")
    if (invalid or collisions) and not args.dry_run:
        raise RuntimeError(f"TFF workbook {workbook_number} has invalid/colliding G1 codes")

    current_cells, projected_cells = _projected_tff_grid_cells(sheets, datasets)
    print(f"[TFF Preflight {workbook_number}] Workbook grid cells: current={current_cells:,}; "
          f"projected with compact eight-column raw history={projected_cells:,}; "
          f"safety limit={args.sheet_cell_limit:,}")
    capacity_safe = projected_cells <= args.sheet_cell_limit
    if not capacity_safe:
        message = f"TFF workbook {workbook_number} exceeds the configured grid safety limit"
        print(f"ERROR: [TFF Preflight] {message}")
        if not args.dry_run:
            raise RuntimeError(message)

    raw_results = []
    for year, dataset in sorted(datasets.items()):
        sheet_name = raw_names.get(year)
        if sheet_name is None or not capacity_safe:
            continue
        try:
            result = sync_tff_raw_sheet(
                service, spreadsheet_id, sheets[sheet_name], dataset, args.dry_run,
                existing_values=values.get(_quote_sheet(sheet_name), []),
            )
            raw_results.append(result)
        except Exception as exc:
            print(f"ERROR: [TFF {sheet_name}] synchronisation failed; sheet preserved: {exc}")

    histories, summary_histories, diagnostics = {}, {}, {}
    changed, zero_matches = [], []
    raw_code_cache: Dict[str, Tuple[pd.DataFrame, Dict[str, Any]]] = {}
    for name in instruments:
        config = configs[name]
        existing_values = values.get(f"{_quote_sheet(name)}!A:I", [])
        if not config.code:
            print(f"[{name}] G1 blank - no CFTC Contract Market Code configured")
            continue
        if not config.valid:
            print(f"[{name}] invalid G1 code {config.raw_value!r}; existing history preserved")
            continue
        try:
            if config.code not in raw_code_cache:
                raw_code_cache[config.code] = build_tff_code_history(datasets, config.code, name)
            history, diagnostic = raw_code_cache[config.code]
            diagnostics[name] = diagnostic
            if diagnostic.get("rows_found", 0) == 0:
                zero_matches.append(name)
                continue
            result, final_history, layout = sync_tff_instrument_sheet(
                service, spreadsheet_id, sheets[name], existing_values, history, args.dry_run
            )
            histories[name] = final_history
            summary_histories[name] = _tff_summary_history(final_history)
            if result.changed:
                changed.append(name)
        except Exception as exc:
            print(f"ERROR: [{name}] TFF history/synchronisation failed; sheet preserved: {exc}")

    summary_result = None
    if SUMMARY_SHEET_NAME in sheets:
        summary_values = _get_values(service, spreadsheet_id, _quote_sheet(SUMMARY_SHEET_NAME))
        try:
            summary_result = sync_summary(
                service, spreadsheet_id, sheets[SUMMARY_SHEET_NAME],
                summary_values, summary_histories, args.dry_run,
                authoritative_complete=authoritative_complete,
            )
        except Exception as exc:
            print(f"ERROR: [TFF Summary] synchronisation failed; sheet preserved: {exc}")
    else:
        print(f"ERROR: TFF workbook {workbook_number} is missing Summary; skipped")
    if summary_result is not None and summary_result.coverage:
        expected = sum(item["valid_flips"] for item in summary_result.coverage.values())
        populated = sum(item["final_populated"] for item in summary_result.coverage.values())
        projected = (100.0 * populated / expected) if expected else 100.0
        print(f"[TFF Summary {workbook_number}] completeness: {populated}/{expected} "
              f"valid Flip cells ({projected:.2f}%), writes={summary_result.writes}")
        if "2Y" in summary_result.coverage:
            two_year = summary_result.coverage["2Y"]
            print(f"[TFF Summary {workbook_number}] 2Y: observations={two_year['observations']}; "
                  f"existing populated={two_year['existing_populated']}; "
                  f"backfilled={two_year['backfilled']}; corrected={two_year['corrected']}; "
                  f"projected coverage={two_year['coverage']:.2f}%")
    _print_tff_code_summary(configs, diagnostics, collisions, histories)
    return {
        "workbook_number": workbook_number, "instruments": instruments,
        "configs": configs, "diagnostics": diagnostics, "histories": histories,
        "raw_results": raw_results, "zero_matches": zero_matches,
        "changed": changed, "summary": summary_result,
        "grid_current": current_cells, "grid_projected": projected_cells,
    }


def run_tff_pipeline(service, args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Download TFF annual files once, then process each workbook independently."""
    current_year = get_current_year()
    years = list(range(TFF_START_YEAR, current_year + 1))
    datasets: Dict[int, TFFYearDataset] = {}
    failed = []
    for year in years:
        try:
            dataset = download_tff_year(year)
            datasets[year] = dataset
            print(f"[TFF COT {year}] Validated {len(dataset.dataframe)} complete source rows "
                  f"({dataset.source_column_count} source columns -> 8 staged columns) "
                  f"from {build_tff_futures_only_url(year)}", flush=True)
        except Exception as exc:
            failed.append(year)
            print(f"ERROR: [TFF COT {year}] download/validation failed: {exc}", flush=True)
    print("\n========== TFF FUTURES ONLY SYNC SUMMARY ==========")
    print(f"Mode: {'DRY RUN - no Google mutations' if args.dry_run else 'LIVE'}")
    print(f"Years checked: {len(years)}")
    print(f"Annual TFF files successfully validated: {len(datasets)}")
    print(f"Failed/unavailable years: {', '.join(map(str, failed)) if failed else 'none'}")
    results = []
    workbook_numbers = [args.tff_workbook] if args.tff_workbook else [1, 2, 3]
    for number in workbook_numbers:
        try:
            result = run_tff_workbook(
                service, TFF_SPREADSHEET_IDS[number - 1], number, datasets, args,
                authoritative_complete=not failed,
            )
            results.append(result)
            summary = result.get("summary")
            print(f"Workbook {number}: instruments checked={len(result['instruments'])}; "
                  f"updated={len(result['changed'])}; blank G1="
                  f"{sum(1 for config in result['configs'].values() if not config.code)}; "
                  f"zero-match codes={len(result['zero_matches'])}; "
                  f"new observations={sum(r.added for r in result['raw_results'])}; "
                  f"corrected observations={sum(r.updated for r in result['raw_results'])}; "
                  f"removed observations={sum(r.removed for r in result['raw_results'])}; "
                  f"Summary new dates={summary.added if summary else 0}; "
                  f"Summary updated cells={summary.updated if summary else 0}; "
                  f"backfilled={summary.backfilled if summary else 0}; "
                  f"corrected={summary.corrected if summary else 0}; "
                  f"cleared={summary.cleared if summary else 0}")
        except Exception as exc:
            print(f"ERROR: TFF pipeline failed for workbook {number}: {exc}")
    total_writes = sum(result.get("summary").writes if result.get("summary") else 0
                       for result in results)
    total_writes += sum(raw.writes for result in results for raw in result["raw_results"])
    print(f"Total TFF Google mutations: {0 if args.dry_run else total_writes}")
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally synchronise 2010-current CFTC Disaggregated Futures Only history."
    )
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID,
                        help="Target Google Spreadsheet ID")
    parser.add_argument("--service-account-path", default=DEFAULT_SERVICE_ACCOUNT_PATH,
                        help="Existing Google service-account JSON path")
    parser.add_argument("--start-year", type=int, default=START_YEAR,
                        help="First historical year (default: 2010)")
    parser.add_argument("--sheet-cell-limit", type=int,
                        default=GOOGLE_SHEETS_SAFE_CELL_LIMIT,
                        help="Workbook cell safety limit used by raw-data preflight")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download, validate and calculate diffs without Google writes")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--commodities-only", action="store_true",
                       help="Run only the existing Disaggregated commodity pipeline")
    scope.add_argument("--tff-only", action="store_true",
                       help="Run only the isolated TFF Futures Only pipeline")
    parser.add_argument("--tff-workbook", type=int, choices=(1, 2, 3),
                        help="With TFF scope, process only workbook 1, 2 or 3")
    args = parser.parse_args()
    current_year = get_current_year()
    if not 2009 < args.start_year <= current_year:
        parser.error(f"--start-year must be between 2010 and {current_year}")
    if args.sheet_cell_limit < 1_000_000:
        parser.error("--sheet-cell-limit must be at least 1000000")
    return args


def main() -> None:
    args = parse_args()
    service_account_path = Path(args.service_account_path)
    if not service_account_path.is_file():
        raise FileNotFoundError(
            f"Service account JSON not found at: {service_account_path}. "
            "Pass --service-account-path with the existing credential file."
        )
    current_year = get_current_year()
    years = list(range(args.start_year, current_year + 1))
    service = _build_google_sheets_service(str(service_account_path))
    if args.tff_only:
        run_tff_pipeline(service, args)
        return
    sheets = _sheet_metadata(service, args.spreadsheet_id)
    summary = RunSummary(years_checked=len(years))

    missing_commodities = [name for name in COMMODITY_SHEETS if name not in sheets]
    for name in missing_commodities:
        print(f"WARNING: Missing commodity sheet '{name}' - skipped")
    missing_raw = [year for year in years if f"COT {year}" not in sheets]
    for year in missing_raw:
        print(f"ERROR: Missing raw sheet 'COT {year}' - source will be retained in memory only")
    if current_year > 2035:
        print(f"WARNING: Current year {current_year} exceeds the workbook's prepared 2035 horizon")

    # Read managed A:E data and G1 configuration separately. G2:G100 is ignored
    # and no write or clear range ever includes column G.
    history_ranges = [
        f"{_quote_sheet(name)}!A:E" for name in COMMODITY_SHEETS if name in sheets
    ]
    code_ranges = [
        f"{_quote_sheet(name)}!G1" for name in COMMODITY_SHEETS if name in sheets
    ]
    requested_ranges = history_ranges + code_ranges
    commodity_values = _batch_get_values(service, args.spreadsheet_id, requested_ranges)
    missing_reads = [a1_range for a1_range in requested_ranges
                     if a1_range not in commodity_values]
    if missing_reads:
        raise RuntimeError(
            "Google Sheets bulk read omitted commodity range(s); refusing all mutations: "
            + ", ".join(missing_reads)
        )

    code_configs: Dict[str, ContractCodeConfig] = {}
    for name in COMMODITY_SHEETS:
        if name not in sheets:
            continue
        code_range = f"{_quote_sheet(name)}!G1"
        code_configs[name] = parse_contract_code_config(
            name, commodity_values.get(code_range, [])
        )
    invalid_codes, code_collisions = contract_code_preflight(code_configs)
    for commodity, config in invalid_codes.items():
        print(f"ERROR: [{commodity}] Invalid CFTC Contract Market Code in G1: "
              f"{config.raw_value!r} -> {config.code!r}")
    for code, commodities in code_collisions.items():
        print(f"ERROR: CFTC Contract Market Code collision.\n\nCode: {code}\n\nConfigured on:")
        for commodity in commodities:
            print(f"- {commodity}")
    if (invalid_codes or code_collisions) and not args.dry_run:
        raise RuntimeError(
            "Invalid/colliding G1 Contract Market Code configuration; workbook left untouched"
        )

    datasets: Dict[int, YearDataset] = {}
    failed_years = []
    changed_raw = []
    # Download and validate every year before any write. Besides protecting
    # downstream histories, this makes a whole-workbook cell-capacity preflight
    # possible and prevents a partially populated raw database.
    for year in years:
        try:
            dataset = download_cot_year(year)
            datasets[year] = dataset
            print(f"[COT {year}] Validated {len(dataset.dataframe)} complete source rows "
                  f"({dataset.source_column_count} source columns -> "
                  f"{len(COMPACT_RAW_HEADERS)} staged columns) from {dataset.url}", flush=True)
        except Exception as exc:
            failed_years.append(year)
            print(f"ERROR: [COT {year}] download/validation failed: {exc}", flush=True)
            continue

    current_cells, projected_cells = _projected_grid_cells(sheets, datasets)
    raw_capacity_safe = projected_cells <= args.sheet_cell_limit
    print(f"[Preflight] Workbook grid cells: current={current_cells:,}; "
          f"projected with compact six-column raw history={projected_cells:,}; "
          f"safety limit={args.sheet_cell_limit:,}", flush=True)
    if not raw_capacity_safe:
        message = (
            "Compact six-column raw history does not fit in the target Google workbook. "
            "No raw tabs will be changed; review the workbook layout or use a separate "
            "raw-data workbook."
        )
        print(f"ERROR: [Preflight] {message}", flush=True)
        if not args.dry_run:
            raise RuntimeError(message)

    # Fetch annual staging tabs in bounded batches. This avoids one network
    # round-trip per year while keeping legacy wide-tab migration possible.
    raw_ranges = [
        _quote_sheet(f"COT {year}") for year in sorted(datasets)
        if f"COT {year}" in sheets
    ]
    raw_values: Dict[str, List[List[Any]]] = {}
    for range_batch in chunk_rows(raw_ranges, chunk_size=4):
        raw_values.update(_batch_get_values(service, args.spreadsheet_id, range_batch))
    missing_raw_reads = [a1_range for a1_range in raw_ranges if a1_range not in raw_values]
    if missing_raw_reads:
        raise RuntimeError(
            "Google Sheets bulk read omitted raw range(s); refusing all mutations: "
            + ", ".join(missing_raw_reads)
        )

    for year, dataset in sorted(datasets.items()):
        raw_name = f"COT {year}"
        if raw_name not in sheets or not raw_capacity_safe:
            continue
        try:
            result = sync_raw_cot_sheet(
                service, args.spreadsheet_id, sheets[raw_name], dataset, args.dry_run,
                existing_values=raw_values.get(_quote_sheet(raw_name), []),
            )
        except Exception as exc:
            print(f"ERROR: [{raw_name}] synchronisation failed; sheet preserved: {exc}")
            continue
        if result.changed:
            summary.raw_updated += 1
            changed_raw.append(raw_name)
        else:
            summary.raw_unchanged += 1
        summary.raw_new += result.added
        summary.raw_corrected += result.updated
        summary.raw_removed += result.removed

    histories: Dict[str, pd.DataFrame] = {}
    history_cache: Dict[str, Tuple[pd.DataFrame, Dict[str, Any]]] = {}
    code_diagnostics: Dict[str, Dict[str, Any]] = {}
    zero_matches = []
    changed_commodities = []
    available_years = set(datasets)
    for name in COMMODITY_SHEETS:
        if name not in sheets:
            summary.commodities_skipped += 1
            continue
        summary.commodities_checked += 1
        history_range = f"{_quote_sheet(name)}!A:E"
        values = commodity_values.get(history_range, [])
        config = code_configs[name]
        existing, _ = parse_existing_history(values)
        if not config.code:
            print(f"[{name}] G1 blank - no CFTC Contract Market Code configured")
            result, final_history = sync_commodity_sheet(
                service, args.spreadsheet_id, sheets[name], values,
                pd.DataFrame(columns=COMMODITY_HEADERS), True, args.dry_run,
            )
        elif not config.valid:
            result = SyncResult(
                name=name, existing=len(existing), target=len(existing),
                unchanged=len(existing), skipped=True,
                reason="invalid G1 CFTC Contract Market Code",
            )
            final_history = existing[COMMODITY_HEADERS].copy()
            print(f"[{name}] Invalid G1 code {config.raw_value!r}; existing A:E preserved")
        else:
            try:
                if config.code not in history_cache:
                    history_cache[config.code] = build_contract_code_history(
                        datasets, config.code, name
                    )
                cached_history, diagnostics = history_cache[config.code]
                extracted = cached_history.copy()
                code_diagnostics[name] = diagnostics
                print_contract_code_diagnostics(name, diagnostics)
                if diagnostics["conflicts"]:
                    result = SyncResult(
                        name=name, existing=len(existing), target=len(existing),
                        unchanged=len(existing), skipped=True,
                        reason="conflicting same-code/date source observations",
                    )
                    final_history = existing[COMMODITY_HEADERS].copy()
                    print(f"[{name}] Synchronisation blocked; existing A:E preserved")
                elif extracted.empty:
                    zero_matches.append(name)
                    result, final_history = sync_commodity_sheet(
                        service, args.spreadsheet_id, sheets[name], values,
                        extracted, False, args.dry_run,
                    )
                else:
                    target = _merge_unavailable_years(
                        extracted, existing, available_years, args.start_year, current_year
                    )
                    result, final_history = sync_commodity_sheet(
                        service, args.spreadsheet_id, sheets[name], values,
                        target, False, args.dry_run,
                    )
            except Exception as exc:
                print(f"ERROR: [{name}] build/synchronisation failed; sheet preserved: {exc}")
                result = SyncResult(name=name, skipped=True, reason=str(exc))
                final_history = existing[COMMODITY_HEADERS].copy()
        histories[name] = final_history
        if result.skipped:
            summary.commodities_skipped += 1
        elif result.changed:
            summary.commodities_updated += 1
            changed_commodities.append(name)
        else:
            summary.commodities_unchanged += 1
        summary.commodity_new += result.added
        summary.commodity_corrected += result.updated
        summary.commodity_removed += result.removed
        summary.commodity_removals_blocked += result.removal_candidates_preserved

    if SUMMARY_SHEET_NAME not in sheets:
        print("ERROR: Missing 'Summary' sheet - summary synchronisation skipped")
    else:
        summary_values = _get_values(
            service, args.spreadsheet_id, _quote_sheet(SUMMARY_SHEET_NAME)
        )
        try:
            summary_result = sync_summary(
                service, args.spreadsheet_id, sheets[SUMMARY_SHEET_NAME],
                summary_values, histories, args.dry_run,
                authoritative_complete=not failed_years,
            )
            summary.summary_new_dates = summary_result.added
            summary.summary_cells_updated = summary_result.updated
            summary.summary_unchanged_cells = summary_result.unchanged_cells
            summary.summary_backfilled = summary_result.backfilled
            summary.summary_corrected = summary_result.corrected
            summary.summary_cleared = summary_result.cleared
        except Exception as exc:
            print(f"ERROR: [Summary] synchronisation failed; sheet preserved: {exc}")

    _print_run_summary(
        summary, histories, failed_years, zero_matches,
        changed_raw, changed_commodities, args.dry_run,
    )
    _print_contract_code_summary(
        code_configs, code_diagnostics, code_collisions, histories
    )
    if not args.commodities_only:
        print("\nCommodity pipeline completed. Starting isolated TFF pipeline.")
        run_tff_pipeline(service, args)


if __name__ == "__main__":
    main()
