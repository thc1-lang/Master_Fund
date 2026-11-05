"""Adapter from frozen v1 artifacts to the additive institutional dossier v2."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from qualitative_analysis.business_intelligence import BusinessModelEngine
from qualitative_analysis.risk_catalyst import extract_catalysts, extract_risks, risk_coverage_diagnostics

from .assembler import assemble_dossier
from pipeline_runner.compatibility import ArtifactCompatibilityStore, canonical_artifact_fingerprint
from pipeline_runner.issuer_resolution import canonical_identity_hash
from .v2_models import AnalystHandoff, InstitutionalDossier, quality_gate_from_records
from .v2_engines import (
    build_business_quality,
    build_competitive_advantage,
    build_guidance_reliability,
    build_industry_structure,
    build_operating_execution,
    canonicalize_catalysts,
    canonicalize_risks,
    materiality_rank,
)


def _jsonl_rows(root: Path, ticker: str) -> list[dict[str, Any]]:
    """Load source records without treating malformed rows as evidence."""
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("ticker", "")).upper() == ticker.upper():
                row = dict(row)
                row.setdefault("_source_file", str(path))
                rows.append(row)
    return rows


def _record_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"value": value}


def _all_jsonl_rows(root: Path, ticker: str) -> list[dict[str, Any]]:
    """Read all cached rows for the ticker, including future cache material."""
    return _jsonl_rows(root, ticker)


def _available(row: Mapping[str, Any], cutoff: str) -> bool:
    for key in ("public_availability_date", "available_date", "acceptance_datetime", "publication_date", "filing_date", "source_date", "issue_date", "effective_date"):
        value = row.get(key)
        if value and str(value)[:10] > str(cutoff)[:10]:
            return False
    for key in ("source", "source_record", "source_metadata", "provenance", "metadata"):
        nested = row.get(key)
        if isinstance(nested, Mapping) and not _available(nested, cutoff):
            return False
    return True


def _sec_form(row: Mapping[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    explicit = metadata.get("sec_form") or metadata.get("form_type") or metadata.get("form") or row.get("sec_form") or row.get("form_type")
    if explicit:
        return str(explicit).upper().replace("DEFA14A", "DEF 14A")
    text = " ".join(str(row.get(key) or "") for key in ("title", "source_url", "local_path"))
    if re.search(r"DEF\s*14A|DEFA14A|proxy statement|annual meeting", text, re.I):
        return "DEF 14A"
    if re.search(r"\b10-K\b|10K", text, re.I):
        return "10-K"
    if re.search(r"\b10-Q\b|10Q", text, re.I):
        return "10-Q"
    if re.search(r"\b20-F\b|20F", text, re.I):
        return "20-F"
    if re.search(r"\b6-K\b|6K", text, re.I):
        return "6-K"
    return ""


def _period_end(row: Mapping[str, Any]) -> str:
    return str(row.get("period_end_date") or (row.get("metadata") or {}).get("period_end_date") or "")[:10]


def _coverage_diagnostics(*, documents: list[dict[str, Any]], claims: list[dict[str, Any]], base: Any, state_rows: list[dict[str, Any]], temporal_rows: list[dict[str, Any]] | None = None, as_of_date: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    sec_docs = [row for row in documents if _sec_form(row)]
    management_docs = [row for row in sec_docs if _sec_form(row) in {"DEF 14A", "10-K", "10-Q", "20-F", "6-K", "8-K"} or str(row.get("document_type") or "").lower() in {"annual_report", "governance_report", "remuneration_report"}]
    proxy_docs = [row for row in sec_docs if _sec_form(row) == "DEF 14A"]
    people_count = int(base.management.get("people_count") or 0)
    if management_docs and people_count == 0:
        diagnostics.append({"code": "MANAGEMENT_SOURCES_AVAILABLE_BUT_ROSTER_EMPTY", "module": "management_intelligence", "severity": "CRITICAL", "reason": "Official SEC management-capable documents are available but no management roster was extracted.", "required_action": "Repair management source routing or parsing and rebuild Component 6A."})
    governance = base.governance or {}
    if proxy_docs and not governance.get("available"):
        diagnostics.append({"code": "AUTHORITATIVE_PROXY_AVAILABLE_BUT_NOT_ROUTED", "module": "governance_intelligence", "severity": "CRITICAL", "reason": "A normalized official DEF 14A exists but no governance snapshot received it.", "required_action": "Route the normalized proxy through GovernanceSourceCatalog and rebuild governance."})
    if proxy_docs and governance.get("available"):
        board_size = int(governance.get("board_size") or 0)
        committees = governance.get("committees") or []
        independence = governance.get("independence") or {}
        has_proxy_headings = any(re.search(r"audit\s+committee|compensation\s+committee|nominating|independent\s+(?:director|nominee)", str(row.get("text") or ""), re.I) for row in proxy_docs)
        def committee_has_members(item: Any) -> bool:
            if not isinstance(item, Mapping):
                return False
            # Governance v2 stores normalized committee membership in either
            # explicit member arrays or the independence map.  The latter is
            # still authoritative membership evidence and must not be treated
            # as an empty committee.
            return bool(item.get("members") or item.get("member_ids") or item.get("member_independence"))

        # A validated board roster is the minimum governance gate.  Committee
        # shells with undisclosed/unparsed memberships are retained as an
        # explicit coverage limitation, but optional committee detail must not
        # turn an otherwise usable proxy into a critical extraction failure.
        if has_proxy_headings and board_size == 0:
            diagnostics.append({"code": "CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE", "module": "governance_intelligence", "severity": "CRITICAL", "reason": "The proxy contains director or committee evidence but the governance snapshot is materially incomplete.", "required_action": "Repair proxy classification/routing/extraction and rebuild governance."})
    bad_guidance = [row for row in claims if str(row.get("dimension") or "") == "guidance" and not row.get("guidance_subtype") and re.search(r"director|board|shareholder|committee|governance|legal|risk|analyst|third[- ]party", str(row.get("evidence_text") or row.get("claim_text") or ""), re.I)]
    if bad_guidance:
        diagnostics.append({"code": "MATERIAL_GUIDANCE_EXTRACTION_SEMANTIC_FAILURE", "module": "qualitative_extraction", "severity": "CRITICAL", "reason": f"{len(bad_guidance)} governance/legal/third-party statements were classified as management guidance.", "required_action": "Re-extract guidance with the business-metric eligibility taxonomy."})
    actual = [row for row in claims if str(row.get("period_type") or "") in {"QUARTER_ACTUAL", "ANNUAL_ACTUAL", "YTD_ACTUAL"}]
    latest_actual = max(actual, key=lambda row: (_period_end(row), str(row.get("period_label") or "")), default=None)
    state_periods = [
        row
        for row in state_rows
        if row.get("state_id") and row.get("current_state") is not None and row.get("period_label")
    ]
    # State materialization time is an implementation timestamp, not the
    # information date.  Select by the issuer reporting period/availability
    # envelope so a later rebuild cannot make a stale state look current.
    chosen = max(state_periods, key=lambda row: (_period_end(row), str(row.get("period_label") or ""), str(row.get("public_availability_date") or row.get("available_date") or "")), default=None)
    if latest_actual and chosen:
        latest_end = _period_end(latest_actual)
        state_end = _period_end(chosen)
        # Original fiscal aliases (for example ``Q2 FY2026`` and ``Q2
        # 2026``) can describe the same issuer period.  Compare the actual
        # period end whenever it is present; a label difference alone is not
        # evidence of stale state.
        stale = bool(latest_end and state_end and state_end < latest_end)
        if not latest_end or not state_end:
            stale = bool(latest_actual.get("period_label") and chosen.get("period_label") != latest_actual.get("period_label"))
        if stale:
            diagnostics.append({"code": "CURRENT_STATE_STALE_DESPITE_NEWER_ACTUAL_REPORT", "module": "state_synthesis", "severity": "CRITICAL", "reason": f"State period {chosen.get('period_label')} is older than latest actual period {latest_actual.get('period_label')}.", "required_action": "Rebuild state synthesis using latest issuer reporting period by period-end date."})
    # Fiscal integrity is checked from the normalized reporting coordinates,
    # independently of the display label.  A repeated fiscal coordinate with
    # incompatible period ends indicates comparative-text contamination.
    coordinates: dict[tuple[Any, Any, str], set[str]] = {}
    # SEC 8-K exhibits and press releases often quote comparative periods; the
    # issuer's 10-Q/10-K filing header is the authoritative fiscal coordinate.
    # Do not let a supplemental 8-K alias manufacture a fiscal conflict.
    for row in documents:
        if _sec_form(row) not in {"10-Q", "10-K"}:
            continue
        period_kind = str(row.get("period_type") or "").upper()
        if period_kind not in {"QUARTER_ACTUAL", "ANNUAL_ACTUAL", "YTD_ACTUAL"}:
            continue
        fy, fq = row.get("fiscal_year"), row.get("fiscal_quarter")
        end = _period_end(row)
        if fy is None or not end:
            continue
        coordinates.setdefault((fy, fq, period_kind), set()).add(end)
    conflicts = [{"fiscal_year": key[0], "fiscal_quarter": key[1], "period_type": key[2], "period_ends": sorted(ends)} for key, ends in coordinates.items() if len(ends) > 1]
    if conflicts:
        diagnostics.append({"code": "FISCAL_CALENDAR_INCONSISTENT", "module": "qualitative_normalization", "severity": "CRITICAL", "reason": "A fiscal year/quarter identity maps to multiple period-end dates.", "details": conflicts, "required_action": "Rebuild normalization with filing-header and issuer-calendar precedence."})
    # Temporal chronology is explicit.  A same-quarter YoY or non-contiguous
    # fallback is valid when no adjacent quarter exists; only a backwards,
    # zero-gap, or mixed-basis comparison is an integrity blocker.
    def _quarter_coord(label: Any) -> int | None:
        match = re.search(r"Q([1-4])\s*(?:FY\s*)?(20\d{2}|19\d{2})", str(label or ""), re.I)
        if not match:
            return None
        return int(match.group(2)) * 4 + int(match.group(1))
    sequence_errors = []
    non_contiguous = []
    for row in temporal_rows or []:
        left, right = _quarter_coord(row.get("from_period")), _quarter_coord(row.get("to_period"))
        relation = str(row.get("sequence_relation") or "").upper()
        if relation in {"NON_CONTIGUOUS", "ANNUAL_TO_QUARTER"}:
            non_contiguous.append({"from_period": row.get("from_period"), "to_period": row.get("to_period"), "sequence_relation": relation, "gap_status": row.get("gap_status")})
            continue
        # Same-quarter year-over-year is an intentional, comparable fallback
        # when an immediately preceding issuer quarter is not yet available.
        # It is not a discontinuity and therefore must not produce the
        # non-contiguous warning or a quality-gate penalty.
        if relation == "SAME_QUARTER_YOY":
            continue
        if left is not None and right is not None and right - left != 1:
            sequence_errors.append({"from_period": row.get("from_period"), "to_period": row.get("to_period"), "change_id": row.get("change_id")})
    if sequence_errors:
        diagnostics.append({"code": "TEMPORAL_SEQUENCE_INVALID", "module": "temporal_comparison", "severity": "CRITICAL", "reason": "Temporal comparison skipped an issuer quarter or used a non-adjacent quarter sequence.", "details": sequence_errors, "required_action": "Select adjacent issuer reporting quarters before computing changes."})
    if non_contiguous:
        diagnostics.append({"code": "TEMPORAL_COMPARISON_NON_CONTIGUOUS", "module": "temporal_comparison", "severity": "WARNING", "reason": "The latest comparison uses an explicitly labelled same-quarter YoY or non-contiguous fallback because no adjacent issuer quarter was available.", "details": non_contiguous})
    # Current-period validity is independent of comparison continuity.  The
    # state is valid when it matches the latest PIT-eligible actual period by
    # period end (or canonical label), even if comparison used same-quarter
    # YoY/non-contiguous fallback.
    if latest_actual and chosen:
        latest_end = _period_end(latest_actual)
        state_end = _period_end(chosen)
        if latest_end and state_end and latest_end != state_end:
            diagnostics.append({"code": "CURRENT_PERIOD_SELECTION_INVALID", "module": "state_synthesis", "severity": "CRITICAL", "reason": f"State current period {chosen.get('period_label')} does not match the latest PIT-eligible actual period {latest_actual.get('period_label')}.", "details": {"state_period": chosen.get("period_label"), "latest_actual_period": latest_actual.get("period_label"), "latest_actual_end": latest_end, "state_end": state_end}, "required_action": "Rebuild state synthesis from the latest issuer reporting period."})
        elif not latest_end and latest_actual.get("period_label") and chosen.get("period_label") != latest_actual.get("period_label"):
            diagnostics.append({"code": "CURRENT_PERIOD_SELECTION_INVALID", "module": "state_synthesis", "severity": "CRITICAL", "reason": f"State current period {chosen.get('period_label')} does not match the latest PIT-eligible actual period {latest_actual.get('period_label')}.", "details": {"state_period": chosen.get("period_label"), "latest_actual_period": latest_actual.get("period_label")}, "required_action": "Rebuild state synthesis from the latest issuer reporting period."})
    return diagnostics


def _point_in_time_audit(*, documents: list[dict[str, Any]], claims: list[dict[str, Any]], all_documents: list[dict[str, Any]], source_rows: list[dict[str, Any]], all_source_rows: list[dict[str, Any]], cutoff: str, all_claims: list[dict[str, Any]] | None = None, temporal_rows: list[dict[str, Any]] | None = None, all_temporal_rows: list[dict[str, Any]] | None = None, state_rows: list[dict[str, Any]] | None = None, all_state_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    future_documents = [row for row in all_documents if not _available(row, cutoff)]
    future_claims_used = [row for row in claims if not _available(row, cutoff)]
    future_claims_cache = [row for row in (all_claims or claims) if not _available(row, cutoff)]
    # Claims are already cutoff-filtered before this audit.  Audit every other
    # source category independently so a future governance/insider row cannot
    # be hidden by a valid qualitative document.
    categories = {
        "management": [row for row in source_rows if str(row.get("record_type") or row.get("source_type") or "").lower().startswith("management")],
        "insider": [row for row in source_rows if any(key in row for key in ("transaction_id", "filing_accession", "is_open_market"))],
        "risks": [row for row in source_rows if "risk" in str(row.get("record_type") or row.get("source_type") or "").lower()],
        "catalysts": [row for row in source_rows if "catalyst" in str(row.get("record_type") or row.get("source_type") or "").lower()],
        "governance": [row for row in source_rows if any(token in str(row.get("record_type") or row.get("source_type") or "").lower() for token in ("governance", "board", "committee", "shareholder"))],
    }
    all_categories = {
        "management": [row for row in all_source_rows if str(row.get("record_type") or row.get("source_type") or "").lower().startswith("management")],
        "insider": [row for row in all_source_rows if any(key in row for key in ("transaction_id", "filing_accession", "is_open_market"))],
        "risks": [row for row in all_source_rows if "risk" in str(row.get("record_type") or row.get("source_type") or "").lower()],
        "catalysts": [row for row in all_source_rows if "catalyst" in str(row.get("record_type") or row.get("source_type") or "").lower()],
        "governance": [row for row in all_source_rows if any(token in str(row.get("record_type") or row.get("source_type") or "").lower() for token in ("governance", "board", "committee", "shareholder"))],
    }
    future_used = {name: sum(1 for row in rows if not _available(row, cutoff)) for name, rows in categories.items()}
    future_cache = {name: sum(1 for row in rows if not _available(row, cutoff)) for name, rows in all_categories.items()}
    temporal = list(temporal_rows or [])
    state = list(state_rows or [])
    future_temporal = sum(1 for row in temporal if not _available(row, cutoff))
    future_state = sum(1 for row in state if not _available(row, cutoff))
    future_temporal_cache = sum(1 for row in (all_temporal_rows or temporal) if not _available(row, cutoff))
    future_state_cache = sum(1 for row in (all_state_rows or state) if not _available(row, cutoff))
    # A row in the filtered working set is by definition eligible.  Keep the
    # explicit counters in the artifact for review and future callers.
    return {
        "future_cache_documents": len(future_documents),
        "future_sources_used": len(future_claims_used) + sum(future_used.values()),
        "future_documents_used": 0,
        "future_claims_used": len(future_claims_used),
        "future_claims_cache": len(future_claims_cache),
        "future_temporal_used": future_temporal,
        "future_state_evidence_used": future_state,
        "future_temporal_cache": future_temporal_cache,
        "future_state_cache": future_state_cache,
        "future_management_used": future_used["management"],
        "future_insider_used": future_used["insider"],
        "future_risks_used": future_used["risks"],
        "future_catalysts_used": future_used["catalysts"],
        "future_governance_used": sum(1 for row in categories["governance"] if not _available(row, cutoff)),
        "future_management_cache": future_cache["management"],
        "future_insider_cache": future_cache["insider"],
        "future_risks_cache": future_cache["risks"],
        "future_catalysts_cache": future_cache["catalysts"],
        "future_governance_cache": future_cache["governance"],
        "status": "PASS" if not future_claims_used and not any(future_used.values()) and not future_temporal and not future_state else "FAIL",
    }


def _analytical_provenance(artifacts_root: str | Path, ticker: str) -> dict[str, Any]:
    """Expose the exact upstream generations embedded in a v2 dossier."""
    root = Path(artifacts_root)
    store = ArtifactCompatibilityStore(root)
    stages = ("qualitative_extraction", "temporal_comparison", "state_synthesis",
              "management_intelligence", "management_track_record",
              "governance_intelligence", "insider_intelligence", "scoring")
    result: dict[str, Any] = {}
    for stage in stages:
        row = store.get(stage, ticker) or {}
        result[stage] = {
            "producer_version": row.get("producer_version"),
            "schema_version": row.get("schema_version"),
            "implementation_fingerprint": row.get("implementation_fingerprint"),
            "configuration_fingerprint": row.get("configuration_fingerprint"),
            "dependency_fingerprints": dict(row.get("dependency_fingerprints") or {}),
            "input_fingerprint": row.get("input_fingerprint"),
            "output_fingerprint": row.get("output_fingerprint") or canonical_artifact_fingerprint(stage, ticker, root),
        }
    return result


def assemble_institutional_dossier(
    ticker: str,
    as_of_date: str,
    profile: str = "core_v1",
    artifacts_root: str = "artifacts",
) -> InstitutionalDossier:
    base = assemble_dossier(ticker, as_of_date, profile, artifacts_root)
    scoring = dict(base.scoring or {})
    scoring.setdefault("calculated_score", scoring.get("overall_score"))
    scoring["score_eligible"] = bool(scoring.get("score_eligible") or scoring.get("status") in {"SCORED", "RELEASED"})
    scoring["released_score"] = scoring.get("calculated_score") if scoring["score_eligible"] else None
    scoring.setdefault("eligibility_reason", "eligible" if scoring["score_eligible"] else "coverage or critical-factor requirements not met")
    claims = int(base.metadata.get("claims", 0))
    documents = int(base.metadata.get("documents", 0))
    # Build additive, generic mechanism records from the same frozen claims.
    # This keeps v1 narrative semantics intact while giving v2 consumers
    # structured business/risk/catalyst objects with explicit evidence IDs.
    claims_path = Path(artifacts_root) / "qualitative_analysis" / "claims.jsonl"
    documents_path = Path(artifacts_root) / "qualitative_analysis" / "documents.jsonl"
    document_rows: list[dict[str, Any]] = []
    if documents_path.exists():
        for line in documents_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("ticker", "")).upper() == base.ticker:
                document_rows.append(row)
    all_document_rows = list(document_rows)
    document_rows = [row for row in document_rows if _available(row, as_of_date)]
    # The qualitative directory contains several JSONL stores (claims,
    # temporal changes, documents, and states).  The freshness audit must use
    # state records only; claim rows also carry period labels and can otherwise
    # masquerade as a stale state from an original fiscal alias.
    state_rows = [
        row
        for row in _jsonl_rows(Path(artifacts_root) / "qualitative_analysis", base.ticker)
        if row.get("state_id") and row.get("current_state") is not None
    ]
    all_temporal_rows = [
        row for row in _jsonl_rows(Path(artifacts_root) / "qualitative_analysis", base.ticker)
        if row.get("change_id") and row.get("from_period") and row.get("to_period")
    ]
    temporal_rows = [row for row in all_temporal_rows if _available(row, as_of_date)]
    claim_rows: list[dict[str, Any]] = []
    all_claim_rows: list[dict[str, Any]] = []
    if claims_path.exists():
        for line in claims_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            availability = str(row.get("available_date") or row.get("publication_date") or row.get("filing_date") or "")[:10]
            if str(row.get("ticker", "")).upper() == base.ticker:
                all_claim_rows.append(row)
                if not availability or availability <= str(as_of_date)[:10]:
                    claim_rows.append(row)
    # Governance and management stores are derived inputs too.  Include them
    # in the release gate so a legacy raw-PDF row cannot be hidden by a valid
    # normalized document in the qualitative store.
    source_rows_all = _jsonl_rows(Path(artifacts_root) / "management_intelligence", base.ticker)
    source_rows = [row for row in source_rows_all if _available(row, as_of_date)]
    all_state_rows = list(state_rows)
    state_rows = [row for row in state_rows if _available(row, as_of_date)]
    point_in_time = _point_in_time_audit(
        documents=document_rows,
        claims=claim_rows,
        all_documents=all_document_rows,
        source_rows=source_rows,
        all_source_rows=source_rows_all,
        cutoff=as_of_date,
        all_claims=all_claim_rows,
        temporal_rows=temporal_rows,
        all_temporal_rows=all_temporal_rows,
        state_rows=state_rows,
        all_state_rows=all_state_rows,
    )
    coverage_diagnostics = _coverage_diagnostics(documents=document_rows, claims=claim_rows, base=base, state_rows=state_rows, temporal_rows=temporal_rows, as_of_date=as_of_date)
    management_view = dict(base.management or {})
    governance_view = dict(base.governance or {})
    management_view["status"] = "EXTRACTION_FAILED" if any(item.get("code") == "MANAGEMENT_SOURCES_AVAILABLE_BUT_ROSTER_EMPTY" for item in coverage_diagnostics) else ("AVAILABLE" if int(management_view.get("people_count") or 0) else "SOURCE_UNAVAILABLE")
    governance_view["status"] = "EXTRACTION_FAILED" if any(item.get("code") in {"AUTHORITATIVE_PROXY_AVAILABLE_BUT_NOT_ROUTED", "CRITICAL_GOVERNANCE_EXTRACTION_INCOMPLETE"} for item in coverage_diagnostics) else (str(governance_view.get("status") or "AVAILABLE") if governance_view.get("available") else "SOURCE_UNAVAILABLE")
    committee_records = [row for row in source_rows if Path(str(row.get("_source_file") or "")).name == "governance_committees.jsonl"]
    if committee_records:
        governance_view["committees"] = [{
            "name": row.get("committee_name"), "committee_type": row.get("committee_type"),
            "members": list(row.get("members") or []), "member_count": len(row.get("members") or []),
            "chair_person_id": row.get("chair_person_id"), "financial_experts": len(row.get("financial_expert_person_ids") or []),
            "member_independence": row.get("member_independence") or {},
            "source_status": row.get("source_status") or "UNKNOWN",
            "membership_status": row.get("membership_status") or "UNKNOWN",
            "source_accession": row.get("source_accession"), "source_date": row.get("source_date"),
            "source_filed_at": row.get("source_filed_at"), "source_period": row.get("source_period"),
            "as_of_eligibility": row.get("as_of_eligibility") or "ELIGIBLE",
            "source_url": row.get("source_url"), "local_source_path": row.get("local_source_path"),
            "normalized_document_id": row.get("normalized_document_id"),
            "source_record_ids": [str(row.get("committee_id") or "")],
        } for row in committee_records]
        statuses = {str(row.get("membership_status") or "UNKNOWN").upper() for row in committee_records}
        if "EXTRACTION_FAILED" in statuses:
            governance_view["status"] = "EXTRACTION_FAILED"
        elif "SOURCE_COVERAGE_INCOMPLETE" in statuses:
            governance_view["status"] = "AVAILABLE_WITH_LIMITATIONS"
    coverage_rows = [row for row in source_rows if Path(str(row.get("_source_file") or "")).name == "governance_coverage.jsonl"]
    if governance_view.get("status") == "AVAILABLE" and coverage_rows:
        coverage_values = {str(row.get(field) or "").upper() for row in coverage_rows for field in ("expertise", "outside_boards", "related_parties", "shareholder_rights", "voting_structure")}
        if coverage_values & {"PARTIAL", "INSUFFICIENT", "UNKNOWN"}:
            governance_view["status"] = "AVAILABLE_WITH_LIMITATIONS"
    management_view["diagnostics"] = [item for item in coverage_diagnostics if item.get("module") == "management_intelligence"]
    governance_view["diagnostics"] = [item for item in coverage_diagnostics if item.get("module") == "governance_intelligence"]
    quality = quality_gate_from_records(
        documents=document_rows,
        claims=claim_rows,
        additional_records=source_rows,
        cutoff=as_of_date,
        score_eligible=bool(scoring.get("score_eligible") or scoring.get("overall_score") is not None),
        proxy_available=bool(base.governance.get("available")),
        people_count=base.management.get("people_count"),
        board_count=base.governance.get("board_size") if base.governance.get("available") else None,
        diagnostics=coverage_diagnostics,
    )
    # A failed/staging governance generation is a current-run blocker even if
    # last-known-good JSONL rows remain on disk for recovery.
    generation_path = Path(artifacts_root) / "management_intelligence" / "governance_generation.json"
    governance_generation_status = None
    if generation_path.exists():
        try:
            generation_payload = json.loads(generation_path.read_text(encoding="utf-8"))
            governance_generation_status = str(((generation_payload.get("tickers") or {}).get(base.ticker) or {}).get("status") or "").upper()
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            governance_generation_status = "FAILED"
    if governance_generation_status in {"FAILED", "STAGING"}:
        governance_view["status"] = "FAILED_CURRENT_RUN"
        governance_view["available"] = False
        governance_view["current_run_valid"] = False
        quality.status = "NOT_RESEARCH_READY"
        quality.blockers.append({"code": "CURRENT_GOVERNANCE_REBUILD_FAILED", "module": "governance_intelligence", "severity": "CRITICAL", "reason": "The current governance generation did not commit successfully; last-known-good rows are withheld from this dossier.", "required_action": "Rebuild governance successfully before release."})
    business_assessments = [item.to_dict() for item in BusinessModelEngine().analyze(claim_rows)]
    raw_risks = [item.to_dict() for item in extract_risks(claim_rows)]
    raw_catalysts = [item.to_dict() for item in extract_catalysts(claim_rows)]
    risk_records = canonicalize_risks(raw_risks, as_of_date)
    risk_diagnostics = risk_coverage_diagnostics(claim_rows, [item for item in extract_risks(claim_rows)], risk_records)
    catalyst_records = canonicalize_catalysts(raw_catalysts, as_of_date)
    competitive_advantage = build_competitive_advantage(claim_rows)
    industry_structure = build_industry_structure(claim_rows)
    commitment_rows = [row for row in source_rows if str(row.get("record_type") or row.get("source_type") or "").lower() in {"strategic_commitment", "operating_commitment"} or "commitment" in str(row.get("_source_file") or "").lower()]
    operating_execution = build_operating_execution(claim_rows, commitment_rows, as_of_date)
    guidance_rows = [row for row in source_rows if "guidance" in str(row.get("_source_file") or "").lower() or row.get("guidance_id") or row.get("fiscal_horizon")]
    outcome_rows = [row for row in source_rows if "outcome" in str(row.get("_source_file") or "").lower() or row.get("outcome_id") or row.get("guidance_outcome_id")]
    guidance_reliability = build_guidance_reliability(claim_rows, {str(row.get("document_id")): row for row in document_rows}, guidance_rows, outcome_rows, as_of_date)
    business_quality = build_business_quality(claim_rows)
    demand_assessment = business_quality
    scoring["business_quality_coverage"] = business_quality["coverage"]
    scoring["management_execution_coverage"] = {"guidance": guidance_reliability["coverage"], "operating_execution": operating_execution["coverage"]}
    scoring["v2_engine_coverage"] = {"competitive_advantage": competitive_advantage["status"], "industry_structure": industry_structure["status"], "operating_execution": operating_execution["status"], "guidance_reliability": guidance_reliability["status"]}
    scoring["v2_factor_inputs"] = {
        "BUSINESS_QUALITY": business_quality["factors"],
        "BUSINESS_TRAJECTORY": base.business_trajectory,
        "MANAGEMENT_EXECUTION": {"guidance_reliability": guidance_reliability, "operating_execution": operating_execution},
        "GOVERNANCE": governance_view,
        "INSIDER_ALIGNMENT": base.insiders,
    }
    scoring["v2_quality_layer"] = {"wired": True, "business_quality_status": business_quality["status"], "factor_count": len(business_quality["factors"])}
    if point_in_time["status"] == "FAIL":
        quality.status = "NOT_RESEARCH_READY"
        if "POINT_IN_TIME_LEAKAGE" not in quality.blockers:
            quality.blockers.append({"code": "POINT_IN_TIME_LEAKAGE", "module": "point_in_time", "severity": "CRITICAL", "reason": "Future-dated records were present in the working source set.", "required_action": "Exclude future records before releasing the dossier."})
    issuer_identity = dict(getattr(base, "issuer_identity", {}) or base.metadata.get("issuer_identity") or {})
    issuer_identity.setdefault("requested_ticker", base.ticker)
    issuer_identity.setdefault("resolved_ticker", base.ticker)
    issuer_identity.setdefault("legal_name", base.company_name)
    issuer_identity.setdefault("issuer_cik", getattr(base, "issuer_cik", None))
    issuer_identity.setdefault("normalized_legal_name", re.sub(r"[^a-z0-9]+", " ", str(issuer_identity.get("legal_name") or "").casefold()).strip())
    issuer_identity.setdefault("identity_version", issuer_identity.get("lineage_version") or "issuer-lineage-v1")
    issuer_identity.setdefault("identity_confidence", "HIGH" if issuer_identity.get("issuer_cik") else "MEDIUM")
    issuer_identity.setdefault("identity_hash", canonical_identity_hash(ticker=base.ticker, exchange=issuer_identity.get("exchange"), cik=issuer_identity.get("issuer_cik"), legal_name=issuer_identity.get("legal_name"), lineage_version=issuer_identity.get("lineage_version") or issuer_identity.get("identity_version") or "issuer-lineage-v1"))
    expected_identity_hash = canonical_identity_hash(ticker=base.ticker, exchange=issuer_identity.get("exchange"), cik=(getattr(base, "issuer_cik", None) or issuer_identity.get("issuer_cik")), legal_name=issuer_identity.get("legal_name"), lineage_version=issuer_identity.get("lineage_version") or issuer_identity.get("identity_version") or "issuer-lineage-v1")
    identity_mismatch = (
        str(issuer_identity.get("resolved_ticker") or base.ticker).upper() != base.ticker
        or str(issuer_identity.get("legal_name") or "").strip().casefold() != str(base.company_name or "").strip().casefold()
        or (getattr(base, "issuer_cik", None) and issuer_identity.get("issuer_cik") and str(getattr(base, "issuer_cik")) != str(issuer_identity.get("issuer_cik")))
        or (issuer_identity.get("identity_hash") and str(issuer_identity.get("identity_hash")) != expected_identity_hash)
    )
    if identity_mismatch and "ISSUER_IDENTITY_MISMATCH" not in quality.blockers:
        quality.status = "NOT_RESEARCH_READY"
        quality.blockers.append({"code": "ISSUER_IDENTITY_MISMATCH", "module": "issuer_identity", "severity": "CRITICAL", "reason": "Dossier identity fields are inconsistent with the requested ticker.", "required_action": "Rebuild from the ticker-scoped authoritative issuer identity."})
    scoring["issuer_identity_valid"] = not identity_mismatch
    scoring["issuer_identity_hash"] = issuer_identity.get("identity_hash")
    scoring["decision_grade"] = quality.status == "RESEARCH_READY"
    scoring["released_score"] = scoring.get("calculated_score") if scoring["decision_grade"] else None
    if not scoring["decision_grade"]:
        scoring["score_status"] = "WITHHELD_BY_QUALITY_GATE"
    handoff = AnalystHandoff(
        key_positive_findings=[item.to_dict() for item in base.key_positives],
        key_negative_findings=[item.to_dict() for item in base.key_negatives],
        fundamental_inflections=[_record_dict(item) for item in base.business_trajectory.get("facts", []) if _record_dict(item).get("direction") in {"improving", "deteriorating", "mixed"}],
        management_findings=list(management_view.get("leaders", [])),
        guidance_findings=[guidance_reliability],
        risk_watchlist=materiality_rank(risk_records),
        catalyst_watchlist=materiality_rank(catalyst_records),
        governance_findings=[governance_view] if governance_view else [],
        insider_findings=[base.insiders] if base.insiders else [],
        unresolved_questions=list(base.coverage_gaps),
        coverage_gaps=list(base.coverage_gaps),
        confidence_constraints=["Analytical confidence is not a calibrated probability."],
    )
    fingerprint = hashlib.sha256(json.dumps(base.to_dict(include_evidence=True), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    return InstitutionalDossier(
        dossier_id=f"dossier-v2:{base.ticker}:{base.as_of_date}:{fingerprint[:16]}",
        ticker=base.ticker,
        company_name=base.company_name,
        as_of_date=base.as_of_date,
        source_cutoff=base.source_cutoff,
        scoring_profile_version=base.profile_version,
        extraction_version="qualitative-extraction-v8",
        normalization_version="qualitative-normalization-v2",
        governance_version="governance-pipeline-v2",
        management_version="management-intelligence-v2",
        management_track_record_version="management-track-record-v2",
        insider_version="insider-intelligence-v2",
        temporal_version="temporal-comparison-v2",
        claim_schema_version="claim-schema-v2",
        risk_version="risk-catalyst-v2",
        catalyst_version="risk-catalyst-v2",
        executive_assessment={"positives": handoff.key_positive_findings, "negatives": handoff.key_negative_findings, "coverage_quality": quality.to_dict()},
        business_model={"mechanisms": business_assessments, "legacy_summary": base.business_quality},
        competitive_advantage=competitive_advantage,
        industry_structure=industry_structure,
        demand_assessment=demand_assessment,
        business_trajectory=base.business_trajectory,
        operating_execution=operating_execution,
        management=management_view,
        guidance_reliability=guidance_reliability,
        capital_allocation={"events": management_view.get("capital_allocation_events", 0)},
        governance=governance_view,
        insider_behaviour=base.insiders,
        risks={"records": risk_records, "raw_candidate_count": len(raw_risks), "canonical_count": len(risk_records), "risk_diagnostics": risk_diagnostics, "legacy_summary": base.risks},
        catalysts={"records": catalyst_records, "raw_candidate_count": len(raw_catalysts), "canonical_count": len(catalyst_records)},
        change_analysis={"changes": base.metadata.get("temporal_changes", 0)},
        evidence_audit={"source_index": [asdict(ref) for ref in base.source_index], "coverage_gaps": base.coverage_gaps, "point_in_time_audit": point_in_time},
        scoring=scoring,
        analyst_handoff=handoff,
        quality_gate=quality,
        release_status=quality.status,
        decision_grade=quality.status == "RESEARCH_READY",
        input_fingerprint=fingerprint,
        generated_at=datetime.now(timezone.utc).isoformat(),
        metadata={
            "v1_dossier_id": base.dossier_id,
            "migration": "additive_adapter",
            "documents": len(document_rows),
            "claims": len(claim_rows),
            "governance_records": len(source_rows),
            "raw_governance_records": len(source_rows_all),
            "point_in_time_audit": point_in_time,
            "risk_candidates": len(raw_risks),
            "canonical_risks": len(risk_records),
            "risk_diagnostics": risk_diagnostics,
            "catalyst_candidates": len(raw_catalysts),
            "canonical_catalysts": len(catalyst_records),
            "source_cutoff": as_of_date,
            "coverage_diagnostics": coverage_diagnostics,
            "issuer_identity": issuer_identity,
        },
        analytical_provenance=_analytical_provenance(artifacts_root, base.ticker),
        issuer_cik=issuer_identity.get("issuer_cik"),
        issuer_identity=issuer_identity,
    )


def _markdown_dossier(dossier: InstitutionalDossier) -> str:
    """Render a reviewable human report from the same structured dossier."""
    q = dossier.quality_gate
    lines = [f"# {dossier.company_name} ({dossier.ticker})", "", f"As of: **{dossier.as_of_date}**", f"Release status: **{dossier.release_status}**", "", "## Quality gate", "", f"Status: **{q.status}**"]
    if q.blockers:
        lines.extend(["", "### Blockers", ""] + [f"- **{item.get('code', item)}**: {item.get('reason', '')}" if isinstance(item, dict) else f"- {item}" for item in q.blockers])
    if q.warnings:
        lines.extend(["", "### Warnings", ""] + [f"- {item}" for item in q.warnings])
    lines.extend(["", "## Business quality", "", f"Status: **{dossier.competitive_advantage.get('status', 'NOT_ESTABLISHED')}**; audited factors: **{len((dossier.scoring.get('v2_factor_inputs') or {}).get('BUSINESS_QUALITY') or [])}**"])
    lines.extend(["", "## State and management", "", f"Management people: **{dossier.management.get('people_count', 0)}**", f"Governance: **{'available' if dossier.governance.get('available') else 'not established'}**", f"Guidance coverage: **{dossier.guidance_reliability.get('status', 'UNKNOWN')}**"])
    lines.extend(["", "## Risks and catalysts", "", f"Canonical risks: **{len(dossier.risks.get('records') or [])}**", f"Canonical catalysts: **{len(dossier.catalysts.get('records') or [])}**"])
    audit = dossier.evidence_audit.get("point_in_time_audit") or {}
    lines.extend(["", "## Point-in-time audit", "", f"Status: **{audit.get('status', 'UNKNOWN')}**", f"Future records used: **{audit.get('future_sources_used', 0)}**", "", "## Scoring", "", f"Score status: **{dossier.scoring.get('score_status', dossier.scoring.get('status', 'UNKNOWN'))}**", f"Released score: **{dossier.scoring.get('released_score') if dossier.scoring.get('released_score') is not None else 'WITHHELD'}**", ""])
    return "\n".join(lines)


def write_institutional_dossier(dossier: InstitutionalDossier, root: str = "artifacts/research_output", *, force: bool = False) -> dict[str, Any]:
    """Write v2 JSON and its reviewable Markdown companion."""
    output_root = Path(root) / dossier.ticker
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"{dossier.ticker}_{dossier.as_of_date}_research-output-v2.json"
    markdown_path = output_root / f"{dossier.ticker}_{dossier.as_of_date}_research-output-v2.md"
    content = json.dumps(dossier.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    markdown = _markdown_dossier(dossier)
    unchanged = path.exists() and path.read_text(encoding="utf-8") == content
    if force or not unchanged:
        path.write_text(content, encoding="utf-8")
    markdown_unchanged = markdown_path.exists() and markdown_path.read_text(encoding="utf-8") == markdown
    if force or not markdown_unchanged:
        markdown_path.write_text(markdown, encoding="utf-8")
    registry = Path(root) / "dossiers-v2.jsonl"
    rows: list[dict[str, Any]] = []
    if registry.exists():
        for line in registry.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    entry = {"dossier_id": dossier.dossier_id, "ticker": dossier.ticker, "as_of_date": dossier.as_of_date, "input_fingerprint": dossier.input_fingerprint, "json_path": str(path), "markdown_path": str(markdown_path), "research_output_version": dossier.research_output_version}
    rows = [row for row in rows if row.get("dossier_id") != dossier.dossier_id] + [entry]
    rows.sort(key=lambda row: (str(row.get("ticker", "")), str(row.get("as_of_date", "")), str(row.get("dossier_id", ""))))
    registry.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return {"json": str(path), "markdown": str(markdown_path), "registry": str(registry), "unchanged": unchanged and markdown_unchanged, "registry_entries": len(rows)}


__all__ = ["assemble_institutional_dossier", "write_institutional_dossier"]
