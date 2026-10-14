from pathlib import Path

path = Path('outputs/COT Reports.py')
s = path.read_text(encoding='utf-8')
s = s.replace('from __future__ import annotations', '''"""Download annual CFTC futures-only Disaggregated/TFF data and audit each contract.

Default destinations: COT, COT2, COT_Audit, COT2_Audit. Existing CLI options and
raw data are preserved. The default year follows the UTC calendar automatically.
Use --dry-run to download/audit without Google credentials or spreadsheet writes.

Audit scope is the selected annual file, not a merged historical archive. Expected
dates are dates observed anywhere in that source from a contract's first valid
observation onwards. Pre-listing dates are excluded; discontinued contracts can
still be flagged as lagging. A wholly absent contract cannot be detected from one
file. Source-wide missing weeks are reported separately as calendar gaps; they
cannot be inferred as exact missing source dates. SOURCE_DELAYED means observation
age exceeds --stale-after-days (default 10), not a verified CFTC publication delay.
Historical --year runs deliberately still measure age against today.
"""

from __future__ import annotations''')
s = s.replace('from urllib.parse import urljoin', 'from urllib.parse import urljoin, urlparse')
s = s.replace('from google.oauth2.service_account import Credentials\nfrom googleapiclient.discovery import build', 'from requests.adapters import HTTPAdapter\nfrom urllib3.util.retry import Retry')
start = s.index('def _looks_like_report_link')
end = s.index('def _read_delimited_text', start)
s = s[:start] + '''def _http_session() -> requests.Session:
    """Retry transient GET failures; never treat permission/network errors as 404."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


HTTP = _http_session()


def _extract_year_candidates(text: str, href: str) -> List[int]:
    """Use digit boundaries: underscores in filenames are word characters."""
    return sorted({int(y) for y in re.findall(r"(?<!\\d)([12]\\d{3})(?!\\d)", f"{text} {href}")})


def _report_stem(section_label: str) -> str:
    if "disaggregated" in section_label.lower():
        return "fut_disagg"
    if "traders in financial futures" in section_label.lower():
        return "fut_fin"
    raise ValueError(f"Unknown report section: {section_label}")


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
        if not prefix.startswith(b"PK\\x03\\x04"):
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
            match = re.fullmatch(rf"{stem}_(txt|xls)_([12]\\d{{3}})\\.zip", filename)
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


''' + s[end:]
start = s.index('def _read_delimited_text')
end = s.index('def to_sheet_values', start)
s = s[:start] + '''def _read_delimited_text(path: Path) -> pd.DataFrame:
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
            first_sep = csv.Sniffer().sniff(sample, delimiters=",\\t;|").delimiter
        except csv.Error:
            first_sep = ","
        for sep in dict.fromkeys([first_sep, ",", "\\t", ";", "|"]):
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


''' + s[end:]
s = s.replace('    creds = Credentials.from_service_account_file', '''    # Lazy imports permit --dry-run and offline audit testing without Google libs.
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file''')
s = s.replace('range=f"\'{safe_sheet_name}\'!A:ZZ"', 'range=f"\'{safe_sheet_name}\'"')
s = s.replace('cell_range = f"\'{safe_sheet_name}\'!A{start_row}:ZZ{end_row}"', 'cell_range = f"\'{safe_sheet_name}\'!A{start_row}"')
# Retrying value writes/reads is safe; creation deliberately remains non-retried.
s = s.replace(').execute()\n        start_row', ').execute(num_retries=3)\n        start_row')
s = s.replace('"Download CFTC Disaggregated Futures Only report Excel for a year "\n            "and push into a Google Sheet tab."', '"Download and audit annual CFTC Disaggregated and TFF futures-only reports."')
s = s.replace('    return parser.parse_args()', '''    parser.add_argument("--dry-run", action="store_true", help="Download/audit without writing Google Sheets")
    parser.add_argument("--stale-after-days", type=int, default=10,
                        help="Flag source observation age above this many days (default: 10)")
    args = parser.parse_args()
    if args.stale_after_days < 0:
        parser.error("--stale-after-days must be nonnegative")
    names = [args.disagg_sheet_name, args.tff_sheet_name,
             f"{args.disagg_sheet_name}_Audit", f"{args.tff_sheet_name}_Audit"]
    if len(set(n.casefold() for n in names)) != 4:
        parser.error("Raw and audit sheet names must all be distinct")
    return args''')
s = s.replace('if not service_account_path.exists():', 'if not args.dry_run and not service_account_path.is_file():')
start = s.index('    for section_label, target_sheet in jobs:')
end = s.index('\n\nif __name__', start)
s = s[:start] + '''    # Prepare and validate both datasets before changing any destination tab.
    prepared = []
    for section_label, target_sheet in jobs:
        report_link = fetch_section_report_link(section_label, args.year)
        print(f"[{section_label}] Found {report_link.year} report link: {report_link.url}")
        df = download_report_to_dataframe(report_link.url)
        dates = _report_dates(df)
        valid = dates.dropna()
        if valid.empty or not valid.dt.year.eq(report_link.year).all():
            raise RuntimeError(f"Report dates do not match selected year {report_link.year}.")
        normalized = {_normalize_column_name(c) for c in df.columns}
        marker = "prodmercpositionslongall" if "disaggregated" in section_label.lower() else "dealerpositionslongall"
        if marker not in normalized:
            raise RuntimeError(f"Unexpected dataset schema for {section_label}; refusing to upload.")
        audit_df, summary = build_data_quality_audit(df, stale_after_days=args.stale_after_days)
        audit_df["Source_Year"] = report_link.year
        audit_df["Source_URL"] = report_link.url
        print_audit_summary(target_sheet, audit_df, summary)
        prepared.append((target_sheet, df, audit_df))

    if args.dry_run:
        print("Dry run complete: no Google Sheet changes made.")
        return
    for target_sheet, df, audit_df in prepared:
        for sheet_name, frame in ((target_sheet, df), (f"{target_sheet}_Audit", audit_df)):
            write_to_google_sheet(
                spreadsheet_id=args.spreadsheet_id, sheet_name=sheet_name,
                values=to_sheet_values(frame), service_account_path=str(service_account_path),
            )
            print(f"Uploaded {len(frame)} rows (+header) to '{sheet_name}'.")
''' + s[end:]
path.write_text(s, encoding='utf-8')
