"""Production path layout for the 9 business models."""

import pytest

from src.core.paths import artifact_key, job_prefix, model_folder, persists_files
from src.domain import BUSINESS_MODELS, DIRECT_UPLOAD_MODELS, JobCreate


def test_youtube_shorts_has_no_production_folder():
    assert not persists_files("YouTube_Shorts")
    assert "YouTube_Shorts" in DIRECT_UPLOAD_MODELS
    with pytest.raises(ValueError, match="direct upload"):
        job_prefix("YouTube_Shorts", 1)


def test_other_models_use_production_prefix():
    for model in BUSINESS_MODELS:
        if model in DIRECT_UPLOAD_MODELS:
            continue
        assert persists_files(model)
        assert model_folder(model) == f"production/{model}"
        assert job_prefix(model, 42) == f"production/{model}/job_42"
        assert artifact_key(model, 42, "final.mp4") == f"production/{model}/job_42/final.mp4"


def test_youtube_shorts_forces_upload_flag():
    body = JobCreate(topic="Ancient Rome", niche="history", business_model="YouTube_Shorts")
    assert body.upload_to_youtube is True


def test_non_youtube_default_no_upload():
    body = JobCreate(topic="Ancient Rome", niche="history", business_model="Podcast_Audio")
    assert body.upload_to_youtube is False
