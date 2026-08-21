"""Cost tracking for pipeline stages."""

from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.core.logging import get_logger

logger = get_logger(__name__)


async def record_cost(
    session: AsyncSession,
    job_id: int,
    provider: Literal["llm", "kokoro_tts", "runpod", "pexels", "other"],
    amount_cents: int,
) -> None:
    """
    Record an incurred cost for a job into the financial_ledger table.
    Also incrementally updates the `cost_cents` total on the Job record.
    """
    if amount_cents == 0:
        return
        
    try:
        # 1. Insert ledger row
        await session.execute(
            text("""
                INSERT INTO financial_ledger 
                (job_id, transaction_type, amount_cents, currency, provider)
                VALUES (:job_id, 'cost', :amount_cents, 'USD', :provider)
            """),
            {"job_id": job_id, "amount_cents": amount_cents, "provider": provider}
        )
        
        # 2. Update Job total cost
        await session.execute(
            text("""
                UPDATE jobs 
                SET cost_cents = cost_cents + :amount_cents 
                WHERE id = :job_id
            """),
            {"job_id": job_id, "amount_cents": amount_cents}
        )
        
        await session.commit()
        logger.info("cost_recorded", extra={"job_id": job_id, "provider": provider, "amount_cents": amount_cents})
    except Exception as e:
        await session.rollback()
        logger.error("failed_to_record_cost", extra={"job_id": job_id, "error": str(e)})


def calculate_llm_cost(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """
    Estimate LLM cost in cents based on model and token counts.
    """
    # Simple approximations (e.g. gpt-4o $5/M prompt, $15/M comp)
    # We'll use a generic fallback for now
    cost_usd = (prompt_tokens * 5.0 / 1_000_000) + (completion_tokens * 15.0 / 1_000_000)
    return int(cost_usd * 100)


def calculate_tts_cost(duration_seconds: float) -> int:
    """
    Amortized compute cost for self-hosted Kokoro TTS.
    Estimating $0.001 per minute of audio.
    """
    minutes = duration_seconds / 60.0
    cost_usd = minutes * 0.001
    cents = int(cost_usd * 100)
    # Ensure at least 1 cent if there's any cost, to avoid 0-cent drops
    return max(1, cents) if duration_seconds > 0 else 0


def calculate_runpod_cost(duration_seconds: float, gpu_type: str = "rtx_4090") -> int:
    """
    RunPod Serverless GPU execution time cost.
    E.g. RTX 4090 is approx $0.74/hour -> $0.0002/second
    """
    rate_per_sec = 0.0002
    cost_usd = duration_seconds * rate_per_sec
    cents = int(cost_usd * 100)
    return max(1, cents) if duration_seconds > 0 else 0
