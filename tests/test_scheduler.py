import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from src.services.scheduler.calendar import ContentCalendar
from src.db import Job

@pytest.mark.asyncio
async def test_scheduler_empty_db():
    """Test that if no prior jobs exist, the model is due immediately with high urgency."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result
    
    calendar = ContentCalendar(mock_db)
    
    # 1. get_next_slot should return high urgency and a time near now
    slot = await calendar.get_next_slot("YouTube_Shorts")
    assert slot.business_model == "YouTube_Shorts"
    assert slot.urgency == "high"
    assert slot.publish_mode == "auto"
    
    # 2. is_due_for_content should be True
    is_due = await calendar.is_due_for_content("YouTube_Shorts")
    assert is_due is True

@pytest.mark.asyncio
async def test_scheduler_recent_job_not_due():
    """Test that a recently queued job prevents scheduling a new one."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    
    # Simulate a job created 1 hour ago
    mock_job = Job(id="123", business_model="YouTube_Shorts", created_at=datetime.utcnow() - timedelta(hours=1))
    mock_result.scalar_one_or_none.return_value = mock_job
    mock_db.execute.return_value = mock_result
    
    calendar = ContentCalendar(mock_db)
    
    slot = await calendar.get_next_slot("YouTube_Shorts")
    assert slot.urgency == "normal"
    
    # The interval for YouTube Shorts is 8 hours, so scheduled_for should be 7 hours from now
    expected_schedule = mock_job.created_at + timedelta(hours=8)
    assert slot.scheduled_for == expected_schedule
    
    is_due = await calendar.is_due_for_content("YouTube_Shorts")
    assert is_due is False

@pytest.mark.asyncio
async def test_scheduler_overdue_job_high_urgency():
    """Test that if a job is overdue by > 50% of its interval, urgency is high."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    
    # Simulate a job created 14 hours ago (interval is 8 hours, 14 > 8 * 1.5)
    mock_job = Job(id="123", business_model="YouTube_Shorts", created_at=datetime.utcnow() - timedelta(hours=14))
    mock_result.scalar_one_or_none.return_value = mock_job
    mock_db.execute.return_value = mock_result
    
    calendar = ContentCalendar(mock_db)
    
    slot = await calendar.get_next_slot("YouTube_Shorts")
    assert slot.urgency == "high"
    
    is_due = await calendar.is_due_for_content("YouTube_Shorts")
    assert is_due is True

@pytest.mark.asyncio
async def test_scheduler_manual_publish_mode():
    """Test that KDP correctly gets manual_review mode."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result
    
    calendar = ContentCalendar(mock_db)
    
    slot = await calendar.get_next_slot("EBooks_KDP")
    assert slot.business_model == "EBooks_KDP"
    assert slot.publish_mode == "manual_review"
