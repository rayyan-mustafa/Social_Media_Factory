"""Gate R enforce flag must honor live .env over stale process env."""

from __future__ import annotations

from pathlib import Path

from src.services.gate_r_env import dotenv_value, gate_r_enforce_enabled


def test_gate_r_enforce_prefers_dotenv_over_stale_process_env(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GATE_R_ENFORCE=false\n", encoding="utf-8")
    # Simulate long-lived farm PID started when enforce was on.
    monkeypatch.setenv("GATE_R_ENFORCE", "true")

    assert dotenv_value("GATE_R_ENFORCE", env_file=env_file) == "false"
    assert gate_r_enforce_enabled(env_file=env_file) is False
    # Synced into process env for later os.getenv readers.
    import os

    assert os.environ["GATE_R_ENFORCE"] == "false"


def test_gate_r_enforce_true_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GATE_R_ENFORCE=true\n", encoding="utf-8")
    monkeypatch.setenv("GATE_R_ENFORCE", "false")

    assert gate_r_enforce_enabled(env_file=env_file) is True


def test_gate_r_enforce_falls_back_to_process_env_when_absent(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("GATE_R_ENFORCE", "true")

    assert gate_r_enforce_enabled(env_file=env_file) is True
