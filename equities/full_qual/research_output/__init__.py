"""Deterministic analyst dossier generation from frozen intelligence artifacts."""

from .assembler import assemble_dossier
from .models import CompanyResearchDossier
from .v2 import assemble_institutional_dossier, write_institutional_dossier
from .v2_models import InstitutionalDossier, QualityGateResult, AnalystHandoff, quality_gate, quality_gate_from_records

__all__ = ["CompanyResearchDossier", "assemble_dossier", "InstitutionalDossier", "QualityGateResult", "AnalystHandoff", "quality_gate", "quality_gate_from_records", "assemble_institutional_dossier", "write_institutional_dossier"]
