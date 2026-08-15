"""Gold thumbnail text must track the current job title (no sticky SHE SURVIVED)."""

from __future__ import annotations

from src.domain.models import Outline, OutlineChapter, Scene, ScriptResult, ScriptValidation
from src.services.thumbnail_prompt import (
    _extract_gold_phrase,
    _gold_shares_title_keywords,
    _pick_gold_text,
    _shortened_title_words,
    build_thumbnail_prompt,
)


def _script(title: str, *, hook: str = "", topic: str | None = None) -> ScriptResult:
    topic = topic or title
    return ScriptResult(
        topic=topic,
        title=title,
        hook=hook or f"Opening beat for {title}",
        outline=Outline(
            title=title,
            chapters=[OutlineChapter(id=1, title="c1", target_sentences=1)],
        ),
        scenes=[Scene(index=0, text="narration survived historically in archives.", visual_prompt="portrait")],
        validation=ScriptValidation(
            ok=True,
            scene_count=1,
            min_scenes=1,
            max_scenes=12,
            target_scenes=5,
            estimated_duration_s=10.0,
        ),
    )


def test_gold_text_differs_for_two_titles_and_matches_keywords():
    t1 = "What If the Mongol Empire Reached the Atlantic Coast?"
    t2 = "What If the Roman Empire Never Fell?"
    g1, _ = _pick_gold_text(_script(t1), t1.lower() + " survived spared", None)
    g2, _ = _pick_gold_text(_script(t2), t2.lower() + " survived outlived", None)
    p1 = _extract_gold_phrase(g1)
    p2 = _extract_gold_phrase(g2)
    assert p1 != p2, (p1, p2)
    assert "SHE SURVIVED" not in p1
    assert "SHE SURVIVED" not in p2
    assert _gold_shares_title_keywords(p1, t1)
    assert _gold_shares_title_keywords(p2, t2)


def test_sticky_she_survived_rejected_by_guard_fallback():
    title = "What If Anne Boleyn Became Regent?"
    # Even with survival noise in corpus, text must track title (regent), not SHE SURVIVED.
    line, conf = _pick_gold_text(_script(title), "anne boleyn survived execution spared", "anne_boleyn")
    phrase = _extract_gold_phrase(line)
    assert "SHE SURVIVED" not in phrase
    assert _gold_shares_title_keywords(phrase, title)
    assert len(phrase.split()) <= 5
    assert conf >= 0.7


def test_shortened_title_words_cap():
    words = _shortened_title_words(
        "What If the Ottoman Empire Consolidated Control of the Mediterranean Trade Routes?",
        max_words=5,
    )
    assert 1 <= len(words.split()) <= 5


def test_build_rules_mode_no_she_survived_for_unrelated_title():
    title = "What If the Tudor Dynasty Fell in 1549?"
    result = build_thumbnail_prompt(_script(title), seed_title=title, mode="rules")
    assert "SHE SURVIVED" not in result.prompt
    assert result.slots is not None
    phrase = _extract_gold_phrase(result.slots.gold_text)
    assert _gold_shares_title_keywords(phrase, title)
