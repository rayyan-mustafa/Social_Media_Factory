"""Tests for the composing stage."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.db import Job
from src.services.composer import VideoComposer
from src.services.storage import ObjectStorage
from src.workers.stages import StageContext
from src.workers.stages.composing import ComposingStage


@pytest.fixture
def mock_storage():
    storage = MagicMock(spec=ObjectStorage)
    storage.upload_file = AsyncMock()
    return storage


@pytest.fixture
def mock_composer():
    composer = MagicMock(spec=VideoComposer)
    
    def mock_create_scene(image_path, audio_path, output_path, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"dummy video data")
        return output_path
        
    def mock_concat(scene_paths, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"final video data")
        return output_path
        
    composer.create_scene_video = MagicMock(side_effect=mock_create_scene)
    composer.concatenate = MagicMock(side_effect=mock_concat)
    return composer


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
    # Patch save_files since it dynamically resolves via persistence rules
    ctx.save_files = True
    return ctx


@pytest.mark.asyncio
async def test_composing_stage_success(stage_context: StageContext, mock_composer, tmp_path: Path):
    """Test successful execution of the composing stage."""
    
    # Create dummy input files
    audio_paths = []
    media_paths = []
    for i in range(2):
        ap = tmp_path / f"scene_{i}.wav"
        ap.write_bytes(b"audio")
        audio_paths.append(ap)
        
        mp = tmp_path / f"scene_{i}.jpg"
        mp.write_bytes(b"image")
        media_paths.append(mp)
        
    stage = ComposingStage(composer=mock_composer)
    result = await stage.execute(stage_context, {"audio_paths": audio_paths, "media_paths": media_paths})
    
    assert "video_path" in result
    assert "scene_videos" in result
    assert len(result["scene_videos"]) == 2
    
    # Check that composer methods were called
    assert mock_composer.create_scene_video.call_count == 2
    assert mock_composer.concatenate.call_count == 1
    
    # Check that final video was uploaded
    stage_context.storage.upload_file.assert_called_once()
    assert stage_context.storage.upload_file.call_args[0][2] == "final.mp4"
