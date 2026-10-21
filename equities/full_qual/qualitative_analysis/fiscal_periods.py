"""Issuer-aware, evidence-preserving fiscal-period extraction."""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import unquote

from .fiscal_calendar_registry import lookup_fiscal_calendar
from .models import FiscalPeriod, IssuerFiscalCalendar

PERIOD_UNKNOWN = "UNKNOWN"
PERIOD_QUARTER_ACTUAL = "QUARTER_ACTUAL"
PERIOD_ANNUAL_ACTUAL = "ANNUAL_ACTUAL"
PERIOD_YTD_ACTUAL = "YTD_ACTUAL"
PERIOD_GUIDANCE_HORIZON = "GUIDANCE_HORIZON"
PERIOD_EVENT_DATE = "EVENT_DATE"

_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
    "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
    "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


def _period(*, year=None, quarter=None, label=None, method=None, confidence=None,
            warning=None, period_type=PERIOD_UNKNOWN, evidence=None, sources=None,
            period_end_date=None, period_start_date=None, issuer_fiscal_calendar=None,
            conflicting_labels=None) -> FiscalPeriod:
    return FiscalPeriod(
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_label=label,
        detection_method=method,
        confidence=confidence,
        warning=warning,
        period_type=period_type,
        detection_evidence=evidence,
        detection_sources=list(sources or []),
        period_end_date=period_end_date,
        period_start_date=period_start_date,
        issuer_fiscal_calendar=dict(issuer_fiscal_calendar or {}),
    )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_from_parts(parts: tuple[int, int, int] | None) -> str | None:
    if not parts:
        return None
    month, day, year = parts
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _date_parts(value: str) -> tuple[int, int, int] | None:
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2})(?:,|\s)\s*(20\d{2}|19\d{2})\b", value, re.I,
    )
    if match:
        return _MONTHS[match.group(1).upper()], int(match.group(2)), int(match.group(3))
    match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2}|19\d{2})\b", value)
    if match:
        # SEC headers use the US month/day/year convention.  Preserve the
        # same (month, day, year) tuple returned by the named-month branch.
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None


def _period_end_date(value: str) -> str | None:
    """Extract a reporting period end, never an arbitrary narrative date."""
    if not value:
        return None
    patterns = (
        r"\b(?:FOR\s+THE\s+)?(?:QUARTERLY\s+PERIOD|THREE\s+MONTHS|QUARTER)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
        r"\b(?:FISCAL\s+YEAR|YEAR)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
        r"\b(?:RESULTS|RESULT)\s+FOR\s+THE\s+(?:QUARTER|YEAR)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return _date_from_parts(_date_parts(match.group(0)))
    return None


def _fiscal_calendar(metadata: Mapping[str, Any] | None, value: str = "") -> IssuerFiscalCalendar:
    """Resolve calendar facts from explicit metadata and filing language."""
    metadata = metadata or {}
    registry = lookup_fiscal_calendar(metadata)
    nested = metadata.get("issuer_fiscal_calendar")
    source_values = {**registry, **(nested if isinstance(nested, Mapping) else {}), **metadata}
    month = _int(source_values.get("fiscal_year_end_month") or source_values.get("fiscal_year_end_month_number"))
    day = _int(source_values.get("fiscal_year_end_day"))
    end_text = str(source_values.get("fiscal_year_end") or source_values.get("fiscal_year_end_date") or "")
    evidence = end_text or None
    search_text = " ".join((end_text, value))
    month_match = re.search(
        r"(?:last\s+(?:Sunday|day)\s+in|fiscal\s+year\s+ended|fiscal\s+year(?:[- ]end)?(?:ing|s)?(?:\s+on)?|year[- ]end)\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)",
        search_text, re.I,
    )
    if month is None and month_match:
        month = _MONTHS[month_match.group(1).upper()]
    week_based = bool(source_values.get("week_based")) or bool(re.search(r"last\s+(?:Sunday|day)\s+in\s+[A-Za-z]+|\b(?:52|53)[- ]week", search_text, re.I))
    if month is None:
        month = 12
    if not week_based and day is None:
        explicit_end = re.search(r"(?:fiscal\s+year|year)\s+ended\s+([A-Za-z]+\s+\d{1,2},?\s+20\d{2})", value, re.I)
        if explicit_end:
            parts = _date_parts(explicit_end.group(1))
            if parts and parts[0] == month:
                day = parts[1]
                evidence = explicit_end.group(0)
    weeks = _int(source_values.get("weeks_per_year"))
    week_match = re.search(r"\b(52|53)[- ]week\s+(?:fiscal\s+)?year", search_text, re.I)
    if weeks is None and week_match:
        weeks = int(week_match.group(1))
    convention = str(source_values.get("fiscal_year_label_convention") or "ending_year").lower()
    if convention not in {"ending_year", "start_year"}:
        convention = "ending_year"
    calendar_type = "calendar" if month == 12 and convention == "ending_year" else "non_calendar"
    return IssuerFiscalCalendar(
        fiscal_year_end_month=month,
        fiscal_year_end_day=day,
        fiscal_year_label_convention=convention,
        quarter_boundaries=dict(source_values.get("quarter_boundaries") or {}),
        week_based=week_based,
        weeks_per_year=weeks,
        calendar_type=calendar_type,
        source=(str(source_values.get("source")) if registry and not nested and not end_text else ("metadata" if metadata and (month != 12 or end_text or nested) else ("filing_text" if value and month != 12 else "default"))),
        evidence=evidence,
    )


def _calendar_dict(calendar: IssuerFiscalCalendar) -> dict[str, Any]:
    return calendar.to_dict()


def _fiscal_year_for_date(period_end: str, calendar: IssuerFiscalCalendar) -> int | None:
    try:
        end = date.fromisoformat(period_end)
    except (TypeError, ValueError):
        return None
    end_year = end.year
    month = calendar.fiscal_year_end_month
    if end.month > month:
        fiscal_end_year = end_year + 1
    elif end.month < month:
        fiscal_end_year = end_year
    elif calendar.fiscal_year_end_day and end.day > calendar.fiscal_year_end_day:
        fiscal_end_year = end_year + 1
    else:
        fiscal_end_year = end_year
    return fiscal_end_year if calendar.fiscal_year_label_convention == "ending_year" else fiscal_end_year - 1


def _quarter_from_end_date(period_end: str, calendar: IssuerFiscalCalendar) -> int | None:
    try:
        end = date.fromisoformat(period_end)
    except (TypeError, ValueError):
        return None
    if calendar.week_based:
        # For 52/53-week issuers, month arithmetic can move a late-July
        # quarter into Q3 even though the issuer's Q2 ends on August 1.  Use
        # the fiscal-year-end anchor and the issuer's week count as a generic
        # quarter coordinate.  Exact SEC quarter boundaries still take
        # precedence below when supplied.
        fiscal_end_year = _fiscal_year_for_date(period_end, calendar)
        if fiscal_end_year is not None:
            try:
                anchor_day = calendar.fiscal_year_end_day or monthrange(fiscal_end_year, calendar.fiscal_year_end_month)[1]
                anchor = date(fiscal_end_year, calendar.fiscal_year_end_month, min(anchor_day, monthrange(fiscal_end_year, calendar.fiscal_year_end_month)[1]))
                days_back = max(0, (anchor - end).days)
                quarter_length = ((calendar.weeks_per_year or 52) * 7) / 4
                elapsed_quarters = min(3, max(0, int(round(days_back / quarter_length))))
                return 4 - elapsed_quarters
            except (TypeError, ValueError):
                pass
    boundaries = calendar.quarter_boundaries
    if isinstance(boundaries, Mapping):
        candidates: list[tuple[str, date]] = []
        for key, value in boundaries.items():
            if isinstance(value, str):
                try:
                    candidates.append((str(key), date.fromisoformat(value)))
                except ValueError:
                    pass
            elif isinstance(value, Mapping) and value.get("end_date"):
                try:
                    candidates.append((str(key), date.fromisoformat(str(value["end_date"]))))
                except ValueError:
                    pass
        for key, boundary in sorted(candidates, key=lambda item: item[1]):
            if end <= boundary:
                q = _int(re.sub(r"[^0-9]", "", key))
                if q in {1, 2, 3, 4}:
                    return q
    return ((end.month - calendar.fiscal_year_end_month - 1) % 12) // 3 + 1


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _period_start_date(period_end: str | None, calendar: IssuerFiscalCalendar, quarter: int | None, period_type: str) -> str | None:
    """Derive an economic period start without using publication dates.

    Month-end issuers use the day after the prior fiscal boundary.  For
    52/53-week issuers, exact boundaries are used when supplied; otherwise a
    conservative thirteen-week estimate is retained as a coordinate only.
    The source period end remains authoritative.
    """
    if not period_end:
        return None
    try:
        end = date.fromisoformat(str(period_end)[:10])
    except (TypeError, ValueError):
        return None
    boundaries = calendar.quarter_boundaries if isinstance(calendar.quarter_boundaries, Mapping) else {}
    if isinstance(boundaries, Mapping) and quarter is not None:
        value = boundaries.get(f"Q{quarter}") or boundaries.get(str(quarter))
        if isinstance(value, Mapping):
            value = value.get("start_date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10]).isoformat()
            except ValueError:
                pass
    if period_type == PERIOD_ANNUAL_ACTUAL or quarter is None:
        fy_end_month = calendar.fiscal_year_end_month or 12
        fy_end_day = calendar.fiscal_year_end_day or monthrange(end.year, end.month)[1]
        year = end.year if end.month <= fy_end_month else end.year + 1
        if calendar.fiscal_year_label_convention == "start_year":
            year -= 1
        prior_year = end.year - 1 if end.month == fy_end_month else end.year
        prior_day = min(fy_end_day, monthrange(prior_year, fy_end_month)[1])
        try:
            from datetime import timedelta
            return (date(prior_year, fy_end_month, prior_day) + timedelta(days=1)).isoformat()
        except ValueError:
            return None
    if calendar.week_based:
        from datetime import timedelta
        return (end - timedelta(days=13 * 7 - 1)).isoformat()
    # A quarter boundary is three fiscal months before the end boundary.
    month = end.month
    year = end.year
    for _ in range(3):
        year, month = _previous_month(year, month)
    day = monthrange(year, month)[1]
    return date(year, month, day).isoformat()


def _period_end_for_coordinate(year: int, quarter: int, calendar: IssuerFiscalCalendar) -> str | None:
    """Derive a fiscal quarter end from an explicit issuer coordinate.

    This is used only when a source supplement identifies its quarter in the
    title/filename but its flattened body also contains a comparative date.
    Structured filing dates remain higher precedence.
    """
    if quarter not in {1, 2, 3, 4}:
        return None
    boundaries = calendar.quarter_boundaries if isinstance(calendar.quarter_boundaries, Mapping) else {}
    if isinstance(boundaries, Mapping):
        value = boundaries.get(f"Q{quarter}") or boundaries.get(str(quarter))
        if isinstance(value, Mapping):
            value = value.get("end_date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10]).isoformat()
            except ValueError:
                pass
    end_year = year if calendar.fiscal_year_label_convention == "ending_year" else year + 1
    end_month = calendar.fiscal_year_end_month or 12
    end_day = calendar.fiscal_year_end_day or monthrange(end_year, end_month)[1]
    try:
        anchor = date(end_year, end_month, min(end_day, monthrange(end_year, end_month)[1]))
    except ValueError:
        return None
    if quarter == 4:
        return anchor.isoformat()
    if calendar.week_based:
        from datetime import timedelta
        weeks = calendar.weeks_per_year or 52
        return (anchor - timedelta(days=round((4 - quarter) * weeks * 7 / 4))).isoformat()
    month, result_year = anchor.month, anchor.year
    for _ in range(3 * (4 - quarter)):
        result_year, month = _previous_month(result_year, month)
    return date(result_year, month, monthrange(result_year, month)[1]).isoformat()


def _format_label(year: int | None, quarter: int | None, calendar: IssuerFiscalCalendar, *, explicit_fiscal: bool = False) -> str | None:
    if year is None:
        return None
    if quarter is None:
        return f"FY {year}"
    if explicit_fiscal or calendar.calendar_type == "non_calendar":
        return f"Q{quarter} FY{year}"
    return f"Q{quarter} {year}"


def _explicit(metadata: Mapping[str, Any] | None) -> FiscalPeriod | None:
    if not metadata:
        return None
    fy = metadata.get("fiscal_year") or metadata.get("sec_fy")
    fq = metadata.get("fiscal_quarter") or metadata.get("sec_fp")
    label = metadata.get("period_label")
    if fy is None and fq is None and label is None:
        return None
    fy, fq = _int(fy), _int(fq)
    if isinstance(metadata.get("sec_fp"), str):
        match_fp = re.search(r"Q([1-4])", str(metadata.get("sec_fp")), re.I)
        fq = int(match_fp.group(1)) if match_fp else fq
    if fq is None and label:
        match = re.search(r"\bQ([1-4])\s*(?:FY\s*)?(20\d{2}|19\d{2})\b", str(label), re.I)
        if match:
            fq, fy = int(match.group(1)), int(match.group(2))
    if fq not in {None, 1, 2, 3, 4}:
        fq = None
    calendar = _fiscal_calendar(metadata)
    if label is None:
        label = _format_label(fy, fq, calendar, explicit_fiscal=True)
    end = metadata.get("period_end_date") or metadata.get("period_end")
    period_type = str(metadata.get("period_type") or (PERIOD_QUARTER_ACTUAL if fq else PERIOD_ANNUAL_ACTUAL))
    end = str(end) if end else None
    calendar_dict = _calendar_dict(calendar)
    return _period(year=fy, quarter=fq, label=str(label) if label else None, method="sec_structured" if metadata.get("sec_fy") or metadata.get("sec_fp") else "metadata", confidence=1.0,
                   period_type=period_type, evidence="SEC structured fiscal metadata" if metadata.get("sec_fy") or metadata.get("sec_fp") else "explicit metadata",
                   sources=["sec_structured"] if metadata.get("sec_fy") or metadata.get("sec_fp") else ["metadata"], period_end_date=end,
                   period_start_date=metadata.get("period_start_date") or _period_start_date(end, calendar, fq, period_type),
                   issuer_fiscal_calendar=calendar_dict)


def _explicit_fiscal_candidates(value: str) -> list[tuple[int, int, str]]:
    if not value:
        return []
    candidates: list[tuple[int, int, str]] = []
    # Keep the captures named because the year-first form has the opposite
    # ordering from the other forms.  Positional ``group(2)`` access here
    # previously crashed on ordinary proxy text such as ``2024 first quarter``.
    patterns = (
        (r"\bQ(?P<quarter>[1-4])\s*(?:FY|FISCAL\s*YEAR|FISCAL)\s*(?P<year>20\d{2}|19\d{2})\b", "q"),
        (r"\b(?P<quarter>[1-4])Q(?P<year>\d{2})\b", "compact"),
        (r"\b(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTERS?\s+(?:OF\s+)?FISCAL\s+(?:YEARS?\s+)?(?P<year>20\d{2}|19\d{2})\b", "word"),
        (r"\b(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTER\s+(?:FISCAL\s+)?(?:YEAR\s*)?(?P<year>20\d{2}|19\d{2})\b", "word"),
        (r"\b(?P<year>20\d{2}|19\d{2})\s+(?:FISCAL\s+)?(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTER\b", "year"),
    )
    for pattern, kind in patterns:
        for match in re.finditer(pattern, value, re.I):
            if kind == "q":
                quarter, year = int(match.group("quarter")), int(match.group("year"))
            elif kind == "compact":
                quarter = int(match.group("quarter"))
                short_year = int(match.group("year"))
                year = 2000 + short_year if short_year < 70 else 1900 + short_year
            elif kind == "word":
                quarter, year = _QUARTER_WORDS[match.group("quarter_word").lower()], int(match.group("year"))
            else:
                year, quarter = int(match.group("year")), _QUARTER_WORDS[match.group("quarter_word").lower()]
            candidates.append((quarter, year, match.group(0)))
    return candidates


def _filing_header(
    value: str,
    metadata: Mapping[str, Any] | None,
    *,
    allow_conflicting_explicit: bool = False,
) -> FiscalPeriod | None:
    if not value:
        return None
    pattern = re.compile(
        r"\b(?:FOR\s+THE\s+)?(?:QUARTERLY\s+PERIOD|THREE\s+MONTHS|QUARTER)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})", re.I,
    )
    match = pattern.search(value)
    # SEC-rendered headers occasionally insert ``Commission file`` or a
    # table-cell break between ``period ended`` and the date.  Resolve that
    # bounded header form before scanning the body, where comparative dates
    # and fiscal labels are abundant.
    header_match = re.search(
        r"\b(?:FOR\s+THE\s+)?(?:QUARTERLY\s+PERIOD|THREE\s+MONTHS|QUARTER)\s+ENDED\b.{0,120}?"
        r"(?P<report_date>[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
        value[:5000], re.I | re.S,
    )
    if header_match:
        header_date = header_match.group("report_date")
        period_end = _date_from_parts(_date_parts(header_date))
        header_start, header_end = header_match.start(), header_match.end()
    else:
        period_end = _date_from_parts(_date_parts(match.group(0))) if match else _period_end_date(value)
        header_start = match.start() if match else 0
        header_end = match.end() if match else 0
    if not period_end:
        return None
    calendar = _fiscal_calendar(metadata, value)
    derived_year = _fiscal_year_for_date(period_end, calendar)
    derived_quarter = _quarter_from_end_date(period_end, calendar)
    # Only labels in the filing header are authoritative.  Flattened SEC
    # HTML/PDF text contains many comparative fiscal phrases later in the
    # document; scanning the entire body made a July filing become "first
    # quarter" simply because that phrase appeared in a comparison table.
    header_window = value[max(0, header_start - 900):header_end + 900] if header_match or match else value[:1800]
    explicit = _explicit_fiscal_candidates(header_window)
    chosen = next((item for item in explicit if item[0] == derived_quarter and item[1] == derived_year), None)
    conflicting_explicit = False
    if chosen is None and explicit and allow_conflicting_explicit:
        # Non-SEC issuer supplements can state their own fiscal coordinate
        # next to a period-end sentence.  With no SEC filing header to anchor
        # precedence, preserve that explicit issuer label.
        chosen = explicit[0]
        conflicting_explicit = True
    elif chosen is None and explicit:
        # A conflicting fiscal phrase in flattened filing text is comparative
        # disclosure until an authoritative structured SEC coordinate or the
        # filing header agrees with it.  Never let ``Second Quarter 2025`` in
        # a June 2026 filing override the reporting-period end date.
        chosen = None
    filename_period = _filename_period(
        str((metadata or {}).get("source_url") or ""),
        str((metadata or {}).get("local_path") or (metadata or {}).get("source_path") or ""),
    )
    filename_matches = bool(
        filename_period
        and filename_period.fiscal_year == derived_year
        and filename_period.fiscal_quarter == derived_quarter
    )
    if chosen:
        quarter, year, evidence = chosen
        filename_matches_explicit = bool(
            filename_period
            and filename_period.fiscal_year == year
            and filename_period.fiscal_quarter == quarter
        )
        sources = ["document_type", "filing_header", "explicit_fiscal_label"]
        if filename_matches or filename_matches_explicit:
            sources.append("source_filename")
            evidence = f"{evidence}; {filename_period.detection_evidence}"
        period_type = PERIOD_QUARTER_ACTUAL
        # Preserve the observed date unless the source filename independently
        # confirms the explicit fiscal coordinate.  In that case a flattened
        # supplement may have surfaced an older comparative date first; use
        # the issuer-calendar coordinate while retaining the filename as
        # provenance.
        resolved_end = _period_end_for_coordinate(year, quarter, calendar) if conflicting_explicit and filename_matches_explicit else period_end
        if resolved_end and resolved_end != period_end and filename_matches_explicit:
            sources.append("fiscal_coordinate_from_source_filename")
        return _period(year=year, quarter=quarter, label=_format_label(year, quarter, calendar, explicit_fiscal=True), method="explicit_fiscal_label", confidence=1.0,
                       period_type=PERIOD_QUARTER_ACTUAL, evidence=f"{match.group(0) if match else period_end}; {evidence}",
                       sources=sources, period_end_date=resolved_end, period_start_date=_period_start_date(resolved_end, calendar, quarter, period_type),
                       issuer_fiscal_calendar=_calendar_dict(calendar))
    sources = ["document_type", "filing_header", "issuer_fiscal_calendar"]
    evidence = match.group(0) if match else period_end
    if filename_matches:
        sources.append("source_filename")
        evidence = f"{evidence}; {filename_period.detection_evidence}"
    return _period(year=derived_year, quarter=derived_quarter, label=_format_label(derived_year, derived_quarter, calendar), method="fiscal_calendar", confidence=0.97,
                   period_type=PERIOD_QUARTER_ACTUAL, evidence=evidence,
                   sources=sources, period_end_date=period_end, period_start_date=_period_start_date(period_end, calendar, derived_quarter, PERIOD_QUARTER_ACTUAL),
                   issuer_fiscal_calendar=_calendar_dict(calendar))


def _filename_period(source_url: str | None, source_path: str | None) -> FiscalPeriod | None:
    value = unquote(" ".join(item for item in (source_url, source_path) if item))
    for pattern, _order in (
        (r"\bQ(?P<quarter>[1-4])\s*(?:FY|FISCAL\s*YEAR|FISCAL)?\s*(?P<year>20\d{2}|19\d{2})\b", "quarter_first"),
        (r"\b(?P<year>20\d{2}|19\d{2})\s*Q(?P<quarter>[1-4])\b", "year_first"),
        (r"\b(?P<quarter>[1-4])Q(?P<year>\d{2})\b", "compact_quarter_first"),
    ):
        match = re.search(pattern, value, re.I)
        if not match:
            continue
        quarter, year = int(match.group("quarter")), int(match.group("year"))
        if len(match.group("year")) == 2:
            year = 2000 + year if year < 70 else 1900 + year
        explicit = bool(re.search(r"FY|FISCAL", match.group(0), re.I))
        label = f"Q{quarter} FY{year}" if explicit else f"Q{quarter} {year}"
        return _period(year=year, quarter=quarter, label=label, method="source_filename", confidence=0.90,
                       period_type=PERIOD_QUARTER_ACTUAL, evidence=match.group(0), sources=["source_filename"])
    return None


def _annual_only(value: str | None, method: str, confidence: float, metadata: Mapping[str, Any] | None = None) -> FiscalPeriod | None:
    if not value:
        return None
    upper = str(value).upper()
    match = re.search(r"\b(?:FY|FISCAL\s+YEAR|ANNUAL\s+REPORT|YEAR\s+ENDED)\s*(20\d{2}|19\d{2})\b", upper)
    if not match:
        match = re.search(r"\b(20\d{2}|19\d{2})\s+ANNUAL\s+REPORT\b", upper)
    if not match:
        return None
    calendar = _fiscal_calendar(metadata, str(value))
    end = _period_end_date(str(value))
    # For an annual filing, the reported year-end date is the economic key.
    # Comparative statements routinely mention older fiscal years first.
    year = _fiscal_year_for_date(end, calendar) if end else int(match.group(1))
    return _period(year=year, label=f"FY {year}", method=method, confidence=max(0.75, confidence - .03),
                   period_type=PERIOD_ANNUAL_ACTUAL, evidence=match.group(0), sources=[method],
                   period_end_date=end, period_start_date=_period_start_date(end, calendar, None, PERIOD_ANNUAL_ACTUAL), issuer_fiscal_calendar=_calendar_dict(calendar))


def _scan(value: str | None, method: str, confidence: float, *, allow_annual=True, metadata=None) -> FiscalPeriod | None:
    if not value:
        return None
    explicit = _explicit_fiscal_candidates(str(value))
    if explicit:
        quarter, year, evidence = explicit[0]
        calendar = _fiscal_calendar(metadata, str(value))
        return _period(year=year, quarter=quarter, label=_format_label(year, quarter, calendar, explicit_fiscal=True), method="explicit_fiscal_label", confidence=1.0,
                       period_type=PERIOD_QUARTER_ACTUAL, evidence=evidence, sources=[method, "explicit_fiscal_label"],
                       period_end_date=_period_end_date(str(value)), issuer_fiscal_calendar=_calendar_dict(calendar))
    # Preserve ordinary calendar-quarter titles when no issuer fiscal marker
    # is present.  These labels are still useful for calendar issuers and are
    # deliberately lower priority than filing headers and explicit fiscal
    # statements above.
    generic_patterns = (
        r"\bQ(?P<quarter>[1-4])\s*(?P<year>20\d{2}|19\d{2})\b",
        r"\b(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTERS?\s+(?P<year>20\d{2}|19\d{2})\b",
    )
    for pattern in generic_patterns:
        match = re.search(pattern, str(value), re.I)
        if not match:
            continue
        if match.groupdict().get("quarter"):
            quarter, year = int(match.group("quarter")), int(match.group("year"))
        else:
            quarter, year = _QUARTER_WORDS[match.group("quarter_word").lower()], int(match.group("year"))
        calendar = _fiscal_calendar(metadata, str(value))
        return _period(year=year, quarter=quarter, label=_format_label(year, quarter, calendar),
                       method=method, confidence=confidence, period_type=PERIOD_QUARTER_ACTUAL,
                       evidence=match.group(0), sources=[method],
                       issuer_fiscal_calendar=_calendar_dict(calendar))
    if not allow_annual:
        return None
    return _annual_only(str(value), method, confidence, metadata)


def detect_fiscal_period(metadata: Mapping[str, Any] | None = None, title: str = "", body: str = "", *, document_type: str | None = None, source_url: str | None = None, source_path: str | None = None) -> FiscalPeriod:
    """Return the issuer reporting period without calendar-year leakage."""
    dtype = (document_type or "").lower()
    period_metadata = dict(metadata or {})
    # SEC form identity is authoritative even when an upstream downloader
    # classified the rendered artifact as a news release or generic report.
    # This ensures the filing header wins over comparative quarter phrases in
    # the body for both calendar and non-calendar issuers.
    sec_form = str(
        period_metadata.get("sec_form")
        or period_metadata.get("form_type")
        or period_metadata.get("form")
        or ""
    ).upper().replace("/A", "")
    if sec_form == "10-K":
        dtype = "annual_report"
    elif sec_form == "10-Q":
        dtype = "quarterly_report"
    if source_url and not period_metadata.get("source_url"):
        period_metadata["source_url"] = source_url
    if source_path and not period_metadata.get("source_path"):
        period_metadata["source_path"] = source_path
    # SEC submissions and downloader manifests may carry the period end only
    # in a title/filename (for example ``TICKER 10-K 2026-01-31``).  Treat that
    # date as filing identity metadata, never as an arbitrary narrative date.
    if not period_metadata.get("period_end_date") and dtype in {"annual_report", "quarterly_report"}:
        title_end = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", str(title))
        if title_end:
            try:
                period_metadata["period_end_date"] = date(int(title_end.group(1)), int(title_end.group(2)), int(title_end.group(3))).isoformat()
            except ValueError:
                pass
    # Proxy and governance documents may contain comparative fiscal language
    # for compensation or peer disclosures.  Unless the source supplies an
    # explicit period in metadata, that language is not the issuer's reporting
    # period and must remain UNKNOWN.
    if dtype in {"proxy_statement", "governance_filing"} and not _explicit(period_metadata):
        return _period(
            warning="PERIOD_NOT_APPLICABLE",
            period_type=PERIOD_UNKNOWN,
            method="document_type_not_applicable",
            confidence=0.0,
            sources=["document_type"],
            issuer_fiscal_calendar=_calendar_dict(_fiscal_calendar(period_metadata, body)),
        )
    sec_header_present = bool(re.search(
        r"\bFORM\s+(?:3|4|5|8-K|10-K|10-Q|DEF\s+14A)\b|"
        r"SECURITIES\s+AND\s+EXCHANGE\s+COMMISSION",
        str(body)[:5000], re.I,
    ))
    # A generic other-report body may legitimately carry an explicit issuer
    # fiscal label alongside a period-end date (for example an earnings
    # supplement).  Treat it through the normal explicit-label path unless
    # the source is recognizably an SEC filing; the bounded filing-header
    # precedence is reserved for SEC/quarterly text where comparative phrases
    # are a known source of corruption.
    header = (
        _filing_header(
            body,
            period_metadata,
            allow_conflicting_explicit=(dtype == "other_report" and not sec_header_present),
        )
        if dtype in {"quarterly_report", "", "other_report"} or (sec_header_present and dtype != "annual_report")
        else None
    )
    if header:
        return header
    found = _explicit(period_metadata)
    if found:
        return found
    if dtype == "annual_report":
        # Some official IR bundles are routed as a 10-K/annual report while
        # their flattened body begins with comparative tables from older
        # years.  A compact title such as ``4Q22`` is a source identity, so
        # use it with the issuer year-end calendar before scanning body text.
        title_fiscal = _explicit_fiscal_candidates(str(title))
        if title_fiscal:
            quarter, year, evidence = title_fiscal[0]
            if quarter == 4:
                calendar = _fiscal_calendar(period_metadata, body)
                end_year = year if calendar.fiscal_year_label_convention == "ending_year" else year + 1
                try:
                    end_day = calendar.fiscal_year_end_day or monthrange(end_year, calendar.fiscal_year_end_month)[1]
                    end = date(end_year, calendar.fiscal_year_end_month, min(end_day, monthrange(end_year, calendar.fiscal_year_end_month)[1])).isoformat()
                except ValueError:
                    end = None
                return _period(
                    year=year,
                    label=f"FY {year}",
                    method="sec_form_title",
                    confidence=0.99,
                    period_type=PERIOD_ANNUAL_ACTUAL,
                    evidence=evidence,
                    sources=["sec_form", "title", "issuer_fiscal_calendar"],
                    period_end_date=end,
                    period_start_date=_period_start_date(end, calendar, None, PERIOD_ANNUAL_ACTUAL),
                    issuer_fiscal_calendar=_calendar_dict(calendar),
                )
        end = period_metadata.get("period_end_date") or _period_end_date(body)
        calendar = _fiscal_calendar(period_metadata, body)
        if end:
            year = _fiscal_year_for_date(str(end), calendar)
            found = _period(year=year, label=f"FY {year}" if year else None, method="period_end_derivation", confidence=.96,
                            period_type=PERIOD_ANNUAL_ACTUAL, evidence=f"period end {end}", sources=["period_end", "issuer_fiscal_calendar"],
                            period_end_date=str(end), period_start_date=_period_start_date(str(end), calendar, None, PERIOD_ANNUAL_ACTUAL),
                            issuer_fiscal_calendar=_calendar_dict(calendar))
        else:
            found = _annual_only(title, "title", .98, period_metadata) or _annual_only(body, "body", .82, period_metadata)
        if found:
            return found
    filename = _filename_period(source_url, source_path)
    if filename and dtype in {"quarterly_report", ""}:
        return filename
    allow_annual = dtype not in {"quarterly_report", "news_release", "event_transcript", "earnings_transcript", "investor_presentation"}
    found = _scan(title, "title", .98, allow_annual=allow_annual, metadata=period_metadata)
    if found:
        return found
    found = _scan(body, "body", .82, allow_annual=allow_annual, metadata=period_metadata)
    return found or _period(warning="PERIOD_UNRESOLVED", period_type=PERIOD_UNKNOWN,
                            issuer_fiscal_calendar=_calendar_dict(_fiscal_calendar(metadata, body)))


def issuer_fiscal_calendar(metadata: Mapping[str, Any] | None = None, text: str = "") -> dict[str, Any]:
    """Public adapter hook for callers that need calendar provenance."""
    return _calendar_dict(_fiscal_calendar(metadata, text))


def canonical_period_label(*, fiscal_year: int | None, fiscal_quarter: int | None, issuer_fiscal_calendar: Mapping[str, Any] | None = None, original_label: str | None = None, period_end_date: str | None = None) -> str | None:
    """Return one issuer-period label while retaining the source label.

    Calendar-year issuers may disclose the same quarter as ``Q1 2025`` and
    ``Q1 FY2025``.  Those labels share one economic period.  Non-calendar
    issuers keep the fiscal marker because ``Q1 FY2027`` is not equivalent to
    a calendar ``Q1 2027``.
    """
    if fiscal_year is None:
        return original_label
    calendar = dict(issuer_fiscal_calendar or {})
    calendar_type = str(calendar.get("calendar_type") or "").lower()
    # Claims created by older callers may carry the source label but no
    # calendar metadata.  Preserve an explicit FY marker until the issuer
    # calendar is known; normalized documents with resolved calendar metadata
    # still collapse calendar ``Qn FYYYYY`` aliases safely.
    non_calendar = calendar_type == "non_calendar" or (not calendar_type and bool(re.search(r"\bFY\s*\d{4}\b", str(original_label or ""), re.I)))
    # For calendar-year issuers, the reported period end is the authoritative
    # quarter coordinate.  Historical comparative tables sometimes contain a
    # misleading ``Qn FYYYYY`` label even though the end date is a normal
    # calendar quarter; normalize that label without changing the source text.
    if not non_calendar and period_end_date:
        try:
            ended = date.fromisoformat(str(period_end_date)[:10])
        except (TypeError, ValueError):
            ended = None
        if ended is not None and fiscal_quarter in {1, 2, 3, 4}:
            fiscal_year = ended.year
            fiscal_quarter = ((ended.month - 1) // 3) + 1
    if fiscal_quarter in {1, 2, 3, 4}:
        return f"Q{fiscal_quarter} FY{fiscal_year}" if non_calendar else f"Q{fiscal_quarter} {fiscal_year}"
    return f"FY {fiscal_year}"


def canonical_period_identity(*, issuer: str | None, fiscal_year: int | None, fiscal_quarter: int | None, period_end_date: str | None, period_type: str | None, issuer_fiscal_calendar: Mapping[str, Any] | None = None, period_start_date: str | None = None) -> str | None:
    label = canonical_period_label(fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter, issuer_fiscal_calendar=issuer_fiscal_calendar, period_end_date=period_end_date)
    if label is None:
        return None
    return "|".join((str(issuer or "").upper(), label, str(period_start_date or ""), str(period_end_date or ""), str(period_type or "UNKNOWN").upper()))


extract_fiscal_period = detect_fiscal_period
classify_fiscal_period = detect_fiscal_period
