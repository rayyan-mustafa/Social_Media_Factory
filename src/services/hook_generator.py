"""Netflix-doc few-shot hook generator + banned-phrase filter + reject log."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR
from src.services.llm import LLMClient, LLMError, parse_json_object
from src.services.settings import Settings, get_settings

HOOK_REJECTS = OPS_DIR / "hook_rejects.jsonl"
HOOK_RATINGS = OPS_DIR / "hook_ratings.jsonl"

BANNED_PHRASES = (
    "today we discuss",
    "in this video",
    "let's explore",
    "lets explore",
    "let's talk about",
    "lets talk about",
    "welcome back",
    "in this episode",
)

HOOK_SYSTEM = """You are a documentary hook writer trained in the style of Netflix true-crime and history documentaries (Making a Murderer, The Crown, Bad Blood, Dirty Money).

RULES — violating any of these makes the hook unusable:
1. NEVER start chronologically. Open on the most emotionally charged or shocking moment, even if it happens at the END of the story.
2. NEVER use these banned phrases: "today we discuss," "in this video," "let's explore," "let's talk about," "welcome back," "in this episode."
3. ALWAYS end the hook on an unresolved question or tension — the viewer must feel something is incomplete.
4. Prefer withholding a name/identity over stating it upfront, when it creates curiosity.
5. Use short, punchy sentences. Maximum 4 sentences per hook. No sentence over 20 words.

STRUCTURE TO FOLLOW:
Sentence 1: The most shocking/emotional moment from the story (not the start of the timeline).
Sentence 2: A consequence or stakes statement that raises the emotional temperature.
Sentence 3: A question OR a false-assumption reversal ("Everyone believes X. They're wrong.").
Sentence 4 (optional): A one-line bridge into the story, WITHOUT resolving the tension.

FEW-SHOT EXAMPLES (study the pattern, don't copy content):

Example 1 (execution/betrayal pattern):
"In 1536, she waited in a room in the Tower of London, certain she'd be pardoned. She wasn't. Six years earlier, the same man who signed her death warrant had broken from Rome just to marry her. So what actually changed?"

Example 2 (identity withheld pattern):
"Nobody suspected the quiet lady-in-waiting. That was exactly the point. By the time the court realized what she'd done, it was already too late. This is the story historians almost missed entirely."

Example 3 (false assumption pattern):
"Everyone assumes the king wanted her dead from the start. He didn't. For years, he protected her from the very people who eventually convinced him to sign the order. The real villain wasn't who you think."

Example 4 (countdown/ticking clock pattern):
"She had nineteen days left, and she had no idea. While she planned her future, the men around her were already building a case to end it. History remembers the execution. It rarely asks who built the trap."
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def validate_hook(hook: str) -> tuple[bool, str | None]:
    low = hook.lower()
    for ban in BANNED_PHRASES:
        if ban in low:
            return False, f"banned phrase: {ban}"
    sents = _sentences(hook)
    if len(sents) < 2 or len(sents) > 5:
        return False, f"sentence count {len(sents)} not in 2..5"
    for s in sents:
        if len(s.split()) > 25:
            return False, "sentence exceeds 25 words"
    return True, None


def _parse_hooks_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("hooks", "variants", "items"):
            if isinstance(raw.get(key), list):
                return [x for x in raw[key] if isinstance(x, dict)]
        if "hook" in raw:
            return [raw]
    return []


def generate_hooks(
    transcript: str,
    topic: str,
    num_variants: int = 3,
    *,
    settings: Settings | None = None,
    channel: str | None = None,
    llm: LLMClient | None = None,
) -> list[dict[str, Any]]:
    """Generate + validate hook variants. Logs rejects to hook_rejects.jsonl."""
    settings = settings or get_settings()
    client = llm or LLMClient(settings, default_model=settings.llm_expand_model)
    n = max(1, min(int(num_variants), 6))
    user = (
        f'Now generate {n} new hooks for the topic: "{topic}"\n'
        f"Base the factual content strictly on this transcript, do not invent facts:\n"
        f"{transcript[:12000]}\n\n"
        'Output ONLY a JSON object: {"hooks":[{"hook":"...","pattern_used":'
        '"execution/betrayal|identity_withheld|false_assumption|ticking_clock|other"}]}'
    )
    try:
        raw = client.chat_json(
            system=HOOK_SYSTEM,
            user=user,
            temperature=0.85,
        )
    except LLMError:
        try:
            text = client.chat_text(
                system=HOOK_SYSTEM,
                user=user + "\n\noutput ONLY valid JSON, no markdown fences.",
                temperature=0.85,
            )
            raw = parse_json_object(text)
        except Exception as exc:  # noqa: BLE001
            _append_jsonl(
                HOOK_REJECTS,
                {
                    "ts": _now(),
                    "topic": topic,
                    "channel": channel,
                    "error": str(exc)[:300],
                    "rejection_reason": "llm_failed",
                },
            )
            return []

    items = _parse_hooks_payload(raw)

    results: list[dict[str, Any]] = []
    for item in items:
        hook = str(item.get("hook") or "").strip()
        pattern = str(item.get("pattern_used") or "other").strip()
        ok, reason = validate_hook(hook) if hook else (False, "empty")
        row = {
            "hook": hook,
            "pattern_used": pattern,
            "passed_validation": ok,
            "rejection_reason": reason,
        }
        if not ok:
            _append_jsonl(
                HOOK_REJECTS,
                {"ts": _now(), "topic": topic, "channel": channel, **row},
            )
        results.append(row)
    return results


def pick_best_hook(
    variants: list[dict[str, Any]],
    *,
    channel: str | None = None,
    preferred_patterns: list[str] | None = None,
) -> dict[str, Any] | None:
    """Pick first passing variant; prefer channel patterns when provided."""
    passed = [v for v in variants if v.get("passed_validation") and v.get("hook")]
    if not passed:
        return None
    prefs = preferred_patterns
    if prefs is None:
        ch = (channel or "").strip().lower()
        if ch == "napping_historian":
            prefs = ["identity_withheld", "other", "false_assumption"]
        else:
            prefs = ["ticking_clock", "false_assumption", "execution/betrayal", "other"]
    for pat in prefs:
        for v in passed:
            if pat in str(v.get("pattern_used") or "").lower().replace(" ", "_"):
                return v
            if pat.replace("_", "/") in str(v.get("pattern_used") or "").lower():
                return v
    return passed[0]


def apply_hook_to_cold_open_text(script_text: str, hook: str) -> str:
    """Prepend hook block if not already present."""
    hook = hook.strip()
    if not hook:
        return script_text
    if hook[:40] in script_text[:500]:
        return script_text
    return f"{hook}\n\n{script_text.lstrip()}"
