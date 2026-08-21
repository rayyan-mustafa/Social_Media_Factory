import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from src.db.repository import get_business_model_stats, mark_job_published
from src.domain import JobStatus

@pytest.mark.asyncio
async def test_yield_stats_counts_manual_publish_as_success():
    """Test that awaiting_manual_publish is summed into total_succeeded."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    
    # Simulate DB returning 1 succeeded, 1 failed, 2 awaiting_manual_publish
    mock_result.all.return_value = [
        (JobStatus.SUCCEEDED.value, 1),
        (JobStatus.FAILED.value, 1),
        (JobStatus.AWAITING_MANUAL_PUBLISH.value, 2)
    ]
    mock_session.execute.return_value = mock_result
    
    stats = await get_business_model_stats(mock_session, "EBooks_KDP", datetime.utcnow())
    
    assert stats["succeeded"] == 1
    assert stats["failed"] == 1
    assert stats["awaiting_manual_publish"] == 2
    
    # Crucially, total_succeeded should be 3 (1 + 2)
    assert stats["total_succeeded"] == 3

@pytest.mark.asyncio
async def test_mark_job_published_transitions_to_succeeded():
    """Test that mark_job_published sets status to SUCCEEDED and creates a PerformanceRecord."""
    # We will just verify it runs without syntax error in the test suite
    # and calls add() for the JobEvent and PerformanceRecord
    mock_session = AsyncMock()
    
    mock_job = MagicMock()
    mock_job.id = 123
    mock_job.stage = "composing"
    mock_job.status = JobStatus.AWAITING_MANUAL_PUBLISH.value
    
    # Mock get_job within repository
    with pytest.MonkeyPatch.context() as m:
        m.setattr("src.db.repository.get_job", AsyncMock(return_value=mock_job))
        
        await mark_job_published(mock_session, 123, "amazon_kdp", "B01234567")
        
        assert mock_job.status == JobStatus.SUCCEEDED.value
        # Assert session.add was called twice (PerformanceRecord + JobEvent)
        assert mock_session.add.call_count == 2
        mock_session.commit.assert_called_once()
