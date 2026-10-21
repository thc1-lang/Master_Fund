"""Offline normalization, provenance, and deterministic qualitative extraction."""

from .models import FiscalPeriod, IngestionFailure, NormalizedDocument
from .document_store import DocumentStore
from .extraction_models import ClaimCandidate, ExtractionChunk, QualitativeClaim, register_dimension
from .extraction_store import ExtractionStore
from .qualitative_extraction import DeterministicExtractionProvider, ExtractionProvider, chunk_document, extract_claims, extract_document, stable_claim_id
from .temporal_models import TemporalChange, TemporalComparisonResult, TemporalEvidence
from .temporal_comparison import DeterministicTemporalComparisonProvider, TemporalComparisonProvider, compare_latest_vs_previous
from .temporal_store import TemporalStore
from .state_models import QualitativeState, StateSynthesisResult
from .state_synthesis import DeterministicStateSynthesisProvider, StateSynthesisProvider, stable_state_id
from .state_store import StateStore
from .state_confidence import calculate_confidence
from .status import SourceStatus, STATUS_VALUES, is_coverage_neutral
from .schema import EvidenceAnchor, MetricRecord, PersonRole, SchemaValidationError, assert_valid, validate_record
from .point_in_time import InformationDates, filter_as_of, point_in_time_status, public_availability_date, assert_point_in_time
from .extraction_safety import looks_like_pdf_corruption, validate_extracted_text, safe_text
from .numeric import MetricValue, NumericValidationError, compare_values, range_midpoint, percentage_point_change, basis_points
from .entity_resolution import CanonicalPerson, EntityMatch, EntityResolver
from .reconciliation import ReconciliationResult, SourceReconciler

__all__ = [
    "DocumentStore", "FiscalPeriod", "IngestionFailure", "NormalizedDocument",
    "ClaimCandidate", "ExtractionChunk", "QualitativeClaim", "ExtractionStore", "register_dimension",
    "ExtractionProvider", "DeterministicExtractionProvider", "chunk_document", "extract_document", "extract_claims", "stable_claim_id",
    "TemporalChange", "TemporalComparisonResult", "TemporalEvidence", "TemporalComparisonProvider",
    "DeterministicTemporalComparisonProvider", "compare_latest_vs_previous", "TemporalStore",
    "QualitativeState", "StateSynthesisResult", "StateSynthesisProvider",
    "DeterministicStateSynthesisProvider", "stable_state_id", "StateStore", "calculate_confidence",
    "SourceStatus", "STATUS_VALUES", "is_coverage_neutral", "EvidenceAnchor", "MetricRecord", "PersonRole", "SchemaValidationError", "assert_valid", "validate_record",
    "InformationDates", "filter_as_of", "point_in_time_status", "public_availability_date", "assert_point_in_time", "looks_like_pdf_corruption", "validate_extracted_text", "safe_text", "MetricValue", "NumericValidationError", "compare_values", "range_midpoint", "percentage_point_change", "basis_points",
    "CanonicalPerson", "EntityMatch", "EntityResolver", "ReconciliationResult", "SourceReconciler",
]
