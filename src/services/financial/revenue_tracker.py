"""Revenue tracking for Tier A and Tier B platforms."""

from typing import Protocol, Literal, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.core.logging import get_logger

logger = get_logger(__name__)


class RevenueSource(Protocol):
    platform: str
    publish_mode: Literal["auto", "manual_review"]

    async def fetch_revenue(self, session: AsyncSession, job_id: int, external_id: str) -> int | None:
        """
        Fetch revenue in cents. Returns None if pending manual entry or unknown.
        """
        ...


class YouTubeRevenueSource:
    platform: str = "youtube"
    publish_mode: Literal["auto", "manual_review"] = "auto"

    async def fetch_revenue(self, session: AsyncSession, job_id: int, external_id: str) -> int | None:
        """Fetch YouTube revenue (or estimate based on views * RPM)."""
        # In a real app, this polls YouTube Analytics API.
        # Here we mock retrieving a value from the PerformanceRecord (Module 2).
        result = await session.execute(
            text("SELECT views, revenue_cents FROM performance_records WHERE platform_content_id = :ext_id"),
            {"ext_id": external_id}
        )
        row = result.fetchone()
        if not row:
            return 0
            
        views, rev = row
        # If real rev is 0, estimate based on $2 RPM (200 cents per 1000 views)
        if rev == 0 and views > 0:
            return int((views / 1000.0) * 200)
        return rev


class KDPRevenueSource:
    platform: str = "kdp"
    publish_mode: Literal["auto", "manual_review"] = "manual_review"

    async def fetch_revenue(self, session: AsyncSession, job_id: int, external_id: str) -> int | None:
        """KDP has no API. Always returns None to indicate PENDING_MANUAL."""
        return None


class ACXRevenueSource:
    platform: str = "acx"
    publish_mode: Literal["auto", "manual_review"] = "manual_review"

    async def fetch_revenue(self, session: AsyncSession, job_id: int, external_id: str) -> int | None:
        """ACX has no API. Always returns None to indicate PENDING_MANUAL."""
        return None


class TeachableRevenueSource:
    platform: str = "teachable"
    publish_mode: Literal["auto", "manual_review"] = "manual_review"

    async def fetch_revenue(self, session: AsyncSession, job_id: int, external_id: str) -> int | None:
        """Teachable course sales."""
        return None


# Map for models to their primary revenue sources
REVENUE_SOURCES = {
    "YouTube_Shorts": YouTubeRevenueSource(),
    "Web_Series": YouTubeRevenueSource(),
    "Sleep_Stories": YouTubeRevenueSource(),
    "EBooks_KDP": KDPRevenueSource(),
    "Audiobooks_ACX": ACXRevenueSource(),
    "Online_Courses_Teachable": TeachableRevenueSource(),
}


async def sync_revenue(session: AsyncSession, job_id: int, business_model: str, external_id: str) -> None:
    """Sync revenue for a given job and business model."""
    source = REVENUE_SOURCES.get(business_model)
    if not source:
        return
        
    revenue = await source.fetch_revenue(session, job_id, external_id)
    
    if revenue is None:
        # PENDING_MANUAL. Do not write a 0 cost/revenue row. It stays NULL implicitly in our aggregates.
        logger.debug("revenue_sync_pending_manual", extra={"job_id": job_id, "platform": source.platform})
        return
        
    if revenue > 0:
        # Record it (assuming it's incremental, or we just update a state). 
        # In this simplistic tracker, we'd update `revenue_cents` on the Job.
        await session.execute(
            text("""
                UPDATE jobs 
                SET revenue_cents = :revenue 
                WHERE id = :job_id
            """),
            {"job_id": job_id, "revenue": revenue}
        )
        await session.commit()
