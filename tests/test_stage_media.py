"""Tests for the media retrieval stage."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.db import Job
from src.services.media_retrieval import MediaRetrievalService
from src.services.storage import ObjectStorage
from src.workers.stages import StageContext
from src.workers.stages.media import MediaStage


@pytest.fixture
def mock_storage(tmp_path: Path):
    storage = MagicMock(spec=ObjectStorage)
    
    # Mock download_json to return a dummy script
    async def mock_download_json(job_id: str, filename: str):
        if filename == "script.json":
            return {
                "scenes": [
                    {"visual_prompt": "A beautiful sunset over mountains", "word_count": 12},
                    {"visual_prompt": "A calm lake", "word_count": 10},
                    {"visual_prompt": "", "word_count": 8}  # Empty prompt
                ]
            }
        raise FileNotFoundError(f"{filename} not found")
        
    storage.download_json = AsyncMock(side_effect=mock_download_json)
    storage.upload_file = AsyncMock()
    return storage


@pytest.fixture
def mock_media_service():
    service = MagicMock(spec=MediaRetrievalService)
    service.fetch_best_image_url = AsyncMock(return_value="http://example.com/image.jpg")
    
    # Mock synchronous download method to just create a dummy file
    def mock_download(url, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"dummy image data")
        return output_path
        
    def mock_placeholder(output_path, size=(1280, 720)):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"placeholder data")
        return output_path
        
    service.download_image = mock_download
    service.placeholder_image = mock_placeholder
    return service


@pytest.fixture
def stage_context(mock_storage, tmp_path: Path):
    job = Job(id=1, topic="Test Topic", business_model="YouTube_Shorts")
    settings = Settings(environment="test")
    session = AsyncMock(spec=AsyncSession)
    
    # Workaround for StageContext.__post_init__ which calls persists_files
    with patch("src.core.paths.persists_files", return_value=True):
        ctx = StageContext(
            job=job,
            session=session,
            settings=settings,
            storage=mock_storage,
            work_dir=tmp_path
        )
    return ctx


@pytest.mark.asyncio
async def test_media_stage_success(stage_context: StageContext, mock_media_service):
    """Test successful execution of the media stage."""
    stage = MediaStage(media_service=mock_media_service)
    
    result = await stage.execute(stage_context, {})
    
    assert "media_paths" in result
    media_paths = result["media_paths"]
    
    # Should be 3 scenes
    assert len(media_paths) == 3
    
    # Check that URLs were fetched for first two scenes, and topic fallback for third
    assert mock_media_service.fetch_best_image_url.call_count == 3
    calls = mock_media_service.fetch_best_image_url.mock_calls
    assert calls[0][1][0] == "A beautiful sunset over mountains"
    assert calls[1][1][0] == "A calm lake"
    assert calls[2][1][0] == "Test Topic"  # The fallback for empty visual_prompt
    
    # Check that files were uploaded to storage
    assert stage_context.storage.upload_file.call_count == 3


@pytest.mark.asyncio
async def test_media_stage_missing_script(stage_context: StageContext, mock_media_service):
    """Test behavior when script.json is missing."""
    stage_context.storage.download_json.side_effect = Exception("File not found in S3")
    
    stage = MediaStage(media_service=mock_media_service)
    
    with pytest.raises(RuntimeError, match="Failed to load script.json"):
        await stage.execute(stage_context, {})
