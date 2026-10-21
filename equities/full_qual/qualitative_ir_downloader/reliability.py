"""Bounded acquisition progress, retry, and checkpoint primitives.

The downloader is deliberately resilient to one inaccessible source, but it
must never confuse resilience with an unbounded wait.  This module contains
the small, dependency-free contract shared by the acquisition stages and the
pipeline runner.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .filesystem import atomic_json


class AcquisitionTimeout(RuntimeError):
    """Base class for a bounded acquisition termination."""

    def __init__(self, code: str, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


class AcquisitionProgressTimeout(AcquisitionTimeout):
    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__("ACQUISITION_PROGRESS_TIMEOUT", message, diagnostics)


class AcquisitionDeadlineExceeded(AcquisitionTimeout):
    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__("ACQUISITION_DEADLINE_EXCEEDED", message, diagnostics)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RetryState:
    attempt: int = 0
    max_attempts: int = 1
    reason: str = ""
    next_delay_seconds: float = 0.0
    terminal: bool = False

    def record(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "reason": self.reason,
            "next_delay_seconds": self.next_delay_seconds,
            "terminal": self.terminal,
        }


@dataclass
class ProgressTracker:
    """Persist a machine-readable, monotonic acquisition checkpoint.

    ``progress`` is the only operation that resets the no-progress watchdog;
    parent heartbeats and log output never do so.  The JSON file is written
    atomically after every meaningful operation so a resumed run can inspect
    the last durable state.
    """

    folder: Path
    ticker: str
    no_progress_timeout: float = 900.0
    deadline: float = 7200.0
    manifest_checkpoint: Callable[[dict[str, Any]], None] | None = None
    progress_path: Path | None = None
    started_monotonic: float = field(default_factory=time.monotonic, init=False)
    last_progress_monotonic: float = field(default_factory=time.monotonic, init=False)
    current_substage: str = field(default="", init=False)
    current_item: str | None = field(default=None, init=False)
    status: str = field(default="RUNNING", init=False)
    subtages: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    counters: dict[str, int] = field(default_factory=lambda: {
        "discovered": 0, "processed": 0, "saved": 0, "reused": 0,
        "failed": 0, "timed_out": 0, "network_requests": 0,
    }, init=False)
    events: list[dict[str, Any]] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.folder = Path(self.folder)
        self.progress_path = self.progress_path or self.folder / "acquisition_progress.json"
        self._last_progress_at = utc_now()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._emit("STARTED", meaningful=False)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    @property
    def no_progress_for(self) -> float:
        return time.monotonic() - self.last_progress_monotonic

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "ticker": self.ticker,
            "status": self.status,
            "current_substage": self.current_substage,
            "current_item": self.current_item,
            "elapsed_seconds": round(self.elapsed, 3),
            "last_progress_at": self._last_progress_at,
            "last_successful_progress_at": self._last_progress_at,
            "no_progress_for_seconds": round(self.no_progress_for, 3),
            "counters": dict(self.counters),
            "substages": self.subtages,
            "events": self.events[-100:],
            "updated_at": utc_now(),
        }

    def _write(self) -> dict[str, Any]:
        payload = self._payload()
        atomic_json(self.progress_path, payload)
        if self.manifest_checkpoint:
            self.manifest_checkpoint(payload)
        # A single structured line lets a supervised parent consume progress
        # without scraping human log text.  It remains valid when stdout is
        # redirected or piped.
        print("PROGRESS_JSON " + json.dumps(payload, sort_keys=True), flush=True)
        return payload

    def _emit(self, state: str, *, meaningful: bool, substage: str | None = None,
              item: str | None = None, **values: Any) -> dict[str, Any]:
        with self._lock:
            if substage is not None:
                self.current_substage = substage
            if item is not None:
                self.current_item = item
            if meaningful:
                self.last_progress_monotonic = time.monotonic()
                self._last_progress_at = utc_now()
            self.status = "RUNNING" if state in {"STARTED", "PROGRESS"} else state
            record = {"state": state, "substage": self.current_substage,
                      "item": self.current_item, "at": utc_now(), **values}
            self.events.append(record)
            stage = self.subtages.setdefault(self.current_substage or "ACQUISITION", {
                "status": "STARTED", "started_at": record["at"],
                "discovered": 0, "processed": 0, "saved": 0,
                "reused": 0, "failed": 0, "timed_out": 0,
                "network_requests": 0,
            })
            stage["status"] = state
            stage["updated_at"] = record["at"]
            for key in self.counters:
                if key in values and isinstance(values[key], (int, float)):
                    self.counters[key] = int(values[key])
                    stage[key] = int(values[key])
            if state == "PROGRESS":
                for key, value in values.items():
                    if key in self.counters and isinstance(value, int):
                        self.counters[key] = value
                        stage[key] = value
            return self._write()

    def start(self, substage: str, *, item: str | None = None, **values: Any) -> dict[str, Any]:
        return self._emit("STARTED", meaningful=False, substage=substage, item=item, **values)

    def progress(self, substage: str | None = None, *, item: str | None = None, **values: Any) -> dict[str, Any]:
        return self._emit("PROGRESS", meaningful=True, substage=substage, item=item, **values)

    def complete(self, substage: str | None = None, *, item: str | None = None, **values: Any) -> dict[str, Any]:
        return self._emit("COMPLETE", meaningful=True, substage=substage, item=item, **values)

    def partial(self, substage: str | None = None, *, item: str | None = None, **values: Any) -> dict[str, Any]:
        return self._emit("PARTIAL", meaningful=True, substage=substage, item=item, **values)

    def failed(self, substage: str | None = None, *, item: str | None = None, **values: Any) -> dict[str, Any]:
        return self._emit("FAILED", meaningful=False, substage=substage, item=item, **values)

    def timed_out(self, substage: str | None = None, *, item: str | None = None, **values: Any) -> dict[str, Any]:
        self.counters["timed_out"] += 1
        return self._emit("TIMED_OUT", meaningful=False, substage=substage, item=item, timed_out=self.counters["timed_out"], **values)

    def skipped(self, substage: str | None = None, *, item: str | None = None, **values: Any) -> dict[str, Any]:
        return self._emit("SKIPPED", meaningful=False, substage=substage, item=item, **values)

    def interrupted(self, reason: str = "KeyboardInterrupt") -> dict[str, Any]:
        self.status = "INTERRUPTED"
        return self._emit("INTERRUPTED", meaningful=False, item=self.current_item, reason=reason)

    def check(self) -> None:
        elapsed = self.elapsed
        no_progress = self.no_progress_for
        diagnostics = {
            "ticker": self.ticker,
            "current_substage": self.current_substage,
            "current_item": self.current_item,
            "elapsed_seconds": round(elapsed, 3),
            "no_progress_for_seconds": round(no_progress, 3),
            "last_progress_at": getattr(self, "_last_progress_at", None),
            "counters": dict(self.counters),
        }
        if self.deadline > 0 and elapsed >= self.deadline:
            self.status = "TIMED_OUT"
            self.timed_out(self.current_substage, item=self.current_item, reason="global_deadline", **diagnostics)
            raise AcquisitionDeadlineExceeded("Acquisition issuer deadline exceeded", diagnostics)
        if self.no_progress_timeout > 0 and no_progress >= self.no_progress_timeout:
            self.status = "TIMED_OUT"
            self.timed_out(self.current_substage, item=self.current_item, reason="no_progress", **diagnostics)
            raise AcquisitionProgressTimeout("No meaningful acquisition progress", diagnostics)

    def finalize(self, status: str = "COMPLETE", **values: Any) -> dict[str, Any]:
        self.status = status
        return self._emit(status, meaningful=False, **values)


def load_progress(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None

