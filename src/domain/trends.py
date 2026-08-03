from datetime import datetime
from pydantic import BaseModel, Field

class TrendCandidate(BaseModel):
    topic: str = Field(description="The core topic or query, e.g., 'artificial intelligence'")
    platform_source: str = Field(description="The platform where this trend was found, e.g., 'reddit', 'youtube'")
    engagement_score: float = Field(description="Normalized engagement score (0.0 to 1.0)", default=0.0)
    raw_data: dict = Field(description="Raw metadata from the platform", default_factory=dict)
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
