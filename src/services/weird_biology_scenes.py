"""Phrase visual timeline for Weird Biology (visual beats only — not TTS scenes)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.services.weird_biology_style import load_visual_config

_SENT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_SECTION_RE = re.compile(
    r"^##\s*(SECTION\s+(\d+)\s*:[^\n]*)\s*$", re.IGNORECASE | re.MULTILINE
)


@dataclass
class VisualBeat:
    index: int
    text: str
    section: int
    section_name: str
    start_s: float
    end_s: float
    word_count: int

    @property
    def duration_s(self) -> float:
        return max(0.05, self.end_s - self.start_s)


@dataclass
class VisualTimeline:
    beats: list[VisualBeat] = field(default_factory=list)
    full_vo_duration_s: float = 0.0
    topic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "full_vo_duration_s": self.full_vo_duration_s,
            "beat_count": len(self.beats),
            "beats": [asdict(b) for b in self.beats],
        }


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _split_phrases(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    # Prefer sentence splits; if a sentence is huge, pack by ~8 words
    raw = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    out: list[str] = []
    for sent in raw:
        words = _WORD_RE.findall(sent)
        if len(words) <= 12:
            out.append(sent)
            continue
        # chunk ~8 words keeping original substrings roughly
        parts = re.findall(r"\S+\s*", sent)
        buf: list[str] = []
        wc = 0
        for part in parts:
            buf.append(part)
            wc += 1 if _WORD_RE.search(part) else 0
            if wc >= 8:
                out.append("".join(buf).strip())
                buf, wc = [], 0
        if buf:
            out.append("".join(buf).strip())
    return [p for p in out if p]


def parse_sections_map(script_sections_md: str, vo_raw: str) -> list[tuple[int, str, str]]:
    """Return list of (section_num, section_name, text) covering vo content.

    Falls back to whole VO as section 1 if headers missing.
    """
    md = script_sections_md or ""
    matches = list(_SECTION_RE.finditer(md))
    if not matches:
        return [(1, "SECTION 1", (vo_raw or "").strip())]

    sections: list[tuple[int, str, str]] = []
    for i, m in enumerate(matches):
        num = int(m.group(2))
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[start:end].strip()
        # Strip markdown noise
        body = re.sub(r"^#+\s*", "", body, flags=re.M).strip()
        sections.append((num, name, body))
    return sections


def build_visual_timeline(
    vo_raw: str,
    *,
    script_sections_md: str = "",
    full_vo_duration_s: float,
    topic: str = "",
) -> VisualTimeline:
    cfg = load_visual_config()
    timing = cfg.get("timing") or {}
    t_min = float(timing.get("target_beat_s_min") or 2.0)
    t_max = float(timing.get("target_beat_s_max") or 4.0)
    wpm = float(timing.get("voice_wpm") or 150.0)
    duration = max(0.5, float(full_vo_duration_s))

    section_blocks = parse_sections_map(script_sections_md, vo_raw)
    # Build phrase list with section tags
    tagged: list[tuple[int, str, str]] = []
    for num, name, body in section_blocks:
        phrases = _split_phrases(body) or ([body] if body.strip() else [])
        for ph in phrases:
            tagged.append((num, name, ph))

    if not tagged:
        phrases = _split_phrases(vo_raw) or [vo_raw.strip() or "…"]
        tagged = [(1, "SECTION 1", p) for p in phrases]

    # Merge tiny phrases toward ~t_min–t_max using word→time estimate
    sec_per_word = 60.0 / max(1.0, wpm)
    merged: list[tuple[int, str, str]] = []
    buf_num, buf_name, buf_text = tagged[0][0], tagged[0][1], tagged[0][2]
    for num, name, ph in tagged[1:]:
        est = word_count(buf_text) * sec_per_word
        if num == buf_num and est < t_min:
            buf_text = f"{buf_text} {ph}".strip()
        else:
            merged.append((buf_num, buf_name, buf_text))
            buf_num, buf_name, buf_text = num, name, ph
    merged.append((buf_num, buf_name, buf_text))

    # If a merged phrase is still huge (> t_max*2), leave it — renderer holds longer

    weights = [max(1, word_count(t)) for _, _, t in merged]
    total_w = sum(weights) or 1
    beats: list[VisualBeat] = []
    t = 0.0
    for i, ((num, name, text), w) in enumerate(zip(merged, weights)):
        if i == len(merged) - 1:
            end = duration
        else:
            end = t + duration * (w / total_w)
        # Clamp individual beat toward band when possible without breaking coverage
        raw_dur = end - t
        if i < len(merged) - 1 and raw_dur > t_max * 2.5:
            # allow long; no further split here (already phrase-split)
            pass
        beats.append(
            VisualBeat(
                index=i,
                text=text,
                section=num,
                section_name=name,
                start_s=round(t, 3),
                end_s=round(end, 3),
                word_count=word_count(text),
            )
        )
        t = end

    # Snap last end exactly
    if beats:
        beats[-1].end_s = round(duration, 3)

    return VisualTimeline(
        beats=beats, full_vo_duration_s=duration, topic=topic or ""
    )


def save_visual_timeline(tl: VisualTimeline, path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tl.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def ensure_core_data_xray_slot(tl: VisualTimeline) -> int | None:
    """Return a beat index in section 3 suitable for x-ray (middle-ish)."""
    sec3 = [b for b in tl.beats if b.section == 3]
    if not sec3:
        return None
    return sec3[len(sec3) // 2].index
