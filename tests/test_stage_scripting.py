import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from src.db import Job
from src.workers.stages import StageContext
from src.workers.stages.scripting import ScriptingStage
from src.domain import ScriptPayload, JobStage


@pytest.fixture
def mock_context():
    job = Job(id=1, topic="Test Topic", niche="Test Niche", business_model="YouTube_Shorts", stage="queued", status="queued")
    session = AsyncMock()
    settings = MagicMock(wavespeed_api_key="test_key")
    storage = MagicMock()
    work_dir = Path("/tmp/work")
    return StageContext(
        job=job,
        session=session,
        settings=settings,
        storage=storage,
        work_dir=work_dir,
    )


@pytest.mark.asyncio
@patch("src.workers.stages.scripting.ScriptGenerator")
async def test_scripting_stage_success(mock_generator_cls, mock_context):
    mock_generator = mock_generator_cls.return_value
    mock_script = ScriptPayload(
        title="Test Title",
        description="Test Desc",
        scenes=[],
    )
    mock_generator.generate.return_value = mock_script

    stage = ScriptingStage()
    result = await stage.execute(mock_context)

    # Check generator called
    mock_generator.generate.assert_called_once_with("Test Topic", "Test Niche", "YouTube_Shorts")
    
    # Check job updated
    assert mock_context.job.script_json == mock_script.model_dump()
    mock_context.session.commit.assert_awaited()
    
    # Check result returned
    assert "script" in result
    assert result["script"] == mock_script
    
    # YouTube Shorts does not save files directly in this mock config (persists_files=False)
    # Check transition
    # The first call to transition_stage should have happened (can't easily assert here due to standalone fn)


@pytest.mark.asyncio
@patch("src.workers.stages.scripting.ScriptGenerator")
async def test_scripting_stage_fallback_on_error(mock_generator_cls, mock_context):
    mock_generator = mock_generator_cls.return_value
    mock_generator.generate.side_effect = Exception("API Error")
    mock_fallback_script = ScriptPayload(
        title="Fallback",
        description="Fallback Desc",
        scenes=[],
    )
    mock_generator.generate_fallback.return_value = mock_fallback_script

    stage = ScriptingStage()
    result = await stage.execute(mock_context)

    # Check fallback called
    mock_generator.generate_fallback.assert_called_once_with("Test Topic", "Test Niche", "YouTube_Shorts")
    
    # Check result is fallback
    assert result["script"] == mock_fallback_script
