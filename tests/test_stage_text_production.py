"""Tests for the text production stage."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.db import Job
from src.services.storage import ObjectStorage
from src.workers.stages import StageContext
from src.workers.stages.text_production import TextProductionStage


@pytest.fixture
def mock_storage():
    storage = MagicMock(spec=ObjectStorage)
    
    async def mock_download_json(job_id: str, filename: str):
        if filename == "script.json":
            return {
                "title": "My Awesome Book",
                "scenes": [
                    {"text": "Once upon a time."},
                    {"text": "The end."}
                ]
            }
        raise FileNotFoundError(f"{filename} not found")
        
    storage.download_json = AsyncMock(side_effect=mock_download_json)
    storage.upload_file = AsyncMock()
    return storage


@pytest.fixture
def stage_context(mock_storage, tmp_path: Path):
    job = Job(id=1, topic="Test Topic", business_model="EBooks_KDP")
    settings = Settings(environment="test")
    session = AsyncMock(spec=AsyncSession)
    
    ctx = StageContext(
        job=job,
        session=session,
        settings=settings,
        storage=mock_storage,
        work_dir=tmp_path
    )
    ctx.save_files = True
    return ctx


@pytest.mark.asyncio
async def test_text_production_stage_success(stage_context: StageContext):
    """Test successful execution of the text production stage."""
    
    stage = TextProductionStage()
    
    result = await stage.execute(stage_context, {})
    
    assert "markdown_path" in result
    assert "pdf_path" in result
    
    md_path = Path(result["markdown_path"])
    pdf_path = Path(result["pdf_path"])
    
    assert md_path.exists()
    assert pdf_path.exists()
    
    content = md_path.read_text(encoding="utf-8")
    assert "# My Awesome Book" in content
    assert "Once upon a time." in content
    assert "The end." in content
    
    # Check that both files were uploaded
    assert stage_context.storage.upload_file.call_count == 2
    calls = stage_context.storage.upload_file.mock_calls
    uploaded_files = [call[1][2] for call in calls]
    assert "content.md" in uploaded_files
    assert "content.pdf" in uploaded_files
