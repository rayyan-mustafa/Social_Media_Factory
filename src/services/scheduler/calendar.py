import logging
from datetime import datetime, timedelta
from typing import Dict, Literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.db import Job
from src.domain.scheduler import ScheduledSlot

logger = logging.getLogger(__name__)

# Static v1 cadence config
CADENCE_MAP: Dict[str, timedelta] = {
    "YouTube_Shorts": timedelta(hours=8),       # ~3x per day
    "Podcast_Audio": timedelta(days=7),         # Weekly
    "SEO_Blogs": timedelta(days=3.5),           # ~2x per week
    "EBooks_KDP": timedelta(days=30),           # Monthly
    "Audiobooks_ACX": timedelta(days=30),       # Monthly
    "Web_Series": timedelta(days=7),            # Weekly
    "Sleep_Stories": timedelta(days=14),        # Bi-weekly
    "Radio_FM": timedelta(days=7),              # Weekly (matching podcast until real FM distribution exists)
    "Online_Courses_Teachable": timedelta(days=7) # Weekly module
}

PUBLISH_MODE_MAP: Dict[str, Literal["auto", "manual_review"]] = {
    "YouTube_Shorts": "auto",
    "Podcast_Audio": "auto",
    "SEO_Blogs": "auto",
    "EBooks_KDP": "manual_review",          # Needs manual outbox flow
    "Audiobooks_ACX": "manual_review",      # Needs manual outbox flow
    "Web_Series": "auto",
    "Sleep_Stories": "auto",
    "Radio_FM": "auto",
    "Online_Courses_Teachable": "manual_review" # Assuming course uploads might need manual finalization
}

class ContentCalendar:
    """
    Module 4: V1 Static Scheduler Engine.
    Determines if business models are due for new content generation.
    """
    
    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session
        
    async def get_next_slot(self, business_model: str) -> ScheduledSlot:
        """
        Calculate exactly when the next piece of content is due.
        Also calculates urgency (if overdue > 50% of the interval).
        """
        interval = CADENCE_MAP.get(business_model, timedelta(days=1))
        publish_mode = PUBLISH_MODE_MAP.get(business_model, "auto")
        
        # Find the most recent job for this model (not failed)
        stmt = (
            select(Job)
            .where(Job.business_model == business_model)
            .where(Job.status != "FAILED")
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        
        result = await self.db_session.execute(stmt)
        last_job = result.scalar_one_or_none()
        
        now = datetime.utcnow()
        
        if not last_job:
            # First time running this model, due immediately, high urgency
            return ScheduledSlot(
                business_model=business_model,
                scheduled_for=now,
                urgency="high",
                publish_mode=publish_mode
            )
            
        time_since_last = now - last_job.created_at
        scheduled_for = last_job.created_at + interval
        
        # Urgency threshold explicitly defined: overdue by more than 50% of the interval
        if time_since_last >= interval * 1.5:
            urgency = "high"
        else:
            urgency = "normal"
            
        return ScheduledSlot(
            business_model=business_model,
            scheduled_for=scheduled_for,
            urgency=urgency,
            publish_mode=publish_mode
        )
        
    async def is_due_for_content(self, business_model: str) -> bool:
        """
        Check if a given business model has passed its cadence interval and needs new content right now.
        """
        slot = await self.get_next_slot(business_model)
        now = datetime.utcnow()
        
        return now >= slot.scheduled_for
