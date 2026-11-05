"""Deterministic comparison of evidence-backed claims across periods."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from calendar import monthrange
from datetime import date, datetime, timezone
from typing import Any, Sequence

from .extraction_models import QualitativeClaim
from .temporal_models import TemporalChange, TemporalComparisonResult, TemporalEvidence
from .temporal_rules import classify_matched, classify_new, extract_metric, text_similarity
from .topic_normalization import TopicIdentity, topic_identity
from .fiscal_periods import canonical_period_label, canonical_period_identity


COMPARISON_VERSION = "temporal-comparison-v3"
RULES_VERSION = "temporal-rules-v2"
PROVIDER_VERSION = "deterministic-temporal-v3"

ACTUAL_PERIOD_TYPES = {"QUARTER_ACTUAL", "ANNUAL_ACTUAL", "YTD_ACTUAL"}

FUTURE_PERIODS_EXCLUDED = "FUTURE_PERIODS_EXCLUDED"
FUTURE_PERIOD_CANDIDATES = "FUTURE_PERIOD_CANDIDATES"
UNKNOWN_PERIOD_END_EXCLUDED = "UNKNOWN_PERIOD_END_EXCLUDED"
FORWARD_LOOKING_PERIODS_EXCLUDED = "FORWARD_LOOKING_PERIODS_EXCLUDED"
PERIODS_WITHOUT_OBSERVED_CLAIMS_EXCLUDED = "PERIODS_WITHOUT_OBSERVED_CLAIMS_EXCLUDED"


def period_type(value: Any) -> str:
    explicit = str(getattr(value, "period_type", "") or "").upper()
    if explicit and explicit != "UNKNOWN":
        return explicit
    label = str(getattr(value, "period_label", value) or "").upper()
    if re.search(r"\bQ[1-4]\b", label):
        return "QUARTER_ACTUAL"
    if re.search(r"\bFY\s*(?:19|20)\d{2}\b", label) or re.search(r"\b(?:19|20)\d{2}\b", label) and not re.search(r"\bQ[1-4]\b", label):
        return "ANNUAL_ACTUAL"
    return "UNKNOWN"


def compatible_period_types(left: Any, right: Any) -> bool:
    left_type, right_type = period_type(left), period_type(right)
    return left_type == right_type and left_type in ACTUAL_PERIOD_TYPES


def period_basis(value: Any) -> str:
    """Return the economic basis of a reporting period, never infer Q4."""
    explicit = str(getattr(value, "period_basis", "") or "").upper()
    if explicit in {"QUARTER_ONLY", "YTD", "ANNUAL", "TTM", "INTERIM"}:
        return explicit
    kind = period_type(value)
    return {
        "QUARTER_ACTUAL": "QUARTER_ONLY",
        "YTD_ACTUAL": "YTD",
        "ANNUAL_ACTUAL": "ANNUAL",
    }.get(kind, "UNKNOWN")


def comparison_sequence_relation(previous: Any, current: Any) -> tuple[str, str]:
    """Classify chronology separately from current-period validity."""
    left_type, right_type = period_type(previous), period_type(current)
    if left_type not in ACTUAL_PERIOD_TYPES or right_type not in ACTUAL_PERIOD_TYPES:
        return "INSUFFICIENT_COMPARABLE_PERIODS", "UNKNOWN"
    if left_type != right_type:
        if {left_type, right_type} == {"ANNUAL_ACTUAL", "QUARTER_ACTUAL"}:
            return "ANNUAL_TO_QUARTER", "MIXED_PERIOD_BASIS"
        return "INSUFFICIENT_COMPARABLE_PERIODS", "MIXED_PERIOD_BASIS"
    if left_type != "QUARTER_ACTUAL":
        return "CONTIGUOUS_QUARTER" if left_type == "YTD_ACTUAL" else "SAME_QUARTER_YOY", "NOT_APPLICABLE"
    left_key, right_key = period_sort_key(previous), period_sort_key(current)
    left_coord = int(left_key[0]) * 4 + int(left_key[1])
    right_coord = int(right_key[0]) * 4 + int(right_key[1])
    gap = right_coord - left_coord
    if gap == 1:
        return "CONTIGUOUS_QUARTER", "CONTIGUOUS"
    if right_key[1] == left_key[1] and int(right_key[0]) - int(left_key[0]) == 1:
        return "SAME_QUARTER_YOY", "YEAR_OVER_YEAR"
    if gap > 1:
        return "NON_CONTIGUOUS", f"GAP_{gap}_QUARTERS"
    return "INSUFFICIENT_COMPARABLE_PERIODS", "NON_FORWARD_SEQUENCE"


def claims_available_as_of(claims: Sequence[QualitativeClaim], as_of_date: str | None) -> list[QualitativeClaim]:
    """Admit known evidence only when its availability is on/before cutoff.

    Unknown availability is retained with its explicit UNKNOWN provenance so
    callers can apply the repository's conservative unknown-date policy rather
    than silently assigning a pre-cutoff date.
    """
    if not as_of_date:
        return list(claims)
    return [claim for claim in claims if not (getattr(claim, "public_availability_date", None) or claim.available_date) or str(getattr(claim, "public_availability_date", None) or claim.available_date)[:10] <= as_of_date]


def _date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _calendar_period_end(claim: QualitativeClaim) -> date | None:
    """Resolve a missing period end only from issuer calendar evidence.

    A default month-end calendar is safe for calendar-year issuers.  For
    non-calendar or week-based issuers we require an explicit boundary from
    the issuer calendar; guessing a nominal Q4 end would reintroduce the
    future-period leak this layer is intended to prevent.
    """
    explicit = _date_value(getattr(claim, "period_end_date", None))
    if explicit:
        return explicit
    year = getattr(claim, "fiscal_year", None)
    quarter = getattr(claim, "fiscal_quarter", None)
    calendar = dict(getattr(claim, "issuer_fiscal_calendar", {}) or {})
    if not isinstance(year, int) or quarter not in {1, 2, 3, 4}:
        return None

    boundaries = calendar.get("quarter_boundaries")
    if isinstance(boundaries, dict):
        for key in (f"Q{quarter}", str(quarter)):
            value = boundaries.get(key)
            if isinstance(value, dict):
                value = value.get("end_date")
            resolved = _date_value(value)
            if resolved:
                return resolved

    calendar_type = str(calendar.get("calendar_type") or "").lower()
    fiscal_end_month = calendar.get("fiscal_year_end_month")
    if calendar_type in {"", "calendar"} and fiscal_end_month in {None, 12, "12"}:
        # For a calendar-year issuer, the fiscal-year label is the ending
        # year and quarter ends are the ordinary calendar quarter ends.
        month = quarter * 3
        return date(year, month, monthrange(year, month)[1])
    return None


def _period_role(claim: QualitativeClaim) -> str:
    explicit = str(getattr(claim, "period_role", "") or "").upper()
    if explicit in {"OBSERVED_PERIOD", "FORWARD_LOOKING_PERIOD", "REFERENCE_PERIOD", "UNKNOWN_PERIOD_ROLE"}:
        return explicit
    # These optional fields are accepted for newer serialized claims without
    # requiring a claim-schema change for older artifacts.
    if getattr(claim, "referenced_period", None) and not getattr(claim, "observation_period", None):
        return "REFERENCE_PERIOD"
    status = str(getattr(claim, "temporal_status", "") or "").upper()
    if status in {"FUTURE", "FORWARD", "FORWARD_LOOKING", "PROSPECTIVE"}:
        return "FORWARD_LOOKING_PERIOD"
    return "OBSERVED_PERIOD"


def _period_candidate_label(values: Sequence[QualitativeClaim]) -> str:
    return period_label(values) if values else "UNKNOWN"


def eligible_observed_claims(
    claims: Sequence[QualitativeClaim],
    *,
    as_of_date: str | None = None,
) -> tuple[list[QualitativeClaim], dict[str, Any]]:
    """Filter claims to periods eligible for point-in-time chronology.

    This deliberately does not delete or rewrite claims.  It only controls
    which claims can supply an observed period to temporal comparison.  A
    guidance/reference claim remains in the extraction store and can be used
    by guidance, catalyst, or other forward-looking consumers.
    """
    cutoff = _date_value(as_of_date)
    groups = period_groups(claims)
    retained: list[QualitativeClaim] = []
    excluded: list[dict[str, str]] = []
    diagnostics: dict[str, Any] = {
        "future_period_candidates": [],
        "future_periods_excluded": [],
        "unknown_period_end": [],
        "forward_looking_periods": [],
        "periods_without_observed_claims": [],
        "duplicate_canonicalized": 0,
    }

    for _key, values in groups.items():
        # Count alternate representations of one fiscal coordinate, rather
        # than the many claims normally present in a period.
        representations = {
            (
                period_type(item),
                getattr(item, "fiscal_year", None),
                getattr(item, "fiscal_quarter", None),
                str(getattr(item, "period_end_date", "") or "")[:10] or "unknown",
            )
            for item in values
        }
        if len(representations) > 1:
            diagnostics["duplicate_canonicalized"] += len(representations) - 1

        label = _period_candidate_label(values)
        observed_values: list[QualitativeClaim] = []
        reasons: set[tuple[str, str]] = set()
        for claim in values:
            claim_type = period_type(claim)
            role = _period_role(claim)
            if claim_type not in ACTUAL_PERIOD_TYPES:
                reasons.add((PERIODS_WITHOUT_OBSERVED_CLAIMS_EXCLUDED, "NON_OBSERVED_PERIOD_TYPE"))
                continue
            if role in {"FORWARD_LOOKING_PERIOD", "REFERENCE_PERIOD", "UNKNOWN_PERIOD_ROLE"}:
                # UNKNOWN_PERIOD_ROLE is only excluded when the serialized
                # claim explicitly declares it; legacy claims default to the
                # observed role above.
                if role != "OBSERVED_PERIOD":
                    reasons.add((FORWARD_LOOKING_PERIODS_EXCLUDED, "FORWARD_LOOKING_REFERENCE_ONLY"))
                    continue

            if cutoff is not None:
                available = _date_value(getattr(claim, "public_availability_date", None) or getattr(claim, "available_date", None))
                if available is None:
                    reasons.add((UNKNOWN_PERIOD_END_EXCLUDED, "PIT_ELIGIBILITY_UNKNOWN"))
                    continue
                if available > cutoff:
                    reasons.add((FUTURE_PERIOD_CANDIDATES, "SOURCE_PUBLICATION_AFTER_AS_OF_DATE"))
                    continue
                end = _calendar_period_end(claim)
                if end is None:
                    reasons.add((UNKNOWN_PERIOD_END_EXCLUDED, "PIT_ELIGIBILITY_UNKNOWN"))
                    continue
                if end > cutoff:
                    reasons.add((FUTURE_PERIOD_CANDIDATES, "FUTURE_PERIOD_END_AFTER_AS_OF_DATE"))
                    continue
            observed_values.append(claim)

        if observed_values:
            retained.extend(observed_values)
            continue
        if not reasons:
            continue
        for diagnostic, reason in sorted(reasons):
            excluded.append({"period": label, "reason": reason, "diagnostic": diagnostic})
            if diagnostic == FUTURE_PERIOD_CANDIDATES:
                diagnostics["future_period_candidates"].append(label)
                diagnostics["future_periods_excluded"].append(label)
            elif diagnostic == UNKNOWN_PERIOD_END_EXCLUDED:
                diagnostics["unknown_period_end"].append(label)
            elif diagnostic == FORWARD_LOOKING_PERIODS_EXCLUDED:
                diagnostics["forward_looking_periods"].append(label)
            elif diagnostic == PERIODS_WITHOUT_OBSERVED_CLAIMS_EXCLUDED:
                diagnostics["periods_without_observed_claims"].append(label)

    # Keep metadata deterministic when a period has many excluded claims.
    for key in ("future_period_candidates", "future_periods_excluded", "unknown_period_end", "forward_looking_periods", "periods_without_observed_claims"):
        diagnostics[key] = sorted(set(diagnostics[key]))
    # Preserve the public diagnostic taxonomy in machine-readable metadata as
    # well as the snake-case convenience keys used by the CLI/tests.
    diagnostics[FUTURE_PERIODS_EXCLUDED] = diagnostics["future_periods_excluded"]
    diagnostics[FUTURE_PERIOD_CANDIDATES] = diagnostics["future_period_candidates"]
    diagnostics[UNKNOWN_PERIOD_END_EXCLUDED] = diagnostics["unknown_period_end"]
    diagnostics[FORWARD_LOOKING_PERIODS_EXCLUDED] = diagnostics["forward_looking_periods"]
    diagnostics[PERIODS_WITHOUT_OBSERVED_CLAIMS_EXCLUDED] = diagnostics["periods_without_observed_claims"]
    diagnostics["excluded_periods"] = sorted(excluded, key=lambda item: (item["period"], item["diagnostic"], item["reason"]))
    diagnostics["as_of_date"] = as_of_date
    return retained, diagnostics


def period_sort_key(claim_or_period: Any) -> tuple[int, int, str, str]:
    """Order fiscal periods before publication dates, including non-calendar years."""
    if isinstance(claim_or_period, str):
        value = claim_or_period.upper()
        year_match = re.search(r"(?:FY\s*)?(20\d{2}|19\d{2})", value)
        quarter_match = re.search(r"Q([1-4])", value)
        return (int(year_match.group(1)) if year_match else -1, int(quarter_match.group(1)) if quarter_match else 5, "", value.replace("FY", ""))
    year = getattr(claim_or_period, "fiscal_year", None)
    quarter = getattr(claim_or_period, "fiscal_quarter", None)
    label = getattr(claim_or_period, "period_label", None) or ""
    if year is None:
        return period_sort_key(label)
    calendar = dict(getattr(claim_or_period, "issuer_fiscal_calendar", {}) or {})
    period_end = getattr(claim_or_period, "period_end_date", None)
    is_calendar = str(calendar.get("calendar_type") or "").lower() != "non_calendar"
    if is_calendar and period_end and quarter in {1, 2, 3, 4}:
        try:
            ended = datetime.fromisoformat(str(period_end)[:10])
        except ValueError:
            ended = None
        if ended is not None:
            year = ended.year
            quarter = ((ended.month - 1) // 3) + 1
    canonical = getattr(claim_or_period, "canonical_period_identity", None) or canonical_period_identity(
        issuer=getattr(claim_or_period, "ticker", None), fiscal_year=int(year), fiscal_quarter=quarter if quarter in {1, 2, 3, 4} else None,
        period_end_date=period_end, period_type=period_type(claim_or_period), issuer_fiscal_calendar=calendar,
    ) or canonical_period_label(fiscal_year=int(year), fiscal_quarter=quarter if quarter in {1, 2, 3, 4} else None, issuer_fiscal_calendar=calendar, original_label=label, period_end_date=period_end) or str(label)
    # The identity's label is the grouping key; the source label remains on
    # each claim/document for audit display.
    type_value = period_type(claim_or_period)
    type_rank = {"QUARTER_ACTUAL": 0, "YTD_ACTUAL": 1, "ANNUAL_ACTUAL": 2}.get(type_value, 9)
    return (int(year), int(quarter) if quarter in {1, 2, 3, 4} else 5, type_rank, canonical)


def period_groups(claims: Sequence[QualitativeClaim]) -> dict[tuple[Any, ...], list[QualitativeClaim]]:
    # Normalized claims from SEC filings and issuer supplements can carry the
    # same quarter with different metadata completeness (for example one row
    # has a period start and another only has the period end).  Group on the
    # issuer fiscal coordinate first, then use period end only when genuinely
    # conflicting ends exist.  This prevents Q2 -> Q2 comparisons caused by
    # provenance-detail differences while preserving a real fiscal conflict.
    coordinates: dict[tuple[str, Any, Any, str], list[QualitativeClaim]] = {}
    for claim in claims:
        key = (str(getattr(claim, "ticker", "")).upper(), getattr(claim, "fiscal_year", None),
               getattr(claim, "fiscal_quarter", None), period_type(claim))
        coordinates.setdefault(key, []).append(claim)
    grouped: dict[tuple[Any, ...], list[QualitativeClaim]] = {}
    for values in coordinates.values():
        known_ends = {str(getattr(item, "period_end_date", ""))[:10] for item in values if getattr(item, "period_end_date", None)}
        if len(known_ends) <= 1:
            representative = next((item for item in values if getattr(item, "period_end_date", None)), values[0])
            grouped.setdefault(period_sort_key(representative), []).extend(values)
            continue
        # Multiple period ends under one fiscal coordinate are retained as
        # separate groups so the quality gate can report the inconsistency.
        for end in sorted(known_ends):
            subset = [item for item in values if str(getattr(item, "period_end_date", ""))[:10] == end]
            grouped.setdefault(period_sort_key(subset[0]), []).extend(subset)
        unknown = [item for item in values if not getattr(item, "period_end_date", None)]
        if unknown:
            grouped.setdefault(period_sort_key(unknown[0]), []).extend(unknown)
    return grouped


def period_label(claims: Sequence[QualitativeClaim]) -> str:
    if not claims:
        return "UNKNOWN"
    first = claims[0]
    label = getattr(first, "period_label", None)
    year = getattr(claims[0], "fiscal_year", None)
    quarter = getattr(claims[0], "fiscal_quarter", None)
    canonical = canonical_period_label(
        fiscal_year=year,
        fiscal_quarter=quarter,
        issuer_fiscal_calendar=dict(getattr(first, "issuer_fiscal_calendar", {}) or {}),
        original_label=str(label) if label else None,
        period_end_date=getattr(first, "period_end_date", None),
    )
    if canonical:
        return str(canonical)
    if year is not None and quarter:
        return f"Q{quarter} {year}"
    if year is not None:
        return f"FY {year}"
    return "UNKNOWN"


def _available_period_labels(ordered: Sequence[tuple[Any, list[QualitativeClaim]]]) -> list[str]:
    """Render one label per canonical group and disambiguate real collisions."""
    labels = [period_label(values) for _, values in ordered]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    rendered: list[str] = []
    for (key, values), label in zip(ordered, labels):
        if counts[label] > 1:
            value_type = period_type(values[0]).lower()
            period_end = next((str(getattr(item, "period_end_date", ""))[:10] for item in values if getattr(item, "period_end_date", None)), "unknown")
            same_type_collision = any(period_type(group_values[0]) == period_type(values[0]) for other_key, group_values in ordered if period_label(group_values) == label and other_key != key)
            suffix = f"{value_type}; end={period_end}" if same_type_collision else value_type
            rendered.append(f"{label} [{suffix}]")
        else:
            rendered.append(label)
    return rendered


def _select_latest_vs_previous(claims: Sequence[QualitativeClaim], horizon: str | None = None) -> tuple[list[QualitativeClaim], list[QualitativeClaim], list[str]] | None:
    groups = period_groups(claims)
    # Guidance horizons and unknown references are never ordinary sequential
    # reporting periods. If quarters exist, compare quarters with quarters;
    # otherwise compare completed annual/YTD groups of the same type.
    eligible = {key: values for key, values in groups.items() if period_type(values[0]) in ACTUAL_PERIOD_TYPES}
    quarter_groups = {key: values for key, values in eligible.items() if period_type(values[0]) == "QUARTER_ACTUAL"}
    annual_groups = {key: values for key, values in eligible.items() if period_type(values[0]) == "ANNUAL_ACTUAL"}
    if horizon == "quarterly":
        groups = quarter_groups
    elif horizon == "annual":
        groups = annual_groups
    elif quarter_groups:
        groups = quarter_groups
    else:
        groups = annual_groups or eligible
    ordered = sorted(groups.items(), key=lambda item: item[0])
    available = _available_period_labels(ordered)
    if len(ordered) < 2:
        return None
    # Prefer a genuinely adjacent issuer quarter.  When the issuer has no
    # standalone Q4 (the normal 10-K/10-Q pattern), prefer same-quarter YoY
    # over silently treating Q3 FYx -> Q1 FY(x+1) as sequential.
    latest_key, latest_values = ordered[-1]
    previous_values = ordered[-2][1]
    if period_type(latest_values[0]) == "QUARTER_ACTUAL":
        latest_coord = int(latest_key[0]) * 4 + int(latest_key[1])
        adjacent = next(
            (values for key, values in reversed(ordered[:-1]) if int(latest_coord - (int(key[0]) * 4 + int(key[1]))) == 1),
            None,
        )
        if adjacent is not None:
            previous_values = adjacent
        else:
            latest_year, latest_quarter = int(latest_key[0]), int(latest_key[1])
            yoy = next(
                (values for key, values in reversed(ordered[:-1]) if int(key[1]) == latest_quarter and int(key[0]) == latest_year - 1),
                None,
            )
            if yoy is not None:
                previous_values = yoy
    return previous_values, latest_values, available


def select_latest_vs_previous(
    claims: Sequence[QualitativeClaim],
    horizon: str | None = None,
    *,
    as_of_date: str | None = None,
) -> tuple[list[QualitativeClaim], list[QualitativeClaim], list[str]] | None:
    """Select adjacent observed periods, applying PIT eligibility first."""
    eligible, _diagnostics = eligible_observed_claims(claims, as_of_date=as_of_date)
    return _select_latest_vs_previous(eligible, horizon=horizon)


def _base_key(claim: QualitativeClaim) -> tuple[str, str, str]:
    identity = topic_identity(claim)
    # A capex outlook can be extracted under ``guidance`` in one transcript
    # and under ``capex`` in another. Treat that one explicit subject as the
    # same comparison family while keeping revenue guidance separate from
    # realized revenue.
    if identity.normalized_subject == "capex":
        return ("capex", "capex", "capex")
    return (identity.dimension, identity.topic, identity.normalized_subject)


def _claim_evidence(claim: QualitativeClaim) -> TemporalEvidence:
    return TemporalEvidence(
        claim_id=claim.claim_id,
        evidence_text=claim.evidence_text,
        document_id=claim.document_id,
        source_location=dict(claim.source_location),
        period_label=claim.period_label,
        public_availability_date=getattr(claim, "public_availability_date", None) or claim.available_date,
        availability_status=str(getattr(claim, "availability_status", "UNKNOWN") or "UNKNOWN"),
    )


def stable_change_id(
    ticker: str,
    normalized_subject: str,
    from_period: str,
    to_period: str,
    from_claim_ids: Sequence[str],
    to_claim_ids: Sequence[str],
    comparison_version: str = COMPARISON_VERSION,
) -> str:
    identity = "|".join((ticker.upper(), normalized_subject, from_period, to_period, ",".join(sorted(from_claim_ids)), ",".join(sorted(to_claim_ids)), comparison_version))
    return "tch_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _match_score(previous: QualitativeClaim, current: QualitativeClaim) -> float:
    previous_identity, current_identity = topic_identity(previous), topic_identity(current)
    if _base_key(previous) != _base_key(current):
        return -1.0
    if previous_identity.metric_unit and current_identity.metric_unit and previous_identity.metric_unit != current_identity.metric_unit:
        return -1.0
    score = 0.35
    if previous_identity.metric_unit == current_identity.metric_unit and previous_identity.metric_unit is not None:
        score += 0.25
    score += min(0.40, text_similarity(previous, current))
    return score


def _explicit_removal(claim: QualitativeClaim) -> bool:
    return bool(re.search(r"\b(?:no longer|resolved|removed|eliminated|discontinued|withdrawn|cancelled|ended|stopped)\b", claim.evidence_text.lower()))


def _low_value_current_only(claim: QualitativeClaim) -> bool:
    """Exclude generic promotional language that supplies no temporal fact."""
    if claim.dimension != "catalysts":
        return False
    if getattr(claim, "direction", "unknown") != "unknown" or extract_metric(claim) is not None:
        return False
    return not re.search(r"\b(?:launched|approved|closed|completed|acquired|revised|raised|lowered)\b", claim.evidence_text.lower())


def _make_change(previous: list[QualitativeClaim], current: list[QualitativeClaim], classification: dict[str, Any], from_period: str, to_period: str, comparison_horizon: str = "unknown") -> TemporalChange:
    anchor = current[0] if current else previous[0]
    identity = topic_identity(anchor)
    from_ids = [claim.claim_id for claim in previous]
    to_ids = [claim.claim_id for claim in current]
    return TemporalChange(
        change_id=stable_change_id(anchor.ticker, identity.normalized_subject, from_period, to_period, from_ids, to_ids),
        ticker=anchor.ticker,
        company_name=anchor.company_name,
        dimension=anchor.dimension,
        topic=anchor.topic,
        subtopic=anchor.subtopic,
        from_period=from_period,
        to_period=to_period,
        change_type=classification["change_type"],
        direction=classification.get("direction", "unknown"),
        summary=classification["summary"],
        from_claim_ids=from_ids,
        to_claim_ids=to_ids,
        from_evidence=[_claim_evidence(claim) for claim in previous],
        to_evidence=[_claim_evidence(claim) for claim in current],
        confidence=float(classification.get("confidence", 0.0)),
        comparison_method=classification.get("comparison_method", "deterministic"),
        comparison_version=COMPARISON_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        normalized_subject=identity.normalized_subject,
        metric_unit=classification.get("metric_unit") or identity.metric_unit,
        rules_version=RULES_VERSION,
        provider=PROVIDER_VERSION,
        comparison_horizon=comparison_horizon,
        period_basis=period_basis(anchor),
    )


class TemporalComparisonProvider(ABC):
    """Provider contract for deterministic or future model-assisted comparison."""

    provider_version = PROVIDER_VERSION
    comparison_version = COMPARISON_VERSION

    @abstractmethod
    def compare(self, previous_claims: Sequence[QualitativeClaim], current_claims: Sequence[QualitativeClaim], from_period: str, to_period: str, comparison_horizon: str = "unknown") -> TemporalComparisonResult:
        raise NotImplementedError


class DeterministicTemporalComparisonProvider(TemporalComparisonProvider):
    """Conservative provider that never compares unrelated normalized subjects."""

    def compare(self, previous_claims: Sequence[QualitativeClaim], current_claims: Sequence[QualitativeClaim], from_period: str, to_period: str, comparison_horizon: str = "unknown") -> TemporalComparisonResult:
        result = TemporalComparisonResult(
            ticker=(current_claims[0].ticker if current_claims else previous_claims[0].ticker if previous_claims else ""),
            from_period=from_period,
            to_period=to_period,
            previous_claims=len(previous_claims),
            current_claims=len(current_claims),
            comparison_horizon=comparison_horizon,
        )
        previous_by_key: dict[tuple[str, str, str], list[QualitativeClaim]] = {}
        current_by_key: dict[tuple[str, str, str], list[QualitativeClaim]] = {}
        for claim in previous_claims:
            previous_by_key.setdefault(_base_key(claim), []).append(claim)
        for claim in current_claims:
            current_by_key.setdefault(_base_key(claim), []).append(claim)

        used_previous: set[str] = set()
        for key, current_group in current_by_key.items():
            previous_group = previous_by_key.get(key, [])
            previous_signs = {1 if getattr(claim, "direction", "unknown") in {"increasing", "improving", "accelerating", "raised", "positive"} else -1 if getattr(claim, "direction", "unknown") in {"decreasing", "deteriorating", "decelerating", "lowered", "negative"} else 0 for claim in previous_group}
            current_signs = {1 if getattr(claim, "direction", "unknown") in {"increasing", "improving", "accelerating", "raised", "positive"} else -1 if getattr(claim, "direction", "unknown") in {"decreasing", "deteriorating", "decelerating", "lowered", "negative"} else 0 for claim in current_group}
            if previous_group and ({-1, 1} <= previous_signs or {-1, 1} <= current_signs):
                used_previous.update(claim.claim_id for claim in previous_group)
                result.topics_matched += 1
                result.changes.append(_make_change(previous_group, current_group, {"change_type": "mixed", "direction": "mixed", "summary": f"{current_group[0].topic or current_group[0].dimension} evidence was mixed within the compared periods.", "confidence": 0.78, "comparison_method": "within_period_conflict", "metric_unit": topic_identity(current_group[0]).metric_unit}, from_period, to_period, comparison_horizon))
                continue
            unmatched_current: list[QualitativeClaim] = []
            for current in sorted(current_group, key=lambda item: item.claim_id):
                candidates = [claim for claim in previous_group if claim.claim_id not in used_previous]
                scored = sorted(((claim, _match_score(claim, current)) for claim in candidates), key=lambda item: item[1], reverse=True)
                current_identity = topic_identity(current)
                threshold = 0.42 if current.dimension in {"demand", "customer_growth", "customer_retention"} and current_identity.normalized_subject in {"ai", "enterprise", "retention", "bookings"} else 0.52
                if scored and scored[0][1] >= threshold:
                    previous, score = scored[0]
                    used_previous.add(previous.claim_id)
                    result.topics_matched += 1
                    classification = classify_matched(previous, current)
                    if classification.get("rejected"):
                        result.rejected.append(f"{current.claim_id}: {classification['rejected']}")
                        continue
                    if classification.get("change_type") == "insufficient_evidence":
                        result.rejected.append(f"{current.claim_id}: insufficient evidence")
                        continue
                    result.changes.append(_make_change([previous], [current], classification, from_period, to_period, comparison_horizon))
                else:
                    # A same-subject pair with explicit but incompatible units is
                    # rejected instead of being silently treated as a new topic.
                    incompatible = next((claim for claim in candidates if topic_identity(claim).metric_unit and topic_identity(current).metric_unit and topic_identity(claim).metric_unit != topic_identity(current).metric_unit), None)
                    if incompatible:
                        result.rejected.append(f"{current.claim_id}: incompatible metric units")
                        continue
                    unmatched_current.append(current)
            if unmatched_current:
                anchor = next((claim for claim in unmatched_current if re.search(r"\b(?:approved|launched|acquired|initiated|submitted|authorized|completed|closed)\b", claim.evidence_text.lower())), unmatched_current[0])
                if _low_value_current_only(anchor):
                    result.rejected.append(f"{anchor.claim_id}: low-value generic current-only mention")
                    continue
                result.changes.append(_make_change([], unmatched_current, classify_new(anchor), from_period, to_period, comparison_horizon))

        # Silence is intentionally not converted into removed/risk_removed. A
        # removal requires an explicit current-period statement.
        for current in current_claims:
            if _explicit_removal(current) and current.claim_id not in {claim_id for change in result.changes for claim_id in change.to_claim_ids}:
                result.changes.append(_make_change([], [current], classify_new(current), from_period, to_period, comparison_horizon))

        result.changes.sort(key=lambda change: change.change_id)
        result.evidence_validation_failures.extend(validate_temporal_changes(result.changes, previous_claims, current_claims))
        result.candidates_generated = len(current_claims)
        result.records_accepted = len(result.changes)
        result.records_rejected = len(result.rejected)
        result.unique_records_accepted = len({change.change_id for change in result.changes})
        result.counts_by_dimension = {}
        result.counts_by_type = {}
        for change in result.changes:
            result.counts_by_dimension[change.dimension] = result.counts_by_dimension.get(change.dimension, 0) + 1
            result.counts_by_type[change.change_type] = result.counts_by_type.get(change.change_type, 0) + 1
        return result


def validate_temporal_changes(changes: Sequence[TemporalChange], previous_claims: Sequence[QualitativeClaim], current_claims: Sequence[QualitativeClaim]) -> list[str]:
    previous = {claim.claim_id: claim for claim in previous_claims}
    current = {claim.claim_id: claim for claim in current_claims}
    errors: list[str] = []
    for change in changes:
        if not change.from_claim_ids and not change.to_claim_ids:
            errors.append(f"{change.change_id}: no claim provenance")
            continue
        for evidence in change.from_evidence:
            claim = previous.get(evidence.claim_id)
            if claim is None or claim.evidence_text != evidence.evidence_text:
                errors.append(f"{change.change_id}: invalid from evidence {evidence.claim_id}")
        for evidence in change.to_evidence:
            claim = current.get(evidence.claim_id)
            if claim is None or claim.evidence_text != evidence.evidence_text:
                errors.append(f"{change.change_id}: invalid to evidence {evidence.claim_id}")
    return errors


def compare_latest_vs_previous(
    ticker: str,
    claims: Sequence[QualitativeClaim],
    dimension: str | None = None,
    provider: TemporalComparisonProvider | None = None,
    horizon: str | None = None,
    *,
    as_of_date: str | None = None,
) -> TemporalComparisonResult:
    scoped = [claim for claim in claims if claim.ticker.upper() == ticker.upper() and (dimension is None or claim.dimension == dimension)]
    eligible_claims, eligibility = eligible_observed_claims(scoped, as_of_date=as_of_date)
    selected = _select_latest_vs_previous(eligible_claims, horizon=horizon)
    groups = period_groups(eligible_claims)
    eligible_labels = _available_period_labels(sorted(groups.items()))
    if selected is None:
        return TemporalComparisonResult(
            ticker=ticker.upper(), status="INSUFFICIENT_PERIODS", available_periods=eligible_labels,
            previous_claims=0, current_claims=0, comparison_horizon=horizon or "unknown",
            as_of_date=as_of_date, eligible_periods=eligible_labels,
            excluded_periods=eligibility.get("excluded_periods", []),
            period_diagnostics=eligibility,
        )
    previous, current, available = selected
    # ``available`` is deliberately generated from the horizon-specific
    # eligible groups, not from all period-like labels in the claim store.
    eligible_labels = available
    integrity_failures: list[str] = []
    relation, gap_status = comparison_sequence_relation(previous[0], current[0])
    basis = period_basis(current[0])
    if relation == "INSUFFICIENT_COMPARABLE_PERIODS":
        integrity_failures.append("TEMPORAL_SEQUENCE_INVALID")
        return TemporalComparisonResult(
            ticker=ticker.upper(), status="TEMPORAL_SEQUENCE_INVALID", comparison_status="TEMPORAL_SEQUENCE_INVALID",
            available_periods=available, from_period=period_label(previous), to_period=period_label(current),
            previous_claims=len(previous), current_claims=len(current), comparison_horizon=horizon or "quarterly",
            candidates_generated=len(current), records_rejected=len(current), period_integrity_failures=integrity_failures,
            as_of_date=as_of_date, eligible_periods=eligible_labels,
            excluded_periods=eligibility.get("excluded_periods", []), period_diagnostics=eligibility,
            period_basis=basis, sequence_relation=relation, gap_status=gap_status,
        )
    if not compatible_period_types(previous[0], current[0]):
        return TemporalComparisonResult(
            ticker=ticker.upper(), status="NOT_COMPARABLE_PERIOD_TYPES", comparison_status="NOT_COMPARABLE_PERIOD_TYPES",
            available_periods=available, from_period=period_label(previous), to_period=period_label(current),
            comparison_horizon=horizon or "unknown", as_of_date=as_of_date, eligible_periods=eligible_labels,
            excluded_periods=eligibility.get("excluded_periods", []), period_diagnostics=eligibility,
        )
    resolved_horizon = horizon or ("quarterly" if period_type(previous[0]) == "QUARTER_ACTUAL" else "annual" if period_type(previous[0]) == "ANNUAL_ACTUAL" else "unknown")
    result = (provider or DeterministicTemporalComparisonProvider()).compare(previous, current, period_label(previous), period_label(current), resolved_horizon)
    result.available_periods = available
    result.as_of_date = as_of_date
    result.eligible_periods = eligible_labels
    result.excluded_periods = eligibility.get("excluded_periods", [])
    result.period_diagnostics = eligibility
    result.comparison_horizon = resolved_horizon
    result.period_basis = basis
    result.sequence_relation = relation
    result.gap_status = gap_status
    result.candidates_generated = len(current)
    result.records_accepted = len(result.changes)
    result.records_rejected = len(result.rejected)
    result.unique_records_accepted = len({change.change_id for change in result.changes})
    result.counts_by_dimension = {}
    result.counts_by_type = {}
    for change in result.changes:
        result.counts_by_dimension[change.dimension] = result.counts_by_dimension.get(change.dimension, 0) + 1
        result.counts_by_type[change.change_type] = result.counts_by_type.get(change.change_type, 0) + 1
    result.period_integrity_failures.extend(integrity_failures)
    for change in result.changes:
        change.comparison_horizon = resolved_horizon
        change.period_basis = basis
        change.sequence_relation = relation
        change.gap_status = gap_status
    return result


__all__ = [
    "TemporalComparisonProvider", "DeterministicTemporalComparisonProvider", "compare_latest_vs_previous",
    "period_sort_key", "period_groups", "select_latest_vs_previous", "stable_change_id", "validate_temporal_changes", "claims_available_as_of",
    "eligible_observed_claims", "FUTURE_PERIODS_EXCLUDED", "FUTURE_PERIOD_CANDIDATES",
    "UNKNOWN_PERIOD_END_EXCLUDED", "FORWARD_LOOKING_PERIODS_EXCLUDED", "PERIODS_WITHOUT_OBSERVED_CLAIMS_EXCLUDED",
]
