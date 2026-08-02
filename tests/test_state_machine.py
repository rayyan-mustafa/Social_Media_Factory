"""Unit tests for job stage state machine."""

from src.domain import JobStage, next_stage


def test_stage_order_happy_path():
    stage = JobStage.QUEUED
    expected = [
        JobStage.SCRIPTING,
        JobStage.TTS,
        JobStage.MEDIA,
        JobStage.COMPOSING,
        JobStage.UPLOADING,
        JobStage.SUCCEEDED,
    ]
    for exp in expected:
        stage = next_stage(stage)
        assert stage == exp
    assert next_stage(JobStage.SUCCEEDED) is None


def test_failed_has_no_next():
    assert next_stage(JobStage.FAILED) is None
