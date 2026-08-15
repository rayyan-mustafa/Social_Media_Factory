"""Headless RMagine-style vision judge (PASS only if confidence >= 8).

Rayyan lock: vision model is ALWAYS ``openrouter/free`` (never paid / never
other free VL ids). The free router often prepends a safety line
(``User Safety: safe|unsafe``); we strip that and force JSON.

Policy (napstorian + napping_historian): Wiki/Met PD/OA first → this judge →
PASS uses the archival still; REJECT / unavailable → Flux for that slot.
Never promote a FAIL/weak PD into compose. Never provisional-PASS when down.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from src.services.settings import Settings, get_settings

log = logging.getLogger(__name__)

PASS_THRESHOLD = 8
# Hard lock — do not read env overrides to a paid/other model.
VISION_MODEL_LOCKED = "openrouter/free"
_VISION_MAX_EDGE = 1280
_VISION_MAX_FILE_BYTES = 12 * 1024 * 1024  # refuse-before-read hard reject
_SAFETY_LINE_RE = re.compile(
    r"^\s*(?:User Safety|Response Safety):\s*(safe|unsafe)\s*$",
    re.IGNORECASE,
)
_SAFETY_CAT_RE = re.compile(r"^\s*Safety Categories:\s*.*$", re.IGNORECASE)
# openrouter/free honors json_schema and skips safety-only preamble noise.
_JUDGE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "vision_judge",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["PASS", "FAIL"]},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["status", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}

# Free-pool pacing: serialize VL calls process-wide + space them out.
_VISION_MIN_INTERVAL_S = float(
    (os.environ.get("VISION_MIN_INTERVAL_S") or "1.8").strip() or "1.8"
)
_VISION_429_RETRIES = int((os.environ.get("VISION_429_RETRIES") or "6").strip() or "6")


class _VisionRateGate:
    """Process-wide mutex + cooldown so 2 scene workers cannot 429 the free pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def acquire(self) -> None:
        """Block until our turn; hold serialization across the HTTP call via lock."""
        self._lock.acquire()
        try:
            now = time.monotonic()
            wait = self._next_ok - now
            if wait > 0:
                time.sleep(wait)
        except Exception:
            self._lock.release()
            raise

    def release(self, *, penalize_s: float = 0.0) -> None:
        gap = max(_VISION_MIN_INTERVAL_S, float(penalize_s or 0.0))
        # Never shrink an existing 429 cooldown.
        self._next_ok = max(self._next_ok, time.monotonic() + gap)
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def note_429(self, retry_after_s: float) -> None:
        """Extend cooldown after a 429 (caller still holds lock or will re-acquire)."""
        self._next_ok = max(
            self._next_ok, time.monotonic() + max(2.0, float(retry_after_s))
        )


_VISION_GATE = _VisionRateGate()


def _retry_after_seconds(resp: httpx.Response, *, attempt: int) -> float:
    """Parse Retry-After / exponential backoff for free-pool 429s.

    Cap waits — free-pool resets often advertise long windows; grinding 90s×N
    stalls the whole farm. Prefer paced retries over multi-minute sleeps.
    """
    cap = 25.0
    hdr = (resp.headers.get("Retry-After") or "").strip()
    if hdr:
        try:
            return max(2.0, min(cap, float(hdr)))
        except ValueError:
            pass
    # OpenRouter sometimes puts reset hints in body / x-ratelimit headers.
    for key in ("X-RateLimit-Reset", "x-ratelimit-reset"):
        raw = (resp.headers.get(key) or "").strip()
        if not raw:
            continue
        try:
            val = float(raw)
            # epoch seconds vs delta seconds
            if val > 1_000_000_000:
                delta = val - time.time()
                return max(2.0, min(cap, delta))
            return max(2.0, min(cap, val))
        except ValueError:
            continue
    return min(cap, 2.0 * (2**attempt))


def _b64_data_url(path: Path) -> str | None:
    """Encode a downscaled JPEG for VL — never load 50–100MB originals into RAM."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > _VISION_MAX_FILE_BYTES:
        return None
    suf = path.suffix.lower()
    if suf in {".pdf", ".djvu", ".svg", ".tif", ".tiff", ".gif"}:
        return None
    try:
        from PIL import Image

        # Bound decompression bombs before decode.
        Image.MAX_IMAGE_PIXELS = 40_000_000
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, float(_VISION_MAX_EDGE) / float(max(w, h, 1)))
            if scale < 1.0:
                nw = max(1, int(w * scale))
                nh = max(1, int(h * scale))
                im = im.resize((nw, nh), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
            raw = buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("vision_judge encode failed %s: %s", path.name, exc)
        return None
    if not raw or len(raw) > 4_000_000:
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _strip_safety_preamble(text: str) -> tuple[str, str | None]:
    """Return (remainder, safety) where safety is 'safe'|'unsafe'|None."""
    lines = (text or "").strip().splitlines()
    safety: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _SAFETY_LINE_RE.match(line)
        if m:
            safety = m.group(1).lower()
            i += 1
            if i < len(lines) and _SAFETY_CAT_RE.match(lines[i].strip()):
                i += 1
            continue
        break
    rest = "\n".join(lines[i:]).strip()
    return rest, safety


def _parse_judge_json(text: str) -> dict[str, Any] | None:
    from src.services.llm import parse_json_object

    body, safety = _strip_safety_preamble(text)
    if safety == "unsafe" and ("{" not in body):
        return {
            "status": "FAIL",
            "confidence": 0,
            "reason": "vision safety gate: unsafe",
        }
    if not body or "{" not in body:
        return None
    # Free models often wrap JSON in ```json fences — strip before parse.
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```\s*$", body, re.IGNORECASE)
    if fence:
        body = fence.group(1).strip()
    try:
        obj = parse_json_object(body)
    except Exception:  # noqa: BLE001
        obj = None
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            try:
                cand = json.loads(body[start : end + 1])
                if isinstance(cand, dict):
                    obj = cand
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return None
    # Normalize status casing from loose free models.
    st = str(obj.get("status") or "").strip().upper()
    if st in {"PASS", "FAIL"}:
        obj["status"] = st
    return obj


def _chat_vision(
    *,
    client: httpx.Client,
    key: str,
    messages: list[dict[str, Any]],
    temperature: float,
    response_format: dict[str, Any] | None = None,
    retries: int | None = None,
) -> str:
    """POST chat/completions with process-wide pacing + 429 backoff retries."""
    payload: dict[str, Any] = {
        "model": VISION_MODEL_LOCKED,
        "temperature": temperature,
        "messages": messages,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost/new_yt_automation",
        "X-Title": "new_yt_automation-vision-judge",
    }
    max_tries = max(1, int(retries if retries is not None else _VISION_429_RETRIES))
    last_err = ""
    for attempt in range(max_tries):
        _VISION_GATE.acquire()
        try:
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            if r.status_code == 429:
                wait = _retry_after_seconds(r, attempt=attempt)
                _VISION_GATE.note_429(wait)
                last_err = (
                    f"openrouter/free rate-limited (429) attempt "
                    f"{attempt + 1}/{max_tries}; sleep {wait:.1f}s"
                )
                log.warning("vision_judge %s", last_err)
                # release with penalty already applied via note_429
                _VISION_GATE.release(penalize_s=0.0)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                _VISION_GATE.release()
                raise RuntimeError(
                    f"openrouter/free HTTP {r.status_code}: {(r.text or '')[:240]}"
                )
            r.raise_for_status()
            msg = (((r.json().get("choices") or [{}])[0]).get("message") or {})
            text = msg.get("content") or ""
            if isinstance(text, list):
                text = "".join(
                    str(p.get("text") or "") for p in text if isinstance(p, dict)
                )
            _VISION_GATE.release()
            return str(text)
        except RuntimeError:
            raise
        except Exception:
            try:
                _VISION_GATE.release()
            except Exception:  # noqa: BLE001
                pass
            raise
    raise RuntimeError(
        last_err
        or "openrouter/free rate-limited (429) — shared free pool; retries exhausted"
    )


def _openrouter_vision(
    *,
    settings: Settings,
    system: str,
    user_text: str,
    image_urls: list[str],
    temperature: float = 0.2,
    http_retries: int | None = None,
) -> dict[str, Any] | None:
    key = (settings.openrouter_api_key or "").strip()
    if not key or key == "replace_me":
        return None

    # Rayyan: ALWAYS openrouter/free — ignore settings/env paid/other VL ids.
    # Image first, then short judge text — free VL models attend better this way.
    content: list[dict[str, Any]] = []
    for u in image_urls[:3]:
        content.append({"type": "image_url", "image_url": {"url": u}})
    content.append({"type": "text", "text": user_text})

    system_locked = (
        system
        + "\nOutput MUST be a single JSON object "
        '{"status":"PASS"|"FAIL","confidence":number,"reason":"string"} '
        "with no markdown. A User Safety preamble is allowed only if JSON follows."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_locked},
        {"role": "user", "content": content},
    ]
    tries = http_retries

    try:
        with httpx.Client(timeout=90.0) as client:
            # Prefer structured outputs — free router picks a schema-capable VL.
            try:
                text_s = _chat_vision(
                    client=client,
                    key=key,
                    messages=messages,
                    temperature=temperature,
                    response_format=_JUDGE_RESPONSE_FORMAT,
                    retries=tries,
                )
            except RuntimeError as exc:
                # Some free providers reject json_schema — fall back to plain.
                if "HTTP 400" not in str(exc):
                    raise
                log.info(
                    "vision_judge json_schema unsupported (%s); plain retry",
                    str(exc)[:120],
                )
                text_s = _chat_vision(
                    client=client,
                    key=key,
                    messages=messages,
                    temperature=temperature,
                    response_format=None,
                    retries=tries,
                )
            parsed = _parse_judge_json(text_s)
            if parsed:
                return parsed

            # Safety-only / empty first turn — force JSON WITH image re-attached
            # (downscaled JPEG; cheap). Dropping the image caused empty force fails.
            log.info(
                "vision_judge openrouter/free non-JSON (len=%s); forcing JSON+image",
                len(text_s or ""),
            )
            force_content: list[dict[str, Any]] = []
            for u in image_urls[:3]:
                force_content.append(
                    {"type": "image_url", "image_url": {"url": u}}
                )
            force_content.append(
                {
                    "type": "text",
                    "text": (
                        "Reply with ONLY this JSON object (no safety lines): "
                        '{"status":"PASS" or "FAIL","confidence":0-10,'
                        '"reason":"one short sentence"}'
                    ),
                }
            )
            messages2: list[dict[str, Any]] = [
                {"role": "system", "content": system_locked},
                {"role": "user", "content": content},
                {"role": "assistant", "content": text_s or "User Safety: safe"},
                {"role": "user", "content": force_content},
            ]
            time.sleep(0.8)  # soft pace against free-pool 429
            try:
                text2 = _chat_vision(
                    client=client,
                    key=key,
                    messages=messages2,
                    temperature=0.0,
                    response_format=_JUDGE_RESPONSE_FORMAT,
                    retries=tries,
                )
            except RuntimeError as exc:
                if "HTTP 400" in str(exc):
                    text2 = _chat_vision(
                        client=client,
                        key=key,
                        messages=messages2,
                        temperature=0.0,
                        response_format=None,
                        retries=tries,
                    )
                else:
                    raise
            parsed2 = _parse_judge_json(text2)
            if parsed2:
                return parsed2
            log.warning(
                "vision_judge openrouter/free still non-JSON after force turn: %r",
                (text2 or "")[:160],
            )
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("vision_judge openrouter failed: %s", exc)
        return None

def validate_image_with_vision_llm(
    image_ref: str,
    expected_context: str,
    historical_figure: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Validate one image. image_ref = http(s) URL or local path."""
    settings = settings or get_settings()
    identity = ""
    if historical_figure:
        identity = (
            f'\nCRITICAL IDENTITY CHECK: expected figure is "{historical_figure}". '
            "FAIL (confidence < 5) if a different person is shown."
        )
    system = (
        "CRITICAL: return ONLY raw JSON: "
        '{"status":"PASS/FAIL","confidence":number,"reason":"string"}'
    )
    user = f"""You are a documentary stills QA judge.
Scene context: {expected_context}
{identity}
Rules:
1. status PASS only if the image truly matches the scene.
2. Only award confidence 8, 9, or 10 (and PASS) for a strong match.
3. Otherwise FAIL with confidence 0-7.
Respond JSON: {{"status":"PASS"|"FAIL","confidence":1-10,"reason":"..."}}"""

    urls: list[str] = []
    if image_ref.startswith("http://") or image_ref.startswith("https://"):
        urls = [image_ref]
    else:
        p = Path(image_ref)
        data = _b64_data_url(p) if p.is_file() else None
        if data:
            urls = [data]

    if not urls:
        return {"status": "FAIL", "confidence": 0, "reason": "no image bytes/url"}

    parsed = _openrouter_vision(
        settings=settings, system=system, user_text=user, image_urls=urls
    )
    if not parsed:
        return {
            "status": "FAIL",
            "confidence": 0,
            "reason": "vision API unavailable — treat as FAIL → AI fill",
        }
    conf = int(parsed.get("confidence") or 0)
    status = (
        "PASS"
        if parsed.get("status") == "PASS" and conf >= PASS_THRESHOLD
        else "FAIL"
    )
    return {
        "status": status,
        "confidence": conf,
        "reason": str(parsed.get("reason") or "")[:400],
        "model": VISION_MODEL_LOCKED,
    }


def probe_vision_ready(
    *,
    settings: Settings | None = None,
    retries: int = 2,
) -> dict[str, Any]:
    """Preflight: confirm openrouter/free returns parseable judge JSON.

    Call BEFORE farm visuals so we do not burn TTS/RunPod/time when vision is down.
    """
    settings = settings or get_settings()
    key = (settings.openrouter_api_key or "").strip()
    if not key or key == "replace_me":
        return {
            "ok": False,
            "model": VISION_MODEL_LOCKED,
            "error": "OPENROUTER_API_KEY missing",
        }

    # Valid tiny JPEG (bogus bytes → OpenRouter 400 on free router).
    try:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), color=(110, 110, 110)).save(
            buf, format="JPEG", quality=70
        )
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "model": VISION_MODEL_LOCKED,
            "error": f"cannot build probe jpeg: {exc}",
        }
    url = f"data:image/jpeg;base64,{b64}"
    last_err = ""
    for attempt in range(max(1, retries)):
        parsed = _openrouter_vision(
            settings=settings,
            system=(
                "CRITICAL: return ONLY raw JSON: "
                '{"status":"FAIL","confidence":0,"reason":"probe"}'
            ),
            user_text=(
                "Probe image (ignore content). Reply JSON "
                '{"status":"FAIL","confidence":0,"reason":"probe_ok"}.'
            ),
            image_urls=[url],
            temperature=0.0,
            http_retries=2,  # preflight must not grind 6×25s on free-pool 429
        )
        if parsed and isinstance(parsed.get("status"), str):
            return {
                "ok": True,
                "model": VISION_MODEL_LOCKED,
                "sample": {
                    "status": parsed.get("status"),
                    "confidence": parsed.get("confidence"),
                },
                "attempt": attempt + 1,
            }
        last_err = "openrouter/free did not return parseable judge JSON"
        time.sleep(1.5 * (attempt + 1))
    return {
        "ok": False,
        "model": VISION_MODEL_LOCKED,
        "error": last_err,
    }


def vision_llm_judge(
    scene_text: str,
    candidates: list[dict[str, Any]],
    historical_figure: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Judge up to 3 candidates. Returns best_candidate_id (url/path) or null."""
    settings = settings or get_settings()
    if not candidates:
        return {
            "best_candidate_id": None,
            "confidence": 0,
            "reasoning": "No candidates provided",
        }

    top = candidates[:3]
    best_id: str | None = None
    best_conf = 0
    reasons: list[str] = []
    for i, c in enumerate(top):
        ref = str(c.get("url") or c.get("path") or c.get("local_path") or "")
        if not ref:
            continue
        # Pace free-pool calls — concurrent scene workers otherwise 429.
        if i:
            time.sleep(0.6)
        v = validate_image_with_vision_llm(
            ref, scene_text, historical_figure, settings=settings
        )
        reasons.append(
            f"[{i}] {v.get('status')} {v.get('confidence')}: {v.get('reason')}"
        )
        conf = int(v.get("confidence") or 0)
        if v.get("status") == "PASS" and conf >= PASS_THRESHOLD and conf > best_conf:
            best_conf = conf
            best_id = ref

    return {
        "best_candidate_id": best_id,
        "confidence": best_conf if best_id else 0,
        "reasoning": " | ".join(reasons)[:800],
    }


def pick_approved_or_none(
    scene_text: str,
    approved_assets: list[dict[str, Any]],
    historical_figure: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Convenience: map staged assets → judge → winner path or None (AI fill)."""
    cands = []
    for a in approved_assets[:3]:
        cands.append(
            {
                "url": a.get("source_url"),
                "path": a.get("local_path"),
                "local_path": a.get("local_path"),
            }
        )
    judged = vision_llm_judge(
        scene_text, cands, historical_figure, settings=settings
    )
    winner = judged.get("best_candidate_id")
    return {
        **judged,
        "use_path": winner,
        "ai_fill": winner is None,
    }
