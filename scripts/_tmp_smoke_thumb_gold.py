#!/usr/bin/env python3
"""Extra smoke: Anne / Roman titles must not sticky SHE SURVIVED."""

from src.domain.models import Outline, OutlineChapter, Scene, ScriptResult, ScriptValidation
from src.services.thumbnail_prompt import (
    build_thumbnail_prompt,
    _extract_gold_phrase,
    _gold_shares_title_keywords,
)


def script(title: str) -> ScriptResult:
    return ScriptResult(
        topic=title,
        title=title,
        hook=title,
        outline=Outline(
            title=title,
            chapters=[OutlineChapter(id=1, title="c1", target_sentences=1)],
        ),
        scenes=[Scene(index=0, text="she survived historically", visual_prompt="x")],
        validation=ScriptValidation(
            ok=True,
            scene_count=1,
            min_scenes=1,
            max_scenes=12,
            target_scenes=5,
            estimated_duration_s=10.0,
        ),
    )


def main() -> None:
    titles = [
        "What If Anne Boleyn's Son Had Survived?",
        "What If Anne Boleyn Became Regent?",
        "What If the Roman Empire Never Fell?",
    ]
    phrases = []
    for t in titles:
        r = build_thumbnail_prompt(script(t), seed_title=t, mode="rules")
        p = _extract_gold_phrase(r.slots.gold_text)
        assert "SHE SURVIVED" not in p, (t, p)
        assert _gold_shares_title_keywords(p, t), (t, p)
        phrases.append(p)
        print(f"{t!r} -> {p}")
    assert len(set(phrases)) >= 2
    print("EXTRA_SMOKE_OK")


if __name__ == "__main__":
    main()
