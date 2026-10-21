"""Non-destructive migrations from frozen v1 artifacts to v2 metadata."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .v2_models import CORE_VERSION, RESEARCH_OUTPUT_VERSION


def migrate_record(record: dict[str, Any], *, record_type: str = "record") -> dict[str, Any]:
    value = dict(record)
    value.setdefault("schema_version", value.get("version") or CORE_VERSION)
    value.setdefault("research_output_version", RESEARCH_OUTPUT_VERSION if record_type in {"dossier", "research_output"} else None)
    value.setdefault("migration_status", "MIGRATED_METADATA_ONLY")
    value.setdefault("legacy_record_type", record_type)
    return value


def migrate_jsonl(source: str | Path, destination: str | Path | None = None, *, record_type: str = "record") -> Path:
    source_path = Path(source)
    destination_path = Path(destination) if destination else source_path.with_name(source_path.stem + "_v2" + source_path.suffix)
    rows = []
    if source_path.exists():
        for line in source_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(migrate_record(value, record_type=record_type))
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination_path.stem + "-", suffix=".tmp", dir=destination_path.parent)
    os.close(fd)
    temp = Path(temporary)
    try:
        temp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in sorted(rows, key=lambda row: str(row.get("record_id") or row.get("document_id") or row.get("claim_id") or ""))), encoding="utf-8")
        os.replace(temp, destination_path)
    finally:
        if temp.exists():
            temp.unlink()
    return destination_path


__all__ = ["migrate_record", "migrate_jsonl"]
