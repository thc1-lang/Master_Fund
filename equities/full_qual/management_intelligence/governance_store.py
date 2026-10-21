"""Deterministic JSONL store for Board & Governance Intelligence (6C)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, TypeVar

from .board_governance_models import (
    BoardCommittee, BoardExpertise, BoardRefreshment, BoardTenure,
    GovernanceBoardMembership, GovernanceCoverage, GovernanceEvent,
    GovernanceRun, GovernanceSnapshot, OverboardingRecord,
    RelatedPartyRelationship, ShareholderRights, VotingClass,
    VotingControlSnapshot,
)


T = TypeVar("T")

_LOGGER = logging.getLogger(__name__)
_TRANSACTION_LOCK = threading.RLock()


class GovernanceStore:
    """Stable-key JSONL persistence with idempotent upserts."""

    SPECS: dict[str, tuple[str, type[Any]]] = {
        "governance_board_memberships": ("board_membership_id", GovernanceBoardMembership),
        "governance_committees": ("committee_id", BoardCommittee),
        "governance_tenure": ("tenure_id", BoardTenure),
        "governance_refreshment": ("refreshment_id", BoardRefreshment),
        "governance_expertise": ("expertise_id", BoardExpertise),
        "governance_overboarding": ("overboarding_id", OverboardingRecord),
        "governance_related_parties": ("relationship_id", RelatedPartyRelationship),
        "governance_voting_classes": ("voting_class_id", VotingClass),
        "governance_voting_control": ("voting_control_id", VotingControlSnapshot),
        "governance_shareholder_rights": ("rights_id", ShareholderRights),
        "governance_events": ("event_id", GovernanceEvent),
        "governance_coverage": ("coverage_id", GovernanceCoverage),
        "governance_snapshots": ("snapshot_id", GovernanceSnapshot),
        "governance_runs": ("run_id", GovernanceRun),
    }

    def __init__(self, root: str | Path = "artifacts/management_intelligence"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.generations_root = self.root / "governance_generations"
        self.generations_root.mkdir(parents=True, exist_ok=True)
        self.generation_manifest = self.root / "governance_generation.json"

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.jsonl"

    def _read_path(self, path: Path, cls: type[T]) -> list[T]:
        if not path.exists():
            return []
        values: list[T] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                values.append(cls.from_dict(json.loads(line)))  # type: ignore[attr-defined]
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return values

    def _generation_state(self) -> dict[str, Any]:
        if not self.generation_manifest.exists():
            return {"schema_version": "governance-generation-v1", "tickers": {}}
        try:
            value = json.loads(self.generation_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"schema_version": "governance-generation-v1", "tickers": {}}
        return value if isinstance(value, dict) else {"schema_version": "governance-generation-v1", "tickers": {}}

    def _read(self, name: str, cls: type[T], ticker: str | None = None) -> list[T]:
        """Read the active generation for a ticker, falling back to legacy JSONL."""
        state = self._generation_state()
        if ticker:
            row = (state.get("tickers") or {}).get(ticker.upper())
            if isinstance(row, dict):
                # A failed/staging generation deliberately exposes no current
                # rows.  Its previous generation remains on disk for recovery,
                # but is never mistaken for current-run evidence.
                if str(row.get("status") or "").upper() != "ACTIVE":
                    return []
                generation = row.get("generation_id")
                if generation:
                    return self._read_path(self.generations_root / str(generation) / f"{name}.jsonl", cls)
        return self._read_path(self._path(name), cls)

    def current_generation(self, ticker: str) -> dict[str, Any] | None:
        row = (self._generation_state().get("tickers") or {}).get(ticker.upper())
        return dict(row) if isinstance(row, dict) else None

    def current_status(self, ticker: str) -> str | None:
        row = self.current_generation(ticker)
        return str(row.get("status")) if row else None

    def mark_failed(self, ticker: str, *, run_id: str, reason: str) -> dict[str, Any]:
        """Invalidate the current ticker view without deleting last-good files."""
        state = self._generation_state()
        previous = state.setdefault("tickers", {}).get(ticker.upper())
        state.setdefault("tickers", {})[ticker.upper()] = {
            "ticker": ticker.upper(), "status": "FAILED", "run_id": run_id,
            "reason": reason, "updated_at": time.time(),
            "previous_generation_id": (previous or {}).get("generation_id") if isinstance(previous, dict) else None,
        }
        self._write_json_atomic(self.generation_manifest, state, generation_id=run_id)
        return state["tickers"][ticker.upper()]

    def _write_json_atomic(self, destination: Path, value: Any, *, generation_id: str = "") -> None:
        self._atomic_replace(destination, json.dumps(value, indent=2, sort_keys=True) + "\n", generation_id=generation_id)

    def _atomic_replace(self, destination: Path, content: str, *, generation_id: str = "", retries: int = 6) -> None:
        """Write and atomically replace a same-directory file safely on Windows."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f"governance-{destination.stem}-", suffix=".tmp", dir=destination.parent)
        temporary = Path(temporary_name)
        started = time.monotonic()
        try:
            # The descriptor is closed by the context manager before replace;
            # this is essential on Windows where an open handle blocks rename.
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            for attempt in range(1, retries + 1):
                try:
                    os.replace(str(temporary), str(destination))
                    with destination.open("r", encoding="utf-8") as verify:
                        verify.read(1)
                    return
                except (PermissionError, OSError) as exc:
                    winerror = getattr(exc, "winerror", None)
                    errno = getattr(exc, "errno", None)
                    transient = isinstance(exc, PermissionError) or winerror in {5, 32, 33} or errno in {13, 16, 26}
                    readable = False
                    try:
                        with destination.open("r", encoding="utf-8"):
                            readable = True
                    except OSError:
                        pass
                    _LOGGER.warning(
                        "governance atomic replace failed: destination=%s temp=%s attempt=%s elapsed=%.3f errno=%s winerror=%s destination_exists=%s temp_exists=%s destination_readable=%s generation=%s",
                        destination, temporary, attempt, time.monotonic() - started, errno, winerror,
                        destination.exists(), temporary.exists(), readable, generation_id,
                    )
                    if not transient or attempt >= retries:
                        raise
                    time.sleep(min(0.05 * (2 ** (attempt - 1)), 0.5))
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                _LOGGER.warning("unable to clean governance temp file: %s generation=%s", temporary, generation_id)

    def _write_all(self, name: str, values: Iterable[Any], key: str) -> None:
        ordered = sorted(values, key=lambda item: str(getattr(item, key)))
        content = "".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in ordered)
        self._atomic_replace(self._path(name), content, generation_id="legacy")

    def upsert(self, name: str, values: Iterable[Any]) -> dict[str, int]:
        if name not in self.SPECS:
            raise KeyError(name)
        key, cls = self.SPECS[name]
        incoming = list(values)
        existing = {str(getattr(item, key)): item for item in self._read(name, cls)}
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        for item in incoming:
            item_key = str(getattr(item, key))
            old = existing.get(item_key)
            if old is not None and old.to_dict() == item.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if old is not None else "added"] += 1
                existing[item_key] = item
        self._write_all(name, existing.values(), key)
        return counts

    def replace_ticker(self, name: str, ticker: str, values: Iterable[Any]) -> dict[str, int]:
        """Replace one issuer's materialized view while retaining other issuers."""
        if name not in self.SPECS:
            raise KeyError(name)
        key, cls = self.SPECS[name]
        incoming = list(values)
        previous = self._read(name, cls)
        existing = {str(getattr(item, key)): item for item in previous if getattr(item, "ticker", "").upper() != ticker.upper()}
        old_for_ticker = {str(getattr(item, key)): item for item in previous if getattr(item, "ticker", "").upper() == ticker.upper()}
        incoming_keys = {str(getattr(item, key)) for item in incoming}
        counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": sum(1 for old_key in old_for_ticker if old_key not in incoming_keys)}
        for item in incoming:
            item_key = str(getattr(item, key))
            old = old_for_ticker.get(item_key)
            if old is not None and old.to_dict() == item.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if old is not None else "added"] += 1
            existing[item_key] = item
        self._write_all(name, existing.values(), key)
        return counts

    def replace_ticker_transaction(self, ticker: str, records: dict[str, Iterable[Any]], *, run_id: str | None = None) -> dict[str, dict[str, int]]:
        """Stage every governance table, then publish one active generation."""
        with _TRANSACTION_LOCK:
            ticker = ticker.upper()
            generation_id = (run_id or f"governance-{ticker}-{uuid.uuid4().hex}").replace(":", "-").replace("\\", "-").replace("/", "-")
            generation_dir = self.generations_root / generation_id
            if generation_dir.exists():
                generation_id = f"{generation_id}-{uuid.uuid4().hex[:10]}"
                generation_dir = self.generations_root / generation_id
            generation_dir.mkdir(parents=True, exist_ok=False)
            state = self._generation_state()
            previous = state.setdefault("tickers", {}).get(ticker)
            state["tickers"][ticker] = {
                "ticker": ticker, "status": "STAGING", "generation_id": generation_id,
                "run_id": generation_id, "previous_generation_id": (previous or {}).get("generation_id") if isinstance(previous, dict) else None,
            }
            # The staging marker is intentionally visible: readers return no current
            # rows until ACTIVE, while the prior generation remains recoverable.
            self._write_json_atomic(self.generation_manifest, state, generation_id=generation_id)
            counts: dict[str, dict[str, int]] = {}
            try:
                for name, incoming_values in records.items():
                    if name not in self.SPECS:
                        continue
                    key, cls = self.SPECS[name]
                    incoming = list(incoming_values)
                    current = {str(getattr(item, key)): item for item in self._read(name, cls) if str(getattr(item, "ticker", "")).upper() != ticker}
                    old_for_ticker = {str(getattr(item, key)): item for item in self._read(name, cls, ticker)}
                    incoming_keys = {str(getattr(item, key)) for item in incoming}
                    stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": sum(1 for old_key in old_for_ticker if old_key not in incoming_keys)}
                    for item in incoming:
                        item_key = str(getattr(item, key))
                        old = old_for_ticker.get(item_key)
                        if old is not None and old.to_dict() == item.to_dict(): stats["unchanged"] += 1
                        else: stats["updated" if old is not None else "added"] += 1
                        current[item_key] = item
                    ordered = sorted(current.values(), key=lambda item: str(getattr(item, key)))
                    content = "".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in ordered)
                    self._atomic_replace(generation_dir / f"{name}.jsonl", content, generation_id=generation_id)
                    counts[name] = stats
                # Validate every staged table before publication.
                for name in records:
                    if name in self.SPECS:
                        _, cls = self.SPECS[name]
                        self._read_path(generation_dir / f"{name}.jsonl", cls)
                state["tickers"][ticker].update({"status": "ACTIVE", "activated_at": time.time()})
                self._write_json_atomic(self.generation_manifest, state, generation_id=generation_id)
                # Keep legacy paths available for older consumers. Readers that
                # understand generations use the immutable generation directory.
                for name in records:
                    staged = generation_dir / f"{name}.jsonl"
                    if staged.exists():
                        self._atomic_replace(self._path(name), staged.read_text(encoding="utf-8"), generation_id=generation_id)
                return counts
            except Exception:
                failed = state.setdefault("tickers", {}).setdefault(ticker, {})
                failed.update({"status": "FAILED", "failed_at": time.time(), "reason": "TRANSACTION_COMMIT_FAILED"})
                try:
                    self._write_json_atomic(self.generation_manifest, state, generation_id=generation_id)
                except OSError:
                    pass
                raise

    def list(self, name: str, ticker: str | None = None) -> list[Any]:
        if name not in self.SPECS:
            raise KeyError(name)
        _, cls = self.SPECS[name]
        values = self._read(name, cls, ticker)
        return [item for item in values if ticker is None or getattr(item, "ticker", "").upper() == ticker.upper()]

    def counts(self, ticker: str | None = None) -> dict[str, int]:
        return {name: len(self.list(name, ticker)) for name in self.SPECS}

    def upsert_all(self, records: dict[str, Iterable[Any]]) -> dict[str, dict[str, int]]:
        return {name: self.upsert(name, values) for name, values in records.items() if name in self.SPECS}

    def record_run(self, run: GovernanceRun) -> dict[str, int]:
        return self.upsert("governance_runs", [run])


__all__ = ["GovernanceStore"]
