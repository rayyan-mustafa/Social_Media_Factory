"""Hook generator validation + pick (no live LLM)."""

from __future__ import annotations

from src.services.hook_generator import pick_best_hook, validate_hook


def test_validate_rejects_banned_and_long():
    ok, reason = validate_hook(
        "In this video we discuss Tudor politics. It was wild. Or was it?"
    )
    assert ok is False
    assert reason and "banned" in reason

    long = "Word " * 30 + ". Next sentence is fine. And another?"
    ok2, reason2 = validate_hook(long.strip())
    assert ok2 is False


def test_validate_accepts_netflix_style():
    hook = (
        "She waited for a pardon that never came. "
        "The man who loved her signed the warrant. "
        "So what actually changed?"
    )
    ok, reason = validate_hook(hook)
    assert ok is True
    assert reason is None


def test_pick_best_prefers_historian_pattern():
    variants = [
        {
            "hook": "A. B. C?",
            "pattern_used": "ticking_clock",
            "passed_validation": True,
            "rejection_reason": None,
        },
        {
            "hook": "Nobody knew her name. That was the point. Too late?",
            "pattern_used": "identity_withheld",
            "passed_validation": True,
            "rejection_reason": None,
        },
    ]
    # Fix first to pass sentence rules
    variants[0]["hook"] = "She had nine days left. She had no idea. Who built the trap?"
    best = pick_best_hook(variants, channel="napping_historian")
    assert best and best["pattern_used"] == "identity_withheld"
