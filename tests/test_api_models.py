"""API model and auth helpers."""

import pytest
from pydantic import ValidationError

from src.domain import BUSINESS_MODELS, JobCreate, JobRead, JobStage, JobStatus


def test_job_create_includes_business_model():
    body = JobCreate(topic="Ancient Rome", niche="history", business_model="YouTube_Shorts")
    assert body.business_model == "YouTube_Shorts"
    assert body.upload_to_youtube is True


def test_job_create_rejects_short_topic():
    with pytest.raises(ValidationError):
        JobCreate(topic="ab")


def test_job_create_rejects_unknown_model():
    with pytest.raises(ValidationError):
        JobCreate(topic="Valid topic here", business_model="Not_A_Real_Model")


def test_all_factory_models_accepted():
    for model in BUSINESS_MODELS:
        body = JobCreate(topic="Valid topic here", business_model=model)
        assert body.business_model == model


def test_job_read_requires_business_model():
    row = JobRead(
        id=1,
        topic="t",
        niche="n",
        business_model="YouTube_Shorts",
        status=JobStatus.QUEUED,
        stage=JobStage.QUEUED,
        attempt=0,
        idempotency_key=None,
        error=None,
        cost_cents=0,
    )
    assert row.business_model == "YouTube_Shorts"
