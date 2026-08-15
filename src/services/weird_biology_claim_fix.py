"""Surgical claim-fix for Weird Human Biology scripts after failed validation.

Same LLM stack as the validator: openrouter/free ×2, then
google/gemini-2.5-flash-lite via WaveSpeed. Only edits unverified claims.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from src.services.llm import LLMClient
from src.services.openrouter import OpenRouterClient
from src.services.settings import Settings, get_settings
from src.services.tts_sanitize import spell_numbers_for_vo
from src.services.whb_llm_router import (
    STAGE_CLAIM_FIX,
    WhbFreeFirstRouter,
    WhbLlmRouterError,
)
from src.services.weird_biology_research import SourcePack
from src.services.weird_biology_script import (
    format_script_sections_md,
    parse_script_output,
)
from src.services.weird_biology_validate import (
    FALLBACK_MODEL,
    OPENROUTER_MODEL,
)

_SOURCE_NEEDED_RE = re.compile(r"\[SOURCE NEEDED:[^\]]*\]", re.IGNORECASE)

CLAIM_FIX_SYSTEM = (
    "You are a surgical claim fixer. Edit only the listed unverified claims "
    "in the script. Do not rewrite sections, restyle, or change verified prose."
)

CLAIM_FIX_USER_TEMPLATE = """You are a surgical claim fixer. You will be given a SCRIPT,
SOURCE MATERIAL, and a list of UNVERIFIED CLAIMS from a fact-check report.

TASK:
Fix ONLY the unverified claims below. For each one:
- If SOURCE MATERIAL supports a corrected wording, replace that claim's
  wording with source-supported language (minimal edit).
- If sources truly lack the fact, replace that claim with
  `[SOURCE NEEDED: brief description of the missing fact]`.

RULES (critical):
- Only change the specific unverified claims — do NOT rewrite whole sections.
- Do not restyle, do not change verified sentences, rhythm, or surrounding
  prose except the minimal edit needed for that claim.
- Do not invent studies, numbers, names, or dates not in SOURCE MATERIAL.
- Keep the same section structure (SECTION 1–5).
- Return the FULL updated script in exactly this format:

=== SCRIPT SECTIONS ===
## SECTION 1: COLD HOOK
...
## SECTION 2: CONTEXT & SETUP
...
## SECTION 3: CORE DATA & EXPERIMENT
...
## SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE
...
## SECTION 5: IMPACTFUL OUTRO
...

=== VO RAW ===
(full narration text, all sections concatenated)

UNVERIFIED CLAIMS TO FIX:
{unverified_claims}

SCRIPT:
{script_output}

SOURCE MATERIAL:
{source_material}
"""

# Markdown table row: | Claim | Yes/No | ... |
_TABLE_CLAIM_RE = re.compile(
    r"^\s*\|\s*(?P<claim>[^|\n]+?)\s*\|\s*(?P<found>Yes|No)\b",
    re.IGNORECASE | re.MULTILINE,
)
_HEADERISH = re.compile(
    r"^(claim|found(\s+in\s+source)?|source\s+quote|risk(\s+level)?|-+:?)$",
    re.IGNORECASE,
)


class WeirdBiologyClaimFixError(RuntimeError):
    pass


@dataclass
class ClaimFixResult:
    script_sections_md: str
    vo_raw: str
    model_used: str
    claims_targeted: list[str]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    raw_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_unverified_claims(
    report_md: str,
    *,
    script: str | None = None,
) -> list[str]:
    """Pull unverified claim strings from a validation report (+ optional script)."""
    claims: list[str] = []
    seen: set[str] = set()

    def _add(item: str) -> None:
        text = (item or "").strip()
        if not text:
            return
        # Skip separator / header cells
        if _HEADERISH.match(text.strip("-: ")):
            return
        if text.startswith("---") or set(text) <= {"-", ":", " "}:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        claims.append(text)

    for m in _TABLE_CLAIM_RE.finditer(report_md or ""):
        if m.group("found").lower() == "no":
            _add(m.group("claim"))

    for blob in (report_md or "", script or ""):
        for m in _SOURCE_NEEDED_RE.finditer(blob):
            _add(m.group(0))

    return claims


def format_claims_block(claims: list[str], report_md: str) -> str:
    """Human-readable claims list for the fix prompt; falls back to report excerpt."""
    if claims:
        return "\n".join(f"- {c}" for c in claims)
    excerpt = (report_md or "").strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000] + "\n…"
    return (
        "(No discrete claim rows parsed — use the validation report below.)\n\n"
        + excerpt
    )


def _build_user(
    script: str,
    source_material: str,
    claims: list[str],
    report_md: str,
) -> str:
    return CLAIM_FIX_USER_TEMPLATE.format(
        unverified_claims=format_claims_block(claims, report_md),
        script_output=script,
        source_material=source_material,
    )


def parse_claim_fix_output(raw: str) -> tuple[str, str]:
    """Parse fix LLM output into (script_sections_md, vo_raw). Raises on failure."""
    sections, vo = parse_script_output(raw)
    for name in list(sections.keys()):
        sections[name] = spell_numbers_for_vo(sections[name])
    vo = spell_numbers_for_vo(vo)
    md = format_script_sections_md(sections)
    return md, vo.strip() + "\n"


class WeirdBiologyClaimFixer:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        openrouter: OpenRouterClient | None = None,
        wavespeed: LLMClient | None = None,
    ):
        self.settings = settings or get_settings()
        self._openrouter = openrouter
        self._wavespeed = wavespeed
        self._router: WhbFreeFirstRouter | None = None

    def _get_openrouter(self) -> OpenRouterClient:
        if self._openrouter is None:
            self._openrouter = OpenRouterClient(
                self.settings, default_model=OPENROUTER_MODEL
            )
        return self._openrouter

    def _get_wavespeed(self) -> LLMClient:
        if self._wavespeed is None:
            self._wavespeed = LLMClient(
                self.settings, default_model=FALLBACK_MODEL
            )
        return self._wavespeed

    def _get_router(self) -> WhbFreeFirstRouter:
        if self._router is None:
            self._router = WhbFreeFirstRouter(
                self.settings,
                channel="weird_human_biology",
                openrouter=self._openrouter,
            )
        return self._router

    def fix_unverified_claims(
        self,
        script: str,
        source_pack: SourcePack,
        validation_report_md: str,
        *,
        also_vo: str | None = None,
    ) -> ClaimFixResult:
        """One-shot surgical fix of unverified claims. Raises if all models fail."""
        claims = extract_unverified_claims(
            validation_report_md, script=script
        )
        if also_vo:
            for m in _SOURCE_NEEDED_RE.finditer(also_vo):
                marker = m.group(0)
                if marker.lower() not in {c.lower() for c in claims}:
                    claims.append(marker)

        combined = script
        if also_vo:
            combined = f"{script}\n\n--- VO RAW ---\n{also_vo}"

        source_material = source_pack.source_material_text()
        user = _build_user(
            combined, source_material, claims, validation_report_md
        )
        last_chars = 0

        def _parse(text: str) -> tuple[str, str]:
            nonlocal last_chars
            last_chars = len(text)
            return parse_claim_fix_output(text)

        def _fallback() -> tuple[str, str]:
            nonlocal last_chars
            text = self._get_wavespeed().chat_text(
                system=CLAIM_FIX_SYSTEM,
                user=user,
                temperature=0.2,
                model=FALLBACK_MODEL,
                timeout_s=120.0,
            )
            last_chars = len(text)
            return parse_claim_fix_output(text)

        try:
            parsed, meta = self._get_router().call(
                system=CLAIM_FIX_SYSTEM,
                user=user,
                parse=_parse,
                fallback=_fallback,
                fallback_model=FALLBACK_MODEL,
                stage=STAGE_CLAIM_FIX,
                temperature=0.2,
                timeout_s=120.0,
            )
        except WhbLlmRouterError as exc:
            raise WeirdBiologyClaimFixError(str(exc)) from exc

        md, vo = parsed
        return ClaimFixResult(
            script_sections_md=md,
            vo_raw=vo,
            model_used=meta.model_used,
            claims_targeted=claims,
            attempts=list(meta.attempts),
            raw_chars=last_chars,
        )


def fix_unverified_claims(
    script: str,
    source_pack: SourcePack,
    validation_report_md: str,
    *,
    also_vo: str | None = None,
    settings: Settings | None = None,
    openrouter: OpenRouterClient | None = None,
    wavespeed: LLMClient | None = None,
) -> ClaimFixResult:
    """Module-level helper matching the sketch name."""
    return WeirdBiologyClaimFixer(
        settings,
        openrouter=openrouter,
        wavespeed=wavespeed,
    ).fix_unverified_claims(
        script,
        source_pack,
        validation_report_md,
        also_vo=also_vo,
    )
