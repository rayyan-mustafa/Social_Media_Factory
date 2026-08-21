import pytest
from unittest.mock import AsyncMock, MagicMock
from src.services.optimizer.scorer import RuleBasedTopicScorer
from src.domain.trends import TrendCandidate

@pytest.mark.asyncio
async def test_optimizer_fallback_no_data():
    """Test that the scorer degrades gracefully when no historical data exists."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    # Simulate DB returning None for avg views
    mock_result.scalar.return_value = None
    mock_db.execute.return_value = mock_result
    
    scorer = RuleBasedTopicScorer(mock_db)
    
    candidates = [
        TrendCandidate(topic="Trend 1", platform_source="youtube", engagement_score=0.8),
        TrendCandidate(topic="Trend 2", platform_source="google", engagement_score=0.5)
    ]
    
    recs = await scorer.rank_candidates(candidates, "YouTube_Shorts", "documentary")
    
    assert len(recs) == 2
    # Ensure they are sorted by score descending
    assert recs[0].candidate.topic == "Trend 1"
    assert recs[0].predicted_score == 0.8  # No multiplier applied
    assert recs[0].confidence_level == "low"
    
    assert recs[1].candidate.topic == "Trend 2"
    assert recs[1].predicted_score == 0.5
    assert recs[1].confidence_level == "low"


@pytest.mark.asyncio
async def test_optimizer_with_historical_data():
    """Test that the scorer applies multipliers correctly when data exists."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    # Simulate DB returning high avg views (boost multiplier)
    mock_result.scalar.return_value = 15000
    mock_db.execute.return_value = mock_result
    
    scorer = RuleBasedTopicScorer(mock_db)
    
    candidates = [
        TrendCandidate(topic="Trend 1", platform_source="youtube", engagement_score=0.8),
    ]
    
    recs = await scorer.rank_candidates(candidates, "YouTube_Shorts", "documentary")
    
    assert len(recs) == 1
    # Multiplier should be 1.5 for views > 10000
    assert recs[0].predicted_score == pytest.approx(1.2)  # 0.8 * 1.5
    assert recs[0].confidence_level == "high"
