"""API contract tests for job create models (no live Redis/Postgres)."""

from src.domain import JobCreate


def test_job_create_validation():
    body = JobCreate(topic="Ancient Rome", niche="history", idempotency_key="k1")
    assert body.topic == "Ancient Rome"
    assert body.upload_to_youtube is False


def test_job_create_rejects_short_topic():
    try:
        JobCreate(topic="ab")
        ok = True
    except Exception:
        ok = False
    assert ok is False
