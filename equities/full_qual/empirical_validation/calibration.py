"""Calibration refusal and manually reviewable gold-standard infrastructure.

The production scoring profile is intentionally not called calibrated until a
reviewer has supplied labelled examples.  This module stores those labels in
an append-only, deterministic JSONL file and exposes only descriptive metrics;
it never invents a benchmark or turns a small pilot into a probability.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import CalibrationCandidate
from .profile_builder import build_candidate


ANNOTATION_VALUES = frozenset({
    "CORRECT_EXTRACTION", "INCORRECT_EXTRACTION", "MISSED_DISCLOSURE",
    "FALSE_POSITIVE", "CORRECT_ENTITY", "INCORRECT_ENTITY",
    "CORRECT_PERIOD", "INCORRECT_PERIOD", "CORRECT_NUMBER",
    "INCORRECT_NUMBER", "CORRECT_INFERENCE", "INCORRECT_INFERENCE",
    "MATERIAL", "IMMATERIAL",
})


@dataclass
class GoldStandardAnnotation:
    """One reviewer label tied to an auditable source record."""

    annotation_id: str
    record_id: str
    ticker: str
    annotation_type: str
    label: str
    reviewer: str = ""
    source_id: str = ""
    normalized_document_id: str = ""
    evidence_anchor: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        self.ticker = str(self.ticker).upper().strip()
        self.annotation_type = str(self.annotation_type).upper().strip()
        self.label = str(self.label).upper().strip()
        if not self.annotation_id or not self.record_id or not self.ticker:
            raise ValueError("annotation_id, record_id and ticker are required")
        if self.annotation_type not in ANNOTATION_VALUES:
            raise ValueError(f"unsupported annotation type: {self.annotation_type}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GoldStandardAnnotation":
        fields = {item.name for item in cls.__dataclass_fields__.values()}
        data = {key: value[key] for key in fields if key in value}
        data.setdefault("evidence_anchor", {})
        return cls(**data)


@dataclass(frozen=True)
class CalibrationSummary:
    status: str
    annotation_count: int
    distinct_companies: int
    distinct_dates: int
    metrics: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GoldStandardStore:
    """Deterministic, manually reviewable annotation store."""

    def __init__(self, root: str | Path = "artifacts/empirical_validation/gold_standard"):
        path = Path(root)
        self.root = path.parent if path.suffix else path
        self.path = path if path.suffix else self.root / "annotations.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, GoldStandardAnnotation]:
        values: dict[str, GoldStandardAnnotation] = {}
        if not self.path.exists():
            return values
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = GoldStandardAnnotation.from_dict(json.loads(line))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            values[item.annotation_id] = item
        return values

    def list_all(self, ticker: str | None = None) -> list[GoldStandardAnnotation]:
        values = list(self._read().values())
        if ticker:
            values = [item for item in values if item.ticker == ticker.upper()]
        return sorted(values, key=lambda item: (item.ticker, item.annotation_id))

    def upsert(self, annotation: GoldStandardAnnotation) -> str:
        values = self._read()
        previous = values.get(annotation.annotation_id)
        action = "added" if previous is None else "unchanged" if previous.to_dict() == annotation.to_dict() else "updated"
        if action == "unchanged":
            return action
        values[annotation.annotation_id] = annotation
        fd, temporary = tempfile.mkstemp(prefix="gold-standard-", suffix=".tmp", dir=self.root)
        os.close(fd)
        temp = Path(temporary)
        try:
            temp.write_text("".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in sorted(values.values(), key=lambda item: (item.ticker, item.annotation_id))), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            if temp.exists():
                temp.unlink()
        return action


def calibration_summary(annotations: Iterable[GoldStandardAnnotation], *, minimum_annotations: int = 100, minimum_companies: int = 10, minimum_dates: int = 3) -> CalibrationSummary:
    values = list(annotations)
    companies = {item.ticker for item in values}
    dates = {item.created_at[:10] for item in values if item.created_at}
    reasons: list[str] = []
    if len(values) < minimum_annotations:
        reasons.append("MINIMUM_ANNOTATIONS_NOT_MET")
    if len(companies) < minimum_companies:
        reasons.append("MINIMUM_UNIQUE_COMPANIES_NOT_MET")
    if len(dates) < minimum_dates:
        reasons.append("MINIMUM_UNIQUE_DATES_NOT_MET")
    counts: dict[str, int] = {}
    for item in values:
        counts[item.annotation_type] = counts.get(item.annotation_type, 0) + 1
    return CalibrationSummary(
        status="NOT_YET_ESTABLISHED" if reasons else "READY_FOR_REVIEW",
        annotation_count=len(values), distinct_companies=len(companies), distinct_dates=len(dates),
        metrics={"annotation_types": counts}, reasons=reasons,
    )


def calibration_refusal(observations, config):
    dates={o.as_of_date for o in observations}; companies={o.ticker for o in observations}
    reasons=[]
    if len(observations)<config.minimum_observations: reasons.append("MINIMUM_OBSERVATIONS_NOT_MET")
    if len(companies)<config.minimum_unique_companies: reasons.append("MINIMUM_UNIQUE_COMPANIES_NOT_MET")
    if len(dates)<config.minimum_unique_dates: reasons.append("MINIMUM_UNIQUE_DATES_NOT_MET")
    return {"status":"INSUFFICIENT_HISTORICAL_DATA" if reasons else "READY_FOR_EXPLORATORY_CALIBRATION","reasons":reasons,"candidate_created":False}
