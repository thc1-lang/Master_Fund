"""Generic institutional intelligence engines used by research-output-v2.

The engines in this module are deliberately evidence first.  They recognise
mechanisms in already validated claims and preserve the source identifiers
that support each record.  They do not infer a moat, an industry conclusion,
or an execution outcome from a keyword alone; every record carries an
assessment status and an explicit evidence status.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date
from typing import Any, Iterable, Mapping


MATERIALITY = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def _text(row: Mapping[str, Any]) -> str:
    return " ".join(str(row.get(k) or "") for k in ("claim_text", "evidence_text", "summary", "description", "commitment_text", "original_statement"))


def _id(row: Mapping[str, Any]) -> str:
    return str(row.get("claim_id") or row.get("document_id") or row.get("commitment_id") or row.get("strategic_commitment_id") or row.get("record_id") or "")


def _date(row: Mapping[str, Any]) -> str:
    for key in ("public_availability_date", "available_date", "acceptance_datetime", "publication_date", "filing_date", "source_date", "issue_date", "issued_date", "actual_available_date", "commitment_date", "event_date", "target_date"):
        value = row.get(key)
        if value:
            return str(value)[:10]
    return ""


def _current_eligible(row: Mapping[str, Any]) -> bool:
    """Keep historical evidence auditable without presenting it as current."""
    return str(row.get("temporal_status") or "").upper() != "HISTORICAL"


def _stable(prefix: str, *parts: Any) -> str:
    return f"{prefix}:{hashlib.sha256('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:20]}"


def _materiality(text: str, *, default: str = "MEDIUM") -> str:
    lower = text.lower()
    if re.search(r"critical|material adverse|substantial|transformative|very significant|major impact", lower):
        return "CRITICAL"
    if re.search(r"material|significant|large|strategic|key|important", lower):
        return "HIGH"
    if re.search(r"minor|limited|routine|ordinary", lower):
        return "LOW"
    return default


def _status_for_evidence(rows: list[Mapping[str, Any]], *, positive: str = "SUPPORTED") -> str:
    if not rows:
        return "NOT_ESTABLISHED"
    positive_rows = [r for r in rows if not re.search(r"risk|challenge|uncertain|may not|could adversely|erosion|declin", _text(r), re.I)]
    negative_rows = [r for r in rows if r not in positive_rows]
    if positive_rows and negative_rows:
        return "CONFLICTED"
    return positive if positive_rows else "INFERRED"


ADVANTAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("SWITCHING_COSTS", r"switching cost|difficult to switch|embedded in|integrat(?:ed|ion).*workflow|migration cost"),
    ("NETWORK_EFFECTS", r"network effect|more users|ecosystem.*users|community of"),
    ("SCALE_ADVANTAGE", r"scale advantage|economies of scale|at scale|largest|scale economics"),
    ("COST_ADVANTAGE", r"lower cost|cost advantage|cost leadership|cost efficient"),
    ("BRAND", r"brand strength|brand recognition|trusted brand|brand loyalty"),
    ("IP_TECHNOLOGY", r"proprietary technology|intellectual property|patent|proprietary platform|technology advantage"),
    ("DATA_ADVANTAGE", r"proprietary data|data advantage|data asset|data network"),
    ("DISTRIBUTION", r"distribution advantage|distribution network|channel reach|go-to-market"),
    ("PLATFORM_ECOSYSTEM", r"platform ecosystem|ecosystem|developer platform|third-party developers"),
    ("CUSTOMER_INTEGRATION", r"customer integration|embedded with customers|mission-critical|mission critical"),
    ("REGULATORY_BARRIER", r"regulatory barrier|license|regulated entry|regulatory approval"),
    ("SUPPLY_ADVANTAGE", r"supply advantage|secured supply|capacity allocation|long-term supply"),
)


def build_competitive_advantage(claims: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for claim in claims:
        text = _text(claim)
        for category, pattern in ADVANTAGE_PATTERNS:
            if re.search(pattern, text, re.I):
                groups[category].append(claim)
    records: list[dict[str, Any]] = []
    for category, rows in sorted(groups.items()):
        evidence_ids = sorted({_id(row) for row in rows if _id(row)})
        mechanism = {
            "SWITCHING_COSTS": "Product or workflow integration may make replacement costly or disruptive.",
            "NETWORK_EFFECTS": "Participation or usage may increase value for other participants.",
            "SCALE_ADVANTAGE": "Scale may reduce unit economics or improve investment capacity.",
            "IP_TECHNOLOGY": "Proprietary technology or protected know-how may differentiate the offering.",
            "CUSTOMER_INTEGRATION": "Operational dependence may increase retention and reduce substitution.",
        }.get(category, f"Evidence is relevant to {category.replace('_', ' ').lower()}.")
        contradictory = [r for r in rows if re.search(r"risk|challenge|erosion|compet(?:e|ition)|substitut|may not|uncertain", _text(r), re.I)]
        records.append({
            "advantage_id": _stable("advantage", category, *evidence_ids),
            "type": category,
            "mechanism": mechanism,
            "supporting_evidence": evidence_ids,
            "contradictory_evidence": sorted({_id(row) for row in contradictory if _id(row)}),
            "proof_indicators": ["customer retention", "pricing stability", "share or usage persistence"],
            "erosion_indicators": ["credible substitutes", "customer loss", "margin compression", "supply displacement"],
            "durability": "UNRESOLVED",
            "materiality": _materiality(" ".join(_text(r) for r in rows), default="HIGH"),
            "source_status": "VERIFIED" if evidence_ids else "UNVERIFIED",
            "analytical_confidence": "MEDIUM" if len(evidence_ids) >= 2 else "LOW",
            "assessment_status": "CONFLICTED" if contradictory else _status_for_evidence(rows),
        })
    return {"status": "AVAILABLE" if records else "NO_PROVEN_ADVANTAGE", "records": records, "coverage": {"candidate_count": len(records), "evidence_backed": sum(bool(r["supporting_evidence"]) for r in records)}}


INDUSTRY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("DIRECT_COMPETITORS", r"compet(?:e|itor|ition)|market share|rival"),
    ("SUBSTITUTES", r"substitut|alternative to|replace(?:ment)?|alternative technology"),
    ("SUPPLIER_CONCENTRATION", r"supplier concentration|single source|sole source|foundry|key supplier"),
    ("CUSTOMER_CONCENTRATION", r"customer concentration|one customer|single customer|largest customer"),
    ("CUSTOMER_BARGAINING_POWER", r"customer bargaining|pricing pressure|price concessions|customer power"),
    ("SUPPLIER_BARGAINING_POWER", r"supplier bargaining|supply constraint|input cost pressure"),
    ("BARRIERS_TO_ENTRY", r"barrier(?:s)? to entry|capital requirement|regulatory barrier|high entry"),
    ("REGULATORY_CONSTRAINTS", r"regulat(?:ory|ion)|license|compliance requirement"),
    ("CAPACITY_STRUCTURE", r"capacity|utilization|production constraint|manufacturing"),
    ("PRICING_DYNAMICS", r"pricing|price increase|price pressure|average selling price"),
    ("TECHNOLOGY_SUBSTITUTION", r"technology substitution|obsolete|next generation|substitut"),
    ("INDUSTRY_CYCLICALITY", r"cyclic|seasonal|downturn|cycle"),
    ("MARKET_CONCENTRATION", r"concentrated market|market concentration|dominant share"),
)


def build_industry_structure(claims: Iterable[Mapping[str, Any]], *, adapter: str = "generic-v1") -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for claim in claims:
        for category, pattern in INDUSTRY_PATTERNS:
            if re.search(pattern, _text(claim), re.I):
                groups[category].append(claim)
    records = []
    for category, rows in sorted(groups.items()):
        ids = sorted({_id(row) for row in rows if _id(row)})
        records.append({"assessment_id": _stable("industry", category, *ids), "category": category, "fact": " ".join(_text(rows[0]).split())[:500], "assessment": "Evidence is relevant to this structural factor; direction and magnitude require company or industry specific corroboration.", "evidence_ids": ids, "status": "FACT" if ids else "NOT_ESTABLISHED", "materiality": _materiality(" ".join(_text(r) for r in rows)), "analytical_confidence": "MEDIUM" if len(ids) > 1 else "LOW"})
    return {"status": "AVAILABLE" if records else "NO_STRUCTURAL_EVIDENCE", "adapter": adapter, "records": records, "coverage": {"categories": len(records)}}


EXECUTION_PATTERNS = r"\b(plan|planned|launch|ramp|capacity|expansion|milestone|restructur|integration|partnership|approval|commercial|cost reduction|production)\b"


def _execution_status(row: Mapping[str, Any], as_of_date: str) -> str:
    text = _text(row).lower()
    explicit = str(row.get("status") or row.get("event_status") or "").upper()
    if explicit in {"OPEN", "ON_TRACK", "COMPLETED", "DELAYED", "REVISED", "FAILED", "CANCELLED", "UNKNOWN"}:
        return explicit
    if re.search(r"cancel|abandon", text): return "CANCELLED"
    if re.search(r"delay|behind", text): return "DELAYED"
    if re.search(r"complete|completed|launched|achieved|delivered", text): return "COMPLETED"
    target = _date(row)
    if target and target < str(as_of_date)[:10] and re.search(r"target|expected|plan", text):
        return "UNKNOWN"  # an unresolved overdue commitment is not a failure
    return "OPEN"


def build_operating_execution(claims: Iterable[Mapping[str, Any]], commitments: Iterable[Mapping[str, Any]], as_of_date: str) -> dict[str, Any]:
    rows: list[Mapping[str, Any]] = [row for row in commitments if _text(row) and _current_eligible(row)]
    rows.extend(row for row in claims if _current_eligible(row) and re.search(EXECUTION_PATTERNS, _text(row), re.I) and re.search(r"plan|target|expected|will|launch|milestone", _text(row), re.I))
    seen: set[str] = set(); records = []
    for row in rows:
        key = re.sub(r"\W+", " ", _text(row).lower()).strip()[:180]
        if not key or key in seen: continue
        seen.add(key)
        ids = [_id(row)] if _id(row) else []
        records.append({"commitment_id": _stable("commitment", key), "original_statement": _text(row)[:1000], "issue_date": _date(row) or None, "target_date_or_window": row.get("target_date") or row.get("target_period") or row.get("expected_date") or None, "expected_result": row.get("expected_result") or "Outcome not yet independently resolved.", "materiality": _materiality(_text(row)), "source": {"record_ids": ids, "source_url": row.get("source_url")}, "evidence_anchor": {"claim_id": row.get("claim_id"), "document_id": row.get("document_id")}, "status": _execution_status(row, as_of_date), "outcome_evidence": []})
    resolved = sum(1 for row in records if row["status"] in {"COMPLETED", "DELAYED", "REVISED", "FAILED", "CANCELLED"})
    return {"status": "AVAILABLE" if records else "NO_COMMITMENTS_IDENTIFIED", "records": records, "coverage": {"commitments": len(records), "resolved": resolved, "resolved_sample_sufficient": resolved >= 2}, "aggregate_status": "INSUFFICIENT_RESOLVED_SAMPLE" if records and resolved < 2 else "AVAILABLE"}


def build_guidance_reliability(claims: Iterable[Mapping[str, Any]], documents: Mapping[str, Mapping[str, Any]], guidance_rows: Iterable[Mapping[str, Any]], outcome_rows: Iterable[Mapping[str, Any]], as_of_date: str) -> dict[str, Any]:
    rows = [row for row in guidance_rows if _current_eligible(row)]
    outcomes = {str(row.get("guidance_id")): row for row in outcome_rows if _current_eligible(row) and (_date(row) <= str(as_of_date)[:10] or not _date(row))}
    records = []
    for row in rows:
        available = _date(row)
        if available and available > str(as_of_date)[:10]: continue
        low, high = row.get("low") or row.get("low_value"), row.get("high") or row.get("high_value")
        midpoint = row.get("midpoint") or row.get("point_value")
        if midpoint is None and low is not None and high is not None:
            midpoint = (float(low) + float(high)) / 2
        outcome = outcomes.get(str(row.get("guidance_id")))
        outcome_value = (outcome.get("result") or outcome.get("outcome") or "OPEN") if outcome else ("OPEN" if low is not None or high is not None else "NOT_COMPARABLE")
        if outcome_value == "MET":
            outcome_value = "IN_LINE"
        records.append({"guidance_id": row.get("guidance_id") or _stable("guidance", _text(row)), "issue_date": available or row.get("issue_date") or row.get("issued_date"), "target_period": row.get("target_period") or row.get("fiscal_horizon"), "metric": row.get("metric"), "low": low, "high": high, "midpoint": midpoint, "unit": row.get("unit"), "currency": row.get("currency"), "accounting_basis": row.get("accounting_basis"), "original_or_revision": row.get("original_or_revised") or row.get("revision_type") or "ORIGINAL", "source": {"claim_id": row.get("source_claim_id"), "document_id": row.get("source_document_id"), "source_url": row.get("source_url")}, "evidence_anchor": {"text": str(row.get("evidence_text") or "")[:500]}, "outcome": outcome_value, "outcome_evidence": outcome or {}, "comparability": "COMPARABLE" if low is not None or high is not None else "NOT_COMPARABLE"})
    numeric_claims = [row for row in claims if _current_eligible(row) and re.search(r"guidance|outlook|forecast|target|expect", _text(row), re.I) and re.search(r"\$?\d+(?:\.\d+)?\s*(?:%|percent|million|billion|bn|m|k)?", _text(row), re.I)]
    # Older track-record stores may not contain a captured commitment even
    # though the frozen claim layer has a numeric guidance statement.  Build a
    # conservative OPEN/NOT_COMPARABLE record from that claim so the dossier
    # reports coverage accurately without inventing an actual or an outcome.
    captured_ids = {str(row.get("source_claim_id")) for row in records if row.get("source_claim_id")}
    for claim in numeric_claims:
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id or claim_id in captured_ids:
            continue
        text = _text(claim)
        metric = next((name for name, pattern in (("EPS", r"\beps\b|earnings per share"), ("revenue", r"revenue|top[- ]line"), ("gross_margin", r"gross margin"), ("operating_margin", r"operating margin"), ("capex", r"capex|capital expenditure"), ("cash_flow", r"free cash flow|cash flow")) if re.search(pattern, text, re.I)), "company_kpi")
        horizon_match = re.search(r"\b(FY\s*20\d{2}|Q[1-4]\s*20\d{2})\b", text, re.I)
        horizon = horizon_match.group(1).upper().replace(" ", "") if horizon_match else claim.get("period_label") or "UNSPECIFIED"
        range_match = re.search(r"(?:\$\s*)?(\d+(?:\.\d+)?)\s*(million|billion|thousand|bn|mm|m|k|%)?\s*(?:to|[-–])\s*(?:\$\s*)?(\d+(?:\.\d+)?)\s*(million|billion|thousand|bn|mm|m|k|%)?", text, re.I)
        low = high = None
        unit = None
        if range_match:
            low, high, unit = float(range_match.group(1)), float(range_match.group(3)), range_match.group(2) or range_match.group(4)
        records.append({"guidance_id": _stable("guidance", claim_id), "issue_date": _date(claim) or None, "target_period": horizon, "metric": metric, "low": low, "high": high, "midpoint": (low + high) / 2 if low is not None and high is not None else None, "unit": unit, "currency": "USD" if "$" in text else None, "accounting_basis": "non-GAAP" if "non-gaap" in text.lower() else "GAAP" if "gaap" in text.lower() else None, "original_or_revision": "ORIGINAL", "source": {"claim_id": claim_id, "document_id": claim.get("document_id"), "source_url": claim.get("source_url")}, "evidence_anchor": {"text": text[:500]}, "outcome": "OPEN" if low is not None or high is not None else "NOT_COMPARABLE", "outcome_evidence": {}, "comparability": "COMPARABLE" if low is not None or high is not None else "NOT_COMPARABLE"})
    comparable_count = sum(1 for row in records if row["comparability"] == "COMPARABLE")
    if records and comparable_count == 0:
        status = "PRESENT_BUT_NOT_COMPARABLE"
    elif records:
        status = "AVAILABLE"
    elif numeric_claims:
        status = "PRESENT_BUT_NOT_CAPTURED"
    else:
        status = "NO_COMPARABLE_GUIDANCE"
    counts = {name: sum(1 for row in records if row["outcome"] == name) for name in ("BEAT", "IN_LINE", "MET", "MISS", "OPEN", "NOT_COMPARABLE", "WITHDRAWN", "REVISED")}
    return {"status": status, "records": records, "coverage": {"comparable_records": comparable_count, "resolved": sum(1 for row in records if row["outcome"] in {"BEAT", "IN_LINE", "MET", "MISS"}), "open": counts["OPEN"], "not_comparable": counts["NOT_COMPARABLE"], "numeric_claims": len(numeric_claims), "reasons": ["No numeric range or point value was available for the captured claim." for row in records if row["comparability"] != "COMPARABLE"][:5]}, "outcome_counts": counts}


QUALITY_PATTERNS: dict[str, str] = {
    "REVENUE_QUALITY": r"recurring|recurrence|renewal|visibility|contracted|backlog|customer retention",
    "DEMAND_QUALITY": r"demand|orders|bookings|backlog|pipeline|customer growth",
    "MARGIN_QUALITY": r"gross margin|operating margin|margin|cost efficiency|mix",
    "PRICING_POWER": r"pricing power|price increase|pricing|average selling price|mix",
    "CUSTOMER_QUALITY": r"customer concentration|retention|enterprise customer|large customer|diversif",
    "SUPPLIER_RESILIENCE": r"supplier|supply chain|multi-source|capacity|foundry",
    "COMPETITIVE_POSITION": r"market share|competitive|technology|proprietary|switching cost",
    "ECONOMIC_DURABILITY": r"durable|long[- ]term|structural|recurring|competitive advantage",
    "CAPITAL_INTENSITY": r"capex|capital expenditure|capital intensive|capacity expansion",
    "CASH_GENERATION_QUALITY": r"free cash flow|cash generation|operating cash flow|conversion",
}

BANK_QUALITY_PATTERNS: dict[str, str] = {
    "DEPOSIT_FRANCHISE_QUALITY": r"deposit franchise|deposit growth|deposit mix|low[- ]cost deposits|core deposits",
    "FUNDING_QUALITY": r"funding|deposit beta|stable funding|liquidity coverage|liquidity",
    "CREDIT_QUALITY": r"credit quality|net charge[- ]offs?|delinquenc|provision(?:s)? for credit|allowance for credit",
    "CAPITAL_STRENGTH": r"CET1|common equity tier 1|capital ratio|risk[- ]weighted assets|regulatory capital",
    "EARNINGS_DIVERSIFICATION": r"investment banking fees?|markets revenue|trading revenue|asset management|fee income",
    "FEE_INCOME_QUALITY": r"fee income|recurring fees?|asset management fees?|payments revenue",
    "OPERATING_EFFICIENCY": r"efficiency ratio|operating leverage|expense discipline|cost[- ]to[- ]income",
    "CUSTOMER_FRANCHISE_SCALE": r"customer franchise|card spending|payment volume|clients?|commercial banking|consumer banking",
    "RISK_DISCIPLINE": r"risk discipline|risk management|stress test|CCAR|risk appetite|controls",
    "THROUGH_CYCLE_PROFITABILITY": r"ROTCE|return on tangible common equity|ROE|through[- ]cycle profitability",
    "BALANCE_SHEET_RESILIENCE": r"balance[- ]sheet resilience|well[- ]capitalized|liquidity|reserves?|capital strength",
    "CAPITAL_GENERATION_CAPACITY": r"capital generation|capital return|buybacks?|dividends?|excess capital",
}

RETAIL_QUALITY_PATTERNS: dict[str, str] = {
    "COMPARABLE_SALES_QUALITY": r"comparable sales|same[- ]store sales|comp sales",
    "TRAFFIC_TICKET_PRODUCTIVITY": r"traffic|transactions|average ticket|basket size|customer frequency",
    "GROSS_MARGIN_DURABILITY": r"gross margin|gross profit rate|operating margin|price investment|sales mix",
    "INVENTORY_DISCIPLINE": r"inventory|markdown|shrink|inventory turns",
    "OMNICHANNEL_EXECUTION": r"e[- ]?commerce|online sales|digital penetration|fulfillment|last[- ]mile",
    "MEMBERSHIP_ECOSYSTEM": r"membership|members",
    "RETAIL_MEDIA_MONETIZATION": r"retail media|advertising revenue|advertising business",
    "STORE_PRODUCTIVITY": r"store count|stores?|clubs?|unit expansion|remodel",
    "SUPPLY_CHAIN_EXECUTION": r"supply[- ]chain|freight|automation|distribution",
    "LABOR_COST_PRESSURE": r"labor costs?|wage pressure|wage inflation",
}


def build_business_quality(claims: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [row for row in claims if _current_eligible(row)]; factors = []
    bank_signal = any(
        str(row.get("dimension") or "") in {"net_interest_income", "net_interest_margin", "deposits", "deposit_growth", "deposit_beta", "cet1", "risk_weighted_assets", "rotce_roe", "stress_test"}
        or re.search(r"\b(?:net interest income|net interest margin|deposit beta|CET1|risk[- ]weighted assets|ROTCE|efficiency ratio)\b", _text(row), re.I)
        for row in rows
    )
    retail_dimensions = {
        "comparable_sales", "same_store_sales", "traffic_transactions", "average_ticket", "unit_volume",
        "ecommerce_sales", "ecommerce_growth", "digital_penetration", "membership_revenue", "membership_growth",
        "advertising_revenue", "retail_media", "store_count", "unit_expansion", "store_remodels", "gross_margin",
        "operating_margin", "operating_expense_rate", "inventory_growth", "inventory_efficiency", "markdowns", "shrink",
        "labor_costs", "wage_pressure", "sales_mix", "price_investment", "private_label_mix", "supply_chain_cost",
        "fulfillment_cost", "automation", "international_comparable_sales", "currency_effect", "customer_frequency",
    }
    retail_signal = any(str(row.get("dimension") or "") in retail_dimensions or re.search(r"\b(?:same[- ]store|comp(?:arable)? sales|retail media|shrink|membership|e[- ]commerce|store productivity|average ticket|traffic|basket size)\b", _text(row), re.I) for row in rows)
    if bank_signal:
        patterns, adapter = BANK_QUALITY_PATTERNS, "bank-v1"
    elif retail_signal:
        patterns, adapter = RETAIL_QUALITY_PATTERNS, "retail-v1"
    else:
        patterns, adapter = QUALITY_PATTERNS, "generic-v1"
    for factor, pattern in patterns.items():
        matches = [row for row in rows if re.search(pattern, _text(row), re.I)]
        ids = sorted({_id(row) for row in matches if _id(row)})
        high_materiality = {"REVENUE_QUALITY", "DEMAND_QUALITY", "MARGIN_QUALITY", "COMPETITIVE_POSITION", "CAPITAL_STRENGTH", "CREDIT_QUALITY", "DEPOSIT_FRANCHISE_QUALITY", "BALANCE_SHEET_RESILIENCE"}
        factors.append({"factor": factor, "favourability": "UNRESOLVED", "evidence_reliability": "HIGH" if ids else "UNKNOWN", "evidence_completeness": "PARTIAL" if ids else "NONE", "analytical_confidence": "MEDIUM" if len(ids) > 1 else "LOW" if ids else "UNCALIBRATED", "materiality": "HIGH" if factor in high_materiality else "MEDIUM", "status": "SUPPORTED_EVIDENCE" if ids else "NOT_ESTABLISHED", "evidence": ids})
    return {"status": "AVAILABLE" if any(row["evidence"] for row in factors) else "NO_AUDITED_QUALITY_EVIDENCE", "industry_adapter": adapter, "factors": factors, "coverage": {"supported_factors": sum(bool(row["evidence"]) for row in factors), "total_factors": len(factors)}}


def canonicalize_risks(rows: Iterable[Mapping[str, Any]], as_of_date: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not _current_eligible(row):
            continue
        text_value = _text(row)
        # Generic section headers and safe-harbor boilerplate do not identify
        # a current economic risk.  Preserve source claims elsewhere, but do
        # not emit an invented canonical risk topic here.
        if re.search(r"\b(?:risk factors?|factors that could|forward[- ]looking statements?|subject to risks and uncertainties)\b", text_value, re.I) and not re.search(r"\b(?:competition|customer|demand|supply|labor|currency|foreign exchange|regulat|cyber|security|inventory|credit|liquidity|interest rate|concentration|litigation|geopolit|data privacy|margin|pricing)\b", text_value, re.I):
            continue
        if re.search(r"\b(?:VaR|value at risk|CVA|XVA)\b", text_value, re.I) and re.search(r"\b(?:methodology|calculated|calculation|non[- ]additive|model definition|validation|assumption)\b", text_value, re.I) and not re.search(r"\b(?:breach|loss|deteriorat|increased|elevated|material exposure|limit exceeded|stress)\b", text_value, re.I):
            # A measurement definition is source evidence, but it is not an
            # economically active risk factor for the current state.
            continue
        key = re.sub(r"\W+", " ", str(row.get("description") or row.get("canonical_risk") or row.get("claim_text") or row.get("evidence_text") or "").lower()).strip()
        key = re.sub(r"\b(the|company|may|could|will)\b", "", key).strip()[:180]
        if key: groups[key].append(row)
    output = []
    for key, members in groups.items():
        text = " ".join(_text(row) for row in members)
        dates = sorted(filter(None, (_date(row) for row in members)))
        current = "STALE" if dates and dates[-1] < str(as_of_date)[:10] and not any(re.search(r"current|ongoing|continue|remain", _text(row), re.I) for row in members) else "OPEN"
        if re.search(r"resolved|mitigated|no longer", text, re.I): current = "RESOLVED"
        if re.search(r"increasing|escalat|worsen", text, re.I): current = "ESCALATING"
        ids = sorted({_id(row) for row in members if _id(row)})
        output.append({"risk_id": _stable("risk", key), "canonical_risk": key, "category": str(members[0].get("topic") or members[0].get("category") or "general"), "first_seen": dates[0] if dates else None, "latest_seen": dates[-1] if dates else None, "current_status": current, "materiality": _materiality(text), "time_horizon": "UNKNOWN", "mechanism": text[:700], "evidence": ids, "contradictory_evidence": [], "mitigants": [], "monitoring_indicators": ["new disclosure or quantitative deterioration"], "change_status": "NEW" if len(members) == 1 else "STABLE", "source_count": len(members)})
    return sorted(output, key=lambda row: (MATERIALITY.index(row["materiality"]), row["risk_id"]))


def canonicalize_catalysts(rows: Iterable[Mapping[str, Any]], as_of_date: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not _current_eligible(row):
            continue
        text = str(row.get("description") or row.get("canonical_event") or row.get("claim_text") or row.get("evidence_text") or "")
        key = re.sub(r"\W+", " ", text.lower()).strip()[:180]
        if key: groups[key].append(row)
    output = []
    for key, members in groups.items():
        text = " ".join(_text(row) for row in members)
        event_dates = sorted(filter(None, (str(row.get("event_date") or row.get("target_date") or "")[:10] for row in members)))
        status = "UPCOMING" if event_dates and event_dates[-1] >= str(as_of_date)[:10] else "STALE" if event_dates else "UNKNOWN"
        if re.search(r"completed|launched|closed|achieved", text, re.I): status = "COMPLETED"
        if re.search(r"delay|delayed", text, re.I): status = "DELAYED"
        ids = sorted({_id(row) for row in members if _id(row)})
        output.append({"catalyst_id": _stable("catalyst", key), "canonical_event": key, "expected_date_or_window": event_dates[-1] if event_dates else None, "materiality": _materiality(text), "mechanism": "The event could change future operating or strategic expectations; magnitude remains evidence-dependent.", "source": ids, "evidence": ids, "current_status": status, "already_known": len(members) > 1, "change_status": "NEW" if len(members) == 1 else "STABLE", "monitoring_trigger": "subsequent company disclosure or reported milestone", "source_count": len(members)})
    return sorted(output, key=lambda row: (MATERIALITY.index(row["materiality"]), row["catalyst_id"]))


def materiality_rank(rows: Iterable[Mapping[str, Any]], *, include_medium: int = 3) -> list[dict[str, Any]]:
    values = list(rows)
    critical_high = [row for row in values if str(row.get("materiality", "MEDIUM")).upper() in {"CRITICAL", "HIGH"}]
    medium = [row for row in values if str(row.get("materiality", "MEDIUM")).upper() == "MEDIUM"][:include_medium]
    return sorted(critical_high + medium, key=lambda row: (MATERIALITY.index(str(row.get("materiality", "MEDIUM")).upper()), str(row.get("risk_id") or row.get("catalyst_id") or "")))


__all__ = ["build_competitive_advantage", "build_industry_structure", "build_operating_execution", "build_guidance_reliability", "build_business_quality", "canonicalize_risks", "canonicalize_catalysts", "materiality_rank"]
