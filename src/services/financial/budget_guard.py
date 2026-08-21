"""Autonomous spending limits and budget checks."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Union

from src.core.logging import get_logger
from src.services.financial.roi_calculator import get_trailing_30_day_roi

logger = get_logger(__name__)

# Daily Cap per model in cents ($5.00)
DAILY_SPENDING_CAP_CENTS = 500

# Minimum allowed ROI threshold (-30%) before dynamic blocking kicks in
MIN_ROI_THRESHOLD = -0.30


async def _get_rolling_24h_spend(session: AsyncSession, business_model: str) -> int:
    """Calculate how much has been spent on this model in the last 24 hours."""
    result = await session.execute(
        text("""
            SELECT SUM(cost_cents)
            FROM jobs
            WHERE business_model = :bm
              AND created_at >= NOW() - INTERVAL '24 hours'
        """),
        {"bm": business_model}
    )
    return result.scalar() or 0


async def can_afford_job(session: AsyncSession, business_model: str, estimated_cost_cents: int) -> bool:
    """
    Evaluate if a job can be created for the given business model based on
    a daily spending cap and dynamic ROI limits.
    """
    # 1. Daily Cap Check (Fixed Limit)
    current_daily_spend = await _get_rolling_24h_spend(session, business_model)
    if (current_daily_spend + estimated_cost_cents) > DAILY_SPENDING_CAP_CENTS:
        logger.warning(
            "budget_guard_daily_cap_breached",
            extra={
                "model": business_model,
                "current_spend": current_daily_spend,
                "estimated_cost": estimated_cost_cents,
                "cap": DAILY_SPENDING_CAP_CENTS
            }
        )
        return False

    # 2. Dynamic ROI Check (Variable Limit)
    roi = await get_trailing_30_day_roi(session, business_model)
    
    # Graceful degradation for Tier B pending manual
    if roi == "UNKNOWN":
        logger.debug(
            "budget_guard_roi_unknown",
            extra={"model": business_model, "reason": "Tier B pending manual revenue"}
        )
        # We allow it to proceed relying entirely on the daily cap
        return True
        
    if isinstance(roi, float) and roi < MIN_ROI_THRESHOLD:
        logger.warning(
            "budget_guard_roi_breached",
            extra={
                "model": business_model,
                "current_roi": roi,
                "threshold": MIN_ROI_THRESHOLD
            }
        )
        # ROI is too heavily negative, block scaling this model
        return False
        
    return True
