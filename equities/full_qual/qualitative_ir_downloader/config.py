"""Configuration shared by collectors; companies always run sequentially."""
from dataclasses import dataclass, field
from pathlib import Path
from .events_config import EventConfig

DEFAULT_SERVICE_ACCOUNT_PATH = Path(r"C:\Code\service_account.json")
SPREADSHEET_ID = "1QF67DQEDF5QB8-q1JCuzyYPXSPJugxCMQ8DbUhOo_1c"
WORKSHEETS = ("Safe", "High Growth Potential", "Turnaround Story")
HEADLESS = True
REQUEST_DELAY_SECONDS = 1.0
NAVIGATION_TIMEOUT_MS = 45000
MAX_RETRIES = 3
DOWNLOAD_ROOT = Path.home() / "Downloads"
HTTP_CONNECT_TIMEOUT_SECONDS = 10.0
HTTP_READ_TIMEOUT_SECONDS = 30.0
HTTP_TOTAL_TIMEOUT_SECONDS = 45.0
ACQUISITION_NO_PROGRESS_TIMEOUT_SECONDS = 900.0
ACQUISITION_DEADLINE_SECONDS = 7200.0
MAX_REDIRECTS = 8
MAX_OFFICIAL_ROUTES = 24

@dataclass(frozen=True)
class Config:
    events: EventConfig = field(default_factory=EventConfig)
    service_account_path: Path = DEFAULT_SERVICE_ACCOUNT_PATH
    spreadsheet_id: str = SPREADSHEET_ID
    download_root: Path = field(default_factory=lambda: DOWNLOAD_ROOT)
    request_delay: float = REQUEST_DELAY_SECONDS
    navigation_timeout_ms: int = NAVIGATION_TIMEOUT_MS
    retries: int = MAX_RETRIES
    http_connect_timeout: float = HTTP_CONNECT_TIMEOUT_SECONDS
    http_read_timeout: float = HTTP_READ_TIMEOUT_SECONDS
    http_total_timeout: float = HTTP_TOTAL_TIMEOUT_SECONDS
    no_progress_timeout: float = ACQUISITION_NO_PROGRESS_TIMEOUT_SECONDS
    acquisition_deadline: float = ACQUISITION_DEADLINE_SECONDS
    stage_timeout: float = 900.0
    max_redirects: int = MAX_REDIRECTS
    max_official_routes: int = MAX_OFFICIAL_ROUTES
    max_archive_pages: int = 250
    max_links_per_company: int = 10000
    max_interactions: int = 500
    max_pdf_bytes: int = 150 * 1024 * 1024
    max_discovery_candidates: int = 15
    max_section_pages: int = 20
    revalidate_ir: bool = False
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
