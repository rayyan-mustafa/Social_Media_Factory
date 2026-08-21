"""Tests for the publishing stage."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.db import Job
from src.services.youtube_uploader import YouTubeUploader
from src.services.storage import ObjectStorage
from src.workers.stages import StageContext
from src.workers.stages.publishing import PublishingStage


@pytest.fixture
def mock_storage():
    return MagicMock(spec=ObjectStorage)


@pytest.fixture
def mock_youtube_uploader():
    uploader = MagicMock(spec=YouTubeUploader)
    uploader.upload = MagicMock(return_value="mock_video_id")
    return uploader


@pytest.fixture
def stage_context(mock_storage, tmp_path: Path):
    job = Job(id=1, topic="Test Topic", business_model="YouTube_Shorts")
    settings = Settings(environment="test")
    session = AsyncMock(spec=AsyncSession)
    
    ctx = StageContext(
        job=job,
        session=session,
        settings=settings,
        storage=mock_storage,
        work_dir=tmp_path
    )
    return ctx


@pytest.mark.asyncio
async def test_publishing_stage_youtube(stage_context: StageContext, mock_youtube_uploader, tmp_path: Path):
    """Test publishing to YouTube."""
    stage = PublishingStage(youtube_uploader=mock_youtube_uploader)
    
    dummy_video = tmp_path / "final.mp4"
    dummy_video.write_bytes(b"video")
    
    artifacts = {"video_path": str(dummy_video)}
    result = await stage.execute(stage_context, artifacts)
    
    assert result["publish_id"] == "mock_video_id"
    assert result["platform"] == "youtube"
    
    mock_youtube_uploader.upload.assert_called_once()
    

@pytest.mark.asyncio
async def test_publishing_stage_audio(stage_context: StageContext, tmp_path: Path):
    """Test publishing audio model."""
    stage_context.job.business_model = "Podcast_Audio"
    # StageContext re-init is skipped, so we just set it
    stage_context.business_model = "Podcast_Audio"
    
    stage = PublishingStage()
    
    artifacts = {"master_audio_path": str(tmp_path / "final.wav")}
    result = await stage.execute(stage_context, artifacts)
    
    assert result["publish_id"] == "mock_audio_id"


@pytest.mark.asyncio
async def test_publishing_stage_text(stage_context: StageContext, tmp_path: Path):
    """Test publishing text model."""
    stage_context.job.business_model = "SEO_Blogs"
    stage_context.business_model = "SEO_Blogs"
    
    stage = PublishingStage()
    
    artifacts = {"markdown_path": str(tmp_path / "content.md")}
    result = await stage.execute(stage_context, artifacts)
    
    assert result["publish_id"] == "mock_text_id"
