import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from src.db import Job
from src.workers.stages import StageContext
from src.workers.stages.tts import TTSStage
from src.domain import ScriptPayload, SceneScript


@pytest.fixture
def mock_context():
    job = Job(id=1, topic="Test Topic", niche="Test Niche", business_model="YouTube_Shorts", stage="scripting", status="running")
    session = AsyncMock()
    settings = MagicMock()
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
@patch("src.workers.stages.tts.KokoroTTSService")
async def test_tts_stage_success(mock_tts_cls, mock_context):
    mock_tts = mock_tts_cls.return_value
    
    script = ScriptPayload(
        title="Test Title",
        description="Test Desc",
        scenes=[
            SceneScript(index=1, text="Scene 1 text", visual_query="scene 1"),
            SceneScript(index=2, text="Scene 2 text", visual_query="scene 2"),
        ]
    )

    stage = TTSStage()
    result = await stage.execute(mock_context, script=script)

    assert mock_tts.synthesize.call_count == 2
    
    assert "audio_paths" in result
    assert len(result["audio_paths"]) == 2
    assert result["audio_paths"][0] == Path("/tmp/work/scene_1.wav")
    assert result["audio_paths"][1] == Path("/tmp/work/scene_2.wav")


@pytest.mark.asyncio
@patch("src.workers.stages.tts.KokoroTTSService")
async def test_tts_stage_fallback_on_error(mock_tts_cls, mock_context):
    mock_tts = mock_tts_cls.return_value
    mock_tts.synthesize.side_effect = Exception("TTS Failed")
    
    script = ScriptPayload(
        title="Test Title",
        description="Test Desc",
        scenes=[
            SceneScript(index=1, text="Scene 1 text", visual_query="scene 1"),
        ]
    )

    stage = TTSStage()
    result = await stage.execute(mock_context, script=script)

    assert mock_tts.synthesize_silence.call_count == 1
    assert result["audio_paths"][0] == Path("/tmp/work/scene_1.wav")
