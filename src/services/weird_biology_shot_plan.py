"""Shot planner for Weird Biology.

Tries ``openrouter/free`` twice, then WaveSpeed ``google/gemini-2.5-flash-lite``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.services.llm import LLMClient, LLMError, parse_json_object
from src.services.openrouter import OpenRouterClient
from src.services.settings import get_settings
from src.services.whb_llm_router import (
    FREE_MODEL,
    STAGE_SHOT_PLAN,
    WAVESPEED_FALLBACK_MODEL,
    WhbFreeFirstRouter,
)
from src.services.weird_biology_scenes import VisualTimeline, ensure_core_data_xray_slot
from src.services.weird_biology_stickman import POSES, PROPS, ShotSpec
from src.services.weird_biology_style import planner_model

PLANNER_MODEL = WAVESPEED_FALLBACK_MODEL

_SYSTEM = (
    "You plan Weird Human Biology stickman shots. Reply with ONLY valid JSON. "
    "Keep plans short. Default layout: clinical teal/slate; crib_door_scene uses warm "
    "cream reference palette. Coral text only for stats/bubble bars."
)

_USER_TMPL = """Plan stickman shots for these narration beats.
Rules:
- pose: one of {poses}
- emotion: neutral|surprise|calm|worry|smile
- props: 0–3 from {props} (minimal environments; use crib/door/bubble/room_corner for nursery beats)
- kinetic_text: short coral pop-in ONLY for stats/numbers/emphasis, else null
- bubble_bars: 0–3 coral bars in a speech bubble (accent only); prefer with crib/baby beats
- layout: default|crib_door_scene (crib_door_scene = baby+adult dual cast with crib/door)
- xray: true only when revealing internal biology (nerve|vessel|muscle)
- camera: wide|face_zoom
- At least one beat in section 3 MUST have xray=true (prefer beat_index={xray_hint})
- layout default: teal/slate world; crib_door_scene: warm cream nursery (not teal walls)
- coral only for kinetic/bubble bars (not full-scene palette)
- props names only from the list

Return JSON:
{{"shots":[{{"beat_index":0,"pose":"stand","emotion":"neutral","props":[],"kinetic_text":null,"xray":false,"internal":"nerve","camera":"wide","layout":"default","bubble_bars":0}}]}}

BEATS:
{beats_json}
"""


def _rule_based_shots(tl: VisualTimeline) -> list[dict[str, Any]]:
    """Deterministic fallback if LLM fails."""
    xray_i = ensure_core_data_xray_slot(tl)
    shots: list[dict[str, Any]] = []
    for b in tl.beats:
        pose = "stand"
        emotion = "neutral"
        props: list[str] = []
        kinetic = None
        camera = "wide"
        xray = False
        internal = "nerve"
        low = b.text.lower()
        if b.section == 1:
            pose = "look_at_arms"
            emotion = "surprise"
        elif b.section == 2:
            pose = "think"
        elif b.section == 3:
            pose = "point"
            if re.search(r"\d|percent|study|nerve|muscle|brain", low):
                kinetic = re.sub(r"[^A-Za-z0-9 %]", "", b.text)[:28] or None
            if xray_i is not None and b.index == xray_i:
                xray = True
                internal = "nerve"
        elif b.section == 4:
            pose = "react"
            emotion = "worry"
        else:
            pose = "stand"
            emotion = "calm"
            camera = "face_zoom"
        layout = "default"
        bubble_bars = 0
        if "door" in low or "enter" in low:
            props = ["door"]
            pose = "enter_door"
        if any(k in low for k in ("crib", "baby", "infant", "nursery", "cradle")):
            layout = "crib_door_scene"
            props = ["crib", "door", "room_corner", "bubble"]
            bubble_bars = 3
            pose = "arms_up"
            emotion = "surprise"
        shots.append(
            {
                "beat_index": b.index,
                "pose": pose,
                "emotion": emotion,
                "props": props,
                "kinetic_text": kinetic,
                "xray": xray,
                "internal": internal,
                "camera": camera,
                "layout": layout,
                "bubble_bars": bubble_bars,
            }
        )
    return _enforce_xray(shots, tl)


def _enforce_xray(shots: list[dict[str, Any]], tl: VisualTimeline) -> list[dict[str, Any]]:
    if any(s.get("xray") for s in shots):
        return shots
    hint = ensure_core_data_xray_slot(tl)
    if hint is None and shots:
        hint = shots[len(shots) // 2]["beat_index"]
    for s in shots:
        if int(s.get("beat_index", -1)) == hint:
            s["xray"] = True
            s["internal"] = s.get("internal") or "nerve"
            break
    return shots


def _normalize_shot(raw: dict[str, Any], beat_index: int) -> dict[str, Any]:
    pose = str(raw.get("pose") or "stand").lower()
    if pose not in POSES:
        pose = "stand"
    props = raw.get("props") or []
    if isinstance(props, str):
        props = [props]
    props = [str(p).lower() for p in props if str(p).lower() in PROPS][:3]
    internal = str(raw.get("internal") or "nerve").lower()
    if internal not in {"nerve", "vessel", "muscle"}:
        internal = "nerve"
    camera = str(raw.get("camera") or "wide").lower()
    if camera not in {"wide", "face_zoom"}:
        camera = "wide"
    kt = raw.get("kinetic_text")
    if kt is not None:
        kt = str(kt).strip() or None
    layout = str(raw.get("layout") or "default").lower()
    if layout not in {"default", "crib_door_scene"}:
        layout = "default"
    try:
        bubble_bars = int(raw.get("bubble_bars") or 0)
    except (TypeError, ValueError):
        bubble_bars = 0
    bubble_bars = max(0, min(5, bubble_bars))
    return {
        "beat_index": beat_index,
        "pose": pose,
        "emotion": str(raw.get("emotion") or "neutral").lower(),
        "props": props,
        "kinetic_text": kt,
        "xray": bool(raw.get("xray")),
        "internal": internal,
        "camera": camera,
        "layout": layout,
        "bubble_bars": bubble_bars,
    }


def _parse_shots_payload(text: str) -> dict[str, Any]:
    data = parse_json_object(text)
    if not isinstance(data.get("shots"), list):
        raise LLMError("missing shots array")
    return data


class WeirdBiologyShotPlanner:
    def __init__(
        self,
        *,
        model: str | None = None,
        llm: LLMClient | None = None,
        openrouter: OpenRouterClient | None = None,
        router: WhbFreeFirstRouter | None = None,
    ):
        self.settings = get_settings()
        self.model = (model or planner_model() or PLANNER_MODEL).strip()
        self._llm = llm
        self._openrouter = openrouter
        self._router = router

    @property
    def llm(self) -> LLMClient:
        return self._get_llm()

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.settings, default_model=self.model)
        return self._llm

    def _get_router(self) -> WhbFreeFirstRouter:
        if self._router is None:
            self._router = WhbFreeFirstRouter(
                self.settings,
                channel="weird_human_biology",
                openrouter=self._openrouter,
            )
        return self._router

    def _fallback_shots_llm(self, system: str, user: str) -> dict[str, Any]:
        llm = self._get_llm()
        try:
            data = llm.chat_json(
                system=system,
                user=user,
                temperature=0.3,
                model=self.model,
                timeout_s=120.0,
                same_model_retries=1,
                fallback_model=None,
            )
        except LLMError:
            text = llm.chat_text(
                system=system,
                user=user,
                temperature=0.3,
                model=self.model,
                timeout_s=120.0,
            )
            data = parse_json_object(text)
        if not isinstance(data.get("shots"), list):
            raise LLMError("missing shots array")
        return data

    def plan(
        self,
        tl: VisualTimeline,
        *,
        batch_size: int = 24,
    ) -> dict[str, Any]:
        """Return shot_plan dict with shots aligned to beats."""
        if not tl.beats:
            return {"model": self.model, "shots": [], "source": "empty"}

        # Try LLM in batches; on failure use rules for that batch
        all_shots: dict[int, dict[str, Any]] = {}
        source = "llm"
        models_used: list[str] = []
        xray_hint = ensure_core_data_xray_slot(tl)
        for start in range(0, len(tl.beats), batch_size):
            batch = tl.beats[start : start + batch_size]
            beats_payload = [
                {
                    "beat_index": b.index,
                    "section": b.section,
                    "text": b.text[:220],
                    "duration_s": round(b.duration_s, 2),
                }
                for b in batch
            ]
            user = _USER_TMPL.format(
                poses=", ".join(POSES),
                props=", ".join(PROPS),
                xray_hint=xray_hint if xray_hint is not None else "middle of section 3",
                beats_json=json.dumps(beats_payload, ensure_ascii=False),
            )
            try:
                data, meta = self._get_router().call(
                    system=_SYSTEM,
                    user=user,
                    parse=_parse_shots_payload,
                    fallback=lambda u=user: self._fallback_shots_llm(_SYSTEM, u),
                    fallback_model=self.model,
                    stage=STAGE_SHOT_PLAN,
                    temperature=0.3,
                    timeout_s=120.0,
                )
                models_used.append(meta.model_used)
                raw_shots = data.get("shots") if isinstance(data, dict) else None
                if not isinstance(raw_shots, list):
                    raise LLMError("missing shots array")
                by_idx = {
                    int(s.get("beat_index")): s
                    for s in raw_shots
                    if isinstance(s, dict) and s.get("beat_index") is not None
                }
                for b in batch:
                    raw = by_idx.get(b.index) or {
                        "beat_index": b.index,
                        "pose": "stand",
                    }
                    all_shots[b.index] = _normalize_shot(raw, b.index)
            except Exception:  # noqa: BLE001
                source = "llm+rules_fallback"
                for s in _rule_based_shots(
                    VisualTimeline(
                        beats=batch,
                        full_vo_duration_s=tl.full_vo_duration_s,
                        topic=tl.topic,
                    )
                ):
                    all_shots[int(s["beat_index"])] = s

        ordered = [all_shots[b.index] for b in tl.beats if b.index in all_shots]
        # Fill any gaps
        for b in tl.beats:
            if b.index not in all_shots:
                ordered.append(
                    _normalize_shot({"beat_index": b.index, "pose": "stand"}, b.index)
                )
        ordered.sort(key=lambda s: int(s["beat_index"]))
        ordered = _enforce_xray(ordered, tl)
        return {
            "model": models_used[-1] if models_used else self.model,
            "fallback_model": self.model,
            "free_model": FREE_MODEL,
            "source": source,
            "shots": ordered,
        }


def shots_to_specs(plan: dict[str, Any]) -> list[ShotSpec]:
    return [
        ShotSpec.from_dict(s, seed=int(s.get("beat_index") or i))
        for i, s in enumerate(plan.get("shots") or [])
    ]
