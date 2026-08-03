from pydantic import BaseModel, Field
from typing import Literal
from src.domain.trends import TrendCandidate

class TopicRecommendation(BaseModel):
    """A ranked topic recommendation combining trend data and historical performance."""
    candidate: TrendCandidate
    business_model: str = Field(description="The target business model (e.g., YouTube_Shorts)")
    niche: str = Field(description="The target niche (e.g., documentary)")
    
    predicted_score: float = Field(
        description="The final heuristic score (raw engagement_score * analytics_multiplier)"
    )
    confidence_level: Literal["low", "medium", "high"] = Field(
        default="low",
        description="Confidence backed by historical analytics. Low means purely trend-based fallback."
    )
