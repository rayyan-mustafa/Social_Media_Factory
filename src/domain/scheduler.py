from datetime import datetime
from pydantic import BaseModel, Field
from typing import Literal

class ScheduledSlot(BaseModel):
    """Represents a scheduled block of time for a specific business model's content."""
    business_model: str = Field(description="The target business model (e.g., YouTube_Shorts)")
    scheduled_for: datetime = Field(description="The exact datetime this content should be published or enqueued")
    
    urgency: Literal["normal", "high"] = Field(
        default="normal",
        description="Priority level. 'high' means it is overdue by more than 50% of its normal interval."
    )
    
    publish_mode: Literal["auto", "manual_review"] = Field(
        default="auto",
        description="Whether this pipeline auto-publishes or stops at a manual outbox folder."
    )
