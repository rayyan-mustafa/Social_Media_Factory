"""ROI calculation for business models."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Union

from src.core.logging import get_logger
from src.services.financial.revenue_tracker import REVENUE_SOURCES

logger = get_logger(__name__)

async def get_trailing_30_day_roi(session: AsyncSession, business_model: str) -> Union[float, str]:
    """
    Calculate the trailing 30-day ROI for a given business model.
    Returns the ROI as a float (e.g. 1.25 for 125% return, -0.5 for -50%).
    Returns 'UNKNOWN' if revenue data is pending manual entry.
    """
    source = REVENUE_SOURCES.get(business_model)
    if source and source.publish_mode == "manual_review":
        # Check if we have any manual revenue entered in the last 30 days.
        # For simplicity, if it's a manual model, we check if revenue_cents > 0 on any job.
        # If not, we degrade gracefully to UNKNOWN.
        res = await session.execute(
            text("""
                SELECT SUM(revenue_cents) 
                FROM jobs 
                WHERE business_model = :bm 
                  AND created_at >= NOW() - INTERVAL '30 days'
            """),
            {"bm": business_model}
        )
        total_rev = res.scalar() or 0
        if total_rev == 0:
            return "UNKNOWN"

    result = await session.execute(
        text("""
            SELECT SUM(cost_cents), SUM(revenue_cents)
            FROM jobs
            WHERE business_model = :bm
              AND created_at >= NOW() - INTERVAL '30 days'
        """),
        {"bm": business_model}
    )
    row = result.fetchone()
    if not row:
        return 0.0
        
    total_cost, total_revenue = row
    total_cost = total_cost or 0
    total_revenue = total_revenue or 0
    
    if total_cost == 0:
        # If no cost, infinite ROI, or 0 if no revenue either
        return float('inf') if total_revenue > 0 else 0.0
        
    roi = (total_revenue - total_cost) / total_cost
    return roi
