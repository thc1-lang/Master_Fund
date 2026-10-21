"""Evidence- and coverage-aware qualitative scoring (Component 7)."""

from .models import (
    CompanyScoreSnapshot,
    FactorScore,
    PillarScore,
    ScoreRun,
)
from .profiles import ProfileNotConfigured, load_profile
from .eligibility import EligibilityDecision, decide_score_eligibility, factor_is_eligible, release_score

__all__ = [
    "CompanyScoreSnapshot",
    "FactorScore",
    "PillarScore",
    "ScoreRun",
    "ProfileNotConfigured",
    "load_profile",
    "EligibilityDecision", "decide_score_eligibility", "factor_is_eligible", "release_score",
]

SCORING_VERSION = "qualitative-scoring-v1.2"
