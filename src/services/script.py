"""ScriptModule — outline → (optional refine) → chapter expansion → scenes.

Dual pacing (profile-driven; same machinery for napstorian + napping_historian):
- Phase A (first ~phase_a_max_words): short beats ~6–12 words @ ~3 s
- Phase B (after): long beats from voice_wpm × phase_b_seconds
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from src.domain.models import (
    ChapterExpansion,
    Outline,
    OutlineChapter,
    Scene,
    ScriptResult,
    ScriptValidation,
)
from src.services.llm import LLMClient, LLMError
from src.services.pacing import (
    assign_chapter_phases,
    estimate_duration_s,
    load_dual_pacing,
    scale_phase_targets,
)
from src.services.settings import (
    ensure_output_dir,
    get_settings,
    load_prompt,
    load_script_settings,
    load_style_hint,
    render_prompt,
)
from src.services.retention_profile import resolve_prompts_dir
from src.services.script_validate import (
    validate_chapter_sentences,
    validation_retry_hint,
)
from src.services.tts_sanitize import sanitize_scene_text

logger = logging.getLogger(__name__)

_DEDUPE_SIMILARITY_THRESHOLD = 0.85


SYSTEM_OUTLINE = (
    "You are ScriptModule for a historical documentary gallery factory. "
    "Return valid JSON only matching the user schema. "
    "Use the required outline arc and dual pacing notes from the user prompt."
)
SYSTEM_OUTLINE_REFINE = (
    "You are Senior Executive Editor refining a documentary chapter outline. "
    "Return valid JSON only matching the user schema. "
    "Polish accuracy, ad-policy safety, retention flow, and tone. "
    "Do not change chapter count significantly."
)
SYSTEM_EXPAND = (
    "You are ScriptModule section writer for a calm historical documentary. "
    "Return valid JSON only matching the user schema. "
    "Obey the pacing phase word/duration rules exactly. "
    "Sensory, sleepy, no YouTube slang (except exact branding lines when required)."
)


class ScriptModule:
    def __init__(self, llm: LLMClient | None = None, *, channel: str | None = None):
        self.settings = get_settings()
        self.channel = (
            (channel or os.getenv("SCRIPT_CHANNEL") or "").strip() or None
        )
        if self.channel:
            os.environ["SCRIPT_CHANNEL"] = self.channel
            try:
                from src.agents.sheet_channels import channel_default_profile
                from src.services.editing_overrides import format_mode_for_channel
                from src.services.retention_profile import load_retention_profiles, normalize_format

                # Farm sets RETENTION_PROFILE from job meta. CLI --channel may not —
                # apply channel default when unset so historian gets epic pacing.
                # High-effort: format_mode foc_epic / primary_source_reading wins when set.
                if not (os.getenv("RETENTION_PROFILE") or "").strip():
                    fmt = format_mode_for_channel(self.channel)
                    if fmt and fmt not in {"standard", ""}:
                        os.environ["RETENTION_PROFILE"] = normalize_format(fmt)
                    else:
                        os.environ["RETENTION_PROFILE"] = channel_default_profile(
                            self.channel
                        )
                    load_retention_profiles.cache_clear()
                    get_settings.cache_clear()
                    self.settings = get_settings()
            except Exception:  # noqa: BLE001
                logger.debug("channel profile bootstrap failed", exc_info=True)

        self.file_cfg = load_script_settings()
        if self.channel:
            try:
                from src.agents.sheet_channels import channel_script_overrides

                overrides = channel_script_overrides(self.channel)
                if overrides:
                    # Channel chapters_target (historian 37) is epic longform only.
                    # short_test / other explicit profiles already set chapters_target —
                    # do not clobber them or the outline will truncate and HOLD.
                    prof = str(
                        self.file_cfg.get("_retention_profile") or ""
                    ).strip().lower()
                    if prof and prof not in {"epic", "retention", ""}:
                        overrides.pop("chapters_target", None)
                    if overrides:
                        self.file_cfg = {**self.file_cfg, **overrides}
            except Exception:  # noqa: BLE001
                logger.debug("channel_script_overrides failed", exc_info=True)
        # Shared client; outline vs expand pick different models per call.
        self.llm = llm or LLMClient(
            self.settings, default_model=self.settings.llm_outline_model
        )
        self.outline_model = self.settings.llm_outline_model or self.settings.llm_model
        self.expand_model = self.settings.llm_expand_model or self.settings.llm_model
        self.outline_fallback_model = (
            (self.settings.llm_outline_fallback_model or "").strip()
            or self.expand_model
        )
        self.expand_fallback_model = (
            (self.settings.llm_expand_fallback_model or "").strip()
            or self.outline_model
        )
        self.style_hint = load_style_hint()
        self.prompts_dir = resolve_prompts_dir(self.file_cfg, channel=self.channel)
        self.pacing = load_dual_pacing(
            self.file_cfg, settings_target_scenes=self.settings.target_scenes
        )
        self._outline_refine_path = self.prompts_dir / "outline_refine.txt"

    def generate(
        self,
        topic: str,
        *,
        niche_notes: str = "",
        save: bool = True,
        out_path: Path | None = None,
    ) -> ScriptResult:
        topic = topic.strip()
        if not topic:
            raise ValueError("topic is required")

        outline = self._generate_outline(topic, niche_notes=niche_notes)
        outline = self._maybe_refine_outline(topic, outline, niche_notes=niche_notes)
        outline = self._normalize_sentence_budget(outline)
        outline, hook_gen_meta = self._maybe_apply_hook_generator(topic, outline)

        expansions: list[ChapterExpansion] = []
        covered_beats: list[str] = []
        prior_texts: list[str] = []
        for chapter in outline.chapters:
            exp = self._expand_chapter(
                outline,
                chapter,
                covered_beats=covered_beats,
                prior_texts=prior_texts,
            )
            expansions.append(exp)
            covered_beats.append(self._chapter_summary(chapter, exp))
            prior_texts.extend(s.text.strip() for s in exp.sentences if s.text.strip())

        scenes = self._flatten_scenes(expansions, outline)
        scenes, dedupe_removed = self._postprocess_scenes(scenes)
        validation = self.validate_scenes(scenes)

        retries = int(self.settings.script_max_retries)
        attempt = 0
        while not validation.ok and attempt < retries:
            attempt += 1
            outline = self._adjust_budget_for_retry(outline, validation.scene_count)
            expansions = []
            covered_beats = []
            prior_texts = []
            for ch in outline.chapters:
                exp = self._expand_chapter(
                    outline, ch, covered_beats=covered_beats, prior_texts=prior_texts
                )
                expansions.append(exp)
                covered_beats.append(self._chapter_summary(ch, exp))
                prior_texts.extend(s.text.strip() for s in exp.sentences if s.text.strip())
            scenes = self._flatten_scenes(expansions, outline)
            scenes, dedupe_removed = self._postprocess_scenes(scenes)
            validation = self.validate_scenes(scenes)

        result = ScriptResult(
            topic=topic,
            title=outline.title,
            hook=outline.hook,
            outline=outline,
            scenes=scenes,
            validation=validation,
            meta={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": self.outline_model,
                "outline_model": self.outline_model,
                "expand_model": self.expand_model,
                "llm_base_url": self.settings.llm_base_url,
                "retries_used": attempt,
                "niche_notes": niche_notes,
                "style_hint": self.style_hint,
                "dual_pacing": {
                    "enabled": self.pacing.enabled,
                    "phase_a_max_words": self.pacing.phase_a_max_words,
                    "phase_a_scenes": self.pacing.phase_a_target_scenes,
                    "phase_a_seconds": self.pacing.phase_a_seconds,
                    "phase_a_words": [
                        self.pacing.phase_a_words_min,
                        self.pacing.phase_a_words_max,
                    ],
                    "phase_b_scenes": self.pacing.phase_b_target_scenes,
                    "phase_b_seconds": self.pacing.phase_b_seconds,
                    "phase_b_words": self.pacing.phase_b_words,
                    "target_scenes": self.pacing.target_scenes,
                },
                "retention_profile": self.file_cfg.get("_retention_profile", "retention"),
                "format_mode": self.file_cfg.get("format_mode")
                or self.file_cfg.get("_retention_profile", "retention"),
                "channel": self.channel,
                "dedupe_removed": dedupe_removed,
                "prompts": {
                    "dir": str(self.prompts_dir),
                    "outline": str(self.prompts_dir / "outline.txt"),
                    "outline_refine": (
                        str(self._outline_refine_path)
                        if self._outline_refine_path.exists()
                        else None
                    ),
                    "chapter_expand": str(self.prompts_dir / "chapter_expand.txt"),
                    "hook_cold_open": (
                        str(self._hook_cold_open_path())
                        if self._hook_cold_open_path()
                        else None
                    ),
                },
                "voice_wpm": self.pacing.voice_wpm,
                "hook_generator": hook_gen_meta,
            },
        )

        if save:
            path = self.save_result(result, out_path=out_path)
            result.meta["saved_to"] = str(path)

        if not validation.ok:
            raise ScriptValidationError(result)

        return result

    def _outline_placeholders(
        self, topic: str, *, niche_notes: str, extra: dict | None = None
    ) -> dict:
        p = self.pacing
        vals: dict = {
            "TOPIC": topic,
            "NICHE_NOTES": niche_notes or "(none)",
            "TARGET_DURATION_MIN": self.file_cfg.get(
                "target_duration_min", self.settings.target_duration_min
            ),
            "TARGET_SECONDS_PER_SCENE": p.phase_a_seconds,
            "TARGET_SCENES": p.target_scenes,
            "MIN_SCENES": p.min_scenes,
            "MAX_SCENES": p.max_scenes,
            "CHAPTERS_TARGET": self.file_cfg.get("chapters_target", 10),
            "PHASE_A_MAX_WORDS": p.phase_a_max_words,
            "PHASE_A_SCENES": p.phase_a_target_scenes,
            "PHASE_A_SECONDS": p.phase_a_seconds,
            "PHASE_A_WORDS_MIN": p.phase_a_words_min,
            "PHASE_A_WORDS_MAX": p.phase_a_words_max,
            "PHASE_B_SCENES": p.phase_b_target_scenes,
            "PHASE_B_SECONDS": p.phase_b_seconds,
            "PHASE_B_WORDS": p.phase_b_words,
            "VOICE_WPM": round(p.voice_wpm, 1),
        }
        if extra:
            vals.update(extra)
        return vals

    def _hook_cold_open_path(self) -> Path | None:
        """Channel twin first, then shared napstorian fallback (epic profile)."""
        from src.services.settings import CONFIG_DIR

        candidates = [self.prompts_dir / "hook_cold_open.txt"]
        ch = (self.channel or "").lower()
        if "historian" in ch:
            candidates.append(
                CONFIG_DIR / "prompts" / "napping_historian" / "hook_cold_open.txt"
            )
        else:
            candidates.append(CONFIG_DIR / "prompts" / "hook_cold_open.txt")
        # De-dupe while preserving order
        seen: set[str] = set()
        for path in candidates:
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            if path.is_file() and path.stat().st_size > 0:
                return path
        return None

    def _load_hook_cold_open_rules(self) -> str:
        path = self._hook_cold_open_path()
        if path is None:
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _maybe_apply_hook_generator(
        self, topic: str, outline: Outline
    ) -> tuple[Outline, dict]:
        """Netflix-doc multi-variant hooks; non-blocking if LLM/validation fails."""
        meta: dict = {"applied": False}
        flag = str(os.environ.get("HOOK_GENERATOR", "1")).strip().lower()
        if flag in {"0", "false", "off", "no"}:
            meta["skipped"] = "HOOK_GENERATOR=off"
            return outline, meta
        try:
            from src.services.hook_generator import generate_hooks, pick_best_hook
            from src.services.editing_overrides import hook_pattern_prefs

            transcript = "\n".join(
                [
                    outline.hook or "",
                    outline.title or "",
                    *[
                        f"{ch.title}: {ch.goal}; " + "; ".join(ch.key_points or [])
                        for ch in (outline.chapters or [])
                    ],
                ]
            )
            variants = generate_hooks(
                transcript,
                topic,
                num_variants=3,
                settings=self.settings,
                channel=self.channel,
                llm=self.llm,
            )
            best = pick_best_hook(
                variants,
                channel=self.channel,
                preferred_patterns=hook_pattern_prefs(self.channel),
            )
            meta["n_variants"] = len(variants)
            meta["n_passed"] = sum(1 for v in variants if v.get("passed_validation"))
            if best and best.get("hook"):
                outline.hook = str(best["hook"])
                meta["applied"] = True
                meta["pattern_used"] = best.get("pattern_used")
            else:
                meta["skipped"] = "no_valid_variant"
        except Exception as exc:  # noqa: BLE001
            logger.warning("hook_generator skipped: %s", exc)
            meta["error"] = str(exc)[:200]
        return outline, meta

    def _generate_outline(self, topic: str, *, niche_notes: str) -> Outline:
        template = load_prompt("outline.txt", prompts_dir=self.prompts_dir)
        user = render_prompt(
            template, self._outline_placeholders(topic, niche_notes=niche_notes)
        )
        hook_rules = self._load_hook_cold_open_rules()
        if hook_rules:
            user = user + "\n\n=== COLD OPEN / RETENTION HOOK ===\n" + hook_rules
        chapters_target = int(self.file_cfg.get("chapters_target") or 10)
        if chapters_target >= 20:
            user = (
                user
                + "\n\nCOMPACT JSON (critical for epic outlines): keep each "
                "key_points array to at most 2 short bullets; goals ≤120 chars; "
                "no prose outside JSON; finish the full chapters array."
            )
        # Sleep policy: same-model 2 retries → failover to expand model → 2 retries → HOLD.
        raw = self.llm.chat_json(
            system=SYSTEM_OUTLINE,
            user=user,
            temperature=0.5,
            model=self.outline_model,
            fallback_model=self.outline_fallback_model,
            same_model_retries=int(
                getattr(self.settings, "llm_json_same_model_retries", 2) or 2
            ),
        )
        return Outline.model_validate(raw)

    def _maybe_refine_outline(
        self, topic: str, outline: Outline, *, niche_notes: str
    ) -> Outline:
        """Second pass when ``outline_refine.txt`` exists in prompts_dir (historian)."""
        if not self._outline_refine_path.exists():
            return outline
        template = load_prompt("outline_refine.txt", prompts_dir=self.prompts_dir)
        raw_chapters = json.dumps(
            outline.model_dump(mode="json"),
            ensure_ascii=False,
        )
        user = render_prompt(
            template,
            self._outline_placeholders(
                topic,
                niche_notes=niche_notes,
                extra={"RAW_CHAPTERS": raw_chapters},
            ),
        )
        try:
            raw = self.llm.chat_json(
                system=SYSTEM_OUTLINE_REFINE,
                user=user,
                temperature=0.3,
                model=self.outline_model,
                fallback_model=self.outline_fallback_model,
                same_model_retries=int(
                    getattr(self.settings, "llm_json_same_model_retries", 2) or 2
                ),
            )
            refined = Outline.model_validate(raw)
            # Guard: do not accept a wildly different chapter count
            n0, n1 = len(outline.chapters), len(refined.chapters)
            if n0 and abs(n1 - n0) > max(3, int(n0 * 0.15)):
                logger.warning(
                    "outline_refine changed chapter count %s → %s; keeping raw",
                    n0,
                    n1,
                )
                return outline
            logger.info(
                "outline_refine applied (%s → %s chapters)", n0, n1
            )
            return refined
        except Exception as exc:  # noqa: BLE001
            logger.warning("outline_refine failed; keeping raw outline: %s", exc)
            return outline

    def _expand_chapter(
        self,
        outline: Outline,
        chapter: OutlineChapter,
        *,
        covered_beats: list[str] | None = None,
        prior_texts: list[str] | None = None,
    ) -> ChapterExpansion:
        template = load_prompt("chapter_expand.txt", prompts_dir=self.prompts_dir)
        phase = (chapter.pacing_phase or "a").lower()
        p = self.pacing
        if phase == "b":
            words_min, words_max = p.phase_b_words_min, p.phase_b_words_max
            seconds = p.phase_b_seconds
            words_target = p.phase_b_words
            pacing_rules = (
                f"PHASE B (deep/slow): write ~{words_target} words per spoken beat "
                f"(accept {words_min}–{words_max}). Each beat ≈ {seconds:.0f}s of calm narration. "
                "Use longer flowing audiobook paragraphs as ONE scene text (still one visual)."
            )
        else:
            words_min, words_max = p.phase_a_words_min, p.phase_a_words_max
            seconds = p.phase_a_seconds
            words_target = int(round(p.phase_a_avg_words))
            pacing_rules = (
                f"PHASE A (gallery hook): write SHORT spoken sentences of {words_min}–{words_max} words "
                f"(aim ~{words_target}). Each sentence ≈ {seconds:.0f}s of TTS. "
                "One short sentence = one scene."
            )

        outline_compact = {
            "title": outline.title,
            "hook": outline.hook,
            "closer": outline.closer,
            "chapters": [
                {
                    "id": c.id,
                    "title": c.title,
                    "target_sentences": c.target_sentences,
                    "pacing_phase": c.pacing_phase,
                }
                for c in outline.chapters
            ],
        }
        covered_list = covered_beats or []
        covered_block = "\n".join(f"- {b}" for b in covered_list[-12:]) or "(none yet)"
        previous_summary = covered_list[-1] if covered_list else "(none — this is chapter 1)"
        chapter_total = len(outline.chapters)
        user = render_prompt(
            template,
            {
                "TARGET_DURATION_MIN": self.file_cfg.get(
                    "target_duration_min", self.settings.target_duration_min
                ),
                "TITLE": outline.title,
                "HOOK": outline.hook,
                "OUTLINE_JSON": json.dumps(outline_compact, ensure_ascii=False),
                "CHAPTER_ID": chapter.id,
                "CHAPTER_INDEX": chapter.id,
                "CHAPTER_TOTAL": chapter_total,
                "CHAPTER_TITLE": chapter.title,
                "CHAPTER_GOAL": chapter.goal,
                "KEY_POINTS": json.dumps(chapter.key_points, ensure_ascii=False),
                "TARGET_SENTENCES": chapter.target_sentences,
                "STYLE_HINT": self.style_hint,
                "WORDS_MIN": words_min,
                "WORDS_MAX": words_max,
                "WORDS_TARGET": words_target,
                "TARGET_SECONDS_PER_SCENE": seconds,
                "PACING_PHASE": phase.upper(),
                "PACING_RULES": pacing_rules,
                "COVERED_BEATS": covered_block,
                "PREVIOUS_SUMMARY": previous_summary,
            },
        )
        # Chapter 1 cold open: append channel twin hook rules so expand (not only
        # outline) obeys title-promise / first-breath retention.
        if int(chapter.id or 0) == 1:
            hook_rules = self._load_hook_cold_open_rules()
            if hook_rules:
                user = (
                    user
                    + "\n\n=== COLD OPEN / RETENTION HOOK (chapter 1 — obey) ===\n"
                    + hook_rules
                    + "\n\nWorking title (contract): "
                    + (outline.title or "").strip()
                    + "\nSpoken hook promise: "
                    + (outline.hook or "(none)").strip()
                )
        # Phase B chapters are small — tighter absolute tolerance
        abs_tol = 2 if phase == "b" else 6
        # Haiku often returns ~7–10 beats when asked for 16; chunk Phase A smaller
        # and always fill remaining parts instead of hard-failing mid-chapter.
        target = max(1, int(chapter.target_sentences))
        chunk_size = 8 if phase == "a" else max(target, 1)

        remaining = target
        part = 0
        all_sents: list = []
        while remaining > 0:
            part += 1
            if part > max(8, target + 2):
                raise LLMError(
                    f"Chapter {chapter.id} ({phase}) fill loop exceeded parts "
                    f"with {len(all_sents)}/{target} beats"
                )
            n_this = min(chunk_size, remaining)
            part_tol = max(1, min(abs_tol, max(2, n_this // 2)))
            if target <= chunk_size and part == 1:
                part_user = user
            else:
                part_user = (
                    user
                    + f"\n\nIMPORTANT: This is PART {part} of a multi-part expansion "
                    f"for this section. Return EXACTLY {n_this} beats in the JSON "
                    f"sentences array (not the full section total). Continue the "
                    f"story from prior parts; do not repeat earlier beats. "
                    f"Still use chapter_id {chapter.id}."
                )
            expansion = self._expand_chapter_once(
                user=part_user,
                chapter_id=chapter.id,
                phase=phase,
                target=n_this,
                abs_tol=part_tol,
                prior_texts=(prior_texts or []) + [s.text for s in all_sents],
                hook_chapter=chapter.id == 1 and not all_sents,
                allow_partial=True,
            )
            if len(expansion.sentences) <= 0:
                raise LLMError(f"Chapter {chapter.id} part {part} returned 0 beats")
            all_sents.extend(expansion.sentences)
            remaining = target - len(all_sents)
            if remaining > 0 and len(expansion.sentences) < max(1, n_this // 4):
                # Pathologically tiny yield — avoid spinning forever
                raise LLMError(
                    f"Chapter {chapter.id} ({phase}) returned "
                    f"{len(expansion.sentences)} beats, target {n_this}"
                )

        if len(all_sents) > target:
            all_sents = all_sents[:target]
        if abs(len(all_sents) - target) > max(abs_tol, int(target * 0.35)):
            raise LLMError(
                f"Chapter {chapter.id} ({phase}) assembled {len(all_sents)} beats, "
                f"target {target}"
            )
        return ChapterExpansion(chapter_id=chapter.id, sentences=all_sents)

    def _expand_chapter_once(
        self,
        *,
        user: str,
        chapter_id: int,
        phase: str,
        target: int,
        abs_tol: int,
        prior_texts: list[str] | None = None,
        hook_chapter: bool = False,
        allow_partial: bool = False,
    ) -> ChapterExpansion:
        last_err: Exception | None = None
        last_partial: ChapterExpansion | None = None
        prompt = user
        max_attempts = max(1, self.settings.script_max_retries + 2)
        for attempt in range(max_attempts):
            try:
                raw = self.llm.chat_json(
                    system=SYSTEM_EXPAND,
                    user=prompt,
                    temperature=0.75,
                    model=self.expand_model,
                    fallback_model=self.expand_fallback_model,
                    same_model_retries=int(
                        getattr(self.settings, "llm_json_same_model_retries", 2) or 2
                    ),
                )
                expansion = ChapterExpansion.model_validate(raw)
                n = len(expansion.sentences)
                tol = max(abs_tol, int(target * 0.35))
                # Overshoot beyond tol → trim when close enough, else retry.
                if n > target + tol:
                    prompt = (
                        user
                        + f"\n\nCORRECTION: You returned {n} beats; this call requires "
                        f"EXACTLY {target} beats in sentences[]. "
                        "Return only JSON with that exact count."
                    )
                    raise LLMError(
                        f"Chapter {chapter_id} ({phase}) returned {n} beats, target {target}"
                    )
                if n > target:
                    expansion = ChapterExpansion(
                        chapter_id=expansion.chapter_id,
                        sentences=expansion.sentences[:target],
                    )
                    n = len(expansion.sentences)
                # Undershoot: keep best partial; retry for a fuller count.
                if n < target - tol:
                    if n > 0:
                        last_partial = expansion
                    prompt = (
                        user
                        + f"\n\nCORRECTION: You returned {n} beats; this call requires "
                        f"EXACTLY {target} beats in sentences[]. "
                        "Return only JSON with that exact count."
                    )
                    raise LLMError(
                        f"Chapter {chapter_id} ({phase}) returned {n} beats, target {target}"
                    )
                val = validate_chapter_sentences(
                    expansion.sentences,
                    chapter_id=chapter_id,
                    prior_texts=prior_texts,
                    hook_chapter=hook_chapter,
                )
                for w in val.warnings:
                    logger.warning("chapter %d validation warn: %s", chapter_id, w)
                if not val.ok and attempt < self.settings.script_max_retries + 1:
                    prompt = user + validation_retry_hint(val.errors)
                    raise LLMError(
                        f"Chapter {chapter_id} retention validation failed"
                    )
                return expansion
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if allow_partial and last_partial is not None and last_partial.sentences:
            logger.warning(
                "chapter %s (%s) accepting partial %s/%s beats after retries",
                chapter_id,
                phase,
                len(last_partial.sentences),
                target,
            )
            return last_partial
        assert last_err is not None
        raise last_err

    def _chapter_summary(self, chapter: OutlineChapter, expansion: ChapterExpansion) -> str:
        first = expansion.sentences[0].text.strip() if expansion.sentences else ""
        snippet = first[:80] + ("..." if len(first) > 80 else "")
        return f"Ch{chapter.id} {chapter.title}: {snippet}"

    def _normalize_sentence_budget(self, outline: Outline) -> Outline:
        """Assign A/B phases and rescale chapter targets to dual-pacing scene totals."""
        chapters = list(outline.chapters)
        p = self.pacing
        phases = assign_chapter_phases(len(chapters), p)
        raw = [max(1, c.target_sentences) for c in chapters]
        scaled_n = scale_phase_targets(raw, phases, p)

        scaled: list[OutlineChapter] = []
        for ch, n, phase in zip(chapters, scaled_n, phases):
            scaled.append(
                ch.model_copy(
                    update={
                        "target_sentences": int(n),
                        "pacing_phase": phase,
                    }
                )
            )

        return outline.model_copy(
            update={
                "chapters": scaled,
                "estimated_sentence_budget": sum(c.target_sentences for c in scaled),
            }
        )

    def _adjust_budget_for_retry(self, outline: Outline, got: int) -> Outline:
        target = self.pacing.target_scenes
        if got <= 0:
            return self._normalize_sentence_budget(outline)
        ratio = target / got
        tweaked = [
            c.model_copy(
                update={
                    "target_sentences": max(1, int(round(c.target_sentences * ratio)))
                }
            )
            for c in outline.chapters
        ]
        return self._normalize_sentence_budget(
            outline.model_copy(update={"chapters": tweaked})
        )

    def _flatten_scenes(
        self, expansions: list[ChapterExpansion], outline: Outline
    ) -> list[Scene]:
        phase_by_chapter = {
            c.id: (c.pacing_phase or "a").lower() for c in outline.chapters
        }
        p = self.pacing
        scenes: list[Scene] = []
        idx = 0
        running_words = 0
        for exp in expansions:
            chapter_phase = phase_by_chapter.get(exp.chapter_id, "a")
            for sent in exp.sentences:
                text = sent.text.strip()
                if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
                    text = text[1:-1].strip()
                words = len(text.split())
                # Soft safety: if cumulative words already past phase A, force B
                phase = chapter_phase
                if p.enabled and running_words >= p.phase_a_max_words:
                    phase = "b"
                target_dur = (
                    p.phase_b_seconds if phase == "b" else p.phase_a_seconds
                )
                scenes.append(
                    Scene(
                        index=idx,
                        text=text,
                        visual_prompt=sent.visual_prompt.strip(),
                        chapter_id=exp.chapter_id,
                        word_count=words,
                        pacing_phase=phase,
                        target_duration_s=float(target_dur),
                        beat_type=(getattr(sent, "beat_type", None) or "").strip(),
                    )
                )
                running_words += words
                idx += 1
        return scenes

    def _postprocess_scenes(self, scenes: list[Scene]) -> tuple[list[Scene], int]:
        """Sanitize TTS text, drop empty scenes, dedupe near-identical beats."""
        before = len(scenes)
        scenes = self._sanitize_scenes(scenes)
        scenes = self._dedupe_scenes(scenes)
        removed = before - len(scenes)
        if before > 0 and removed / before > 0.10:
            logger.warning(
                "dedupe removed %d/%d scenes (>10%% — check chapter memory)",
                removed,
                before,
            )
        return scenes, removed

    def _sanitize_scenes(self, scenes: list[Scene]) -> list[Scene]:
        kept: list[Scene] = []
        for scene in scenes:
            cleaned, warnings = sanitize_scene_text(scene.text)
            for w in warnings:
                logger.warning("scene %d sanitize: %s", scene.index, w)
            if not cleaned:
                logger.warning(
                    "scene %d removed after sanitization (empty text)", scene.index
                )
                continue
            words = len(cleaned.split())
            kept.append(
                scene.model_copy(
                    update={"text": cleaned, "word_count": words},
                )
            )
        for i, scene in enumerate(kept):
            if scene.index != i:
                kept[i] = scene.model_copy(update={"index": i})
        return kept

    def _dedupe_scenes(self, scenes: list[Scene]) -> list[Scene]:
        kept: list[Scene] = []
        for scene in scenes:
            norm = _normalize_text_for_dedupe(scene.text)
            if not norm:
                continue
            duplicate_of: int | None = None
            for prior in kept:
                prior_norm = _normalize_text_for_dedupe(prior.text)
                ratio = SequenceMatcher(None, norm, prior_norm).ratio()
                if ratio > _DEDUPE_SIMILARITY_THRESHOLD:
                    duplicate_of = prior.index
                    break
            if duplicate_of is not None:
                logger.warning(
                    "removed duplicate scene %d (>%.0f%% similar to scene %d)",
                    scene.index,
                    _DEDUPE_SIMILARITY_THRESHOLD * 100,
                    duplicate_of,
                )
                continue
            kept.append(scene)
        for i, scene in enumerate(kept):
            if scene.index != i:
                kept[i] = scene.model_copy(update={"index": i})
        return kept

    def validate_scenes(self, scenes: list[Scene]) -> ScriptValidation:
        n = len(scenes)
        p = self.pacing
        min_s = p.min_scenes
        max_s = p.max_scenes
        target = p.target_scenes
        est = estimate_duration_s(scenes, p)

        warnings: list[str] = []
        errors: list[str] = []

        if n < min_s or n > max_s:
            errors.append(
                f"scene_count={n} outside dual-pacing band [{min_s}, {max_s}] "
                f"(target ~{target} = {p.phase_a_target_scenes}A + {p.phase_b_target_scenes}B)"
            )

        a_scenes = [s for s in scenes if s.pacing_phase != "b"]
        b_scenes = [s for s in scenes if s.pacing_phase == "b"]
        a_words = [s.word_count or len(s.text.split()) for s in a_scenes]
        b_words = [s.word_count or len(s.text.split()) for s in b_scenes]

        median_wc = None
        all_wc = a_words + b_words
        if all_wc:
            median_wc = float(statistics.median(all_wc))

        if a_words:
            med_a = float(statistics.median(a_words))
            if med_a < p.phase_a_words_min - 2 or med_a > p.phase_a_words_max + 4:
                warnings.append(
                    f"Phase A median words={med_a:.1f}; preferred "
                    f"{p.phase_a_words_min}–{p.phase_a_words_max} for ~{p.phase_a_seconds:.0f}s"
                )
        if b_words:
            med_b = float(statistics.median(b_words))
            if med_b < p.phase_b_words_min - 10 or med_b > p.phase_b_words_max + 15:
                warnings.append(
                    f"Phase B median words={med_b:.1f}; preferred ~{p.phase_b_words} "
                    f"({p.phase_b_words_min}–{p.phase_b_words_max}) for ~{p.phase_b_seconds:.0f}s"
                )
        elif p.enabled:
            warnings.append("No Phase B scenes — expected long ~60s beats after ~1500 words")

        total_words = sum(all_wc)
        if p.enabled and total_words < p.phase_a_max_words * 0.7:
            warnings.append(
                f"total_words={total_words} well under Phase A budget {p.phase_a_max_words}"
            )

        missing_visual = sum(1 for s in scenes if not s.visual_prompt.strip())
        if missing_visual:
            errors.append(f"{missing_visual} scenes missing visual_prompt")

        return ScriptValidation(
            ok=not errors,
            scene_count=n,
            min_scenes=min_s,
            max_scenes=max_s,
            target_scenes=target,
            estimated_duration_s=est,
            median_word_count=median_wc,
            warnings=warnings,
            errors=errors,
        )

    def save_result(self, result: ScriptResult, *, out_path: Path | None = None) -> Path:
        ensure_output_dir()
        if out_path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in result.topic
            )[:60]
            out_path = ensure_output_dir() / f"{stamp}_{safe}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        narr_path = out_path.with_suffix(".txt")
        header = [
            f"TITLE: {result.title}",
            f"TOPIC: {result.topic}",
            f"SCENES: {result.validation.scene_count}",
            f"EST_DURATION_S: {result.validation.estimated_duration_s:.0f}",
            "",
        ]
        body = []
        for s in result.scenes:
            phase = (s.pacing_phase or "a").upper()
            body.append(f"[{s.index:03d}|{phase}] {s.text}")
        narr_path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
        return out_path


class ScriptValidationError(RuntimeError):
    def __init__(self, result: ScriptResult):
        self.result = result
        msg = "; ".join(result.validation.errors) or "script validation failed"
        super().__init__(msg)


def _normalize_text_for_dedupe(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def generate_script(
    topic: str,
    *,
    niche_notes: str = "",
    save: bool = True,
    out_path: Path | None = None,
) -> ScriptResult:
    return ScriptModule().generate(
        topic, niche_notes=niche_notes, save=save, out_path=out_path
    )
