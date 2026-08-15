"""Phoneme-aware sentence packing for Kokoro hybrid TTS.

Outer strategy: synthesize per chapter (or ~60–90s groups).
Inner strategy: if chapter text exceeds ~510 phonemes, split ONLY at
sentence endings (. ? !) — never at commas/semicolons — then call
create(..., trim=False) per batch.

Kokoro-onnx's default splitter breaks at ``.,!?;`` and with trim=True can
clip words at batch boundaries; this module avoids that path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Leave headroom under kokoro-onnx MAX_PHONEME_LENGTH (510).
DEFAULT_MAX_PHONEMES = 500

# Conservative fallback when Tokenizer is unavailable (~phonemes per word).
_FALLBACK_PHONEMES_PER_WORD = 8.0

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"\S+")

_tokenizer = None
_tokenizer_failed = False


def _get_tokenizer():
    """Lazy-load kokoro_onnx Tokenizer for real phoneme counts."""
    global _tokenizer, _tokenizer_failed
    if _tokenizer is not None or _tokenizer_failed:
        return _tokenizer
    try:
        from kokoro_onnx.tokenizer import Tokenizer

        _tokenizer = Tokenizer()
    except Exception as exc:  # pragma: no cover - env without kokoro
        logger.warning("kokoro Tokenizer unavailable; using word heuristic: %s", exc)
        _tokenizer_failed = True
        _tokenizer = None
    return _tokenizer


def estimate_phoneme_count(text: str, *, lang: str = "en-us") -> int:
    """Return phoneme length for ``text`` (kokoro Tokenizer, else heuristic)."""
    text = (text or "").strip()
    if not text:
        return 0
    tok = _get_tokenizer()
    if tok is not None:
        try:
            return len(tok.phonemize(text, lang=lang))
        except Exception as exc:  # pragma: no cover
            logger.warning("phonemize failed; falling back: %s", exc)
    words = len(re.findall(r"\b\w+(?:['’]\w+)?\b", text))
    return max(1, int(round(words * _FALLBACK_PHONEMES_PER_WORD)))


def split_sentences(text: str) -> list[str]:
    """Split on sentence endings (. ? !) only — never commas/semicolons."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _force_split_oversized(
    sentence: str,
    *,
    max_phonemes: int,
    phoneme_len: Callable[[str], int],
) -> list[str]:
    """Last resort: pack words when a single sentence exceeds the phoneme cap."""
    words = _WORD_RE.findall(sentence)
    if not words:
        return [sentence] if sentence.strip() else []
    batches: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = (" ".join(cur + [w])).strip()
        if cur and phoneme_len(trial) > max_phonemes:
            batches.append(" ".join(cur))
            cur = [w]
            if phoneme_len(w) > max_phonemes:
                logger.warning(
                    "single word exceeds phoneme cap (%d): %r",
                    phoneme_len(w),
                    w[:40],
                )
                batches.append(w)
                cur = []
        else:
            cur.append(w)
    if cur:
        batches.append(" ".join(cur))
    return batches


def pack_sentences_by_phoneme_limit(
    text: str,
    *,
    max_phonemes: int = DEFAULT_MAX_PHONEMES,
    lang: str = "en-us",
    phoneme_len: Callable[[str], int] | None = None,
) -> list[str]:
    """Pack sentences into batches each ≤ ``max_phonemes``.

    Splits only at sentence endings. Never at commas/semicolons.
    If one sentence alone exceeds the cap, falls back to word packing.
    """
    if max_phonemes < 1:
        raise ValueError("max_phonemes must be >= 1")

    counter = phoneme_len or (lambda t: estimate_phoneme_count(t, lang=lang))
    sentences = split_sentences(text)
    if not sentences:
        return []

    batches: list[str] = []
    current: list[str] = []

    def current_text() -> str:
        return " ".join(current).strip()

    for sent in sentences:
        sent_len = counter(sent)
        if sent_len > max_phonemes:
            if current:
                batches.append(current_text())
                current = []
            logger.warning(
                "sentence exceeds phoneme cap (%d > %d); word-splitting",
                sent_len,
                max_phonemes,
            )
            batches.extend(
                _force_split_oversized(
                    sent, max_phonemes=max_phonemes, phoneme_len=counter
                )
            )
            continue

        if not current:
            current = [sent]
            continue

        trial = f"{current_text()} {sent}".strip()
        if counter(trial) <= max_phonemes:
            current.append(sent)
        else:
            batches.append(current_text())
            current = [sent]

    if current:
        batches.append(current_text())
    return batches


@dataclass(frozen=True)
class ChapterChunk:
    """Outer TTS unit: one continuous Kokoro pass (may inner-split)."""

    chapter_id: int
    chunk_index: int
    scene_indices: list[int]
    text: str


def group_scenes_into_chapter_chunks(
    scenes: list,
    *,
    target_chunk_words: int | None = None,
) -> list[ChapterChunk]:
    """Group scenes by ``chapter_id``, optionally sub-chunk by word budget.

    ``target_chunk_words`` ≈ words for a ~60–90s listen (e.g. 180–220 at
    ~160 WPM). When None, one chunk per chapter_id.
    """
    if not scenes:
        return []

    by_chapter: dict[int, list] = {}
    order: list[int] = []
    for scene in scenes:
        ch = int(getattr(scene, "chapter_id", None) or 1)
        if ch not in by_chapter:
            by_chapter[ch] = []
            order.append(ch)
        by_chapter[ch].append(scene)

    chunks: list[ChapterChunk] = []
    for ch in order:
        group = by_chapter[ch]
        if target_chunk_words is None or target_chunk_words <= 0:
            text = " ".join((s.text or "").strip() for s in group if (s.text or "").strip())
            chunks.append(
                ChapterChunk(
                    chapter_id=ch,
                    chunk_index=0,
                    scene_indices=[int(s.index) for s in group],
                    text=text,
                )
            )
            continue

        buf: list = []
        words = 0
        sub_i = 0

        def flush(buf_scenes: list, sub: int) -> ChapterChunk:
            return ChapterChunk(
                chapter_id=ch,
                chunk_index=sub,
                scene_indices=[int(s.index) for s in buf_scenes],
                text=" ".join(
                    (s.text or "").strip() for s in buf_scenes if (s.text or "").strip()
                ),
            )

        for scene in group:
            wc = int(getattr(scene, "word_count", 0) or 0)
            if wc <= 0:
                wc = len(re.findall(r"\b\w+(?:['’]\w+)?\b", scene.text or ""))
            if buf and words + wc > target_chunk_words:
                chunks.append(flush(buf, sub_i))
                sub_i += 1
                buf = [scene]
                words = wc
            else:
                buf.append(scene)
                words += wc
        if buf:
            chunks.append(flush(buf, sub_i))

    return chunks
