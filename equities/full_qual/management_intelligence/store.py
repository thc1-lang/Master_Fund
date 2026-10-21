"""JSONL persistence for factual management intelligence records."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, TypeVar

from .models import BoardMembership, CareerEntry, EducationEntry, InsiderIdentityLink, ManagementPerson, ManagementRun, PersonCompanyRelationship, RoleAssertion, RoleChangeEvent
from .source_registry import ManagementSourceRegistry
from qualitative_analysis.extraction_safety import validate_extracted_text


T = TypeVar("T")


class ManagementStore:
    def __init__(self, root: str | Path = "artifacts/management_intelligence"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sources = ManagementSourceRegistry(self.root)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.jsonl"

    def _read(self, name: str, cls: type[T]) -> list[T]:
        path = self._path(name)
        if not path.exists():
            return []
        result: list[T] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                result.append(cls.from_dict(json.loads(line)))  # type: ignore[attr-defined]
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return result

    def _upsert_typed(self, name: str, values: Iterable[Any], key: str, cls: type[Any]) -> dict[str, int]:
        items = list(values)
        previous = self._read(name, cls)
        existing = {str(getattr(item, key)): item for item in previous}
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        for item in items:
            if name == "roles":
                title = str(getattr(item, "title_as_reported", None) or getattr(item, "role", ""))
                if title.isdigit() or title.upper().startswith("%PDF-"):
                    raise ValueError("RoleAssertion title is not a valid human role")
                try:
                    validate_extracted_text(getattr(item, "evidence_text", ""))
                except ValueError as exc:
                    raise ValueError(f"RoleAssertion evidence is invalid: {exc}") from exc
            if name in {"people", "boards", "career_history"}:
                candidate_names = {
                    "people": getattr(item, "full_name", ""),
                    "boards": getattr(item, "person_id", ""),
                    "career_history": getattr(item, "employer", ""),
                }
                for field_name, candidate in candidate_names.items():
                    text = str(candidate or "").strip()
                    if text.isdigit() or text.upper().startswith("%PDF-"):
                        raise ValueError(f"{cls.__name__} {field_name} is malformed")
            if name in {"people", "boards", "career_history"}:
                try:
                    validate_extracted_text(getattr(item, "evidence_text", ""))
                except ValueError as exc:
                    raise ValueError(f"{cls.__name__} evidence is invalid: {exc}") from exc
            item_key = str(getattr(item, key))
            old = existing.get(item_key)
            if old is not None and old.to_dict() == item.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if old else "added"] += 1
                existing[item_key] = item
        self._write_all(name, existing.values(), key)
        return counts

    def _write_all(self, name: str, values: Iterable[Any], key: str) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f"management-{name}-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        path = Path(temporary)
        try:
            ordered = sorted(values, key=lambda item: str(getattr(item, key)))
            path.write_text("".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in ordered), encoding="utf-8")
            os.replace(path, self._path(name))
        finally:
            if path.exists():
                path.unlink()

    @staticmethod
    def _source_accessions(item: Any) -> set[str]:
        """Return every SEC accession carried by a derived record.

        A forced parser rebuild must replace records emitted from the source
        being rebuilt.  Looking at the provenance fields keeps that cleanup
        source-scoped and leaves records from older filings intact.
        """
        values: set[str] = set()
        for field in ("accession_number", "source_accession", "source_accession_number"):
            value = getattr(item, field, None)
            if value:
                values.add(str(value))
        for field in ("source_accession_numbers", "source_accessions"):
            value = getattr(item, field, None)
            if isinstance(value, (list, tuple, set)):
                values.update(str(item) for item in value if item)
        return values

    def remove_source_records(self, ticker: str, accession_number: str) -> dict[str, int]:
        """Remove stale derived rows emitted from one SEC filing.

        This is used immediately before an explicit/forced source rebuild.
        It never touches the SEC source registry or downloaded source cache,
        and it deliberately preserves rows whose provenance belongs to other
        filings (including later role-change filings).
        """
        ticker = ticker.strip().upper()
        accession = str(accession_number or "").strip()
        if not ticker or not accession:
            return {}
        specs = (
            ("people", ManagementPerson),
            ("roles", RoleAssertion),
            ("career_history", CareerEntry),
            ("board_history", BoardMembership),
            ("relationships", PersonCompanyRelationship),
            ("education", EducationEntry),
            ("insider_links", InsiderIdentityLink),
        )
        removed: dict[str, int] = {}
        for name, cls in specs:
            previous = self._read(name, cls)
            kept = []
            count = 0
            for item in previous:
                item_ticker = str(getattr(item, "ticker", "") or getattr(item, "board_ticker", "")).upper()
                if item_ticker == ticker and accession in self._source_accessions(item):
                    count += 1
                else:
                    kept.append(item)
            if count:
                key = {
                    "people": "person_id", "roles": "assertion_id", "career_history": "career_id",
                    "board_history": "board_id", "relationships": "person_company_id",
                    "education": "education_id", "insider_links": "link_id",
                }[name]
                self._write_all(name, kept, key)
            removed[name] = count
        return removed

    def upsert_people(self, values: Iterable[ManagementPerson]) -> dict[str, int]:
        return self._upsert_typed("people", values, "person_id", ManagementPerson)

    def upsert_roles(self, values: Iterable[RoleAssertion]) -> dict[str, int]:
        return self._upsert_typed("roles", values, "assertion_id", RoleAssertion)

    def upsert_career(self, values: Iterable[CareerEntry]) -> dict[str, int]:
        return self._upsert_typed("career_history", values, "career_id", CareerEntry)

    def upsert_boards(self, values: Iterable[BoardMembership]) -> dict[str, int]:
        return self._upsert_typed("board_history", values, "board_id", BoardMembership)

    def upsert_relationships(self, values: Iterable[PersonCompanyRelationship]) -> dict[str, int]:
        return self._upsert_typed("relationships", values, "person_company_id", PersonCompanyRelationship)

    def upsert_education(self, values: Iterable[EducationEntry]) -> dict[str, int]:
        return self._upsert_typed("education", values, "education_id", EducationEntry)

    def upsert_insider_links(self, values: Iterable[InsiderIdentityLink]) -> dict[str, int]:
        return self._upsert_typed("insider_links", values, "link_id", InsiderIdentityLink)

    def upsert_role_changes(self, values: Iterable[RoleChangeEvent]) -> dict[str, int]:
        return self._upsert_typed("role_changes", values, "event_id", RoleChangeEvent)

    def record_run(self, run: ManagementRun) -> None:
        self._upsert_typed("runs", [run], "run_id", ManagementRun)

    def list_people(self, ticker: str | None = None) -> list[ManagementPerson]:
        values = self._read("people", ManagementPerson)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_roles(self, ticker: str | None = None) -> list[RoleAssertion]:
        values = self._read("roles", RoleAssertion)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_career(self, ticker: str | None = None) -> list[CareerEntry]:
        values = self._read("career_history", CareerEntry)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_boards(self, ticker: str | None = None) -> list[BoardMembership]:
        values = self._read("board_history", BoardMembership)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_relationships(self, ticker: str | None = None) -> list[PersonCompanyRelationship]:
        values = self._read("relationships", PersonCompanyRelationship)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_education(self, ticker: str | None = None) -> list[EducationEntry]:
        values = self._read("education", EducationEntry)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_insider_links(self, ticker: str | None = None) -> list[InsiderIdentityLink]:
        values = self._read("insider_links", InsiderIdentityLink)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_role_changes(self, ticker: str | None = None) -> list[RoleChangeEvent]:
        values = self._read("role_changes", RoleChangeEvent)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_runs(self) -> list[ManagementRun]:
        return self._read("runs", ManagementRun)


__all__ = ["ManagementStore"]
