"""Prop Synthesizer Agent for Weird Biology.

Scans scripts, identifies required visual concepts, synthesizes Python SVG drawing code
via LLM, validates in a CairoSVG sandbox, and auto-registers new props into the persistent library.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

import cairosvg

from src.services.llm import LLMClient, LLMError, parse_json_object
from src.services.openrouter import OpenRouterClient
from src.services.weird_biology_prop_registry import (
    compile_prop_code,
    get_all_props,
    has_prop,
    register_custom_prop,
    render_custom_prop,
    search_props,
)
from src.services.weird_biology_style import palette_rgb, planner_model
from src.services.settings import get_settings
from src.services.whb_llm_router import WhbFreeFirstRouter

logger = logging.getLogger(__name__)

SYNTHESIZER_SYSTEM = (
    "You are an expert Python SVG graphics coder for the Weird Human Biology stickman channel. "
    "Your job is to write Python code snippet that appends SVG elements to a list `parts` to draw a hand-drawn, minimalist prop. "
    "Rules:\n"
    "- Output ONLY a JSON object containing key 'python_code'\n"
    "- Available variables in scope: W (canvas width e.g. 1280), H (canvas height e.g. 720), "
    "rng (random.Random instance for hand-drawn jitter), parts (list of SVG XML string parts), "
    "stroke (hex color e.g. '#111111'), fill (hex color e.g. '#5A7A7A'), coral (hex color e.g. '#E8724C'), "
    "white (hex color e.g. '#FFFFFF'), stick (hex color e.g. '#111111'), math module.\n"
    "- Keep shapes minimalist, clean, hand-drawn XKCD style. Use 3.0 to 5.0 stroke-width.\n"
    "- Position the prop in a sensible area on canvas (e.g. ground level H*0.82 or top-right corner H*0.2).\n"
    "- All paths, circles, rects, ellipses MUST be valid SVG XML format.\n"
    "- Example python_code:\n"
    "  bx, by = W * 0.75, H * 0.25\n"
    "  parts.append(f'<ellipse cx=\"{bx:.1f}\" cy=\"{by:.1f}\" rx=\"35\" ry=\"25\" fill=\"{fill}\" stroke=\"{stick}\" stroke-width=\"4\"/>')"
)


def validate_prop_code(prop_name: str, python_code: str) -> tuple[bool, str]:
    """Sandbox validation of synthesized Python SVG code using CairoSVG rasterization.

    Verifies python execution, valid SVG syntax, and CairoSVG rasterization.
    """
    W, H = 1280, 720
    pal = palette_rgb()
    rng = random.Random(42)
    parts: list[str] = []

    # 1. Test execute python code
    def _hex(rgb: tuple[int, int, int]) -> str:
        return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"

    stroke = _hex(pal.get("prop_stroke", (0x11, 0x11, 0x11)))
    fill = _hex(pal.get("prop_fill", (0x5A, 0x7A, 0x7A)))
    coral = _hex(pal.get("coral_accent", (0xE8, 0x72, 0x4C)))
    white = _hex(pal.get("head_fill", (0xFF, 0xFF, 0xFF)))
    stick = _hex(pal.get("stick_stroke", (0x11, 0x11, 0x11)))

    import math

    exec_scope = {
        "W": W,
        "H": H,
        "rng": rng,
        "pal": pal,
        "parts": parts,
        "stroke": stroke,
        "fill": fill,
        "coral": coral,
        "white": white,
        "stick": stick,
        "math": math,
        "_hex": _hex,
    }

    exec_scope["__builtins__"] = {
        "range": range,
        "min": min,
        "max": max,
        "abs": abs,
        "round": round,
        "len": len,
    }
    try:
        exec(compile_prop_code(python_code), exec_scope)  # noqa: S102
    except Exception as err:
        return False, f"Python execution error: {err}"

    if not parts:
        return False, "Code executed but generated no SVG elements in 'parts'"

    # 2. Test SVG XML wrapping & CairoSVG compilation
    svg_content = (
        f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">\n'
        + "\n".join(parts)
        + "\n</svg>"
    )

    try:
        png_data = cairosvg.svg2png(bytestring=svg_content.encode("utf-8"))
        if not png_data or len(png_data) < 100:
            return False, "CairoSVG output empty or truncated"
    except Exception as err:
        return False, f"CairoSVG rendering error: {err}"

    return True, "OK"


class PropSynthesizer:
    """Agent that synthesizes missing visual props for Weird Biology scripts."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = planner_model()
        self.router = WhbFreeFirstRouter(self.settings)
        self.llm = LLMClient(self.settings, default_model=self.model)

    def _fallback_json(self, system: str, user: str) -> dict[str, Any]:
        try:
            return self.llm.chat_json(
                system=system,
                user=user,
                temperature=0.2,
                model=self.model,
                timeout_s=120.0,
                same_model_retries=1,
                fallback_model=None,
            )
        except LLMError:
            text = self.llm.chat_text(
                system=system,
                user=user,
                temperature=0.2,
                model=self.model,
                timeout_s=120.0,
            )
            return parse_json_object(text)

    def _request_json(self, system: str, user: str) -> dict[str, Any]:
        def parse(text: str) -> dict[str, Any]:
            data = parse_json_object(text)
            if not isinstance(data, dict):
                raise LLMError("expected JSON object")
            return data

        data, _meta = self.router.call(
            system=system,
            user=user,
            parse=parse,
            fallback=lambda: self._fallback_json(system, user),
            fallback_model=self.model,
            stage="prop_synthesizer",
            temperature=0.2,
            timeout_s=120.0,
        )
        return data

    def synthesize_prop(
        self,
        prop_name: str,
        description: str,
        topic: str = "",
        category: str = "biology",
        tags: list[str] | None = None,
        max_retries: int = 3,
    ) -> bool:
        """Synthesize, sandbox test, self-heal, and register a new prop."""
        clean_name = re.sub(
            r"[^a-z0-9_]", "", prop_name.strip().lower().replace("-", "_")
        )
        if not clean_name:
            return False

        if has_prop(clean_name):
            logger.info("Prop '%s' already exists in registry", clean_name)
            return True

        user_prompt = (
            f"Topic context: {topic}\n"
            f"Prop Name: {clean_name}\n"
            f"Description: {description}\n\n"
            f"Write the Python SVG code for 'python_code' key in JSON response."
        )

        last_error = ""
        for attempt in range(1, max_retries + 1):
            if last_error:
                current_prompt = (
                    f"{user_prompt}\n\n"
                    f"Previous attempt failed validation error:\n{last_error}\n"
                    f"Please fix the code so it is 100% valid Python and SVG."
                )
            else:
                current_prompt = user_prompt

            try:
                parsed = self._request_json(SYNTHESIZER_SYSTEM, current_prompt)
                py_code = parsed.get("python_code") or ""
                if not py_code:
                    last_error = "JSON response missing 'python_code' key"
                    continue

                valid, msg = validate_prop_code(clean_name, py_code)
                if valid:
                    register_custom_prop(
                        name=clean_name,
                        description=description,
                        python_code=py_code,
                        category=category,
                        tags=tags or [clean_name, category],
                    )
                    logger.info(
                        "Successfully synthesized prop '%s' on attempt %d",
                        clean_name,
                        attempt,
                    )
                    return True
                else:
                    last_error = msg
                    logger.warning(
                        "Validation failed for '%s' (attempt %d/%d): %s",
                        clean_name,
                        attempt,
                        max_retries,
                        msg,
                    )
            except Exception as err:
                last_error = f"LLM Router error: {err}"
                logger.warning(
                    "Error during prop synthesis attempt %d: %s", attempt, err
                )

        logger.error("Failed to synthesize prop '%s' after %d attempts", clean_name, max_retries)
        return False

    def auto_expand_props_for_script(
        self, script_text: str, topic: str = ""
    ) -> list[str]:
        """Scan script, determine missing visual props, synthesize them, and return active prop list."""
        system_prompt = (
            "You are a visual director for minimalist stickman biology videos. "
            "Examine the script narration and topic. Identify 1 to 3 distinct physical/biological props "
            "that would make the visuals look awesome and engaging (e.g., 'earwax_tunnel', 'stomach_acid', 'ribcage_contour'). "
            "Reply with ONLY JSON: {\"props\": [{\"name\": \"...\", \"description\": \"...\", \"tags\": [\"...\"]}]}"
        )
        user_prompt = f"Topic: {topic}\n\nScript:\n{script_text[:1500]}"

        ensured_props: list[str] = []
        try:
            parsed = self._request_json(system_prompt, user_prompt)
            props_list = parsed.get("props") or []
            if isinstance(props_list, list):
                for p_item in props_list:
                    if isinstance(p_item, dict):
                        p_name = p_item.get("name") or ""
                        p_desc = p_item.get("description") or p_name
                        p_tags = p_item.get("tags") or []
                        clean = re.sub(
                            r"[^a-z0-9_]", "", p_name.strip().lower().replace("-", "_")
                        )
                        if not clean:
                            continue

                        # Check if a matching or similar prop exists in registry
                        matches = search_props([clean] + p_tags)
                        if matches and matches[0][1] >= 1.0:
                            ensured_props.append(matches[0][0])
                        else:
                            # Synthesize new prop!
                            success = self.synthesize_prop(
                                prop_name=clean,
                                description=p_desc,
                                topic=topic,
                                tags=p_tags,
                            )
                            if success:
                                ensured_props.append(clean)
        except Exception as err:
            logger.warning("Error during auto_expand_props_for_script: %s", err)

        return ensured_props
