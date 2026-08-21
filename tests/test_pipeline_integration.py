"""Integration test for the full pipeline using real StageProcessors but mocked services."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import Job
from src.workers.pipeline import run_pipeline
from src.domain import JobStatus, JobStage
from src.core.config import Settings
from src.domain import ScriptPayload, SceneScript


@pytest.fixture
def mock_get_job():
    with patch("src.workers.pipeline.get_job") as mock:
        yield mock


@pytest.fixture
def mock_transition_stage():
    with patch("src.workers.stages.scripting.transition_stage") as mock_script, \
         patch("src.workers.stages.tts.transition_stage") as mock_tts, \
         patch("src.workers.pipeline.transition_stage") as mock_pipeline:
        yield

@pytest.fixture
def mock_session_factory():
    with patch("src.workers.pipeline.get_session_factory") as mock:
        factory_instance = MagicMock()
        session_instance = AsyncMock()
        factory_instance.return_value.__aenter__.return_value = session_instance
        mock.return_value = factory_instance
        yield session_instance


@pytest.fixture
def mock_storage():
    with patch("src.workers.pipeline.ObjectStorage") as mock:
        storage_instance = MagicMock()
        storage_instance.download_json = AsyncMock(return_value={"scenes": [{"visual_prompt": "test"}]})
        storage_instance.upload_file = AsyncMock()
        storage_instance.checksum_file.return_value = "fake_checksum"
        mock.return_value = storage_instance
        yield storage_instance


@pytest.fixture
def mock_notify():
    with patch("src.workers.pipeline.notify_job_event", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture
def mock_discord():
    with patch("src.workers.pipeline.send_discord_alert", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture
def mock_services():
    """Mock all external API services inside the stage processors."""
    with patch("src.workers.stages.scripting.ScriptGenerator") as mock_sg, \
         patch("src.workers.stages.tts.KokoroTTSService") as mock_tts, \
         patch("src.workers.stages.media.MediaRetrievalService") as mock_media, \
         patch("src.workers.stages.composing.VideoComposer") as mock_composer, \
         patch("src.workers.stages.publishing.YouTubeUploader") as mock_yt:
         
        # Script Generator Mock
        sg_instance = MagicMock()
        mock_script = ScriptPayload(
            title="Test Title",
            description="Test Desc",
            tags=["test"],
            scenes=[SceneScript(index=1, text="Test text.", visual_query="Test")]
        )
        sg_instance.generate.return_value = mock_script
        sg_instance.generate_fallback.return_value = mock_script
        mock_sg.return_value = sg_instance
        
        # TTS Mock
        tts_instance = MagicMock()
        def synthesize_mock(text, path, *args, **kwargs):
            import wave
            with wave.open(str(path), 'w') as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(24000)
                f.writeframes(b'')
        tts_instance.synthesize.side_effect = synthesize_mock
        mock_tts.return_value = tts_instance
        
        # Media Mock
        media_instance = MagicMock()
        media_instance.fetch_best_image_url = AsyncMock(return_value="http://fake.url/img.jpg")
        def download_mock(url, path):
            Path(path).write_bytes(b"image")
        media_instance.download_image.side_effect = download_mock
        mock_media.return_value = media_instance
        
        # Composer Mock
        composer_instance = MagicMock()
        def concat_mock(*args, **kwargs):
            out = kwargs.get("output_path") or args[1]
            Path(out).write_bytes(b"final_video")
        composer_instance.concatenate.side_effect = concat_mock
        mock_composer.return_value = composer_instance
        
        # YT Mock
        yt_instance = MagicMock()
        def yt_upload_mock(file_path, title, description, **kwargs):
            return "fake_yt_id"
        yt_instance.upload = yt_upload_mock
        mock_yt.return_value = yt_instance

        yield


@pytest.mark.asyncio
async def test_run_pipeline_youtube_shorts(
    mock_get_job,
    mock_transition_stage,
    mock_session_factory,
    mock_storage,
    mock_notify,
    mock_services
):
    """Test full pipeline for YouTube_Shorts model."""
    job = Job(id=1, topic="Test", business_model="YouTube_Shorts", attempt=0, status=JobStatus.QUEUED.value, stage=JobStage.QUEUED.value)
    mock_get_job.return_value = job
    
    ctx = {"redis": AsyncMock()}
    
    result = await run_pipeline(ctx, 1)
    
    assert result["status"] == "succeeded"
    assert result["publish_id"] == "fake_yt_id"
    assert result["business_model"] == "YouTube_Shorts"
    
    # Check that redis was called to pend processing
    ctx["redis"].sadd.assert_called_once_with("youtube:pending_processing", "1:fake_yt_id")
    
    # Check notifications
    mock_notify.assert_called_once()
    args, kwargs = mock_notify.call_args
    assert args[0] == "job.succeeded"


@pytest.mark.asyncio
async def test_run_pipeline_podcast_audio(
    mock_get_job,
    mock_transition_stage,
    mock_session_factory,
    mock_storage,
    mock_notify,
    mock_services
):
    """Test full pipeline for Podcast_Audio model."""
    job = Job(id=2, topic="Test", business_model="Podcast_Audio", attempt=0, status=JobStatus.QUEUED.value, stage=JobStage.QUEUED.value)
    mock_get_job.return_value = job
    
    ctx = {}
    
    result = await run_pipeline(ctx, 2)
    
    assert result["status"] == "succeeded"
    assert result["publish_id"] == "mock_audio_id"
    assert result["business_model"] == "Podcast_Audio"


@pytest.mark.asyncio
async def test_run_pipeline_seo_blog(
    mock_get_job,
    mock_transition_stage,
    mock_session_factory,
    mock_storage,
    mock_notify,
    mock_services
):
    """Test full pipeline for SEO_Blogs model."""
    job = Job(id=3, topic="Test", business_model="SEO_Blogs", attempt=0, status=JobStatus.QUEUED.value, stage=JobStage.QUEUED.value)
    mock_get_job.return_value = job
    
    ctx = {}
    
    result = await run_pipeline(ctx, 3)
    
    assert result["status"] == "succeeded"
    assert result["publish_id"] == "mock_text_id"
    assert result["business_model"] == "SEO_Blogs"
