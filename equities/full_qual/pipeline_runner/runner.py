from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from .models import AcquisitionResult, PipelineRunResult, StageResult, StageStatus
from .issuer_resolution import IssuerMetadata, IssuerResolutionError, resolve_issuer_metadata
from .compatibility import (ArtifactCompatibilityStore, CompatibilityDecision,
                            STAGE_CONTRACTS, configuration_fingerprint,
                            canonical_artifact_fingerprint, dependency_fingerprint,
                            implementation_fingerprint_for)
from management_intelligence.governance_store import GovernanceStore
from qualitative_analysis.extraction_safety import looks_like_pdf_corruption
from qualitative_ir_downloader.google_sheets_publisher import (
    DEFAULT_QUALITATIVE_SPREADSHEET_ID,
    GoogleSheetsPublisher,
)


MODULES = {
    "acquisition": "qualitative_ir_downloader.main",
    "qualitative": "qualitative_analysis.main",
    "insider": "insider_intelligence.main",
    "management": "management_intelligence.main",
    "track_record": "management_intelligence.track_record_cli",
    "governance": "management_intelligence.governance_cli",
    "scoring": "scoring_engine.main",
    "dossier": "research_output.main",
    "validation": "empirical_validation.main",
}
STAGE_ORDER = (
    "acquisition", "normalization", "qualitative_extraction", "temporal_comparison",
    "state_synthesis", "insider_intelligence", "management_intelligence",
    "management_track_record", "governance_intelligence", "scoring", "dossier_generation",
    "google_sheets_publication",
)
EXIT_SUCCESS = 0
EXIT_UNEXPECTED_FAILURE = 1
EXIT_ACQUISITION_FAILURE = 2
EXIT_NORMALIZATION_FAILURE = 3
EXIT_INVALID_CONFIGURATION = 4
EXIT_QUALITY_GATE_FAILURE = 5
EXIT_GOOGLE_SHEETS_FAILURE = 6
STAGE_NUMBERS = {name: index for index, name in enumerate(STAGE_ORDER, start=1)}
DEFAULT_DATA_ROOT = Path(r"C:\NAllenAI\Data\Microeconomics")
FOLDER_NAMES = {
    "acquisition": "01_Acquisition",
    "documents": "02_Normalized_Documents",
    "claims": "03_Claims",
    "temporal": "04_Temporal_Analysis",
    "states": "05_Qualitative_State",
    "insider": "06_Insider_Intelligence",
    "management": "07_Management_Intelligence",
    "governance": "08_Board_Governance",
    "scoring": "09_Scoring",
    "dossier": "10_Research_Dossier",
    "summaries": "11_Run_Summaries",
    "google_sheets": "12_Google_Sheets",
}


def _run_module(module: str, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a component with continuously drained pipes and a hard bound."""
    command = [sys.executable, "-m", module, *args]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=creationflags)
    stdout: list[str] = []
    stderr: list[str] = []

    def drain(stream, target: list[str]) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            target.append(line)
        stream.close()

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        return_code = process.wait(timeout=timeout)
    except KeyboardInterrupt:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, text=True, check=False)
        else:
            process.kill()
        stderr.append("INTERRUPTED: acquisition child terminated by parent\n")
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        raise
    except subprocess.TimeoutExpired:
        # Terminate the complete child tree.  A single hung browser/media
        # child must never leave the runner waiting on a pipe or orphan.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, text=True, check=False)
        else:
            process.kill()
        return_code = 124
        stderr.append(f"ACQUISITION_DEADLINE_EXCEEDED after {timeout}s\n")
    stdout_thread.join()
    stderr_thread.join()
    return subprocess.CompletedProcess(command, return_code, "".join(stdout), "".join(stderr))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _ticker_match(row: dict[str, Any], ticker: str) -> bool:
    ticker = ticker.upper()
    for key in ("ticker", "issuer_ticker", "board_ticker", "employer_ticker", "target_ticker"):
        if str(row.get(key) or "").upper() == ticker:
            return True
    # Some historical stores identify an issuer only through a nested company object.
    for value in row.values():
        if isinstance(value, dict) and _ticker_match(value, ticker):
            return True
    return False


def _rows(root: Path, relative: str, ticker: str) -> list[dict[str, Any]]:
    return [row for row in _read_jsonl(root / relative) if _ticker_match(row, ticker)]


def _count(root: Path, relative: str, ticker: str) -> int:
    return len(_rows(root, relative, ticker))


def _git_commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], text=True, capture_output=True, check=False)
        return result.stdout.strip() or "UNKNOWN"
    except OSError:
        return "UNKNOWN"


class PipelineRunner:
    """Run existing component entry points without owning their analytical logic."""

    def __init__(
        self,
        ticker: str,
        as_of_date: str,
        profile: str = "core_v1",
        mode: str = "existing",
        skip_acquisition: bool = False,
        run_validation_audit: bool = False,
        artifacts_root: str | Path = "artifacts",
        download_root: str | Path | None = None,
        config_root: str | Path = "config/scoring",
        output_root: str | Path = "artifacts/research_output",
        validation_config: str | Path = "config/calibration/validation_v1.json",
        data_root: str | Path = DEFAULT_DATA_ROOT,
        clean_output: bool = False,
        rebuild_analysis: bool = False,
        acquisition_timeout: float = 7200.0,
        executor: Callable[[str, list[str]], Any] | None = None,
        verbose: bool = False,
        manual_ticker: bool = False,
        google_sheets: bool | None = None,
        google_spreadsheet_id: str | None = None,
        google_service_account: str | Path | None = None,
        google_sheets_publisher: Any | None = None,
    ) -> None:
        if mode not in {"existing", "fresh"}:
            raise ValueError("mode must be 'existing' or 'fresh'")
        self.ticker = ticker.upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,15}", self.ticker):
            raise ValueError("ticker must be a simple symbol without path separators")
        self.as_of_date = as_of_date
        self.profile = profile
        self.mode = mode
        self.skip_acquisition = skip_acquisition
        self.run_validation_audit = run_validation_audit
        self.artifacts_root = Path(artifacts_root)
        self.download_root = Path(download_root) if download_root else Path.home() / "Downloads"
        self.config_root = Path(config_root)
        self.output_root = Path(output_root)
        self.validation_config = Path(validation_config)
        self.data_root = Path(data_root)
        self.clean_output = clean_output
        self.rebuild_analysis = bool(rebuild_analysis)
        self.acquisition_timeout = float(acquisition_timeout)
        self.verbose = verbose
        # The CLI sets this for an explicit --ticker. Batch --tickers runs
        # retain worksheet selection semantics in the acquisition component.
        self.manual_ticker = bool(manual_ticker)
        # Direct library callers retain the historical no-external-side-effect
        # default.  The CLI explicitly passes True unless --no-google-sheets
        # is supplied, making publication the normal command-line behavior.
        self.google_sheets_enabled = google_sheets is True
        self.google_spreadsheet_id = google_spreadsheet_id or DEFAULT_QUALITATIVE_SPREADSHEET_ID
        # Keep None when no CLI override was supplied so the publisher can
        # apply CLI > FULL_QUAL_GOOGLE_SERVICE_ACCOUNT > default precedence.
        self.google_service_account = Path(google_service_account) if google_service_account else None
        self._google_sheets_publisher = google_sheets_publisher
        self._publication_summary: dict[str, Any] = {}
        self._google_sheets_status = "NOT_REQUESTED"
        self._research_status = "SUCCESS"
        self.ticker_root = self.data_root / self.ticker
        self.production_dossier_root = self.ticker_root / FOLDER_NAMES["dossier"]
        self.production_pipeline_root = self.ticker_root / FOLDER_NAMES["summaries"] / "Pipeline"
        self.production_download_root = self.ticker_root / FOLDER_NAMES["acquisition"]
        if self.mode == "fresh":
            resolved_cache = self.download_root.expanduser().resolve(strict=False)
            resolved_data_root = self.data_root.expanduser().resolve(strict=False)
            if resolved_cache == self.production_download_root.resolve(strict=False) or resolved_data_root in resolved_cache.parents:
                raise ValueError("Fresh-mode acquisition cache must be outside the production data root")
        self.executor = executor or _run_module
        self._default_executor = executor is None
        self._stages: list[StageResult] = []
        self._warnings: list[str] = []
        self._current_stage = ""
        self._active_stage = ""
        self._manifest_ready = False
        self._clean_output_status = "NOT_RUN"
        self._failure_stage = ""
        self._failure_reason = ""
        self._exit_code = EXIT_SUCCESS
        self._run_started = time.perf_counter()
        self._git_commit_value = _git_commit()
        self._acquisition_result: AcquisitionResult | None = None
        self._issuer_metadata: IssuerMetadata | None = None
        self._compatibility = ArtifactCompatibilityStore(self.artifacts_root)
        self._compatibility_decisions: dict[str, CompatibilityDecision] = {}
        self._run_id = f"pipeline-run:{self.ticker}:{self.as_of_date}:{uuid.uuid4().hex[:12]}"
        self._failed_dependencies: set[str] = set()
        # Stages executed earlier in this run only invalidate direct
        # dependants when their canonical output changed.  This gives the
        # runner transitive invalidation without turning every execution into
        # a rebuild.
        self._changed_stages: set[str] = set()
        self._canonical_identity_changed = False

    def _safe_clean_target(self) -> None:
        root = self.data_root.expanduser().resolve(strict=False)
        target = self.ticker_root.expanduser().resolve(strict=False)
        unsafe = {
            Path("C:\\").resolve(strict=False),
            Path(r"C:\NAllenAI").resolve(strict=False),
            Path(r"C:\NAllenAI\Data").resolve(strict=False),
            root,
        }
        if target in unsafe or target.parent != root:
            raise ValueError(f"Refusing to clean unsafe production path: {target}")
        if self.ticker_root.is_symlink():
            raise ValueError(f"Refusing to clean symlinked production path: {self.ticker_root}")
        if self.ticker_root.exists():
            shutil.rmtree(self.ticker_root)

    def acquisition_invocation(self, issuer: IssuerMetadata | None = None) -> tuple[str, list[str]]:
        """Return the frozen acquisition CLI invocation without executing it."""
        args = [
            "--ticker", self.ticker,
            "--download-root", str(self.download_root),
            "--acquisition-deadline", str(self.acquisition_timeout),
            "--as-of-date", self.as_of_date,
        ]
        if issuer is not None:
            args.extend(["--company-name", issuer.company_name])
            if issuer.cik:
                args.extend(["--issuer-cik", issuer.cik])
        return MODULES["acquisition"], args

    def _resolve_manual_issuer(self) -> IssuerMetadata:
        if self._issuer_metadata is None:
            self._issuer_metadata = resolve_issuer_metadata(
                self.ticker,
                download_root=self.download_root,
                data_root=self.data_root,
                as_of_date=self.as_of_date,
            )
        return self._issuer_metadata

    def _persist_issuer_identity(self) -> None:
        """Persist the resolved issuer identity in a ticker-scoped sidecar.

        Analytical stages remain independent of the runner, but downstream
        dossier/publisher code can consume this authoritative identity without
        consulting a global worksheet or a previous ticker's state.
        """
        if self._issuer_metadata is None:
            return
        path = self.artifacts_root / "issuer_identity" / f"{self.ticker}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                previous_cik = str(previous.get("issuer_cik") or previous.get("sec_registrant_cik") or "").zfill(10)
                current_cik = str(self._issuer_metadata.cik or "").zfill(10)
                previous_hash = str(previous.get("identity_hash") or "")
                if previous_cik and current_cik and previous_cik != current_cik:
                    self._canonical_identity_changed = True
                elif previous_hash and previous_hash != self._issuer_metadata.identity_hash:
                    self._canonical_identity_changed = True
            except (OSError, UnicodeError, json.JSONDecodeError):
                self._canonical_identity_changed = True
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._issuer_metadata.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _mark_interrupted_acquisition(self, reason: str) -> None:
        """Close a child checkpoint when the parent had to terminate it."""
        candidates = sorted(self.download_root.glob(f"{self.ticker}_*/manifest.json"),
                            key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if str(payload.get("status") or "") not in {"IN_PROGRESS", "RUNNING", "INTERRUPTED"}:
                    continue
                payload["status"] = "INTERRUPTED"
                payload["acquisition_status"] = reason
                payload.setdefault("errors", []).append({"code": reason, "message": "Parent terminated acquisition child"})
                temporary = path.with_suffix(path.suffix + ".part")
                temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                temporary.replace(path)
                progress_path = path.with_name("acquisition_progress.json")
                if progress_path.exists():
                    progress = json.loads(progress_path.read_text(encoding="utf-8"))
                    progress["status"] = "INTERRUPTED"
                    progress["interruption_reason"] = reason
                    progress_temp = progress_path.with_suffix(progress_path.suffix + ".part")
                    progress_temp.write_text(json.dumps(progress, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    progress_temp.replace(progress_path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue

    def _stage_label(self, name: str) -> str:
        return name.replace("_", " ").upper()

    def _stage_total(self) -> int:
        return len(STAGE_ORDER) if self.google_sheets_enabled else len(STAGE_ORDER) - 1

    def _announce_start(self, name: str) -> None:
        self._current_stage = name
        self._active_stage = name
        position = STAGE_NUMBERS.get(name)
        if position is None:
            return
        print(f"[{position}/{self._stage_total()}] {self._stage_label(name)} — STARTED", flush=True)
        if name == "acquisition":
            print(f"Ticker: {self.ticker}", flush=True)
            print(f"Mode: {self.mode}", flush=True)
        elif name == "normalization":
            handoff = self._acquisition_result
            if handoff is not None:
                print(f"Ticker: {self.ticker}", flush=True)
                print(f"Source root: {handoff.download_root}", flush=True)
                print(f"Manifests: {len(handoff.manifest_paths)}", flush=True)
                print(f"Eligible source artifacts: {handoff.eligible_artifacts}", flush=True)
            print(f"Production output: {self.ticker_root / FOLDER_NAMES['documents']}", flush=True)
        if self.verbose:
            if name == "normalization" and self._acquisition_result is not None:
                print(f"Input artifacts: {self._acquisition_result.download_root}")
            else:
                print(f"Input artifacts: {self.artifacts_root}")
            print(f"Production root: {self.ticker_root}")
        self._write_manifest_snapshot()

    def _announce_heartbeat(self, name: str, started: float, stop: threading.Event) -> None:
        position = STAGE_NUMBERS.get(name, 0)
        while not stop.wait(60):
            elapsed = int(time.perf_counter() - started)
            progress = None
            try:
                from qualitative_ir_downloader.reliability import load_progress
                candidates = sorted(self.download_root.glob(f"{self.ticker}_*/acquisition_progress.json"),
                                    key=lambda path: path.stat().st_mtime, reverse=True)
                if candidates:
                    progress = load_progress(candidates[0])
            except (OSError, ValueError, TypeError):
                progress = None
            if progress:
                counters = progress.get("counters") or {}
                no_progress = progress.get("no_progress_for_seconds", 0)
                print(
                    f"[{position}/{self._stage_total()}] ACQUISITION "
                    f"substage={progress.get('current_substage') or 'UNKNOWN'} "
                    f"elapsed={elapsed}s no_progress_for={int(no_progress)}s "
                    f"processed={counters.get('processed', 0)} saved={counters.get('saved', 0)} "
                    f"reused={counters.get('reused', 0)} failed={counters.get('failed', 0)}",
                    flush=True,
                )
            else:
                print(f"[{position}/{self._stage_total()}] ACQUISITION NO_PROGRESS_FOR={elapsed}s (no checkpoint yet)", flush=True)

    def _manifest_path(self) -> Path:
        return self.production_pipeline_root / f"{self.ticker}_{self.as_of_date}_pipeline_run.json"

    def _reported_dossier_paths(self, profile_version: str) -> tuple[str | None, str | None]:
        """Return only dossier files produced/reused by the current run."""
        dossier_stage = next((stage for stage in self._stages if stage.name == "dossier_generation"), None)
        if dossier_stage is None or dossier_stage.status not in {StageStatus.SUCCESS, StageStatus.REUSED}:
            return None, None
        dossier_json, dossier_md = self._dossier_paths(profile_version)
        return (
            str(dossier_json) if dossier_json.exists() else None,
            str(dossier_md) if dossier_md.exists() and self.profile == "core_v1" else None,
        )

    def _manifest_payload(self, *, final_status: StageStatus | str = "RUNNING", exit_code: int | None = None) -> dict[str, Any]:
        profile_version = self._profile_version()
        dossier_json_value, dossier_md_value = self._reported_dossier_paths(profile_version)
        payload: dict[str, Any] = {
            "ticker": self.ticker,
            "as_of_date": self.as_of_date,
            "profile": self.profile,
            "profile_version": profile_version,
            "mode": self.mode,
            "final_status": final_status.value if isinstance(final_status, StageStatus) else final_status,
            "stages": [stage.to_dict() for stage in self._stages],
            "warnings": list(dict.fromkeys(self._warnings)),
            "manifest_path": str(self._manifest_path()),
            "dossier_paths": {"json": dossier_json_value, "markdown": dossier_md_value},
            "production_root": str(self.ticker_root),
            "source_artifacts_root": str(self.artifacts_root),
            "acquisition_cache_root": str(self.download_root),
            "clean_output": self._clean_output_status,
            "validation_audit_requested": self.run_validation_audit,
            "production_layout": {key: str(self.ticker_root / folder) for key, folder in FOLDER_NAMES.items()},
            "exit_code": self._exit_code if exit_code is None else exit_code,
            "failure_stage": self._failure_stage,
            "failure_reason": self._failure_reason,
            "research_status": self._research_status,
            "google_sheets_status": self._google_sheets_status,
            "google_sheets_publication": dict(self._publication_summary),
            "active_stage": self._active_stage,
            "acquisition_result": self._acquisition_result.to_dict() if self._acquisition_result else None,
            "issuer_resolution": self._issuer_metadata.to_dict() if self._issuer_metadata else None,
        }
        return payload

    def _write_manifest_snapshot(self, *, final_status: StageStatus | str = "RUNNING", exit_code: int | None = None) -> None:
        if not self._manifest_ready:
            return
        manifest = self._manifest_path()
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = self._manifest_payload(final_status=final_status, exit_code=exit_code)
        payload["git_commit"] = self._git_commit_value
        payload["versions"] = {
            "pipeline_runner": "pipeline-runner-v2",
            "requested_profile": self.profile,
            "research_output": "research-output-v2" if self.profile != "core_v1" else "research-output-v1",
        }
        payload["compatibility"] = {stage: decision.to_dict() for stage, decision in self._compatibility_decisions.items()}
        manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _append_not_run_stages(self, after: str) -> None:
        try:
            start = STAGE_ORDER.index(after) + 1
        except ValueError:
            start = 0
        for name in STAGE_ORDER[start:]:
            if name == "google_sheets_publication" and not self.google_sheets_enabled:
                continue
            if any(stage.name == name for stage in self._stages):
                continue
            self._stages.append(StageResult(name=name, status=StageStatus.NOT_RUN,
                                             warnings=[f"Not run because {after} failed"], change="REUSED"))

    def _build_result(self, final_status: StageStatus, *, exit_code: int | None = None) -> PipelineRunResult:
        profile_version = self._profile_version()
        dossier_json_value, dossier_md_value = self._reported_dossier_paths(profile_version)
        return PipelineRunResult(
            self.ticker, self.as_of_date, self.profile, profile_version, self.mode,
            list(self._stages), list(dict.fromkeys(self._warnings)), str(self._manifest_path()),
            dossier_json_value or "", dossier_md_value or "", final_status,
            time.perf_counter() - self._run_started, str(self.ticker_root), "",
            self._exit_code if exit_code is None else exit_code,
            self._failure_stage, self._failure_reason,
            self._research_status, self._google_sheets_status, dict(self._publication_summary),
        )

    def _finish_failure(self, stage: str, reason: str, exit_code: int) -> PipelineRunResult:
        self._failure_stage = stage
        self._failure_reason = reason
        self._exit_code = exit_code
        self._append_not_run_stages(stage)
        if self._manifest_ready:
            try:
                self._materialize_acquisition()
                self._materialize_acquisition_summaries()
            except OSError as exc:
                self._warnings.append(f"Failed to materialize partial acquisition output: {exc}")
        self._write_manifest_snapshot(final_status=StageStatus.FAILED, exit_code=exit_code)
        return self._build_result(StageStatus.FAILED, exit_code=exit_code)

    def _report_unexpected_exception(self, exc: BaseException) -> None:
        print("PIPELINE FAILED")
        print(f"Stage: {self._current_stage or 'orchestration'}")
        print(f"Exception type: {type(exc).__name__}")
        print(f"Message: {exc}")
        if self.verbose:
            traceback.print_exc()

    def _ensure_production_layout(self) -> None:
        if self.clean_output:
            print("Cleaning production output:", flush=True)
            print(str(self.ticker_root.resolve(strict=False)), flush=True)
            print("\nPreserving acquisition/cache data outside target output:", flush=True)
            print("YES", flush=True)
            self._safe_clean_target()
            self.clean_output = False
            self._clean_output_status = "SUCCESS"
        elif self._clean_output_status == "NOT_RUN":
            self._clean_output_status = "NOT_REQUESTED"
        for key in FOLDER_NAMES:
            (self.ticker_root / FOLDER_NAMES[key]).mkdir(parents=True, exist_ok=True)
        for child in ("News_Releases", "Reports", "Events", "Transcripts"):
            (self.production_download_root / child).mkdir(parents=True, exist_ok=True)
        for child in ("Pipeline", "Production", "Excel_Analysis"):
            (self.ticker_root / FOLDER_NAMES["summaries"] / child).mkdir(parents=True, exist_ok=True)
        if self.verbose:
            print(f"Resolved acquisition cache: {self.download_root}")
            print(f"Resolved production root: {self.ticker_root}")

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
        path.write_text(content, encoding="utf-8")
        return True

    def _materialize_jsonl(self, relative: str, destination: Path) -> int:
        rows = _rows(self.artifacts_root, relative, self.ticker)
        if not rows:
            return 0
        content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        self._write_if_changed(destination, content)
        return len(rows)

    def _materialize_folder_jsonl(self, source_folder: str, destination: Path, names: tuple[str, ...]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in names:
            counts[name[:-6]] = self._materialize_jsonl(f"{source_folder}/{name}", destination / name)
        return counts

    def _materialize_acquisition(self) -> None:
        if not self.download_root.exists():
            return
        same_root = self.download_root.resolve(strict=False) == self.production_download_root.resolve(strict=False)
        category_names = {
            "News Releases": "News_Releases",
            "News_Releases": "News_Releases",
            "Reports": "Reports",
            "Events": "Events",
            "Transcripts": "Transcripts",
        }
        for candidate in self.download_root.iterdir():
            if not candidate.is_dir() or not candidate.name.upper().startswith(self.ticker + "_"):
                continue
            manifest_path = candidate / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                manifest = {}
            manifest_ticker = str(manifest.get("ticker") or "").upper()
            if manifest_ticker and manifest_ticker != self.ticker:
                continue
            target = self.production_download_root / candidate.name
            if not same_root:
                if target.exists():
                    shutil.copytree(candidate, target, dirs_exist_ok=True)
                else:
                    shutil.copytree(candidate, target)
            # Keep the downloader's resumable company folder intact, and also
            # expose canonical category views at the production root.  The
            # views are namespaced by company folder so files from separate
            # runs cannot collide.
            source = target if same_root else candidate
            for source_name, category in category_names.items():
                source_dir = source / source_name
                if source_dir.exists() and source_dir.is_dir():
                    category_target = self.production_download_root / category / candidate.name
                    shutil.copytree(source_dir, category_target, dirs_exist_ok=True)

    def _materialize_acquisition_summaries(self) -> None:
        """Keep downloader run summaries out of ``01_Acquisition`` itself."""
        destination = self.ticker_root / FOLDER_NAMES["summaries"] / "Production"
        patterns = (
            "qualitative_analysis_run_summary_*.json",
            "qualitative_analysis_run_summary_*.csv",
            "production_run_summary_*.json",
            "generated_search_summary_*.json",
        )
        for pattern in patterns:
            for source in self.production_download_root.glob(pattern):
                target = destination / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.suffix.lower() == ".json":
                    self._write_if_changed(target, source.read_text(encoding="utf-8"))
                else:
                    shutil.copy2(source, target)
                source.unlink()

    def _materialize_outputs(self) -> dict[str, Any]:
        self._ensure_production_layout()
        self._materialize_acquisition()
        self._materialize_acquisition_summaries()
        qa = self.ticker_root / FOLDER_NAMES["documents"]
        qb = self.ticker_root / FOLDER_NAMES["claims"]
        qt = self.ticker_root / FOLDER_NAMES["temporal"]
        qs = self.ticker_root / FOLDER_NAMES["states"]
        counts = {
            "documents": self._materialize_jsonl("qualitative_analysis/documents.jsonl", qa / "documents.jsonl"),
            "claims": self._materialize_jsonl("qualitative_analysis/claims.jsonl", qb / "claims.jsonl"),
            "temporal_changes": self._materialize_jsonl("qualitative_analysis/temporal_changes.jsonl", qt / "temporal_changes.jsonl"),
            "states": self._materialize_jsonl("qualitative_analysis/qualitative_states.jsonl", qs / "qualitative_states.jsonl"),
        }
        for key, folder, names in (
            ("insider", FOLDER_NAMES["insider"], ("people.jsonl", "transactions.jsonl", "ownership.jsonl", "filings.jsonl", "ownership_baselines.jsonl", "ownership_reconciliations.jsonl", "alignment_snapshots.jsonl", "clusters.jsonl", "insider_links.jsonl")),
            ("management", FOLDER_NAMES["management"], ("people.jsonl", "roles.jsonl", "career_history.jsonl", "education.jsonl", "sources.jsonl", "relationships.jsonl", "role_changes.jsonl", "runs.jsonl", "tenures.jsonl", "financial_facts.jsonl", "guidance_commitments.jsonl", "guidance_outcomes.jsonl", "guidance_coverage_diagnostics.jsonl", "strategic_commitments.jsonl", "strategic_outcomes.jsonl", "capital_allocation_events.jsonl", "operating_outcomes.jsonl", "track_record_snapshots.jsonl", "track_record_runs.jsonl")),
            ("governance", FOLDER_NAMES["governance"], ("board_history.jsonl", "governance_board_memberships.jsonl", "governance_committees.jsonl", "governance_snapshots.jsonl", "governance_coverage.jsonl", "governance_events.jsonl", "governance_expertise.jsonl", "governance_overboarding.jsonl", "governance_refreshment.jsonl", "governance_related_parties.jsonl", "governance_shareholder_rights.jsonl", "governance_voting_classes.jsonl", "governance_voting_control.jsonl", "governance_runs.jsonl", "governance_tenure.jsonl")),
        ):
            counts[key] = self._materialize_folder_jsonl("insider_intelligence" if key == "insider" else "management_intelligence", self.ticker_root / folder, names)
        scoring_dir = self.ticker_root / FOLDER_NAMES["scoring"]
        counts["scoring"] = {}
        for name in ("factor_scores.jsonl", "pillar_scores.jsonl", "company_scores.jsonl", "score_runs.jsonl"):
            counts["scoring"][name[:-6]] = self._materialize_jsonl(f"scoring_engine/{name}", scoring_dir / name)
        source_json, source_md = self._source_dossier_paths(self._profile_version())
        target_json = self.production_dossier_root / source_json.name
        target_md = self.production_dossier_root / source_md.name
        for source, target in ((source_json, target_json), (source_md, target_md)):
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for key, filename in (("documents", "document_index.json"), ("claims", "claim_index.json"), ("temporal_changes", "temporal_index.json"), ("states", "state_index.json")):
            folder = {"documents": qa, "claims": qb, "temporal_changes": qt, "states": qs}[key]
            source_name = {"documents": "documents.jsonl", "claims": "claims.jsonl", "temporal_changes": "temporal_changes.jsonl", "states": "qualitative_states.jsonl"}[key]
            index = {"ticker": self.ticker, "count": counts[key], "source": str(folder / source_name)}
            (folder / filename).write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        return {"counts": counts, "dossier_json": str(target_json), "dossier_markdown": str(target_md)}

    @staticmethod
    def _record_artifact_path(company_root: Path, record: dict[str, Any]) -> Path | None:
        """Resolve a downloader record without searching outside its company folder."""
        for key in ("local_filename", "local_pdf", "local_json", "retained_media"):
            value = record.get(key)
            if not value:
                continue
            path = Path(str(value))
            if not path.is_absolute():
                path = company_root / path
            if path.exists() and path.is_file():
                return path
        return None

    def _company_manifest_dirs(self) -> list[Path]:
        root = self.download_root
        if not root.exists() or not root.is_dir():
            return []
        candidates = [root] if (root / "manifest.json").exists() else [
            path for path in root.iterdir() if path.is_dir() and (path / "manifest.json").exists()
        ]
        selected: list[tuple[Path, dict[str, Any]]] = []
        for path in candidates:
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            manifest_ticker = str(manifest.get("ticker") or "").upper()
            if manifest_ticker == self.ticker:
                selected.append((path, manifest))
        selected.sort(key=lambda item: (str(item[1].get("run_timestamp") or ""), item[0].stat().st_mtime, item[0].name), reverse=True)
        return [path for path, _ in selected]

    def _critical_source_handoff_complete(self) -> bool:
        """Return whether an interrupted acquisition left a usable critical corpus.

        A hard parent timeout can terminate the browser after the critical
        annual/interim/governance/management checkpoint has been persisted.
        In that case the cache is a valid partial handoff and analytical
        stages should continue; broad archive incompleteness is represented as
        a limitation rather than destroying the usable source handoff.
        """
        for company_dir in self._company_manifest_dirs():
            try:
                payload = json.loads((company_dir / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            coverage = payload.get("critical_source_coverage") or {}
            if bool(coverage.get("minimum_research_corpus_acquired")) or str(payload.get("critical_source_coverage_status") or "") == "COMPLETE":
                return True
        return False

    def _resolve_acquisition_result(self, *, allow_local_fallback: bool | None = None) -> AcquisitionResult:
        """Resolve the current ticker's acquisition handoff from manifests.

        The downloader owns cache layout and semantics.  The runner only reads
        its manifests and uses the parent cache root with an explicit ticker
        selector for normalization; it never recursively ingests the whole
        external download directory.
        """
        if allow_local_fallback is None:
            # Fresh acquisition must be grounded in the cache/manifests just
            # resolved by acquisition.  Existing mode and an explicit
            # acquisition skip may intentionally operate on an established
            # local store.
            allow_local_fallback = self.mode != "fresh" or self.skip_acquisition
        company_dirs = self._company_manifest_dirs()
        if not company_dirs:
            local_counts = self._qual_counts()
            if allow_local_fallback and local_counts.get("documents", 0):
                return AcquisitionResult(
                    ticker=self.ticker,
                    status="LOCAL_ARTIFACTS",
                    download_root=str(self.artifacts_root),
                    company_acquisition_root=str(self.artifacts_root),
                    record_counts={**local_counts, "eligible_source_artifacts": local_counts["documents"]},
                    warnings=["No downloader manifest was found; using explicitly supplied local artifacts"],
                )
            return AcquisitionResult(
                ticker=self.ticker,
                status="NO_SOURCE",
                download_root=str(self.download_root),
                record_counts={"eligible_source_artifacts": 0},
            )

        manifest_paths = [path / "manifest.json" for path in company_dirs]
        news_paths: list[str] = []
        report_paths: list[str] = []
        event_paths: list[str] = []
        transcript_paths: list[str] = []
        news_records = reports_records = event_records = 0
        reusable = 0
        saved = 0
        warnings: list[str] = []
        seen_records = {"news": set(), "reports": set(), "events": set()}
        for company_dir in company_dirs:
            try:
                manifest = json.loads((company_dir / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            for kind, target in (("news", news_paths), ("reports", report_paths), ("events", event_paths)):
                records = manifest.get(kind) or []
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    record_key = str(record.get("source_url") or record.get("event_url") or record.get("official_ir_url") or "")
                    if not record_key:
                        record_key = str(record.get("local_filename") or record.get("local_json") or record.get("title") or "")
                    if record_key in seen_records[kind]:
                        continue
                    seen_records[kind].add(record_key)
                    path = self._record_artifact_path(company_dir, record)
                    # HTML/description records are still valid normalization
                    # inputs even when no separate local file was required.
                    eligible = path is not None or bool(record.get("content_html") or record.get("description"))
                    if kind == "news":
                        news_records += int(eligible)
                    elif kind == "reports":
                        reports_records += int(eligible)
                    else:
                        # Events are discovery records even when replay media is
                        # unavailable; only locally materialized media is an
                        # eligible normalization artifact.
                        event_records += 1
                    if path is not None:
                        value = str(path)
                        if value not in target:
                            target.append(value)
                        if kind == "events" and (path.suffix.lower() == ".json" or record.get("transcript_method") or record.get("method") in {"official_transcript", "generated_transcript"}):
                            if value not in transcript_paths:
                                transcript_paths.append(value)
                    if record.get("cache_hit") or record.get("existing_artifact"):
                        reusable += 1
                    elif eligible:
                        saved += 1
            status = str(manifest.get("overall_status") or manifest.get("status") or "UNKNOWN")
            if status == "SUCCESS_WITH_LIMITATIONS":
                completeness = manifest.get("completeness") or {}
                warnings.extend(str(item) for item in completeness.get("limitations", []) if item)
                event_status = manifest.get("events_status")
                if event_status and event_status not in {"SUCCESS", "NOT_APPLICABLE"}:
                    warnings.append(f"events: {event_status}")
        dedup_warnings = list(dict.fromkeys(warnings))
        latest_manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
        status = str(latest_manifest.get("overall_status") or latest_manifest.get("status") or "UNKNOWN")
        critical_coverage = latest_manifest.get("critical_source_coverage") or {}
        if (
            status in {"FAILED", "INTERRUPTED"}
            and bool(critical_coverage.get("minimum_research_corpus_acquired"))
            and (reports_records or report_paths)
        ):
            # Parent termination after the persisted critical checkpoint is a
            # partial acquisition handoff, not an empty/failed source cache.
            status = "PARTIAL"
        cache_metrics = latest_manifest.get("cache_metrics") or {}
        latest_events = latest_manifest.get("events") or []
        if isinstance(latest_events, list):
            event_records = len(latest_events)
        if cache_metrics.get("artifact_cache_hits") is not None:
            reusable = int(cache_metrics.get("artifact_cache_hits") or 0)
        return AcquisitionResult(
            ticker=self.ticker,
            status=status,
            download_root=str(self.download_root),
            company_acquisition_root=str(company_dirs[0]),
            manifest_paths=[str(path) for path in manifest_paths],
            provenance_paths=[str(path) for path in manifest_paths],
            news_paths=news_paths,
            report_paths=report_paths,
            event_paths=event_paths,
            transcript_paths=transcript_paths,
            record_counts={
                "news": news_records,
                "reports": reports_records,
                "events": event_records,
                "transcripts": len(transcript_paths),
                "manifests": len(manifest_paths),
                "reused": reusable,
                "saved": saved,
                "eligible_source_artifacts": len(set(news_paths + report_paths)) + sum(
                    1 for path in event_paths if path not in set(news_paths + report_paths)
                ),
            },
            warnings=dedup_warnings,
        )

    def _invoke(self, module: str, args: list[str]) -> tuple[int, str]:
        if self.verbose:
            safe_args = " ".join(str(value) for value in args)
            print(f"Invoking {module}: python -m {module} {safe_args}")
        started = time.perf_counter()
        heartbeat_stop = threading.Event()
        heartbeat = None
        if module == MODULES["acquisition"]:
            heartbeat = threading.Thread(target=self._announce_heartbeat,
                                         args=("acquisition", started, heartbeat_stop), daemon=True)
            heartbeat.start()
        try:
            if self._default_executor and module == MODULES["acquisition"]:
                result = _run_module(module, args, timeout=self.acquisition_timeout)
            else:
                result = self.executor(module, args)
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=0.2)
        if isinstance(result, int):
            return result, ""
        output = str(getattr(result, "stdout", "") or "") + str(getattr(result, "stderr", "") or "")
        if self.verbose and output:
            print(output[-2000:])
        return int(getattr(result, "returncode", 1)), output

    def _stage(self, name: str, status: StageStatus, *, counts: dict[str, int] | None = None,
               inputs: list[Path] | None = None, outputs: list[Path] | None = None,
               warnings: list[str] | None = None, message: str = "", started: float | None = None,
               change: str | None = None, provenance: dict[str, Any] | None = None) -> StageResult:
        input_values = [str(p) for p in inputs or []]
        output_values = [str(p) for p in outputs or []]
        stage_provenance = provenance or {
            "ticker": self.ticker,
            "input_paths": input_values,
            "output_paths": output_values,
        }
        compatibility = stage_provenance.get("compatibility") if isinstance(stage_provenance, dict) else None
        compatibility_action = str(compatibility.get("action") or "") if isinstance(compatibility, dict) else ""
        # Keep input compatibility separate from the action actually taken.
        # A forced rebuild may start from compatible inputs, while a stale
        # artifact may be rebuilt after an incompatibility decision.
        if isinstance(stage_provenance, dict):
            stage_provenance.setdefault("input_compatibility", compatibility_action or "UNKNOWN")
            if status in {StageStatus.SUCCESS, StageStatus.PARTIAL} and compatibility_action == "REBUILD_REQUIRED":
                stage_provenance["stage_execution_action"] = "REBUILT"
            elif status in {StageStatus.REUSED, StageStatus.NO_CHANGES}:
                stage_provenance.setdefault("stage_execution_action", "REUSED")
            else:
                stage_provenance.setdefault("stage_execution_action", "CREATED")
        if compatibility_action == "REBUILD_REQUIRED" and status in {StageStatus.SUCCESS, StageStatus.PARTIAL}:
            change = "REBUILT"
        result = StageResult(name=name, status=status, record_counts=counts or {},
                             input_paths=input_values, output_paths=output_values,
                             warnings=warnings or [], message=message,
                             duration_seconds=(time.perf_counter() - started) if started else 0.0,
                             change=change or ("CREATED" if status == StageStatus.SUCCESS else "REUSED"),
                             provenance=stage_provenance,
                             run_id=self._run_id,
                             artifact_generation_id=str(stage_provenance.get("artifact_generation_id") or "") if isinstance(stage_provenance, dict) else "",
                             producer_fingerprint=implementation_fingerprint_for(name) if name in STAGE_CONTRACTS else "",
                             input_fingerprint=dependency_fingerprint(stage_provenance) if isinstance(stage_provenance, dict) else "")
        contract = STAGE_CONTRACTS.get(name)
        if contract:
            result.versions = {
                "producer_version": contract.producer_version,
                "schema_version": contract.schema_version,
                "implementation_fingerprint": implementation_fingerprint_for(name),
                "configuration_fingerprint": configuration_fingerprint(name),
            }
            dependency_values = {
                "profile": self.profile,
                "profile_version": self._profile_version(),
                "inputs": input_values,
                "outputs": output_values,
            }
            # A compatible REUSED artifact keeps the producer/input metadata
            # that made it compatible. Re-recording the current decision text
            # would mutate its input fingerprint and spuriously rebuild every
            # downstream stage on the next run.
            existing_metadata = self._compatibility.get(name, self.ticker)
            output_fingerprint = canonical_artifact_fingerprint(
                name, self.ticker, self.artifacts_root, extra_paths=outputs or []
            )
            previous_output_fingerprint = existing_metadata.get("output_fingerprint") if existing_metadata else None
            # A successful execution represents a changed dependency only when
            # its canonical analytical content changed.  Timestamps, mtimes,
            # and run identifiers are excluded by canonical_artifact_fingerprint.
            if status in {StageStatus.SUCCESS, StageStatus.PARTIAL} and status != StageStatus.REUSED:
                if previous_output_fingerprint != output_fingerprint:
                    self._changed_stages.add(name)
            if status != StageStatus.REUSED or existing_metadata is None:
                self._compatibility.record(
                    name,
                    self.ticker,
                    dependency_values=dependency_values,
                    artifacts=output_values,
                    status="ACTIVE" if status in {StageStatus.SUCCESS, StageStatus.REUSED, StageStatus.PARTIAL} else "FAILED",
                    output_fingerprint=output_fingerprint,
                )
            elif "output_fingerprint" not in existing_metadata or "configuration_fingerprint" not in existing_metadata:
                # Upgrade legacy metadata in place without changing its input
                # or dependency fingerprints.  This is a metadata migration,
                # not a re-execution and cannot make an unchanged artifact
                # appear newly produced.
                self._compatibility.record(
                    name,
                    self.ticker,
                    dependency_values=existing_metadata.get("dependencies") or dependency_values,
                    artifacts=list(existing_metadata.get("artifacts") or output_values),
                    status=str(existing_metadata.get("status") or "ACTIVE"),
                    producer_version=existing_metadata.get("producer_version"),
                    schema_version=existing_metadata.get("schema_version"),
                    implementation_fingerprint=existing_metadata.get("implementation_fingerprint"),
                    dependency_fingerprints=existing_metadata.get("dependency_fingerprints") or {},
                    input_fingerprint=existing_metadata.get("input_fingerprint") or existing_metadata.get("dependency_fingerprint"),
                    output_fingerprint=output_fingerprint,
                    configuration_fingerprint=configuration_fingerprint(name),
                    created_at=existing_metadata.get("created_at"),
                )
            compatibility = (provenance or {}).get("compatibility") if isinstance(provenance, dict) else None
            if status in {StageStatus.SUCCESS, StageStatus.PARTIAL} and isinstance(compatibility, dict) and compatibility.get("action") == "REBUILD_REQUIRED":
                self._compatibility.quarantine(
                    name,
                    self.ticker,
                    reason=str(compatibility.get("reason") or "REBUILD_REQUIRED"),
                    artifact_ids=[f"{name}:{self.ticker}"],
                    producer_version=compatibility.get("stored_producer_version"),
                    replacement_id=f"{name}:{self.ticker}:{contract.producer_version}",
                )
        self._stages.append(result)
        if status == StageStatus.FAILED:
            self._failed_dependencies.add(name)
        self._write_run_state()
        self._warnings.extend(result.warnings)
        position = STAGE_NUMBERS.get(name)
        if position is not None and status != StageStatus.NOT_RUN:
            if status == StageStatus.FAILED:
                print(f"[{position}/{self._stage_total()}] {self._stage_label(name)} — FAILED", flush=True)
                if result.message or result.warnings:
                    print(f"Reason: {result.message or '; '.join(result.warnings)}", flush=True)
            elif status == StageStatus.SKIPPED_DEPENDENCY_FAILURE:
                print(f"[{position}/{self._stage_total()}] {self._stage_label(name)} — SKIPPED_DEPENDENCY_FAILURE", flush=True)
            else:
                print(f"[{position}/{self._stage_total()}] {self._stage_label(name)} — COMPLETE", flush=True)
        if self._active_stage == name:
            self._active_stage = ""
        self._write_manifest_snapshot()
        return result

    def _run_state_path(self) -> Path:
        return self.artifacts_root / "pipeline_run_state.json"

    def _write_run_state(self, final_status: str | None = None) -> None:
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self._run_id, "ticker": self.ticker, "as_of_date": self.as_of_date,
            "status": final_status or ("FAILED" if self._failed_dependencies else "RUNNING"),
            "stages": [stage.to_dict() for stage in self._stages],
            "failed_dependencies": sorted(self._failed_dependencies),
        }
        temporary = self._run_state_path().with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self._run_state_path())

    def _qual_counts(self) -> dict[str, int]:
        return {"documents": _count(self.artifacts_root, "qualitative_analysis/documents.jsonl", self.ticker),
                "claims": _count(self.artifacts_root, "qualitative_analysis/claims.jsonl", self.ticker),
                "temporal_changes": _count(self.artifacts_root, "qualitative_analysis/temporal_changes.jsonl", self.ticker),
                "states": _count(self.artifacts_root, "qualitative_analysis/qualitative_states.jsonl", self.ticker)}

    def _compatibility_for(self, stage: str, *, dependency_values: dict[str, Any] | None = None) -> CompatibilityDecision:
        decision = self._compatibility.decide(stage, self.ticker, profile=self.profile, dependency_values=dependency_values)
        contract = STAGE_CONTRACTS.get(stage)
        if self._canonical_identity_changed:
            decision.status = "INCOMPATIBLE"
            decision.action = "REBUILD_REQUIRED"
            decision.reason = "CANONICAL_REGISTRANT_CHANGED"
            if "CANONICAL_REGISTRANT_CHANGED" not in decision.diagnostics:
                decision.diagnostics.append("CANONICAL_REGISTRANT_CHANGED")
        if self.profile != "core_v1" and contract and any(dependency in self._changed_stages for dependency in contract.dependencies):
            decision.status = "INCOMPATIBLE"
            decision.action = "REBUILD_REQUIRED"
            decision.reason = "UPSTREAM_DEPENDENCY_CHANGED"
            if "UPSTREAM_DEPENDENCY_CHANGED" not in decision.diagnostics:
                decision.diagnostics.append("UPSTREAM_DEPENDENCY_CHANGED")
        if self.rebuild_analysis and stage != "acquisition":
            decision.status = "INCOMPATIBLE"
            decision.action = "REBUILD_REQUIRED"
            decision.reason = "EXPLICIT_REBUILD_ANALYSIS"
            if "EXPLICIT_REBUILD_ANALYSIS" not in decision.diagnostics:
                decision.diagnostics.append("EXPLICIT_REBUILD_ANALYSIS")
        integrity_issue = self._artifact_integrity_issue(stage)
        if self.profile != "core_v1" and integrity_issue:
            decision.status = "INCOMPATIBLE"
            decision.action = "REBUILD_REQUIRED"
            decision.reason = integrity_issue
            decision.diagnostics.append(integrity_issue)
        if self._canonical_identity_changed:
            decision.status = "INCOMPATIBLE"
            decision.action = "REBUILD_REQUIRED"
            decision.reason = "CANONICAL_REGISTRANT_CHANGED"
            if "CANONICAL_REGISTRANT_CHANGED" not in decision.diagnostics:
                decision.diagnostics.append("CANONICAL_REGISTRANT_CHANGED")
        self._compatibility_decisions[stage] = decision
        if self.verbose and self.profile != "core_v1":
            print(
                f"Compatibility {stage}: stored={decision.stored_producer_version or 'UNKNOWN'} "
                f"required={decision.required_producer_version} action={decision.action} reason={decision.reason}",
                flush=True,
            )
        return decision

    def _artifact_integrity_issue(self, stage: str) -> str | None:
        """Reject sidecar metadata when the actual derived records disagree."""
        if self.profile == "core_v1":
            return None
        if stage == "normalization":
            rows = _rows(self.artifacts_root, "qualitative_analysis/documents.jsonl", self.ticker)
            if not rows:
                return "NORMALIZED_DOCUMENT_STORE_EMPTY"
            if any(str(row.get("normalization_version") or "") != STAGE_CONTRACTS[stage].producer_version or str(row.get("schema_version") or "") != STAGE_CONTRACTS[stage].schema_version for row in rows):
                return "NORMALIZED_DOCUMENT_VERSION_MISMATCH"
        elif stage == "qualitative_extraction":
            rows = _rows(self.artifacts_root, "qualitative_analysis/claims.jsonl", self.ticker)
            if rows and any(str(row.get("claim_schema_version") or "") != STAGE_CONTRACTS[stage].schema_version for row in rows):
                return "CLAIM_SCHEMA_VERSION_MISMATCH"
        elif stage == "temporal_comparison":
            rows = _rows(self.artifacts_root, "qualitative_analysis/temporal_changes.jsonl", self.ticker)
            if rows and any(str(row.get("temporal_version") or "") != STAGE_CONTRACTS[stage].producer_version for row in rows):
                return "TEMPORAL_VERSION_MISMATCH"
        elif stage == "state_synthesis":
            rows = _rows(self.artifacts_root, "qualitative_analysis/qualitative_states.jsonl", self.ticker)
            if rows and any(str(row.get("state_version") or "") != STAGE_CONTRACTS[stage].producer_version for row in rows):
                return "STATE_VERSION_MISMATCH"
        elif stage == "governance_intelligence":
            rows = _rows(self.artifacts_root, "management_intelligence/governance_shareholder_rights.jsonl", self.ticker)
            proxy_docs = [row for row in _rows(self.artifacts_root, "qualitative_analysis/documents.jsonl", self.ticker)
                          if str(row.get("document_type") or "").lower() in {"proxy_statement", "def 14a", "def14a"}]
            for row in rows:
                evidence = str(row.get("evidence_text") or "")
                if looks_like_pdf_corruption(evidence):
                    return "RAW_PDF_GOVERNANCE_EVIDENCE"
                if row.get("source_accession") and row.get("extraction_status") != "VERIFIED":
                    return "UNVERIFIED_GOVERNANCE_EVIDENCE"
                if row.get("source_accession") and not row.get("normalized_document_id"):
                    return "GOVERNANCE_SOURCE_NOT_LINKED_TO_NORMALIZED_DOCUMENT"
            if proxy_docs and not any(row.get("source_accession") for row in rows):
                return "PROXY_DOCUMENT_NOT_ROUTED_TO_GOVERNANCE"
        elif stage == "dossier_generation":
            path, _ = self._source_dossier_paths(self._profile_version())
            if not path.exists():
                return "V2_DOSSIER_MISSING"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return "V2_DOSSIER_INVALID_JSON"
            if payload.get("research_output_version") != "research-output-v2":
                return "V2_DOSSIER_VERSION_MISMATCH"
        return None

    def _quarantine_for_rebuild(self, stage: str, decision: CompatibilityDecision) -> None:
        if decision.status in {"CORRUPT", "INCOMPATIBLE", "UNKNOWN_VERSION"}:
            artifact_ids = [f"{stage}:{self.ticker}"]
            if stage == "governance_intelligence":
                rows = _rows(self.artifacts_root, "management_intelligence/governance_shareholder_rights.jsonl", self.ticker)
                artifact_ids = [str(row.get("rights_id") or row.get("record_id") or f"governance-rights:{self.ticker}") for row in rows] or artifact_ids
            elif stage == "normalization":
                rows = _rows(self.artifacts_root, "qualitative_analysis/documents.jsonl", self.ticker)
                artifact_ids = [str(row.get("document_id") or f"normalized-document:{self.ticker}") for row in rows] or artifact_ids
            self._compatibility.quarantine(
                stage,
                self.ticker,
                reason=decision.reason or decision.status,
                artifact_ids=artifact_ids,
                producer_version=decision.stored_producer_version,
            )

    def _optional_counts(self) -> dict[str, dict[str, int]]:
        return {
            "insider_intelligence": {name[:-6]: _count(self.artifacts_root, f"insider_intelligence/{name}", self.ticker)
                                     for name in ("people.jsonl", "transactions.jsonl", "ownership.jsonl")},
            "management_intelligence": {name[:-6]: _count(self.artifacts_root, f"management_intelligence/{name}", self.ticker)
                                         for name in ("people.jsonl", "roles.jsonl")},
            "management_track_record": {name[:-6]: _count(self.artifacts_root, f"management_intelligence/{name}", self.ticker)
                                         for name in ("track_record_snapshots.jsonl", "guidance_commitments.jsonl", "strategic_commitments.jsonl", "operating_outcomes.jsonl")},
            "governance_intelligence": {name[:-6]: _count(self.artifacts_root, f"management_intelligence/{name}", self.ticker)
                                         for name in ("board_history.jsonl", "governance_committees.jsonl", "governance_snapshots.jsonl")},
        }

    def _qual_args(self, *extra: str) -> list[str]:
        # The frozen qualitative layer reads the downloader's cache.  Keep
        # that cache outside the cleanable production tree in every mode;
        # outputs are materialized into the numbered production folders later.
        args = ["--ticker", self.ticker, "--download-root", str(self._normalization_download_root()),
                "--store-root", str(self.artifacts_root / "qualitative_analysis"),
                "--as-of-date", self.as_of_date]
        if self._issuer_metadata and self._issuer_metadata.cik:
            args.extend(["--issuer-cik", self._issuer_metadata.cik])
        return [*args, *extra]

    def _normalization_download_root(self) -> Path:
        """Choose an existing validated source cache without guessing files.

        A normal run uses ``--download-root``.  Existing-mode production
        materialization also preserves manifests under ``01_Acquisition``;
        that copy is a valid source cache when the external cache has been
        moved or is unavailable.  Never use this fallback for fresh runs.
        """
        if self.mode == "existing":
            root = self.download_root
            if root.exists() and any(path.is_dir() and (path / "manifest.json").exists() for path in root.iterdir()):
                return root
            production = self.production_download_root
            if production.exists() and any(path.is_dir() and (path / "manifest.json").exists() for path in production.iterdir()):
                return production
        return self.download_root

    def _run_qualitative(self, name: str, extra: list[str], before: dict[str, int], key: str,
                         *, source_root: Path | None = None,
                         provenance: dict[str, Any] | None = None) -> StageResult:
        started = time.perf_counter()
        self._announce_start(name)
        code, output = self._invoke(MODULES["qualitative"], self._qual_args(*extra))
        after = self._qual_counts()
        if code != 0:
            return self._stage(name, StageStatus.FAILED, counts=after, warnings=[f"{name} command failed"], message=output[-500:], started=started, change="UPDATED")
        changed = after.get(key, 0) != before.get(key, 0)
        compatibility = provenance.get("compatibility") if isinstance(provenance, dict) else None
        rebuilding = isinstance(compatibility, dict) and compatibility.get("action") == "REBUILD_REQUIRED"
        # Some qualitative stores are append-only and report the same row
        # count after an explicit forced rewrite.  A compatibility-triggered
        # execution is still a successful rebuild; otherwise the old metadata
        # would be retained and the same implementation mismatch would recur
        # on every subsequent run.
        effective_status = StageStatus.SUCCESS if changed or rebuilding else StageStatus.REUSED
        return self._stage(name, effective_status, counts=after,
                           inputs=[source_root or (self.artifacts_root / "qualitative_analysis")],
                           outputs=[self.artifacts_root / "qualitative_analysis"],
                           provenance=provenance,
                           message=output[-500:], started=started, change="UPDATED" if changed else "REUSED")

    def _profile_version(self) -> str:
        rows = _rows(self.artifacts_root, "scoring_engine/company_scores.jsonl", self.ticker)
        if self.profile != "core_v1":
            rows = [row for row in rows if str(row.get("profile") or "") == self.profile]
        versions = [str(row["profile_version"]) for row in rows if row.get("profile_version")]
        if versions:
            # Stores are append-only and may contain earlier component versions.
            # Select the greatest numeric version so dossier paths follow the
            # current scoring materialization rather than file order.
            def version_key(value: str) -> tuple[int, ...]:
                return tuple(int(part) for part in re.findall(r"\d+", value))
            return max(versions, key=version_key)
        if self.profile == "core_v1":
            return "core-v1.2"
        # Versioned profiles are configuration data, not path conventions.
        # Resolve the declared profile version so v2 output paths remain
        # deterministic even before the first score row is materialized.
        profile_path = self.config_root / f"{self.profile}.json"
        try:
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
            return str(payload.get("profile_version") or self.profile)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return self.profile

    def _run_optional_fresh(self, name: str, module_key: str, args: list[str], counts: dict[str, int], *, provenance: dict[str, Any] | None = None) -> StageResult:
        started = time.perf_counter()
        self._announce_start(name)
        code, output = self._invoke(MODULES[module_key], args)
        after = self._optional_counts().get(name, counts)
        if code == 0:
            if name == "governance_intelligence":
                generation = GovernanceStore(self.artifacts_root / "management_intelligence").current_generation(self.ticker)
                if isinstance(provenance, dict) and generation:
                    provenance = dict(provenance)
                    provenance["artifact_generation_id"] = str(generation.get("generation_id") or "")
                    provenance["dependency_status"] = "CURRENT_RUN_VALID" if str(generation.get("status")) == "ACTIVE" else str(generation.get("status"))
            research_warnings: list[str] = []
            if name == "governance_intelligence":
                # Governance CLI completion is a technical result.  Its JSON
                # payload carries the research extraction status separately.
                payload = None
                for line in reversed(output.splitlines()):
                    try:
                        candidate = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(candidate, dict) and ("warnings" in candidate or "status" in candidate):
                        payload = candidate
                        break
                raw_warnings = [str(item) for item in (payload or {}).get("warnings", [])]
                # Missing optional charter/bylaw exhibits limit rights-level
                # coverage but do not invalidate an otherwise complete proxy
                # board extraction.  Keep source limitations visible without
                # downgrading the governance stage itself.
                noncritical_warnings = {
                    "cached charter/bylaw exhibits unavailable; rights remain proxy-limited",
                    "SOURCE_COVERAGE_INCOMPLETE",
                }
                research_warnings = [w for w in raw_warnings if w not in noncritical_warnings]
                governance_status = str((payload or {}).get("status") or "").upper()
                if governance_status in {"EXTRACTION_FAILED", "GOVERNANCE_EXTRACTION_FAILED", "GOVERNANCE_INSUFFICIENT", "PARTIAL"}:
                    research_warnings.append(f"RESEARCH_STATUS_{governance_status}")
                if research_warnings:
                    research_warnings = sorted(set(research_warnings))
            stage_status = StageStatus.PARTIAL if research_warnings or not sum(after.values()) else StageStatus.SUCCESS
            return self._stage(name, stage_status,
                                counts=after, inputs=[self.artifacts_root], warnings=research_warnings or ([] if sum(after.values()) else [f"{name.replace('_', ' ').title()} completed without records"]),
                                message=output[-500:], started=started, change="UPDATED", provenance=provenance)
        if name == "governance_intelligence":
            GovernanceStore(self.artifacts_root / "management_intelligence").mark_failed(
                self.ticker, run_id=self._run_id, reason=output[-500:] or f"{module_key} exited with return code {code}"
            )
        return self._stage(name, StageStatus.FAILED, counts=after,
                           warnings=[f"{name.replace('_', ' ').title()} command was unavailable or failed"],
                           message=output[-500:], started=started, change="UPDATED", provenance=provenance)

    def _source_dossier_paths(self, profile_version: str) -> tuple[Path, Path]:
        directory = self.output_root / self.ticker
        if self.profile != "core_v1":
            stem = f"{self.ticker}_{self.as_of_date}_research-output-v2"
            return directory / f"{stem}.json", directory / f"{stem}.md"
        stem = f"{self.ticker}_{self.as_of_date}_{profile_version}_dossier"
        return directory / f"{stem}.json", directory / f"{stem}.md"

    def _dossier_paths(self, profile_version: str) -> tuple[Path, Path]:
        source_json, source_md = self._source_dossier_paths(profile_version)
        return self.production_dossier_root / source_json.name, self.production_dossier_root / source_md.name

    def _publish_google_sheets(self, dossier_path: Path) -> StageResult:
        """Publish the finalized dossier without changing analytical stores."""
        started = time.perf_counter()
        self._announce_start("google_sheets_publication")
        if not dossier_path.exists():
            self._google_sheets_status = "FAILED"
            self._publication_summary = {"status": "FAILED", "code": "DOSSIER_NOT_AVAILABLE"}
            return self._stage(
                "google_sheets_publication", StageStatus.FAILED,
                warnings=["GOOGLE_SHEETS_PUBLISH_FAILED", "DOSSIER_NOT_AVAILABLE"],
                message="GOOGLE_SHEETS_PUBLISH_FAILED: finalized dossier is unavailable",
                started=started, change="UPDATED",
            )
        publisher = self._google_sheets_publisher or GoogleSheetsPublisher(
            spreadsheet_id=self.google_spreadsheet_id,
            service_account=self.google_service_account,
        )
        try:
            summary = publisher.publish(
                dossier_path,
                ticker=self.ticker,
                as_of_date=self.as_of_date,
                artifacts_root=self.artifacts_root,
                production_root=self.ticker_root,
                run_id=self._run_id,
            )
        except Exception as exc:
            self._google_sheets_status = "FAILED"
            self._publication_summary = {"status": "FAILED", "code": "GOOGLE_SHEETS_PUBLISH_FAILED", "message": str(exc)}
            return self._stage(
                "google_sheets_publication", StageStatus.FAILED,
                warnings=["GOOGLE_SHEETS_PUBLISH_FAILED"], message=str(exc),
                started=started, change="UPDATED",
            )
        payload = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary or {})
        self._publication_summary = payload
        self._google_sheets_status = str(payload.get("status") or "FAILED")
        status_text = self._google_sheets_status
        if status_text in {"CREATED", "UPDATED"}:
            stage_status, change = StageStatus.SUCCESS, status_text
        elif status_text == "NO_CHANGES":
            stage_status, change = StageStatus.NO_CHANGES, "NO_CHANGES"
        else:
            stage_status, change = StageStatus.FAILED, "UPDATED"
        warnings = [] if stage_status in {StageStatus.SUCCESS, StageStatus.REUSED} else [str(payload.get("code") or "GOOGLE_SHEETS_PUBLISH_FAILED")]
        if stage_status == StageStatus.FAILED:
            self._exit_code = EXIT_GOOGLE_SHEETS_FAILURE
        return self._stage(
            "google_sheets_publication", stage_status,
            counts={
                "records_added": int(payload.get("records_added") or 0),
                "records_updated": int(payload.get("records_updated") or 0),
                "records_unchanged": int(payload.get("records_unchanged") or 0),
                "records_stale": int(payload.get("records_stale") or 0),
            },
            outputs=[Path(payload["snapshot_path"])] if payload.get("snapshot_path") else [self.ticker_root / FOLDER_NAMES["google_sheets"]],
            warnings=warnings, message=str(payload.get("message") or ""), started=started,
            change=change, provenance={"publication": payload, "schema_version": "full-qual-sheet-v5.1", "publisher_version": "full-qual-google-sheets-v5.1"},
        )

    def _run(self) -> PipelineRunResult:
        self._ensure_production_layout()
        self._manifest_ready = True
        self._write_manifest_snapshot()
        # Acquisition is the only stage that is intentionally allowed to touch the network.
        started = time.perf_counter()
        self._announce_start("acquisition")
        # Resolve explicit tickers before any acquisition/existing branch.  A
        # resolution error remains an acquisition-stage failure (rather than
        # an opaque orchestration exception) for stable CLI semantics.
        if self.manual_ticker:
            try:
                self._issuer_metadata = self._resolve_manual_issuer()
                self._persist_issuer_identity()
            except IssuerResolutionError as exc:
                reason = str(exc)
                self._stage("acquisition", StageStatus.FAILED, inputs=[self.download_root],
                            warnings=["ISSUER_RESOLUTION_FAILED"], message=reason,
                            started=started, change="UPDATED")
                return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)
        if self.manual_ticker and self.mode == "existing" and self._canonical_identity_changed:
            reason = "REBUILD_REQUIRED: CANONICAL_REGISTRANT_CHANGED"
            self._stage("acquisition", StageStatus.FAILED, inputs=[self.download_root],
                        warnings=["CANONICAL_REGISTRANT_CHANGED", "REBUILD_REQUIRED"], message=reason,
                        started=started, change="UPDATED", provenance={"reason": "CANONICAL_REGISTRANT_CHANGED"})
            return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)
        if self.skip_acquisition:
            self._acquisition_result = self._resolve_acquisition_result(allow_local_fallback=True)
            self._stage("acquisition", StageStatus.SKIPPED, counts=dict(self._acquisition_result.record_counts),
                        inputs=[Path(self._acquisition_result.download_root)],
                        warnings=["Acquisition explicitly skipped; using local artifacts"], started=started, change="REUSED",
                        provenance=self._acquisition_result.to_dict())
        elif self.mode == "existing":
            self._acquisition_result = self._resolve_acquisition_result(allow_local_fallback=True)
            self._stage("acquisition", StageStatus.REUSED, counts={**self._acquisition_result.record_counts, "local_artifacts": int(self.download_root.exists())},
                        inputs=[self.download_root], warnings=["Existing mode does not invoke acquisition"], started=started, change="REUSED",
                        provenance=self._acquisition_result.to_dict())
        else:
            issuer = None
            if self.manual_ticker:
                try:
                    issuer = self._resolve_manual_issuer()
                except IssuerResolutionError as exc:
                    reason = str(exc)
                    self._stage("acquisition", StageStatus.FAILED, inputs=[self.download_root],
                                warnings=["ISSUER_RESOLUTION_FAILED"], message=reason,
                                started=started, change="UPDATED")
                    return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)
            acquisition_module, acquisition_args = self.acquisition_invocation(issuer)
            self.download_root.mkdir(parents=True, exist_ok=True)
            try:
                code, output = self._invoke(acquisition_module, acquisition_args)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._stage("acquisition", StageStatus.FAILED, inputs=[self.download_root],
                            warnings=["Acquisition invocation raised an exception"], message=reason,
                            started=started, change="UPDATED")
                return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)
            if code == 124:
                self._mark_interrupted_acquisition("ACQUISITION_DEADLINE_EXCEEDED")
            # A supplied executor is an integration-test/in-process adapter;
            # it may intentionally mock acquisition while providing a local
            # normalized fixture.  The real fresh CLI remains manifest-gated.
            self._acquisition_result = self._resolve_acquisition_result(allow_local_fallback=not self._default_executor)
            if code == 124:
                # A launcher can finish a fraction after its real child.  A
                # second checkpoint closes that race before the parent writes
                # its failure handoff.
                self._mark_interrupted_acquisition("ACQUISITION_DEADLINE_EXCEEDED")
            acquisition_warnings = list(self._acquisition_result.warnings)
            critical_handoff = code in {2, 124} and self._critical_source_handoff_complete() and self._acquisition_result.eligible_artifacts > 0
            acquisition_stage_status = StageStatus.SUCCESS if code == 0 else StageStatus.PARTIAL if critical_handoff else StageStatus.FAILED
            if critical_handoff:
                acquisition_warnings.append("ACQUISITION_DEADLINE_EXCEEDED_AFTER_CRITICAL_CORPUS")
            self._stage("acquisition", acquisition_stage_status,
                        inputs=[self.download_root, Path(self._acquisition_result.company_acquisition_root)] if self._acquisition_result.company_acquisition_root else [self.download_root],
                        counts=dict(self._acquisition_result.record_counts),
                        warnings=acquisition_warnings if code == 0 or critical_handoff else ["Acquisition command failed"],
                        provenance=self._acquisition_result.to_dict(),
                        message=output[-500:], started=started, change="UPDATED")
            if code != 0 and not critical_handoff:
                reason = output[-1000:] or f"Acquisition exited with return code {code}"
                return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)

        counts = self._qual_counts()
        handoff = self._acquisition_result or self._resolve_acquisition_result()
        eligible_inputs = handoff.eligible_artifacts
        # A fresh run is admitted only by the current acquisition handoff.
        # Existing normalized rows in the generic store cannot substitute for
        # a missing current cache, unless acquisition was explicitly skipped.
        can_normalize = (
            eligible_inputs > 0
            if self.mode == "fresh" and not self.skip_acquisition
            else counts["documents"] > 0 or eligible_inputs > 0
        )
        if can_normalize:
            normalization_decision = self._compatibility_for("normalization")
            rebuild_normalization = self.mode == "existing" and normalization_decision.action != "REUSE"
            if rebuild_normalization:
                self._quarantine_for_rebuild("normalization", normalization_decision)
            if self.mode == "existing" and not rebuild_normalization:
                self._stage("normalization", StageStatus.REUSED, counts=counts,
                            inputs=[self.artifacts_root / "qualitative_analysis"],
                            outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED",
                            provenance={"compatibility": normalization_decision.to_dict()})
            else:
                if self.mode == "existing" and rebuild_normalization and eligible_inputs <= 0:
                    self._stage("normalization", StageStatus.FAILED, counts=counts,
                                inputs=[Path(handoff.download_root)],
                                warnings=["REBUILD_REQUIRED", "SOURCE_CACHE_UNAVAILABLE"],
                                message="REBUILD_REQUIRED: compatible normalized source is unavailable",
                                change="UPDATED", provenance={"compatibility": normalization_decision.to_dict()})
                    return self._finish_failure("normalization", "REBUILD_REQUIRED: SOURCE_CACHE_UNAVAILABLE", EXIT_NORMALIZATION_FAILURE)
                normalization = self._run_qualitative(
                    "normalization", ["--replace-ticker"], counts, "documents",
                    source_root=Path(handoff.download_root),
                    provenance={
                        "input_root": handoff.download_root,
                        "company_acquisition_root": handoff.company_acquisition_root,
                        "input_manifests": list(handoff.manifest_paths),
                        "provenance_paths": list(handoff.provenance_paths),
                        "eligible_source_artifacts": eligible_inputs,
                        "output_root": str(self.artifacts_root / "qualitative_analysis"),
                        "ticker": self.ticker,
                        "compatibility": normalization_decision.to_dict(),
                    },
                )
                if rebuild_normalization and normalization.status == StageStatus.REUSED:
                    normalization.status = StageStatus.SUCCESS
                    normalization.change = "UPDATED"
                if normalization.status == StageStatus.FAILED:
                    reason = normalization.message or "NORMALIZATION_FAILED"
                    return self._finish_failure("normalization", reason, EXIT_NORMALIZATION_FAILURE)
        else:
            if self.verbose:
                print(f"Eligible acquisition artifacts: {eligible_inputs}", flush=True)
                print(f"Resolved source root: {handoff.download_root}", flush=True)
                print(f"Expected manifests: {len(handoff.manifest_paths)}", flush=True)
            self._stage("normalization", StageStatus.FAILED, counts=counts,
                        inputs=[Path(handoff.download_root)],
                        warnings=["No normalized documents are available for this ticker"],
                        message="MISSING_NORMALIZED_INPUTS", change="UPDATED",
                        provenance={
                            "input_root": handoff.download_root,
                            "input_manifests": list(handoff.manifest_paths),
                            "eligible_source_artifacts": eligible_inputs,
                            "expected_manifests": len(handoff.manifest_paths),
                            "ticker": self.ticker,
                        })
            return self._finish_failure("normalization", "MISSING_NORMALIZED_INPUTS", EXIT_NORMALIZATION_FAILURE)

        counts = self._qual_counts()
        if counts["documents"]:
            extraction_decision = self._compatibility_for("qualitative_extraction")
            rebuild_extraction = self.mode == "existing" and extraction_decision.action != "REUSE"
            if rebuild_extraction:
                self._quarantine_for_rebuild("qualitative_extraction", extraction_decision)
            if self.mode == "existing" and not rebuild_extraction:
                extraction = self._stage("qualitative_extraction", StageStatus.REUSED, counts=counts,
                                         inputs=[self.artifacts_root / "qualitative_analysis"],
                                         outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED",
                                         provenance={"compatibility": extraction_decision.to_dict()})
            else:
                extraction = self._run_qualitative("qualitative_extraction", ["--extract", "--force-extraction"], counts, "claims",
                                                   provenance={"compatibility": extraction_decision.to_dict()})
                if rebuild_extraction and extraction.status == StageStatus.REUSED:
                    extraction.status = StageStatus.SUCCESS
                    extraction.change = "UPDATED"
        else:
            extraction = self._stage("qualitative_extraction", StageStatus.SKIPPED, counts=counts,
                                     warnings=["Skipped because normalization has no documents"], change="REUSED")
        counts = self._qual_counts()
        if counts["claims"] and extraction.status not in {StageStatus.FAILED, StageStatus.SKIPPED}:
            temporal_decision = self._compatibility_for("temporal_comparison")
            rebuild_temporal = self.mode == "existing" and temporal_decision.action != "REUSE"
            if rebuild_temporal:
                self._quarantine_for_rebuild("temporal_comparison", temporal_decision)
            if self.mode == "existing" and not rebuild_temporal:
                temporal = self._stage("temporal_comparison", StageStatus.REUSED, counts=counts,
                                       inputs=[self.artifacts_root / "qualitative_analysis"],
                                       outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED",
                                       provenance={"compatibility": temporal_decision.to_dict()})
            else:
                temporal = self._run_qualitative("temporal_comparison", ["--compare", "--latest-vs-previous", "--force-comparison"], counts, "temporal_changes",
                                                 provenance={"compatibility": temporal_decision.to_dict()})
                if rebuild_temporal and temporal.status == StageStatus.REUSED:
                    temporal.status = StageStatus.SUCCESS
                    temporal.change = "UPDATED"
        else:
            temporal = self._stage("temporal_comparison", StageStatus.SKIPPED, counts=counts,
                                   warnings=["Skipped because qualitative claims are unavailable"], change="REUSED")
        counts = self._qual_counts()
        if counts["claims"] and temporal.status not in {StageStatus.FAILED, StageStatus.SKIPPED}:
            state_decision = self._compatibility_for("state_synthesis")
            rebuild_state = self.mode == "existing" and state_decision.action != "REUSE"
            if rebuild_state:
                self._quarantine_for_rebuild("state_synthesis", state_decision)
            if self.mode == "existing" and not rebuild_state:
                self._stage("state_synthesis", StageStatus.REUSED, counts=counts,
                            inputs=[self.artifacts_root / "qualitative_analysis"],
                            outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED",
                            provenance={"compatibility": state_decision.to_dict()})
            else:
                state = self._run_qualitative("state_synthesis", ["--state", "--force-state"], counts, "states",
                                              provenance={"compatibility": state_decision.to_dict()})
                if rebuild_state and state.status == StageStatus.REUSED:
                    state.status = StageStatus.SUCCESS
                    state.change = "UPDATED"
        else:
            self._stage("state_synthesis", StageStatus.SKIPPED, counts=counts,
                        warnings=["Skipped because temporal comparison has no usable claims"], change="REUSED")

        optional = self._optional_counts()
        for name, values in optional.items():
            started = time.perf_counter()
            total = sum(values.values())
            decision = self._compatibility_for(name)
            rebuild = self.mode == "existing" and decision.action != "REUSE"
            if rebuild:
                self._quarantine_for_rebuild(name, decision)
            if self.mode == "fresh" or rebuild:
                module_key = {"insider_intelligence": "insider", "management_intelligence": "management",
                              "management_track_record": "track_record", "governance_intelligence": "governance"}[name]
                optional_args = {
                    "insider_intelligence": ["--ticker", self.ticker, "--store-root", str(self.artifacts_root / "insider_intelligence")],
                    "management_intelligence": [self.ticker, "--as-of-date", self.as_of_date, "--store-root", str(self.artifacts_root / "management_intelligence"), "--normalized-root", str(self.artifacts_root / "qualitative_analysis")],
                    "management_track_record": [self.ticker, "--as-of-date", self.as_of_date, "--store-root", str(self.artifacts_root / "management_intelligence"), "--qualitative-root", str(self.artifacts_root / "qualitative_analysis")],
                    "governance_intelligence": [self.ticker, "--as-of-date", self.as_of_date, "--store-root", str(self.artifacts_root / "management_intelligence"), "--normalized-root", str(self.artifacts_root / "qualitative_analysis")],
                }[name]
                if rebuild and module_key in {"management", "track_record", "governance"}:
                    optional_args.append("--force")
                self._run_optional_fresh(name, module_key, optional_args, values,
                                         provenance={"compatibility": decision.to_dict(), "rebuild": rebuild})
            elif total:
                self._stage(name, StageStatus.REUSED, counts=values, inputs=[self.artifacts_root], started=started, change="REUSED",
                            provenance={"compatibility": decision.to_dict()})
            else:
                self._stage(name, StageStatus.PARTIAL, counts=values,
                            warnings=[f"No cached {name.replace('_', ' ')} records for {self.ticker}"], started=started, change="REUSED",
                            provenance={"compatibility": decision.to_dict()})

        started = time.perf_counter()
        self._announce_start("scoring")
        scoring_decision = self._compatibility_for("scoring")
        if scoring_decision.action != "REUSE":
            self._quarantine_for_rebuild("scoring", scoring_decision)
        profile_version = self._profile_version()
        if "governance_intelligence" in self._failed_dependencies:
            score_stage = self._stage(
                "scoring", StageStatus.SKIPPED_DEPENDENCY_FAILURE,
                counts={}, outputs=[self.artifacts_root / "scoring_engine"],
                warnings=["SKIPPED_DEPENDENCY_FAILURE", "UPSTREAM_GOVERNANCE_STAGE_FAILED"],
                message="Governance rebuild failed; governance-dependent scoring was withheld.", started=started,
                change="UPDATED", provenance={"compatibility": scoring_decision.to_dict(), "dependency_status": "FAILED_CURRENT_RUN"},
            )
        else:
            score_before = _rows(self.artifacts_root, "scoring_engine/company_scores.jsonl", self.ticker)
            if self.profile != "core_v1" and scoring_decision.action == "REUSE" and score_before:
                score_stage = self._stage("scoring", StageStatus.REUSED,
                                          counts={"company_scores": len(score_before)}, outputs=[self.artifacts_root / "scoring_engine"],
                                          started=started, change="REUSED",
                                          provenance={"compatibility": scoring_decision.to_dict()})
            else:
                score_args = ["--ticker", self.ticker, "--profile", self.profile, "--as-of-date", self.as_of_date,
                              "--artifacts-root", str(self.artifacts_root), "--config-root", str(self.config_root),
                              "--output-root", str(self.artifacts_root / "scoring_engine")]
                code, output = self._invoke(MODULES["scoring"], score_args)
                score_rows = _rows(self.artifacts_root, "scoring_engine/company_scores.jsonl", self.ticker)
                score_warnings = []
                if "INSUFFICIENT_SCORING_COVERAGE" in output:
                    score_warnings.append("Overall score is unavailable because scoring coverage is insufficient; this is a valid result")
                score_stage = self._stage("scoring", StageStatus.SUCCESS if code == 0 else StageStatus.FAILED,
                                          counts={"company_scores": len(score_rows)}, outputs=[self.artifacts_root / "scoring_engine"],
                                          warnings=score_warnings if code == 0 else ["Scoring command failed"], message=output[-500:], started=started,
                                          change=("REBUILT" if scoring_decision.action == "REBUILD_REQUIRED" and code == 0 else ("CREATED" if code == 0 else "UPDATED")),
                                          provenance={"compatibility": scoring_decision.to_dict()})

        source_dossier_json, source_dossier_md = self._source_dossier_paths(profile_version)
        dossier_json, dossier_md = self._dossier_paths(profile_version)
        started = time.perf_counter()
        dossier_stage = None
        if score_stage.status not in {StageStatus.FAILED}:
            self._announce_start("dossier_generation")
            dossier_decision = self._compatibility_for("dossier_generation")
            existed_before = source_dossier_json.exists() and source_dossier_md.exists()
            if self.profile != "core_v1" and dossier_decision.action == "REUSE" and existed_before:
                dossier_stage = self._stage("dossier_generation", StageStatus.REUSED,
                                            counts={"files": 2}, outputs=[dossier_json, dossier_md],
                                            started=started, change="REUSED",
                                            provenance={"compatibility": dossier_decision.to_dict(), "output_version": "v2" if self.profile != "core_v1" else "v1"})
                code, output = 0, ""
            else:
                code, output = self._invoke(MODULES["dossier"], ["--ticker", self.ticker, "--as-of-date", self.as_of_date,
                    "--profile", self.profile, "--format", "both", "--detail", "standard", "--artifacts-root", str(self.artifacts_root),
                    "--output-root", str(self.output_root), *( ["--output-version", "v2", "--force"] if self.profile != "core_v1" else [] )])
            exists = source_dossier_json.exists() and source_dossier_md.exists()
            dossier_outputs = [dossier_json, dossier_md]
            quality_status = None
            if self.profile != "core_v1" and source_dossier_json.exists():
                try:
                    quality_status = str(json.loads(source_dossier_json.read_text(encoding="utf-8")).get("quality_gate", {}).get("status") or "")
                except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                    quality_status = ""
            dossier_status = (StageStatus.PARTIAL if quality_status == "NOT_RESEARCH_READY" else StageStatus.SUCCESS) if code == 0 and exists else StageStatus.FAILED
            dossier_warnings = [] if code == 0 and exists else ["Dossier generation failed"]
            if quality_status == "NOT_RESEARCH_READY":
                dossier_warnings.append("QUALITY_GATE_NOT_RESEARCH_READY; dossier is diagnostic and not decision-grade")
                self._exit_code = EXIT_QUALITY_GATE_FAILURE
            if dossier_stage is None:
                dossier_stage = self._stage("dossier_generation", dossier_status,
                                        counts={"files": int(source_dossier_json.exists()) + int(source_dossier_md.exists())},
                                        outputs=dossier_outputs, warnings=dossier_warnings,
                                        message=output[-500:], started=started,
                                        change=("REBUILT" if dossier_decision.action == "REBUILD_REQUIRED" and code == 0 and exists else ("REUSED" if existed_before and code == 0 and exists else ("CREATED" if code == 0 and exists else "REUSED"))),
                                        provenance={"compatibility": dossier_decision.to_dict(), "output_version": "v2" if self.profile != "core_v1" else "v1"})
        else:
            dossier_stage = self._stage("dossier_generation", StageStatus.SKIPPED, warnings=["Skipped because scoring failed"], started=started, change="REUSED")

        if self.run_validation_audit:
            started = time.perf_counter()
            self._announce_start("validation_audit")
            code, output = self._invoke(MODULES["validation"], ["audit-data", "--config", str(self.validation_config)])
            self._warnings.append("Validation audit is informational and non-calibrating")
            self._stage("validation_audit", StageStatus.SUCCESS if code == 0 else StageStatus.PARTIAL,
                        warnings=[] if code == 0 else ["Validation audit did not complete"], message=output[-500:], started=started, change="UPDATED")

        # Materialize production copies before publication: the sheet consumes
        # the same local dossier path handed to the human/AI consumers.
        production = self._materialize_outputs()
        analytical_stages = [stage for stage in self._stages if stage.name != "google_sheets_publication"]
        if any(stage.status == StageStatus.FAILED for stage in analytical_stages if stage.name in {"scoring", "dossier_generation", "governance_intelligence", "management_intelligence", "management_track_record"}):
            self._research_status = "FAILED"
        elif any(stage.status in {StageStatus.PARTIAL, StageStatus.FAILED} for stage in analytical_stages):
            self._research_status = "PARTIAL"
        else:
            self._research_status = "SUCCESS"
        if self.google_sheets_enabled:
            dossier_for_publication = Path(production["dossier_json"]) if production.get("dossier_json") else dossier_json
            if dossier_stage.status in {StageStatus.SUCCESS, StageStatus.PARTIAL, StageStatus.REUSED}:
                self._publish_google_sheets(dossier_for_publication)
            else:
                self._google_sheets_status = "SKIPPED"
                self._publication_summary = {"status": "SKIPPED", "code": "DOSSIER_NOT_READY"}
                self._stage("google_sheets_publication", StageStatus.SKIPPED,
                            warnings=["Dossier was not ready for publication"], started=time.perf_counter(), change="REUSED")

        statuses = [stage.status for stage in self._stages]
        if any(stage.status == StageStatus.FAILED for stage in self._stages if stage.name in {"scoring", "dossier_generation", "governance_intelligence", "management_intelligence", "management_track_record"}):
            final = StageStatus.FAILED
            self._exit_code = self._exit_code if self._exit_code != EXIT_SUCCESS else EXIT_UNEXPECTED_FAILURE
            failed_stage = next(stage for stage in self._stages if stage.status == StageStatus.FAILED and stage.name in {"scoring", "dossier_generation", "governance_intelligence", "management_intelligence", "management_track_record"})
            self._failure_stage = failed_stage.name
            self._failure_reason = failed_stage.message or "; ".join(failed_stage.warnings)
        elif any(status in {StageStatus.PARTIAL, StageStatus.FAILED} for status in statuses):
            final = StageStatus.PARTIAL
            if any(stage.name == "dossier_generation" and "QUALITY_GATE_NOT_RESEARCH_READY" in stage.warnings for stage in self._stages):
                self._exit_code = EXIT_QUALITY_GATE_FAILURE
        else:
            final = StageStatus.SUCCESS
        result = self._build_result(final)
        # Keep the materialized dossier paths returned by the production copy.
        result.dossier_json = production["dossier_json"]
        result.dossier_markdown = production["dossier_markdown"]
        self._write_run_state(final_status=final.value)
        self._write_manifest_snapshot(final_status=final, exit_code=self._exit_code)
        return result

    def run(self) -> PipelineRunResult:
        """Run the orchestration with an auditable failure result."""
        try:
            return self._run()
        except Exception as exc:
            self._failure_stage = self._current_stage or "orchestration"
            self._failure_reason = f"{type(exc).__name__}: {exc}"
            self._exit_code = EXIT_UNEXPECTED_FAILURE
            self._report_unexpected_exception(exc)
            # The layout may not exist if validation failed before setup.  Do
            # not retry a requested cleanup; only create the reporting parent.
            try:
                if not self._manifest_ready:
                    if self.clean_output:
                        raise
                    self.clean_output = False
                    self._ensure_production_layout()
                    self._manifest_ready = True
                self._append_not_run_stages(self._current_stage or "")
                result = self._build_result(StageStatus.FAILED, exit_code=EXIT_UNEXPECTED_FAILURE)
                self._write_manifest_snapshot(final_status=StageStatus.FAILED, exit_code=EXIT_UNEXPECTED_FAILURE)
                return result
            except Exception:
                # The CLI will report a configuration/path failure if even
                # the failure manifest cannot be safely materialized.
                raise
