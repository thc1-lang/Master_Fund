"""Versioned institutional dossier schema and release gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from qualitative_analysis.status import SourceStatus
from qualitative_analysis.extraction_safety import looks_like_pdf_corruption
from qualitative_analysis.point_in_time import point_in_time_status


RESEARCH_OUTPUT_VERSION = "research-output-v2"
CORE_VERSION = "core-v2.0"


@dataclass
class QualityGateResult:
    status: str
    blockers: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    passed_checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "blockers": [dict(item) if isinstance(item, Mapping) else str(item) for item in self.blockers],
            "warnings": list(self.warnings),
            "passed_checks": list(self.passed_checks),
            "checked": list(self.checked),
        }


class BlockerList(list):
    """List of structured blockers that remains compatible with code checking codes."""

    def __contains__(self, value: object) -> bool:
        if super().__contains__(value):
            return True
        return any(isinstance(item, Mapping) and item.get("code") == value for item in self)


def _blocker(code: str, module: str, reason: str, *, severity: str = "CRITICAL", required_action: str = "") -> dict[str, str]:
    return {"code": code, "module": module, "severity": severity, "reason": reason, "required_action": required_action}


@dataclass
class AnalystHandoff:
    key_positive_findings: list[dict[str, Any]] = field(default_factory=list)
    key_negative_findings: list[dict[str, Any]] = field(default_factory=list)
    fundamental_inflections: list[dict[str, Any]] = field(default_factory=list)
    management_findings: list[dict[str, Any]] = field(default_factory=list)
    guidance_findings: list[dict[str, Any]] = field(default_factory=list)
    risk_watchlist: list[dict[str, Any]] = field(default_factory=list)
    catalyst_watchlist: list[dict[str, Any]] = field(default_factory=list)
    governance_findings: list[dict[str, Any]] = field(default_factory=list)
    insider_findings: list[dict[str, Any]] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    confidence_constraints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InstitutionalDossier:
    dossier_id: str
    ticker: str
    company_name: str
    as_of_date: str
    source_cutoff: str
    schema_version: str = CORE_VERSION
    research_output_version: str = RESEARCH_OUTPUT_VERSION
    scoring_profile_version: str = ""
    extraction_version: str = ""
    normalization_version: str = ""
    industry_adapter_version: str = "generic-v1"
    governance_version: str = "governance-pipeline-v2"
    management_version: str = "management-intelligence-v2"
    management_track_record_version: str = "management-track-record-v2"
    insider_version: str = "insider-intelligence-v2"
    temporal_version: str = "temporal-comparison-v2"
    claim_schema_version: str = "claim-schema-v2"
    risk_version: str = "risk-catalyst-v2"
    catalyst_version: str = "risk-catalyst-v2"
    executive_assessment: dict[str, Any] = field(default_factory=dict)
    business_model: dict[str, Any] = field(default_factory=dict)
    competitive_advantage: dict[str, Any] = field(default_factory=dict)
    industry_structure: dict[str, Any] = field(default_factory=dict)
    demand_assessment: dict[str, Any] = field(default_factory=dict)
    business_trajectory: dict[str, Any] = field(default_factory=dict)
    operating_execution: dict[str, Any] = field(default_factory=dict)
    management: dict[str, Any] = field(default_factory=dict)
    guidance_reliability: dict[str, Any] = field(default_factory=dict)
    capital_allocation: dict[str, Any] = field(default_factory=dict)
    governance: dict[str, Any] = field(default_factory=dict)
    insider_behaviour: dict[str, Any] = field(default_factory=dict)
    risks: dict[str, Any] = field(default_factory=dict)
    catalysts: dict[str, Any] = field(default_factory=dict)
    change_analysis: dict[str, Any] = field(default_factory=dict)
    evidence_audit: dict[str, Any] = field(default_factory=dict)
    scoring: dict[str, Any] = field(default_factory=dict)
    analyst_handoff: AnalystHandoff = field(default_factory=AnalystHandoff)
    quality_gate: QualityGateResult = field(default_factory=lambda: QualityGateResult("NOT_RESEARCH_READY"))
    release_status: str = "NOT_RESEARCH_READY"
    decision_grade: bool = False
    input_fingerprint: str = ""
    generated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # Exact analytical generations used to build this dossier.  A dossier is
    # only reusable when these fingerprints match its current dependencies.
    analytical_provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["analyst_handoff"] = self.analyst_handoff.to_dict()
        value["quality_gate"] = self.quality_gate.to_dict()
        return value


def quality_gate(*, documents: int, material_claims: int, evidence_failures: int = 0, raw_pdf_contamination: int = 0, point_in_time_leaks: int = 0, critical_conflicts: int = 0, zero_roster_with_proxy: bool = False, score_eligible: bool = False, unavailable_evidence: bool = False, diagnostics: Iterable[Mapping[str, Any]] = ()) -> QualityGateResult:
    blockers: BlockerList = BlockerList()
    warnings: list[str] = []
    checked = ["schema_integrity", "evidence_provenance", "point_in_time", "source_conflicts", "coverage"]
    if raw_pdf_contamination:
        blockers.append(_blocker("RAW_PDF_EVIDENCE", "evidence_audit", "Raw PDF bytes or object streams were retained as evidence text.", required_action="Rebuild the affected normalized or governance records from extracted text."))
    if evidence_failures:
        blockers.append(_blocker("MATERIAL_EVIDENCE_UNVERIFIED", "evidence_audit", "Material records do not have verified source evidence.", required_action="Repair provenance and evidence validation before release."))
    if point_in_time_leaks:
        blockers.append(_blocker("POINT_IN_TIME_LEAKAGE", "point_in_time", "A record used by the dossier was first public after the analysis cutoff.", required_action="Exclude future records and rebuild the dossier at the requested cutoff."))
    if critical_conflicts:
        blockers.append(_blocker("CRITICAL_SOURCE_CONFLICT", "source_reconciliation", "Conflicting official source values remain unresolved.", required_action="Reconcile the conflicting records or mark the assessment unresolved."))
    if zero_roster_with_proxy:
        blockers.append(_blocker("MANAGEMENT_OR_BOARD_PARSE_FAILURE", "management_governance", "A current proxy exists but the expected management or board roster is empty.", required_action="Repair the management or governance parser and rerun the source catalog."))
    if documents == 0:
        warnings.append("NO_SOURCE_DOCUMENTS")
    if material_claims == 0 and documents:
        warnings.append("DOCUMENTS_PRESENT_BUT_EXTRACTION_COVERAGE_INSUFFICIENT")
    if unavailable_evidence:
        warnings.append("SOURCE_COVERAGE_UNAVAILABLE")
    for diagnostic in diagnostics:
        code = str(diagnostic.get("code") or diagnostic.get("diagnostic") or "").strip()
        if not code:
            continue
        severity = str(diagnostic.get("severity") or "WARNING").upper()
        if severity == "CRITICAL":
            blockers.append(_blocker(code, str(diagnostic.get("module") or "quality_gate"), str(diagnostic.get("reason") or code), required_action=str(diagnostic.get("required_action") or "Repair the affected source or parser before release.")))
        else:
            warnings.append(code)
    if blockers:
        status = "NOT_RESEARCH_READY"
    elif not score_eligible or material_claims == 0:
        status = "RESEARCH_READY_WITH_LIMITATIONS"
    else:
        status = "RESEARCH_READY"
    passed = [item for item in checked if item not in {"point_in_time"} or not point_in_time_leaks]
    return QualityGateResult(status, blockers, warnings, checked, passed)


def quality_gate_from_records(*, documents: Iterable[Mapping[str, Any]] = (), claims: Iterable[Mapping[str, Any]] = (), additional_records: Iterable[Mapping[str, Any]] = (), cutoff: str | None = None, score_eligible: bool = False, proxy_available: bool = False, people_count: int | None = None, board_count: int | None = None, diagnostics: Iterable[Mapping[str, Any]] = ()) -> QualityGateResult:
    """Apply the release contract directly to records from a v2 build.

    Unknown timestamps produce a coverage warning; explicit future records are
    blockers.  This keeps missing coverage neutral while making look-ahead
    leakage release-blocking.
    """
    def mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else asdict(value)
    def typed(values: Iterable[Any], fallback: str) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        for value in values:
            row = dict(mapping(value))
            row.setdefault("_coverage_type", fallback)
            result.append(row)
        return result

    docs = typed(documents, "documents")
    material = typed(claims, "claims")
    additional = typed(additional_records, "additional")
    raw_pdf = sum(1 for row in [*docs, *material, *additional] if looks_like_pdf_corruption(str(row.get("text") or row.get("evidence_text") or row.get("exact_text") or "")))
    leaks = 0
    unknown = 0
    unknown_by_type: dict[str, int] = {}
    material_unknown = 0

    def record_type(row: Mapping[str, Any]) -> str:
        explicit = (row.get("record_type") or row.get("source_type") or row.get("dimension")
                    or row.get("document_type") or row.get("type"))
        if not explicit and row.get("_source_file"):
            explicit = Path(str(row["_source_file"])).stem
        if not explicit:
            explicit = row.get("_coverage_type")
        return str(explicit or "unknown").lower().replace(" ", "_")

    def material_released(row: Mapping[str, Any]) -> bool:
        materiality = str(row.get("materiality") or row.get("materiality_level") or "").upper()
        released = row.get("released") is True or row.get("release_status") in {"RELEASED", "PUBLIC", "CURRENT"} or row.get("released_score") is not None or row.get("decision_grade") is True
        return materiality in {"HIGH", "CRITICAL", "MATERIAL"} and released

    if cutoff:
        for row in [*docs, *material, *additional]:
            status = point_in_time_status(row, cutoff)
            if status == "FAIL":
                leaks += 1
            elif status == "UNKNOWN":
                unknown += 1
                bucket = record_type(row)
                unknown_by_type[bucket] = unknown_by_type.get(bucket, 0) + 1
                if material_released(row):
                    material_unknown += 1
    roster_failure = bool(proxy_available and ((people_count == 0) or (board_count == 0)))
    result = quality_gate(documents=len(docs), material_claims=len(material), raw_pdf_contamination=raw_pdf, point_in_time_leaks=leaks, zero_roster_with_proxy=roster_failure, score_eligible=score_eligible, unavailable_evidence=not docs, diagnostics=diagnostics)
    if unknown:
        result.warnings.append(f"{unknown}_RECORDS_HAVE_UNKNOWN_PUBLIC_AVAILABILITY")
        result.warnings.append("UNKNOWN_PUBLIC_AVAILABILITY_BY_TYPE:" + ",".join(f"{key}={unknown_by_type[key]}" for key in sorted(unknown_by_type)))
    if material_unknown:
        result.blockers.append(_blocker("MATERIAL_RELEASED_FINDING_PUBLIC_AVAILABILITY_UNKNOWN", "point_in_time", f"{material_unknown} material released finding(s) lack provable public availability.", required_action="Attach an authoritative SEC acceptance or official publication date, or withhold the finding."))
    return result


__all__ = ["RESEARCH_OUTPUT_VERSION", "CORE_VERSION", "QualityGateResult", "AnalystHandoff", "InstitutionalDossier", "quality_gate", "quality_gate_from_records"]
