"""Shared numeric normalization and conservative arithmetic helpers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


class NumericValidationError(ValueError):
    pass


SCALE_FACTORS = {
    "unit": 1.0,
    "units": 1.0,
    "thousand": 1_000.0,
    "k": 1_000.0,
    "million": 1_000_000.0,
    "mm": 1_000_000.0,
    "m": 1_000_000.0,
    "billion": 1_000_000_000.0,
    "bn": 1_000_000_000.0,
}


@dataclass(frozen=True)
class MetricValue:
    metric: str
    value: float | None
    unit: str
    currency: str | None = None
    scale: str = "units"
    period: str | None = None
    period_type: str = "UNKNOWN"
    reported: bool = True
    accounting_basis: str | None = None
    source_id: str | None = None
    evidence_anchor: dict[str, Any] = field(default_factory=dict)
    status: str = "VERIFIED"
    precision: int | None = None

    def normalized_value(self) -> float | None:
        if self.value is None:
            return None
        factor = SCALE_FACTORS.get(str(self.scale or "").lower())
        if factor is None:
            raise NumericValidationError(f"unknown scale: {self.scale}")
        return float(self.value) * factor

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_number(value: Any, scale: str | None = None) -> float:
    if isinstance(value, bool):
        raise NumericValidationError("boolean is not a numeric value")
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError) as exc:
        raise NumericValidationError(f"invalid numeric value: {value!r}") from exc
    if not math.isfinite(number):
        raise NumericValidationError("numeric value must be finite")
    factor = SCALE_FACTORS.get(str(scale or "units").lower())
    if factor is None:
        raise NumericValidationError(f"unknown scale: {scale}")
    return number * factor


def percentage(value: Any, *, allow_ratio: bool = False) -> float:
    number = normalize_number(value)
    if allow_ratio and 0 <= number <= 1:
        number *= 100
    if not -100 <= number <= 100:
        raise NumericValidationError("percentage must be between -100 and 100")
    return number


def percentage_change(previous: float | None, current: float | None) -> float | None:
    if previous is None or current is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


def percentage_point_change(previous: float | None, current: float | None) -> float | None:
    """Difference in percentage points, distinct from percent growth."""
    if previous is None or current is None:
        return None
    return float(current) - float(previous)


def basis_points(value: Any) -> float:
    """Validate and return a basis-point value without converting it to a percent."""
    number = normalize_number(value)
    if not math.isfinite(number):
        raise NumericValidationError("basis points must be finite")
    return number


def range_midpoint(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    if low > high:
        raise NumericValidationError("range low exceeds high")
    return (low + high) / 2.0


def validate_metric(value: MetricValue | dict[str, Any]) -> list[str]:
    data = value.to_dict() if isinstance(value, MetricValue) else dict(value)
    errors: list[str] = []
    if not str(data.get("metric") or "").strip():
        errors.append("metric is required")
    if data.get("value") is not None:
        try:
            normalize_number(data["value"], data.get("scale"))
        except NumericValidationError as exc:
            errors.append(str(exc))
    unit = str(data.get("unit") or "").lower()
    if unit in {"%", "percent", "percentage"} and data.get("value") is not None:
        try:
            percentage(data["value"])
        except NumericValidationError as exc:
            errors.append(str(exc))
    if data.get("period_type") not in {"UNKNOWN", "QUARTER_ACTUAL", "ANNUAL_ACTUAL", "GUIDANCE_HORIZON", "YTD", "POINT_IN_TIME"}:
        errors.append("unsupported period_type")
    return errors


def compare_values(previous: MetricValue, current: MetricValue) -> dict[str, Any]:
    if previous.metric != current.metric or previous.unit != current.unit or previous.currency != current.currency:
        return {"status": "NOT_COMPARABLE", "reason": "metric or unit mismatch"}
    left, right = previous.normalized_value(), current.normalized_value()
    if left is None or right is None:
        return {"status": "UNKNOWN", "reason": "missing value"}
    return {"status": "COMPARABLE", "absolute_change": right - left, "percentage_change": percentage_change(left, right)}


__all__ = [
    "NumericValidationError", "MetricValue", "normalize_number", "percentage",
    "percentage_change", "percentage_point_change", "basis_points", "range_midpoint", "validate_metric", "compare_values",
]
