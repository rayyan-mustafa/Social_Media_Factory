"""Tests for the audio production stage."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.db import Job
from src.services.storage import ObjectStorage
from src.workers.stages import StageContext
from src.workers.stages.audio_production import AudioProductionStage


@pytest.fixture
def mock_storage():
    storage = MagicMock(spec=ObjectStorage)
    storage.upload_file = AsyncMock()
    return storage


@pytest.fixture
def stage_context(mock_storage, tmp_path: Path):
    job = Job(id=1, topic="Test Topic", business_model="Podcast_Audio")
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
async def test_audio_production_stage_success(stage_context: StageContext, tmp_path: Path):
    """Test successful execution of the audio production stage."""
    
    # Create valid dummy audio files
    audio_paths = []
    samplerate = 22050
    for i in range(2):
        ap = tmp_path / f"scene_{i}.wav"
        # Generate 0.1s of random noise
        data = np.random.uniform(-1, 1, int(samplerate * 0.1)).astype(np.float32)
        sf.write(str(ap), data, samplerate)
        audio_paths.append(ap)
        
    stage = AudioProductionStage()
    
    result = await stage.execute(stage_context, audio_paths=audio_paths)
    
    assert "master_audio_path" in result
    
    master_path = Path(result["master_audio_path"])
    assert master_path.exists()
    
    # Check that length is exactly 0.2s
    data, sr = sf.read(str(master_path))
    assert sr == samplerate
    assert len(data) == int(samplerate * 0.2)
    
    # Check that it was uploaded
    stage_context.storage.upload_file.assert_called_once()
    assert stage_context.storage.upload_file.call_args[0][2] == "final.wav"
