from __future__ import annotations

import argparse
import csv
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

CFTC_INDEX_URL = (
    "https://www.cftc.gov/MarketReports/CommitmentsofTraders/"
    "HistoricalCompressed/index.htm"
)
DEFAULT_SERVICE_ACCOUNT_PATH = r'C:\\Code\\service_account.json'
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SPREADSHEET_ID = "10fs18m9tX_t_4i8aj__P-NaiH_hsbDeHkudSAJjNQfw"
DEFAULT_DISAGG_SHEET_NAME = "COT"
DEFAULT_TFF_SHEET_NAME = "COT2"
DISAGG_SECTION_LABEL = "Disaggregated Futures Only Reports"
TFF_SECTION_LABEL = "Traders in Financial Futures ; Futures Only Reports"


@dataclass
class ExcelLink:
    year: int
    url: str


def _looks_like_report_link(text: str, href: str) -> bool:
    value = f"{text} {href}".lower()
    return any(token in value for token in ["excel", "text", "xls", "xlsx", "txt", "zip", "csv"])


def _extract_year_candidates(text: str, href: str) -> List[int]:
    candidates = set()
    for source in [text, href]:
        for match in re.findall(r"\b(20\d{2})\b", source):
            candidates.add(int(match))
    return sorted(candidates)


def _section_keywords(section_label: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", section_label.lower()).strip()
    if "disaggregated" in normalized:
        return ["disaggregated", "fut_disagg", "disagg"]
    if "traders in financial futures" in normalized:
        return ["traders in financial futures", "financial futures", "fin_fut", "fut_fin", "tff"]
    return normalized.split()


def _fallback_urls(section_label: str, target_year: int) -> List[str]:
    base = "https://www.cftc.gov/files/dea/history/"
    normalized = section_label.lower()
    if "disaggregated" in normalized:
        stems = [f"fut_disagg_txt_{target_year}.zip", f"fut_disagg_xls_{target_year}.zip"]
    elif "traders in financial futures" in normalized:
        stems = [f"fut_fin_txt_{target_year}.zip", f"fut_fin_xls_{target_year}.zip"]
    else:
        stems = []
    return [urljoin(base, stem) for stem in stems]


def _url_exists(url: str) -> bool:
    try:
        head = requests.head(url, timeout=20, allow_redirects=True)
        if head.ok:
            return True
    except Exception:
        pass
    try:
        resp = requests.get(url, timeout=20, stream=True)
        ok = resp.ok
        resp.close()
        return ok
    except Exception:
        return False


def fetch_section_report_link(section_label: str, year: int | None = None) -> ExcelLink:
    """
    Parse the CFTC index page and return a report link (Excel/text/ZIP) for a
    given section and year. If year is None, selects the latest available year.
    """
    response = requests.get(CFTC_INDEX_URL, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    keywords = _section_keywords(section_label)

    # Build candidates from all links and score section relevance using href,
    # anchor text, parent text, and nearby row/div text.
    candidates: List[tuple[int, str, str, int]] = []  # (year, text, full_url, section_score)
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a["href"]
        full_url = urljoin(CFTC_INDEX_URL, href)

        if not _looks_like_report_link(text, href):
            continue

        parent_text = a.parent.get_text(" ", strip=True).lower() if a.parent else ""
        container = a.find_parent(["tr", "td", "table", "div", "p", "li"])
        container_text = container.get_text(" ", strip=True).lower() if container else ""
        joined = f"{text} {href} {parent_text} {container_text}".lower()
        section_score = sum(1 for kw in keywords if kw in joined)

        years = _extract_year_candidates(text, href)
        for y in years:
            candidates.append((y, text, full_url, section_score))

    if not candidates:
        # Hard fallback: probe canonical historical ZIP paths directly.
        if year is not None:
            probe_years = [year]
        else:
            current_year = pd.Timestamp.now(tz="UTC").year
            probe_years = list(range(current_year, 2009, -1))

        for y in probe_years:
            for candidate_url in _fallback_urls(section_label, y):
                if _url_exists(candidate_url):
                    return ExcelLink(year=y, url=candidate_url)
        raise RuntimeError(
            f"No report links found on CFTC page for section '{section_label}', "
            "and canonical fallback URLs were unavailable."
        )

    section_candidates = [c for c in candidates if c[3] > 0]
    if not section_candidates:
        # Fallback: if section matching fails due page markup changes, keep all
        # report-like links so user still gets a best-effort result.
        section_candidates = candidates

    available_years = sorted({c[0] for c in section_candidates})
    selected_year = year if year is not None else available_years[-1]
    year_links = [c for c in section_candidates if c[0] == selected_year]

    if not year_links:
        # Year wasn't found in parsed links. Try canonical URL fallback for that year.
        for candidate_url in _fallback_urls(section_label, selected_year):
            if _url_exists(candidate_url):
                return ExcelLink(year=selected_year, url=candidate_url)
        raise RuntimeError(
            f"Year {selected_year} is unavailable for section '{section_label}'. "
            f"Available years: {available_years}"
        )

    # Prefer text/CSV/ZIP outputs first (to avoid legacy .xls/xlrd dependency),
    # then Excel outputs.
    def score(item: tuple[int, str, str, int]) -> int:
        _, text, url, section_score = item
        value = f"{text} {url}".lower()
        base = section_score * 10
        if any(token in value for token in [".txt", ".csv", "text", ".zip"]):
            return base + 2
        if any(token in value for token in [".xlsx", ".xls", "excel"]):
            return base + 1
        return base

    best = sorted(year_links, key=score, reverse=True)[0]
    return ExcelLink(year=best[0], url=best[2])


def _read_delimited_text(path: Path) -> pd.DataFrame:
    """
    Read a CFTC text export into a DataFrame.
    Attempts dialect sniffing first, then CSV and tab-delimited fallbacks.
    """
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            sample = f.read(16384)
        dialect = csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";", "|"])
        sep = dialect.delimiter
        return pd.read_csv(path, sep=sep, dtype=object, low_memory=False)
    except Exception:
        pass

    for sep in [",", "\t", ";", "|"]:
        try:
            return pd.read_csv(path, sep=sep, dtype=object, low_memory=False)
        except Exception:
            continue

    raise RuntimeError(f"Unable to parse delimited file: {path}")


def download_report_to_dataframe(report_url: str) -> pd.DataFrame:
    """
    Download CFTC report URL (Excel/text/ZIP), parse it into a DataFrame, and
    ensure temporary downloads are deleted after processing.
    """
    suffix = Path(report_url).suffix.lower() or ".bin"
    with tempfile.TemporaryDirectory(prefix="cot_download_") as temp_dir:
        temp_dir_path = Path(temp_dir)
        download_path = temp_dir_path / f"report{suffix}"

        response = requests.get(report_url, timeout=60)
        response.raise_for_status()
        download_path.write_bytes(response.content)

        if suffix == ".zip":
            with zipfile.ZipFile(download_path, "r") as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                if not names:
                    raise RuntimeError("ZIP download was empty.")

                preferred = (
                    [n for n in names if n.lower().endswith((".xlsx", ".xls"))]
                    or [n for n in names if n.lower().endswith((".txt", ".csv"))]
                    or names
                )
                member_name = preferred[0]
                extracted_path = temp_dir_path / Path(member_name).name
                extracted_path.write_bytes(zf.read(member_name))
        else:
            extracted_path = download_path

        ext = extracted_path.suffix.lower()
        if ext in {".xlsx", ".xls", ".xlsm"}:
            data = pd.read_excel(extracted_path, sheet_name=0, dtype=object)
        elif ext in {".txt", ".csv"}:
            data = _read_delimited_text(extracted_path)
        else:
            # Final fallback: try Excel first, then delimited text.
            try:
                data = pd.read_excel(extracted_path, sheet_name=0, dtype=object)
            except Exception:
                data = _read_delimited_text(extracted_path)

    # Keep sheet as values only and normalize NaN/NaT into empty strings.
    data = data.fillna("")
    return data


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


def write_to_google_sheet(
    spreadsheet_id: str,
    sheet_name: str,
    values: List[List[str]],
    service_account_path: str,
) -> None:
    """Clear destination tab and write all values from row 1/col A."""
    creds = Credentials.from_service_account_file(service_account_path, scopes=SHEETS_SCOPE)
    service = build("sheets", "v4", credentials=creds)
    safe_sheet_name = sheet_name.replace("'", "''")

    required_rows = max(len(values), 1)
    required_cols = max((len(r) for r in values), default=1)
    required_cols = max(required_cols, 26)

    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = metadata.get("sheets", [])
    by_title = {s["properties"]["title"]: s["properties"] for s in sheets}

    if sheet_name not in by_title:
        add_resp = (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": sheet_name,
                                    "gridProperties": {
                                        "rowCount": max(required_rows, 1000),
                                        "columnCount": max(required_cols, 26),
                                    },
                                }
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    else:
        props = by_title[sheet_name]
        sheet_id = props["sheetId"]
        current_rows = props.get("gridProperties", {}).get("rowCount", 0)
        current_cols = props.get("gridProperties", {}).get("columnCount", 0)
        if current_rows < required_rows or current_cols < required_cols:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id,
                                    "gridProperties": {
                                        "rowCount": max(current_rows, required_rows),
                                        "columnCount": max(current_cols, required_cols),
                                    },
                                },
                                "fields": "gridProperties(rowCount,columnCount)",
                            }
                        }
                    ]
                },
            ).execute()

    # Clear existing data first.
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{safe_sheet_name}'!A:ZZ",
        body={},
    ).execute()

    # Write in chunks so very large sheets remain reliable.
    start_row = 1
    for chunk in chunk_rows(values, chunk_size=1000):
        end_row = start_row + len(chunk) - 1
        cell_range = f"'{safe_sheet_name}'!A{start_row}:ZZ{end_row}"
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": chunk},
        ).execute()
        start_row = end_row + 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download CFTC Disaggregated Futures Only report Excel for a year "
            "and push into a Google Sheet tab."
        )
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Year to fetch for both sections (default: latest available year per section)",
    )
    parser.add_argument(
        "--spreadsheet-id",
        default=DEFAULT_SPREADSHEET_ID,
        help="Target Google Spreadsheet ID",
    )
    parser.add_argument("--disagg-sheet-name", default=DEFAULT_DISAGG_SHEET_NAME, help="Disaggregated destination tab (default: COT)")
    parser.add_argument("--tff-sheet-name", default=DEFAULT_TFF_SHEET_NAME, help="TFF destination tab (default: COT2)")
    parser.add_argument(
        "--service-account-path",
        default=DEFAULT_SERVICE_ACCOUNT_PATH,
        help="Path to Google service account JSON",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    service_account_path = Path(args.service_account_path)
    if not service_account_path.exists():
        raise FileNotFoundError(
            f"Service account JSON not found at: {service_account_path}. "
            "Pass --service-account-path with the correct file path."
        )

    jobs = [
        (DISAGG_SECTION_LABEL, args.disagg_sheet_name),
        (TFF_SECTION_LABEL, args.tff_sheet_name),
    ]

    for section_label, target_sheet in jobs:
        report_link = fetch_section_report_link(section_label, args.year)
        print(f"[{section_label}] Found {report_link.year} report link: {report_link.url}")

        df = download_report_to_dataframe(report_link.url)
        values = to_sheet_values(df)

        write_to_google_sheet(
            spreadsheet_id=args.spreadsheet_id,
            sheet_name=target_sheet,
            values=values,
            service_account_path=str(service_account_path),
        )

        print(
            f"[{section_label}] Uploaded {len(values) - 1} data rows (+header) "
            f"to '{target_sheet}' in spreadsheet {args.spreadsheet_id}."
        )


if __name__ == "__main__":
    main()