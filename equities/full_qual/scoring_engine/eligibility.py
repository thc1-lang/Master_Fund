"""Release gating for partial and uncalibrated scores."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class EligibilityDecision:
    calculated_score: float | None
    released_score: float | None
    score_eligible: bool
    eligibility_reason: str
    evidence_coverage: float
    scoring_coverage: float
    calibration_status: str
    horizon: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_score_eligibility(*, calculated_score: float | None, evidence_coverage: float, scoring_coverage: float, minimum_coverage: float, critical_missing: Iterable[str] = (), calibration_status: str = "NOT_YET_ESTABLISHED", horizon: str = "STRUCTURAL_AND_NEAR_TERM", require_calibration: bool = False) -> EligibilityDecision:
    missing = list(critical_missing)
    if calculated_score is None:
        reason = "NO_CALCULATED_SCORE"
    elif missing:
        reason = "CRITICAL_FACTORS_MISSING:" + ",".join(sorted(missing))
    elif scoring_coverage < minimum_coverage:
        reason = f"SCORING_COVERAGE_BELOW_THRESHOLD:{scoring_coverage:.4f}<{minimum_coverage:.4f}"
    elif require_calibration and calibration_status not in {"CALIBRATED", "VALIDATED"}:
        reason = "CALIBRATION_NOT_ESTABLISHED"
    else:
        return EligibilityDecision(calculated_score, calculated_score, True, "ELIGIBLE", max(0.0, min(1.0, evidence_coverage)), max(0.0, min(1.0, scoring_coverage)), calibration_status, horizon)
    return EligibilityDecision(calculated_score, None, False, reason, max(0.0, min(1.0, evidence_coverage)), max(0.0, min(1.0, scoring_coverage)), calibration_status, horizon)


def factor_is_eligible(factor: Mapping[str, Any], *, minimum_coverage: float = 0.0) -> bool:
    return bool(factor.get("status") == "SCORED" and factor.get("score") is not None and float(factor.get("scoring_coverage", factor.get("coverage", 0.0)) or 0.0) >= minimum_coverage)


def release_score(snapshot: Mapping[str, Any], *, minimum_coverage: float, critical_missing: Iterable[str] = (), require_calibration: bool = False) -> dict[str, Any]:
    decision = decide_score_eligibility(
        calculated_score=snapshot.get("calculated_score", snapshot.get("overall_score")),
        evidence_coverage=float(snapshot.get("overall_evidence_coverage", snapshot.get("overall_coverage", 0.0)) or 0.0),
        scoring_coverage=float(snapshot.get("overall_scoring_coverage", snapshot.get("overall_coverage", 0.0)) or 0.0),
        minimum_coverage=minimum_coverage,
        critical_missing=critical_missing or snapshot.get("critical_missing_factors", []),
        calibration_status=str(snapshot.get("calibration_status") or "NOT_YET_ESTABLISHED"),
        horizon=str(snapshot.get("horizon") or "STRUCTURAL_AND_NEAR_TERM"),
        require_calibration=require_calibration,
    )
    result = dict(snapshot)
    result.update(decision.to_dict())
    result["overall_score"] = decision.released_score
    result["score_status"] = "RELEASED" if decision.score_eligible else "WITHHELD"
    return result


__all__ = ["EligibilityDecision", "decide_score_eligibility", "factor_is_eligible", "release_score"]
