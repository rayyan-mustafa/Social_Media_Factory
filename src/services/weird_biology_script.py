"""Weird Human Biology beat-sheet script writer.

Default: Gemini via WaveSpeed. Models prefixed ``openrouter/`` use OpenRouterClient
(same pattern as the claim validator).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.services.llm import LLMClient
from src.services.openrouter import OpenRouterClient
from src.services.settings import Settings, get_settings, load_prompt, render_prompt
from src.services.tts_sanitize import spell_numbers_for_vo
from src.services.weird_biology_research import SourcePack

CHANNEL = "weird_human_biology"
PROMPTS_DIR = Path("config/prompts/weird_human_biology")
SCRIPT_MODEL_DEFAULT = "google/gemini-2.5-flash"

SECTION_ORDER = (
    "SECTION 1: COLD HOOK",
    "SECTION 2: CONTEXT & SETUP",
    "SECTION 3: CORE DATA & EXPERIMENT",
    "SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE",
    "SECTION 5: IMPACTFUL OUTRO",
)

# (key, min_words, max_words)
SECTION_BANDS: dict[str, tuple[int, int]] = {
    "SECTION 1: COLD HOOK": (90, 110),
    "SECTION 2: CONTEXT & SETUP": (180, 220),
    "SECTION 3: CORE DATA & EXPERIMENT": (450, 550),
    "SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE": (350, 400),
    "SECTION 5: IMPACTFUL OUTRO": (150, 200),
}

_SECTION_HEADER_RE = re.compile(
    r"^##\s*(SECTION\s+[1-5]:\s*[^\n]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_WORD_RE = re.compile(r"[A-Za-z0-9']+")


class WeirdBiologyScriptError(RuntimeError):
    pass


@dataclass
class SectionCheck:
    name: str
    words: int
    min_words: int
    max_words: int
    ok: bool
    detail: str = ""


@dataclass
class WeirdBiologyScriptResult:
    topic: str
    sections: dict[str, str]
    script_sections_md: str
    vo_raw: str
    band_checks: list[SectionCheck]
    model: str
    rewrite_attempted: bool = False  # always False; kept for meta/artifact compat
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def bands_ok(self) -> bool:
        return all(c.ok for c in self.band_checks)


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def check_section_bands(sections: dict[str, str]) -> list[SectionCheck]:
    checks: list[SectionCheck] = []
    for name in SECTION_ORDER:
        lo, hi = SECTION_BANDS[name]
        body = sections.get(name, "")
        n = word_count(body)
        ok = lo <= n <= hi
        detail = "" if ok else f"expected {lo}-{hi} words, got {n}"
        checks.append(
            SectionCheck(
                name=name, words=n, min_words=lo, max_words=hi, ok=ok, detail=detail
            )
        )
    return checks


def parse_script_output(raw: str) -> tuple[dict[str, str], str]:
    """Parse LLM output into section map + VO raw text."""
    text = (raw or "").strip()
    if not text:
        raise WeirdBiologyScriptError("empty script response")

    vo = ""
    sections_blob = text
    if "=== VO RAW ===" in text:
        sections_blob, vo = text.split("=== VO RAW ===", 1)
        vo = vo.strip()
    if "=== SCRIPT SECTIONS ===" in sections_blob:
        sections_blob = sections_blob.split("=== SCRIPT SECTIONS ===", 1)[1].strip()

    sections: dict[str, str] = {name: "" for name in SECTION_ORDER}
    # Normalize headers to canonical names
    matches = list(_SECTION_HEADER_RE.finditer(sections_blob))
    if not matches:
        # Fallback: try plain SECTION N headers without ##
        alt = re.compile(
            r"^(SECTION\s+[1-5]:\s*[^\n]+)\s*$", re.IGNORECASE | re.MULTILINE
        )
        matches = list(alt.finditer(sections_blob))
    if not matches:
        raise WeirdBiologyScriptError(
            "could not find SECTION headers in script output"
        )

    for i, m in enumerate(matches):
        raw_name = m.group(1).strip()
        canon = _canonicalize_section(raw_name)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(sections_blob)
        body = sections_blob[start:end].strip()
        if canon:
            sections[canon] = body

    missing = [n for n in SECTION_ORDER if not sections.get(n, "").strip()]
    if missing:
        raise WeirdBiologyScriptError(f"missing sections: {', '.join(missing)}")

    if not vo.strip():
        vo = "\n\n".join(sections[n] for n in SECTION_ORDER)

    return sections, vo.strip()


def _canonicalize_section(raw_name: str) -> str | None:
    upper = re.sub(r"\s+", " ", raw_name.strip().upper())
    for name in SECTION_ORDER:
        if upper.startswith(name.split(":")[0]):  # SECTION N
            # Match by number
            m = re.match(r"SECTION\s+(\d)", upper)
            m2 = re.match(r"SECTION\s+(\d)", name.upper())
            if m and m2 and m.group(1) == m2.group(1):
                return name
    return None


def format_script_sections_md(sections: dict[str, str]) -> str:
    parts: list[str] = []
    for name in SECTION_ORDER:
        parts.append(f"## {name}\n\n{sections[name].strip()}\n")
    return "\n".join(parts).strip() + "\n"


def _is_openrouter_model(model: str) -> bool:
    return model.strip().lower().startswith("openrouter/")


class WeirdBiologyScriptWriter:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        llm: LLMClient | None = None,
        openrouter: OpenRouterClient | None = None,
    ):
        self.settings = settings or get_settings()
        self.model = (model or SCRIPT_MODEL_DEFAULT).strip()
        self._llm = llm
        self._openrouter = openrouter

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.settings, default_model=self.model)
        return self._llm

    def _get_openrouter(self) -> OpenRouterClient:
        if self._openrouter is None:
            self._openrouter = OpenRouterClient(
                self.settings, default_model=self.model
            )
        return self._openrouter

    def generate(
        self,
        topic: str,
        source_pack: SourcePack,
    ) -> WeirdBiologyScriptResult:
        """Generate beat-sheet script once; band misses are reported only (no rewrite)."""
        template = load_prompt("script_beat_sheet.txt", prompts_dir=PROMPTS_DIR)
        source_material = source_pack.source_material_text()
        user_prompt = render_prompt(
            template,
            {"TOPIC": topic, "SOURCE_MATERIAL": source_material},
        )
        system = (
            "You write Weird Human Biology YouTube scripts. Follow the user "
            "beat-sheet and source-grounding rules exactly. Never invent studies. "
            "OUTPUT FORMAT IS MANDATORY: start with === SCRIPT SECTIONS === then "
            "exactly five markdown headers "
            "## SECTION 1: COLD HOOK, ## SECTION 2: CONTEXT & SETUP, "
            "## SECTION 3: CORE DATA & EXPERIMENT, "
            "## SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE, "
            "## SECTION 5: IMPACTFUL OUTRO — then === VO RAW === with the full VO."
        )
        # openrouter/free is flaky on headers; allow a second attempt like the validator
        max_attempts = 2 if _is_openrouter_model(self.model) else 1
        raw = ""
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            prompt = user_prompt
            if attempt > 1:
                prompt = (
                    user_prompt
                    + "\n\nREMINDER: Your previous reply omitted the required "
                    "## SECTION N: … headers. Re-emit the FULL script with "
                    "=== SCRIPT SECTIONS ===, all five ## SECTION headers, "
                    "and === VO RAW ===. No prose-only dump."
                )
            try:
                if _is_openrouter_model(self.model):
                    raw = self._get_openrouter().chat_text(
                        system=system,
                        user=prompt,
                        temperature=0.55,
                        model=self.model,
                        timeout_s=180.0,
                        reasoning_enabled=False,
                    )
                else:
                    raw = self._get_llm().chat_text(
                        system=system,
                        user=prompt,
                        temperature=0.55,
                        model=self.model,
                        timeout_s=180.0,
                    )
                sections, vo = parse_script_output(raw)
                break
            except WeirdBiologyScriptError as exc:
                last_err = exc
                if attempt >= max_attempts:
                    raise
                continue
        else:
            raise WeirdBiologyScriptError(
                f"script generation failed after {max_attempts} attempt(s): {last_err}"
            )

        # Spell numbers in all outputs
        for name in list(sections.keys()):
            sections[name] = spell_numbers_for_vo(sections[name])
        vo = spell_numbers_for_vo(vo)
        md = format_script_sections_md(sections)
        checks = check_section_bands(sections)

        return WeirdBiologyScriptResult(
            topic=topic,
            sections=sections,
            script_sections_md=md,
            vo_raw=vo,
            band_checks=checks,
            model=self.model,
            rewrite_attempted=False,
            meta={
                "source_usable_count": len(source_pack.usable_items()),
                "raw_chars": len(raw),
            },
        )
