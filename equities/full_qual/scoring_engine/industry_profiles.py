"""Data-driven industry and horizon overrides for scoring profiles."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import FactorDefinition, ProfileDefinition


def apply_industry_adapter(profile: ProfileDefinition, industry: str | None = None, horizon: str | None = None) -> ProfileDefinition:
    """Return an adapted profile without mutating the configured base profile.

    Overrides are optional JSON data.  Unknown industries/horizons retain the
    base design weights and are explicitly marked as design weights.
    """

    result = deepcopy(profile)
    industry_key = str(industry or "").lower()
    horizon_key = str(horizon or "").lower()
    override = result.industry_overrides.get(industry_key, {}) if industry_key else {}
    horizon_override = result.horizon_overrides.get(horizon_key, {}) if horizon_key else {}
    factor_weights = {}
    factor_weights.update(override.get("factor_weights", {}) if isinstance(override, dict) else {})
    factor_weights.update(horizon_override.get("factor_weights", {}) if isinstance(horizon_override, dict) else {})
    pillar_weights = {}
    pillar_weights.update(override.get("pillar_weights", {}) if isinstance(override, dict) else {})
    pillar_weights.update(horizon_override.get("pillar_weights", {}) if isinstance(horizon_override, dict) else {})
    if pillar_weights and abs(sum(float(value) for value in pillar_weights.values()) - 1.0) <= 1e-9:
        result.pillar_weights.update({key: float(value) for key, value in pillar_weights.items()})
    for factor in result.factors:
        if factor.factor_key in factor_weights:
            factor.weight = float(factor_weights[factor.factor_key])
            factor.weight_status = "DESIGN_WEIGHT"
        if horizon:
            factor.horizon = horizon
    return result


__all__ = ["apply_industry_adapter"]
