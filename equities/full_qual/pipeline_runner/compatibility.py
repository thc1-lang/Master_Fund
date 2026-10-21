"""Semantic compatibility and quarantine for derived pipeline artifacts.

Compatibility is based on the implementation that produced an artifact, not
only on a hand-maintained version string. Source acquisition has its own
stable contract and excludes analytical implementation files, so analytical
changes never invalidate the source cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import math
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class StageContract:
    stage: str
    producer_version: str
    schema_version: str
    dependencies: tuple[str, ...] = ()


STAGE_CONTRACTS: dict[str, StageContract] = {
    "acquisition": StageContract("acquisition", "acquisition-v1", "acquisition-manifest-v1"),
    "normalization": StageContract("normalization", "qualitative-normalization-v2", "normalized-document-schema-v2", ("acquisition",)),
    "qualitative_extraction": StageContract("qualitative_extraction", "qualitative-extraction-v8", "claim-schema-v2", ("normalization",)),
    "temporal_comparison": StageContract("temporal_comparison", "temporal-comparison-v2", "temporal-schema-v2", ("qualitative_extraction",)),
    "state_synthesis": StageContract("state_synthesis", "qualitative-state-v2", "state-schema-v2", ("qualitative_extraction", "temporal_comparison")),
    "insider_intelligence": StageContract("insider_intelligence", "insider-intelligence-v2", "insider-schema-v2", ("acquisition",)),
    "management_intelligence": StageContract("management_intelligence", "management-intelligence-v2", "management-schema-v2", ("normalization",)),
    "management_track_record": StageContract("management_track_record", "management-track-record-v2", "track-record-schema-v2", ("management_intelligence", "qualitative_extraction")),
    "governance_intelligence": StageContract("governance_intelligence", "governance-pipeline-v2", "governance-schema-v2", ("normalization", "management_intelligence")),
    "scoring": StageContract("scoring", "qualitative-scoring-v2.0", "score-schema-v2", ("state_synthesis", "management_track_record", "governance_intelligence", "insider_intelligence")),
    # The dossier assembles the complete analytical handoff, so every
    # material upstream generation is part of its compatibility contract.
    # Keeping only scoring/governance here allowed a changed management
    # track, temporal, or state artifact to leave an older dossier reusable.
    "dossier_generation": StageContract(
        "dossier_generation", "research-output-v2", "research-output-v2",
        (
            "normalization", "qualitative_extraction", "temporal_comparison",
            "state_synthesis", "insider_intelligence", "management_intelligence",
            "management_track_record", "governance_intelligence", "scoring",
        ),
    ),
}


# Stage fingerprints cover the code that defines each stage. The compatibility
# implementation is part of every derived stage; acquisition intentionally
# excludes it and all analytical modules to keep source caches reusable.
_COMMON_ANALYTICAL = ("pipeline_runner/compatibility.py",)
STAGE_SOURCE_FILES: dict[str, tuple[str, ...]] = {
    "acquisition": (
        "qualitative_ir_downloader/main.py",
        "qualitative_ir_downloader/manifest.py",
        "qualitative_ir_downloader/models.py",
    ),
    "normalization": _COMMON_ANALYTICAL + ("qualitative_analysis/ingestion.py", "qualitative_analysis/normalization.py", "qualitative_analysis/models.py", "qualitative_analysis/fiscal_periods.py", "qualitative_analysis/point_in_time.py"),
    "qualitative_extraction": _COMMON_ANALYTICAL + ("qualitative_analysis/extraction_rules.py", "qualitative_analysis/qualitative_extraction.py", "qualitative_analysis/extraction_models.py", "qualitative_analysis/evidence.py", "qualitative_analysis/point_in_time.py", "qualitative_analysis/topic_normalization.py", "qualitative_analysis/fiscal_periods.py"),
    "temporal_comparison": _COMMON_ANALYTICAL + ("qualitative_analysis/main.py", "qualitative_analysis/temporal_comparison.py", "qualitative_analysis/temporal_models.py", "qualitative_analysis/temporal_rules.py", "qualitative_analysis/point_in_time.py"),
    "state_synthesis": _COMMON_ANALYTICAL + ("qualitative_analysis/state_synthesis.py", "qualitative_analysis/state_models.py", "qualitative_analysis/state_rules.py", "qualitative_analysis/state_store.py", "qualitative_analysis/state_confidence.py", "qualitative_analysis/point_in_time.py", "qualitative_analysis/fiscal_periods.py", "qualitative_analysis/topic_normalization.py"),
    "insider_intelligence": _COMMON_ANALYTICAL + ("insider_intelligence/main.py", "insider_intelligence/sec_sources.py", "insider_intelligence/models.py"),
    "management_intelligence": _COMMON_ANALYTICAL + ("management_intelligence/main.py", "management_intelligence/normalized_sources.py", "management_intelligence/sec_sources.py", "management_intelligence/models.py", "management_intelligence/store.py", "management_intelligence/organization_resolution.py", "management_intelligence/career_history.py"),
    "management_track_record": _COMMON_ANALYTICAL + ("management_intelligence/guidance_track_record.py", "management_intelligence/guidance_database.py", "management_intelligence/track_record_models.py", "management_intelligence/operating_outcomes.py", "management_intelligence/capital_allocation_engine.py", "qualitative_analysis/business_intelligence.py"),
    "governance_intelligence": _COMMON_ANALYTICAL + ("management_intelligence/governance_cli.py", "management_intelligence/governance_store.py", "management_intelligence/governance_sources.py", "management_intelligence/board_committees.py", "management_intelligence/board_governance_models.py", "management_intelligence/shareholder_rights.py", "management_intelligence/store.py", "management_intelligence/board_composition.py", "management_intelligence/career_history.py"),
    "scoring": _COMMON_ANALYTICAL + ("scoring_engine/engine.py", "scoring_engine/qualitative_scoring.py", "scoring_engine/aggregation.py", "scoring_engine/factor_registry.py", "scoring_engine/factor_rules.py", "scoring_engine/governance_scoring.py", "scoring_engine/management_scoring.py", "scoring_engine/insider_scoring.py", "scoring_engine/coverage.py", "scoring_engine/profiles.py", "scoring_engine/models.py", "config/scoring/core_v2.json", "config/scoring/factor_definitions.json"),
    "dossier_generation": _COMMON_ANALYTICAL + ("research_output/v2.py", "research_output/v2_models.py", "research_output/v2_engines.py", "research_output/assembler.py", "research_output/models.py", "research_output/main.py"),
}

# Configuration is deliberately separate from implementation source.  These
# values describe semantic switches, rather than runtime paths or generated
# output.  A caller can provide additional stage configuration to
# ``configuration_fingerprint``/``record`` when a profile has configurable
# semantics.
STAGE_CONFIGURATIONS: dict[str, dict[str, Any]] = {
    "temporal_comparison": {"period_semantics": "issuer-fiscal-period-v2"},
    "state_synthesis": {"current_period_selection": "point-in-time-v2"},
}


def _canonicalize(value: Any) -> Any:
    """Convert supported values to a JSON-safe, deterministic structure.

    Fingerprints must never use ``repr`` (which may contain memory addresses)
    or filesystem enumeration order.  Sets are sorted by their canonical JSON
    representation; mappings are sorted by key by ``canonical_json``.
    """
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        values = [_canonicalize(item) for item in value]
        return sorted(values, key=lambda item: canonical_json(item))
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    # Dataclasses and simple record objects are represented by public fields,
    # avoiding the process-specific repr used by object.__repr__.
    if hasattr(value, "__dataclass_fields__"):
        return _canonicalize(asdict(value))
    if hasattr(value, "__dict__"):
        return {str(key): _canonicalize(item) for key, item in vars(value).items() if not str(key).startswith("_")}
    text = str(value)
    text = re.sub(r"0x[0-9a-fA-F]+", "<address>", text)
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}", "value": text}


def canonical_json(value: Any) -> str:
    """Serialize a fingerprint value with fixed encoding and no whitespace."""
    return json.dumps(_canonicalize(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def implementation_fingerprint_inputs(stage: str, *, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the explicit, inspectable source inputs for ``stage``.

    The allowlist is intentional: no recursive directory walks, mtimes,
    generated artifacts, caches, logs, or runtime paths can enter this hash.
    """
    base = Path(root).resolve() if root else _repo_root()
    relative_paths = sorted({Path(relative).as_posix() for relative in STAGE_SOURCE_FILES.get(stage, _COMMON_ANALYTICAL)})
    inputs: list[dict[str, Any]] = []
    for relative in relative_paths:
        path = base / Path(relative)
        try:
            content = path.read_bytes() if path.is_file() else b"<MISSING>"
            status = "PRESENT" if path.is_file() else "MISSING"
        except OSError:
            content = b"<UNREADABLE>"
            status = "UNREADABLE"
        inputs.append({
            "path": relative,
            "type": "source",
            "status": status,
            "content_sha256": hashlib.sha256(content).hexdigest(),
        })
    return inputs


def implementation_fingerprint(stage: str, *, root: str | Path | None = None) -> str:
    """Return a deterministic fingerprint of stable stage implementation."""
    contract = STAGE_CONTRACTS.get(stage)
    payload = {
        "contract": {
            "stage": stage,
            "producer_version": contract.producer_version if contract else "UNKNOWN",
            "schema_version": contract.schema_version if contract else "UNKNOWN",
        },
        "source_inputs": implementation_fingerprint_inputs(stage, root=root),
    }
    return _hash_payload(payload)


def implementation_fingerprint_for(stage: str) -> str:
    return implementation_fingerprint(stage)


def configuration_fingerprint(stage: str, configuration: Mapping[str, Any] | None = None) -> str:
    """Fingerprint semantic configuration independently of implementation."""
    values = dict(STAGE_CONFIGURATIONS.get(stage, {})) if configuration is None else dict(configuration)
    return _hash_payload({"stage": stage, "configuration": values})


@dataclass
class CompatibilityDecision:
    stage: str
    status: str
    action: str
    required_producer_version: str
    required_schema_version: str
    stored_producer_version: str | None = None
    stored_schema_version: str | None = None
    dependency_fingerprint: str | None = None
    reason: str = ""
    diagnostics: list[str] = field(default_factory=list)
    required_implementation_fingerprint: str | None = None
    stored_implementation_fingerprint: str | None = None
    required_dependency_fingerprints: dict[str, str] = field(default_factory=dict)
    stored_dependency_fingerprints: dict[str, str] = field(default_factory=dict)
    input_fingerprint: str | None = None
    stored_input_fingerprint: str | None = None
    required_configuration_fingerprint: str | None = None
    stored_configuration_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def dependency_fingerprint(values: Mapping[str, Any] | None = None) -> str:
    return _hash_payload(dict(values or {}))


# Generated stores contain timestamps, run identifiers and operational paths
# alongside analytical records.  Those fields must not make a semantically
# unchanged artifact look new.  The allowlist of stage files below keeps a
# stage's fingerprint scoped to its own canonical output rather than hashing
# the entire shared artifact directory.
_NON_SEMANTIC_KEYS = {
    "created_at", "updated_at", "recorded_at", "generated_at", "extracted_at",
    "timestamp", "run_id", "pipeline_run_id", "artifact_generation_id",
    "duration_seconds", "processing_seconds", "input_paths", "output_paths",
    "local_path", "local_source_path", "source_local_path", "cache_path",
}
_STAGE_ARTIFACT_FILES: dict[str, tuple[str, ...]] = {
    "normalization": ("qualitative_analysis/documents.jsonl",),
    "qualitative_extraction": ("qualitative_analysis/claims.jsonl",),
    "temporal_comparison": ("qualitative_analysis/temporal_changes.jsonl", "qualitative_analysis/temporal_comparison_metadata.json"),
    "state_synthesis": ("qualitative_analysis/qualitative_states.jsonl", "qualitative_analysis/state_metadata.json"),
    "insider_intelligence": ("insider_intelligence/people.jsonl", "insider_intelligence/transactions.jsonl", "insider_intelligence/ownership.jsonl"),
    "management_intelligence": ("management_intelligence/people.jsonl", "management_intelligence/roles.jsonl"),
    "management_track_record": ("management_intelligence/track_record_snapshots.jsonl", "management_intelligence/guidance_commitments.jsonl", "management_intelligence/strategic_commitments.jsonl", "management_intelligence/operating_outcomes.jsonl"),
    "governance_intelligence": ("management_intelligence/board_history.jsonl", "management_intelligence/governance_committees.jsonl", "management_intelligence/governance_snapshots.jsonl", "management_intelligence/governance_shareholder_rights.jsonl"),
    "scoring": ("scoring_engine/company_scores.jsonl", "scoring_engine/pillar_scores.jsonl", "scoring_engine/factor_scores.jsonl"),
}


def _artifact_ticker_match(value: Any, ticker: str) -> bool:
    if isinstance(value, Mapping):
        for key in ("ticker", "issuer_ticker", "board_ticker", "employer_ticker", "target_ticker"):
            if str(value.get(key) or "").upper() == ticker.upper():
                return True
        return any(_artifact_ticker_match(item, ticker) for item in value.values() if isinstance(item, (Mapping, list, tuple)))
    if isinstance(value, (list, tuple)):
        return any(_artifact_ticker_match(item, ticker) for item in value)
    return False


def _semantic_artifact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _semantic_artifact_value(item) for key, item in value.items()
                if str(key).lower() not in _NON_SEMANTIC_KEYS}
    if isinstance(value, (list, tuple)):
        values = [_semantic_artifact_value(item) for item in value]
        return sorted(values, key=canonical_json)
    return _canonicalize(value)


def canonical_artifact_fingerprint(stage: str, ticker: str, root: str | Path,
                                   *, extra_paths: list[str | Path] | None = None) -> str | None:
    """Hash the canonical analytical content emitted by one stage.

    This intentionally ignores timestamps, mtimes and operational paths while
    retaining claim/document/evidence fields.  Acquisition is excluded: source
    cache identity is governed by its frozen manifest contract.
    """
    if stage == "acquisition":
        return None
    base = Path(root)
    relative_paths = list(_STAGE_ARTIFACT_FILES.get(stage, ()))
    paths = [base / Path(relative) for relative in relative_paths]
    for item in extra_paths or []:
        path = Path(item)
        if path.is_file() and path not in paths:
            paths.append(path)
        elif path.is_dir() and stage == "dossier_generation":
            paths.extend(sorted(path.glob("*.json")))
    payload: list[dict[str, Any]] = []
    found = False
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        found = True
        relative = path.as_posix()
        try:
            if path.suffix.lower() == ".jsonl":
                values = []
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(item, Mapping) and _artifact_ticker_match(item, ticker):
                        values.append(_semantic_artifact_value(item))
                payload.append({"path": relative, "rows": sorted(values, key=canonical_json)})
            else:
                item = json.loads(path.read_text(encoding="utf-8"))
                if _artifact_ticker_match(item, ticker) or stage == "dossier_generation":
                    payload.append({"path": relative, "value": _semantic_artifact_value(item)})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    if not found:
        return None
    return _hash_payload({"stage": stage, "ticker": ticker.upper(), "artifacts": payload})


class ArtifactCompatibilityStore:
    """Small atomic JSON metadata store beside derived artifacts."""

    def __init__(self, root: str | Path = "artifacts") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "artifact_compatibility.json"
        self.quarantine_path = self.root / "artifact_quarantine.jsonl"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "artifact-compatibility-v2", "stages": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"schema_version": "artifact-compatibility-v2", "stages": {}, "corrupt": True}
        return value if isinstance(value, dict) else {"schema_version": "artifact-compatibility-v2", "stages": {}, "corrupt": True}

    def _write(self, value: dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix="artifact-compatibility-", suffix=".tmp", dir=self.root)
        os.close(fd)
        temporary = Path(temp_name)
        try:
            temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def get(self, stage: str, ticker: str) -> dict[str, Any] | None:
        value = self._read()
        stages = value.get("stages") if isinstance(value.get("stages"), dict) else {}
        rows = stages.get(stage) if isinstance(stages.get(stage), dict) else {}
        item = rows.get(ticker.upper())
        return item if isinstance(item, dict) else None

    def _current_dependency_fingerprints(self, stage: str, ticker: str) -> dict[str, str]:
        contract = STAGE_CONTRACTS.get(stage)
        if contract is None:
            return {}
        values: dict[str, str] = {}
        for dependency in contract.dependencies:
            row = self.get(dependency, ticker)
            values[dependency] = dependency_fingerprint({
                "producer_version": row.get("producer_version") if row is not None else None,
                "schema_version": row.get("schema_version") if row is not None else None,
                "implementation_fingerprint": (row.get("implementation_fingerprint") if row is not None else None) or "MISSING",
                "output_fingerprint": (row.get("output_fingerprint") if row is not None else None),
                "input_fingerprint": (row.get("input_fingerprint") or row.get("dependency_fingerprint")) if row is not None else None,
            })
        return values

    def record(
        self,
        stage: str,
        ticker: str,
        *,
        dependency_values: Mapping[str, Any] | None = None,
        artifacts: list[str] | None = None,
        status: str = "ACTIVE",
        producer_version: str | None = None,
        schema_version: str | None = None,
        implementation_fingerprint: str | None = None,
        dependency_fingerprints: Mapping[str, str] | None = None,
        input_fingerprint: str | None = None,
        output_fingerprint: str | None = None,
        configuration: Mapping[str, Any] | None = None,
        configuration_fingerprint: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        contract = STAGE_CONTRACTS.get(stage)
        input_value = input_fingerprint or dependency_fingerprint(dependency_values)
        resolved_dependency_fingerprints = dict(
            dependency_fingerprints if dependency_fingerprints is not None
            else self._current_dependency_fingerprints(stage, ticker)
        )
        # Keep the stage's upstream dependency identity distinct from the
        # runtime/data inputs used to produce it.  Older rows used the same
        # value for both fields; successful reuse/rebuild upgrades them.
        upstream_value = dependency_fingerprint(resolved_dependency_fingerprints)
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        row = {
            "ticker": ticker.upper(),
            "stage": stage,
            "producer_version": producer_version or (contract.producer_version if contract else "UNKNOWN"),
            "schema_version": schema_version or (contract.schema_version if contract else "UNKNOWN"),
            "implementation_fingerprint": implementation_fingerprint or implementation_fingerprint_for(stage),
            "configuration_fingerprint": configuration_fingerprint or globals()["configuration_fingerprint"](stage, configuration),
            "dependency_fingerprint": upstream_value,
            "input_fingerprint": input_value,
            "output_fingerprint": output_fingerprint,
            "dependency_fingerprints": resolved_dependency_fingerprints,
            "dependencies": dict(dependency_values or {}),
            "artifacts": list(artifacts or []),
            "status": status,
            "created_at": timestamp,
            "recorded_at": timestamp,
        }
        value = self._read()
        value["schema_version"] = "artifact-compatibility-v2"
        value.setdefault("stages", {})
        value["stages"].setdefault(stage, {})[ticker.upper()] = row
        self._write(value)
        return row

    def quarantine(self, stage: str, ticker: str, *, reason: str, artifact_ids: list[str] | None = None, producer_version: str | None = None, replacement_id: str | None = None) -> dict[str, Any]:
        row = {
            "artifact_id": artifact_ids or [],
            "ticker": ticker.upper(),
            "stage": stage,
            "reason": reason,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "producer_version": producer_version or (STAGE_CONTRACTS.get(stage).producer_version if stage in STAGE_CONTRACTS else "UNKNOWN"),
            "replacement_id": replacement_id,
            "status": "REPLACED" if replacement_id else "CORRUPT",
        }
        self.quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        with self.quarantine_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def decide(self, stage: str, ticker: str, *, profile: str, dependency_values: Mapping[str, Any] | None = None,
               configuration: Mapping[str, Any] | None = None) -> CompatibilityDecision:
        contract = STAGE_CONTRACTS.get(stage)
        if contract is None:
            return CompatibilityDecision(stage, "UNKNOWN_VERSION", "REJECT", "UNKNOWN", "UNKNOWN", reason="NO_STAGE_CONTRACT")
        required_impl = implementation_fingerprint_for(stage)
        required_config = globals()["configuration_fingerprint"](stage, configuration)
        required_dependencies = self._current_dependency_fingerprints(stage, ticker)
        stored = self.get(stage, ticker)
        if profile == "core_v1":
            return CompatibilityDecision(
                stage, "COMPATIBLE_REUSE", "REUSE", contract.producer_version, contract.schema_version,
                stored.get("producer_version") if stored else None, stored.get("schema_version") if stored else None,
                stored.get("dependency_fingerprint") if stored else None, reason="FROZEN_V1_COMPATIBILITY",
                required_implementation_fingerprint=required_impl,
                stored_implementation_fingerprint=stored.get("implementation_fingerprint") if stored else None,
                required_dependency_fingerprints=required_dependencies,
                stored_dependency_fingerprints=dict(stored.get("dependency_fingerprints") or {}) if stored else {},
                stored_input_fingerprint=stored.get("input_fingerprint") if stored else None,
                required_configuration_fingerprint=required_config,
                stored_configuration_fingerprint=stored.get("configuration_fingerprint") if stored else None,
            )
        if not stored:
            return CompatibilityDecision(stage, "UNKNOWN_VERSION", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="NO_COMPATIBILITY_METADATA", required_implementation_fingerprint=required_impl,
                                         required_dependency_fingerprints=required_dependencies,
                                         required_configuration_fingerprint=required_config)
        common = dict(
            stored_producer_version=stored.get("producer_version"), stored_schema_version=stored.get("schema_version"),
            dependency_fingerprint=stored.get("dependency_fingerprint"), required_implementation_fingerprint=required_impl,
            stored_implementation_fingerprint=stored.get("implementation_fingerprint"),
            required_dependency_fingerprints=required_dependencies,
            stored_dependency_fingerprints=dict(stored.get("dependency_fingerprints") or {}),
            stored_input_fingerprint=stored.get("input_fingerprint"),
            required_configuration_fingerprint=required_config,
            # Rows written by the previous compatibility implementation did
            # not have this field.  They are interpreted as the default static
            # configuration and upgraded on the next successful reuse/rebuild.
            stored_configuration_fingerprint=stored.get("configuration_fingerprint") or required_config,
        )
        if str(stored.get("status") or "").upper() == "CORRUPT":
            return CompatibilityDecision(stage, "CORRUPT", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="QUARANTINED_ARTIFACT", **common)
        if str(stored.get("status") or "").upper() not in {"ACTIVE", ""}:
            return CompatibilityDecision(stage, "INCOMPATIBLE", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="ARTIFACT_STATUS_NOT_REUSABLE", **common)
        if stored.get("producer_version") != contract.producer_version:
            return CompatibilityDecision(stage, "INCOMPATIBLE", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="PRODUCER_VERSION_CHANGED", **common)
        if stored.get("schema_version") != contract.schema_version:
            return CompatibilityDecision(stage, "INCOMPATIBLE", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="SCHEMA_VERSION_CHANGED", **common)
        if stored.get("configuration_fingerprint") and stored.get("configuration_fingerprint") != required_config:
            return CompatibilityDecision(stage, "INCOMPATIBLE", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="CONFIGURATION_FINGERPRINT_CHANGED", **common)
        if not stored.get("implementation_fingerprint") or stored.get("implementation_fingerprint") != required_impl:
            return CompatibilityDecision(stage, "INCOMPATIBLE", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="IMPLEMENTATION_FINGERPRINT_CHANGED", **common)
        if dict(stored.get("dependency_fingerprints") or {}) != required_dependencies:
            return CompatibilityDecision(stage, "INCOMPATIBLE", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                         reason="UPSTREAM_DEPENDENCY_CHANGED", **common)
        if dependency_values is not None:
            expected = dependency_fingerprint(dependency_values)
            stored_input = stored.get("input_fingerprint") or stored.get("dependency_fingerprint")
            if stored_input != expected:
                return CompatibilityDecision(stage, "INCOMPATIBLE", "REBUILD_REQUIRED", contract.producer_version, contract.schema_version,
                                             reason="DEPENDENCY_FINGERPRINT_CHANGED", input_fingerprint=expected, **common)
        return CompatibilityDecision(stage, "COMPATIBLE_REUSE", "REUSE", contract.producer_version, contract.schema_version,
                                     reason="COMPATIBLE_METADATA", input_fingerprint=(dependency_fingerprint(dependency_values) if dependency_values is not None else None), **common)

    def diagnose(self, stage: str, ticker: str, *, profile: str = "core_v2",
                 configuration: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return a compact, machine-readable fingerprint comparison."""
        decision = self.decide(stage, ticker, profile=profile, configuration=configuration)
        differences: list[str] = []
        if decision.stored_implementation_fingerprint != decision.required_implementation_fingerprint:
            differences.append("implementation_fingerprint")
        if decision.stored_configuration_fingerprint != decision.required_configuration_fingerprint:
            differences.append("configuration_fingerprint")
        if decision.stored_dependency_fingerprints != decision.required_dependency_fingerprints:
            differences.append("dependency_fingerprints")
        return {
            "stage": stage,
            "ticker": ticker.upper(),
            "stored": {
                "implementation_fingerprint": decision.stored_implementation_fingerprint,
                "configuration_fingerprint": decision.stored_configuration_fingerprint,
                "dependency_fingerprints": decision.stored_dependency_fingerprints,
            },
            "current": {
                "implementation_fingerprint": decision.required_implementation_fingerprint,
                "configuration_fingerprint": decision.required_configuration_fingerprint,
                "dependency_fingerprints": decision.required_dependency_fingerprints,
            },
            "match": not differences and decision.action == "REUSE",
            "differing_components": differences,
            "action": decision.action,
            "reason": decision.reason,
        }


__all__ = [
    "StageContract", "STAGE_CONTRACTS", "STAGE_SOURCE_FILES", "CompatibilityDecision",
    "ArtifactCompatibilityStore", "dependency_fingerprint", "implementation_fingerprint",
    "implementation_fingerprint_for", "implementation_fingerprint_inputs", "canonical_json",
    "configuration_fingerprint", "STAGE_CONFIGURATIONS", "canonical_artifact_fingerprint",
]
