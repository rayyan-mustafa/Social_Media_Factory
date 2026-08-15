"""Claim-vs-source validator for Weird Human Biology scripts.

Primary: openrouter/free (2 attempts). Fallback only after both fail:
google/gemini-2.5-flash-lite via WaveSpeed.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from src.services.llm import LLMClient
from src.services.openrouter import OpenRouterClient
from src.services.settings import Settings, get_settings
from src.services.whb_llm_router import (
    FREE_ATTEMPTS,
    FREE_MODEL,
    STAGE_VALIDATE,
    WAVESPEED_FALLBACK_MODEL,
    WhbFreeFirstRouter,
    WhbLlmRouterError,
)
from src.services.weird_biology_research import SourcePack

VALIDATION_SYSTEM = (
    "You are a fact-checking validator. Check factual claims against SOURCE "
    "MATERIAL only — not style, not grammar, not outside knowledge."
)

VALIDATION_USER_TEMPLATE = """You are a fact-checking validator. You will be given a SCRIPT and its
SOURCE MATERIAL. Your only job is to check factual claims — not style,
not grammar.

TASK:
1. Extract every specific factual claim in the script: numbers,
   percentages, dates, years, study names, researcher names, university
   names, and named biological/scientific terms presented as fact.
2. For each claim, check whether it is explicitly supported by the
   SOURCE MATERIAL below.
3. Output a table with columns: Claim | Found in Source (Yes/No) | Source Quote (if Yes) | Risk Level (if No: High if it's a stat/date/name, Low if it's general phrasing).

RULES:
- Do not use outside knowledge to "confirm" a claim — only check against
  the provided SOURCE MATERIAL.
- Flag ANY claim not directly traceable to the sources, even if it
  sounds plausible.
- If every claim is verified, output "ALL CLAIMS VERIFIED" at the top.
  Otherwise output "VALIDATION FAILED — N UNVERIFIED CLAIMS" at the top.
- Also flag any `[SOURCE NEEDED: ...]` markers as UNVERIFIED (High risk).

SCRIPT:
{script_output}

SOURCE MATERIAL:
{source_material}
"""

_VERIFIED_RE = re.compile(r"^\s*ALL CLAIMS VERIFIED\b", re.IGNORECASE | re.MULTILINE)
_FAILED_RE = re.compile(
    r"^\s*VALIDATION FAILED\s*[—\-–-]?\s*(\d+)\s+UNVERIFIED CLAIMS\b",
    re.IGNORECASE | re.MULTILINE,
)
_SOURCE_NEEDED_RE = re.compile(r"\[SOURCE NEEDED:[^\]]*\]", re.IGNORECASE)

OPENROUTER_MODEL = FREE_MODEL
FALLBACK_MODEL = WAVESPEED_FALLBACK_MODEL
OPENROUTER_ATTEMPTS = FREE_ATTEMPTS


class WeirdBiologyValidationError(RuntimeError):
    pass


@dataclass
class ValidationResult:
    ok: bool
    status_line: str
    report_md: str
    unverified_count: int
    model_used: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    source_needed_markers: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_validation_report(text: str) -> tuple[bool, str, int]:
    """Return (ok, status_line, unverified_count). Raises if ambiguous."""
    raw = (text or "").strip()
    if not raw:
        raise WeirdBiologyValidationError("empty validation response")

    m_ok = _VERIFIED_RE.search(raw)
    m_fail = _FAILED_RE.search(raw)
    if m_ok and not m_fail:
        return True, "ALL CLAIMS VERIFIED", 0
    if m_fail:
        n = int(m_fail.group(1))
        return False, f"VALIDATION FAILED — {n} UNVERIFIED CLAIMS", n

    # Soft signals
    lower = raw.lower()
    if "all claims verified" in lower[:500]:
        return True, "ALL CLAIMS VERIFIED", 0
    if "validation failed" in lower[:800]:
        m_n = re.search(r"(\d+)\s+unverified", lower)
        n = int(m_n.group(1)) if m_n else 1
        return False, f"VALIDATION FAILED — {n} UNVERIFIED CLAIMS", n

    raise WeirdBiologyValidationError(
        "ambiguous validation output (missing ALL CLAIMS VERIFIED / VALIDATION FAILED)"
    )


def _build_user(script: str, source_material: str) -> str:
    return VALIDATION_USER_TEMPLATE.format(
        script_output=script,
        source_material=source_material,
    )


class WeirdBiologyValidator:
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

    def validate(
        self,
        script: str,
        source_pack: SourcePack,
        *,
        also_check_vo: str | None = None,
    ) -> ValidationResult:
        source_material = source_pack.source_material_text()
        combined = script
        if also_check_vo:
            combined = f"{script}\n\n--- VO RAW ---\n{also_check_vo}"
        markers = len(_SOURCE_NEEDED_RE.findall(combined))
        user = _build_user(combined, source_material)

        def _parse(text: str) -> str:
            parse_validation_report(text)
            return text

        def _fallback() -> str:
            text = self._get_wavespeed().chat_text(
                system=VALIDATION_SYSTEM,
                user=user,
                temperature=0.1,
                model=FALLBACK_MODEL,
                timeout_s=120.0,
            )
            parse_validation_report(text)
            return text

        try:
            text, meta = self._get_router().call(
                system=VALIDATION_SYSTEM,
                user=user,
                parse=_parse,
                fallback=_fallback,
                fallback_model=FALLBACK_MODEL,
                stage=STAGE_VALIDATE,
                temperature=0.1,
                timeout_s=120.0,
            )
        except WhbLlmRouterError as exc:
            raise WeirdBiologyValidationError(str(exc)) from exc

        attempts = list(meta.attempts)
        ok, status, n = parse_validation_report(text)
        if markers and ok:
            # Fail closed on leftover SOURCE NEEDED markers
            ok = False
            n = max(n, markers)
            status = f"VALIDATION FAILED — {n} UNVERIFIED CLAIMS"
            text = (
                f"{status}\n\n"
                f"(Local gate: {markers} [SOURCE NEEDED] marker(s) in script.)\n\n"
                + text
            )
        if attempts:
            attempts[-1]["ok"] = ok
            attempts[-1]["status"] = status
            attempts[-1]["chars"] = len(text)
        return ValidationResult(
            ok=ok,
            status_line=status,
            report_md=text.strip() + "\n",
            unverified_count=n if not ok else 0,
            model_used=meta.model_used,
            attempts=attempts,
            source_needed_markers=markers,
        )


def save_validation(result: ValidationResult, *, md_path: Any, json_path: Any) -> None:
    from pathlib import Path

    Path(md_path).write_text(result.report_md, encoding="utf-8")
    Path(json_path).write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
