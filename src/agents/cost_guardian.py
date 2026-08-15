"""Cost / Budget Guardian — abort when monthly spend exceeds caps.

Estimates are quantity × unit prices for what this farm actually uses:
ephemeral RunPod Comfy/Flux stills (pod A5000 primary), WaveSpeed LLMs,
OpenRouter meta ($0 free), WaveSpeed Seedream thumbs (always on), Kokoro TTS ($0),
VPS FFmpeg ($0). Default production pack: retention (~15 min / 125 stills).
"""

from __future__ import annotations

import json
import math
from typing import Any

from src.agents.ledger import OpsLedger
from src.agents.store import OpsStore
from src.services.retention_profile import active_profile_name
from src.services.settings import CONFIG_DIR, get_settings


class CostGuardian:
    def __init__(self, store: OpsStore | None = None, ledger: OpsLedger | None = None):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.cfg = _load_agents_settings()
        self.s = get_settings()

    def _default_profile(self) -> str:
        rates = self.cfg.get("cost_rates") or {}
        return (
            str(rates.get("production_pack_profile") or "").strip().lower()
            or active_profile_name()
            or "retention"
        )

    def estimate_video_breakdown(self, *, profile: str | None = None) -> dict[str, Any]:
        """Full per-video cost breakdown from real unit rates × usage quantities."""
        rates = self.cfg.get("cost_rates") or {}
        prof = (profile or self._default_profile()).strip().lower()
        if rates.get("use_explicit_video_estimates"):
            key = {
                "longform": "estimated_usd_per_longform_video",
                "epic": "estimated_usd_per_epic_video",
                "retention": "estimated_usd_per_retention_video",
            }.get(prof, "estimated_usd_per_retention_video")
            total = float(rates.get(key) or 0.0)
            return {
                "profile": prof,
                "mode": "explicit",
                "total_usd": round(total, 4),
                "line_items": {"explicit_override": round(total, 4)},
                "notes": rates.get("notes"),
            }

        stills = self._stills_for_profile(prof, rates)
        retry_mult = float(rates.get("runpod_retry_multiplier") or 1.05)
        usd_per_still = rates.get("runpod_usd_per_still")
        sec = float(
            rates.get("runpod_estimate_seconds_per_still")
            or rates.get("runpod_measured_seconds_per_still_median")
            or 30.5
        )
        visuals_mode = str(rates.get("runpod_visuals_mode") or "pod").lower()
        if visuals_mode == "pod":
            hr_rate = float(
                rates.get("runpod_pod_a5000_usd_per_hour")
                or rates.get("runpod_comfy_usd_per_hour")
                or 0.16
            )
            gpu_label = str(
                rates.get("runpod_gpu_class")
                or "RTX A5000 Community ($0.16/hr)"
            )
        else:
            hr_rate = float(
                rates.get("runpod_serverless_usd_per_hour")
                or rates.get("runpod_comfy_usd_per_hour")
                or 0.69
            )
            gpu_label = "PRO 6000 MIG serverless ($0.69/hr)"
        cold_min = float(rates.get("runpod_cold_start_overhead_minutes") or 0.0)
        # Prefer empirical $/still when set; else seconds × hourly (+ optional cold start).
        if usd_per_still is not None and float(usd_per_still) > 0:
            runpod = stills * float(usd_per_still)
            runpod_mode = "usd_per_still"
            usd_still_eff = float(usd_per_still)
        else:
            runpod = stills * (sec / 3600.0) * hr_rate * retry_mult
            if cold_min > 0:
                runpod += (cold_min / 60.0) * hr_rate
            runpod_mode = "seconds_x_hourly_plus_cold"
            usd_still_eff = (sec / 3600.0) * hr_rate * retry_mult

        llm_parts = self._llm_cost_usd(prof, rates)
        llm = float(llm_parts["total_usd"])

        openrouter_calls = int(rates.get("openrouter_meta_calls_per_video") or 2)
        openrouter_rate = float(rates.get("openrouter_usd_per_call") or 0.0)
        openrouter = openrouter_calls * openrouter_rate

        # Seedream thumb is always required in production.
        thumbs = max(1, int(rates.get("seedream_thumbs_per_video") or 1))
        if rates.get("seedream_thumbs_required") is False:
            thumbs = int(rates.get("seedream_thumbs_per_video") or 0)
        thumb_rate = float(rates.get("wavespeed_seedream_thumb_usd_per_image") or 0.035)
        thumb = thumbs * thumb_rate

        ai_clips = 0.0
        if bool(getattr(self.s, "enable_ai_video_clips", False)):
            n = int(getattr(self.s, "max_ai_video_clips_per_job", 0) or 0)
            sec_clip = float(getattr(self.s, "ai_video_seconds_per_clip", 5.0) or 5.0)
            per_s = float(rates.get("runpod_pruna_video_usd_per_second") or 0.02)
            ai_clips = n * sec_clip * per_s

        kokoro = float(rates.get("kokoro_tts") or 0.0)
        ffmpeg = float(rates.get("vps_ffmpeg") or 0.0)
        youtube = float(rates.get("youtube_api") or 0.0)

        line_items = {
            "runpod_stills": round(runpod, 4),
            "wavespeed_llm": round(llm, 4),
            "openrouter_meta": round(openrouter, 4),
            "wavespeed_seedream_thumb": round(thumb, 4),
            "runpod_ai_video_clips": round(ai_clips, 4),
            "kokoro_tts": round(kokoro, 4),
            "vps_ffmpeg": round(ffmpeg, 4),
            "youtube_api": round(youtube, 4),
        }
        total = sum(line_items.values())

        # A40 Secure last-resort stills $ (same still count / sec) for comparison.
        a40_hr = float(rates.get("runpod_pod_a40_secure_usd_per_hour") or 0.44)
        a40_stills = stills * (sec / 3600.0) * a40_hr * retry_mult
        if cold_min > 0:
            a40_stills += (cold_min / 60.0) * a40_hr
        a40_delta = round(a40_stills - runpod, 4)

        # Realistic Community fallback: 3090 @$0.22/hr (when A5000 Community empty).
        rtx3090_hr = float(rates.get("runpod_pod_3090_usd_per_hour") or 0.22)
        rtx3090_stills = stills * (sec / 3600.0) * rtx3090_hr * retry_mult
        if cold_min > 0:
            rtx3090_stills += (cold_min / 60.0) * rtx3090_hr

        return {
            "profile": prof,
            "mode": "unit_rates",
            "currency": rates.get("currency") or "USD",
            "total_usd": round(total, 4),
            "total_usd_rounded": round(total, 2),
            "line_items": line_items,
            "quantities": {
                "stills": stills,
                "runpod_mode": runpod_mode,
                "runpod_usd_per_still": (
                    float(usd_per_still) if usd_per_still is not None else None
                ),
                "runpod_usd_per_still_effective": round(usd_still_eff, 6),
                "runpod_seconds_per_still": sec,
                "runpod_usd_per_hour": hr_rate,
                "runpod_retry_multiplier": retry_mult,
                "runpod_gpu_class": gpu_label,
                "runpod_visuals_mode": visuals_mode,
                "runpod_cold_start_minutes": cold_min,
                "llm": llm_parts.get("quantities"),
                "openrouter_calls": openrouter_calls,
                "openrouter_model": rates.get("openrouter_model")
                or getattr(self.s, "openrouter_model", "openrouter/free"),
                "seedream_thumbs": thumbs,
                "seedream_usd_per_image": thumb_rate,
                "seedream_required": rates.get("seedream_thumbs_required", True) is not False,
                "ai_video_clips_enabled": bool(
                    getattr(self.s, "enable_ai_video_clips", False)
                ),
                "fallback_3090_community_stills_usd": round(rtx3090_stills, 4),
                "fallback_a40_secure_stills_usd": round(a40_stills, 4),
                "fallback_a40_secure_extra_usd_vs_primary": a40_delta,
            },
            "pricing_sources": rates.get("pricing_sources") or {},
            "notes": rates.get("notes"),
            "formula": {
                "stills": (
                    f"{stills} stills × ({sec}/3600) × ${hr_rate}/hr × {retry_mult}"
                    f" + {cold_min}min cold"
                    if runpod_mode == "seconds_x_hourly_plus_cold"
                    else f"{stills} × ${usd_per_still}/still"
                ),
                "total": "stills + wavespeed_llm + seedream_thumb (+ $0 kokoro/ffmpeg/meta)",
            },
        }

    def estimate_video_usd(self, *, profile: str | None = None) -> float:
        """Estimate $ for one production video (rounded to cents for budget checks)."""
        return float(self.estimate_video_breakdown(profile=profile)["total_usd_rounded"])

    def estimate_monthly_pack(
        self,
        *,
        channels: int | None = None,
        videos_per_channel: int | None = None,
        profile: str | None = None,
        include_network_volume: bool = False,
        pkr_per_usd: float | None = None,
    ) -> dict[str, Any]:
        """Pakistan pack: N channels × retention videos/mo (Kokoro + Seedream required)."""
        rates = self.cfg.get("cost_rates") or {}
        channels = int(
            channels
            if channels is not None
            else rates.get("production_pack_channels")
            or 2
        )
        videos_per_channel = int(
            videos_per_channel
            if videos_per_channel is not None
            else rates.get("production_pack_videos_per_channel")
            or 15
        )
        profile = (
            profile
            or rates.get("production_pack_profile")
            or "retention"
        ).strip().lower()
        pkr_per_usd = float(
            pkr_per_usd
            if pkr_per_usd is not None
            else rates.get("pkr_per_usd_approx")
            or 280.0
        )
        br = self.estimate_video_breakdown(profile=profile)
        items = dict(br.get("line_items") or {})
        # Seedream is always required in production — never zero out.
        if float(items.get("wavespeed_seedream_thumb") or 0) <= 0:
            items["wavespeed_seedream_thumb"] = round(
                float(rates.get("wavespeed_seedream_thumb_usd_per_image") or 0.035)
                * max(1, int(rates.get("seedream_thumbs_per_video") or 1)),
                4,
            )
        per_video = round(sum(float(v) for v in items.values()), 4)
        n = channels * videos_per_channel
        subtotal = round(per_video * n, 4)
        # Netvol deleted 2026-08-07 — rate is 0 in config; weights re-download per pod.
        volume = (
            float(rates.get("runpod_network_volume_usd_per_month") or 0.0)
            if include_network_volume
            else 0.0
        )
        total = round(subtotal + volume, 4)
        a40_extra = float(
            (br.get("quantities") or {}).get(
                "fallback_a40_secure_extra_usd_vs_primary"
            )
            or 0.0
        )
        return {
            "profile": profile,
            "channels": channels,
            "videos_per_channel": videos_per_channel,
            "videos_per_month": n,
            "tts": str(rates.get("production_pack_tts") or "kokoro"),
            "seedream_required": True,
            "per_video_usd": per_video,
            "per_video_line_items": items,
            "per_video_formula": (br.get("formula") or {}),
            "stills_quantities": {
                k: (br.get("quantities") or {}).get(k)
                for k in (
                    "stills",
                    "runpod_seconds_per_still",
                    "runpod_usd_per_hour",
                    "runpod_usd_per_still_effective",
                    "runpod_cold_start_minutes",
                    "runpod_gpu_class",
                    "fallback_3090_community_stills_usd",
                    "fallback_a40_secure_stills_usd",
                )
            },
            "subtotal_usd": subtotal,
            "network_volume_usd": volume,
            "total_usd": total,
            "total_pkr_approx": round(total * pkr_per_usd, 0),
            "pkr_per_usd": pkr_per_usd,
            "if_all_a40_secure_extra_usd_per_mo": round(a40_extra * n, 2),
            "notes": (
                f"{channels} channels × {videos_per_channel} {profile} "
                f"(~15 min / 125 stills); Kokoro $0; Seedream always on; "
                f"primary A5000 @$0.16/hr (optimistic; 3090 Community @$0.22 realistic "
                f"Community fallback) × {br.get('quantities', {}).get('runpod_seconds_per_still')}s/still."
            ),
        }

    def rate_card(self) -> dict[str, Any]:
        return dict(self.cfg.get("cost_rates") or {})

    def check_can_start_job(self, *, estimated_usd: float | None = None) -> tuple[bool, str]:
        est = (
            float(estimated_usd)
            if estimated_usd is not None
            else self.estimate_video_usd(profile=self._default_profile())
        )
        total = self.store.month_spend_total()
        cap = float(
            getattr(self.s, "monthly_budget_usd", None)
            or self.cfg.get("monthly_budget_usd")
            or 25
        )
        if total + est > cap:
            msg = f"budget exceeded: spent ${total:.2f} + est ${est:.2f} > ${cap:.2f}"
            self.ledger.write(
                agent="cost_guardian",
                problem=msg,
                action="abort new job",
                severity="critical",
                extra={
                    "rate_card": self.rate_card(),
                    "breakdown": self.estimate_video_breakdown(
                        profile=self._default_profile()
                    ),
                },
            )
            return False, msg
        return True, f"ok spent=${total:.2f} +est=${est:.2f} cap=${cap:.2f}"

    def record(
        self,
        *,
        category: str,
        amount_usd: float,
        note: str = "",
        job_id: str | None = None,
        channel: str | None = None,
    ) -> None:
        self.store.record_spend(
            category=category,
            amount_usd=amount_usd,
            note=note,
            job_id=job_id,
            channel=channel,
        )
        total = self.store.month_spend_total()
        runpod_cap = float(self.cfg.get("runpod_budget_usd") or 25)
        if category == "runpod" and total > runpod_cap:
            self.ledger.write(
                agent="cost_guardian",
                problem=f"runpod-heavy month spend ${total:.2f} (cap ${runpod_cap:.2f})",
                action="warn — consider pause stills",
                job_id=job_id,
                severity="warn",
            )

    def record_estimated_video(
        self, *, job_id: str | None = None, profile: str | None = None
    ) -> float:
        prof = (profile or self._default_profile()).strip().lower()
        br = self.estimate_video_breakdown(profile=prof)
        items = br["line_items"]
        self.record(
            category="runpod",
            amount_usd=float(items.get("runpod_stills") or 0.0),
            note=f"{prof}|stills",
            job_id=job_id,
        )
        if float(items.get("runpod_ai_video_clips") or 0) > 0:
            self.record(
                category="runpod",
                amount_usd=float(items["runpod_ai_video_clips"]),
                note=f"{prof}|ai_video",
                job_id=job_id,
            )
        self.record(
            category="llm",
            amount_usd=float(items.get("wavespeed_llm") or 0.0),
            note=f"{prof}|wavespeed",
            job_id=job_id,
        )
        meta_thumb = float(items.get("openrouter_meta") or 0.0) + float(
            items.get("wavespeed_seedream_thumb") or 0.0
        )
        self.record(
            category="meta_thumb",
            amount_usd=round(meta_thumb, 4),
            note=f"{prof}|openrouter+seedream",
            job_id=job_id,
        )
        return float(br["total_usd_rounded"])

    def _stills_for_profile(self, prof: str, rates: dict[str, Any]) -> float:
        if prof == "epic":
            return float(rates.get("runpod_stills_per_epic_video") or 240)
        if prof == "longform":
            return float(rates.get("runpod_stills_per_longform_video") or 210)
        return float(rates.get("runpod_stills_per_retention_video") or 125)

    def _llm_cost_usd(self, prof: str, rates: dict[str, Any]) -> dict[str, Any]:
        """WaveSpeed outline (DeepSeek) + expand (Claude Haiku) from token rates."""
        outline_in = float(rates.get("llm_outline_input_tokens") or 4000)
        outline_out = float(rates.get("llm_outline_output_tokens") or 2500)
        # Epic outlines are longer (15 chapters)
        if prof == "epic":
            outline_in = float(rates.get("llm_epic_outline_input_tokens") or outline_in * 1.4)
            outline_out = float(
                rates.get("llm_epic_outline_output_tokens") or outline_out * 1.5
            )
        outline_in_rate = float(rates.get("wavespeed_outline_usd_per_m_input") or 0.26)
        outline_out_rate = float(rates.get("wavespeed_outline_usd_per_m_output") or 0.38)
        outline = (outline_in / 1_000_000.0) * outline_in_rate + (
            outline_out / 1_000_000.0
        ) * outline_out_rate

        if prof == "epic":
            phase_a = float(rates.get("epic_phase_a_scenes") or 200)
            chunk = float(rates.get("llm_expand_chunk_size") or 16)
            # Longer Phase B paragraphs → more tokens per expand; more chapter calls
            phase_b_calls = float(rates.get("epic_phase_b_expand_calls") or 12)
            expand_calls = math.ceil(phase_a / chunk) + phase_b_calls
            exp_in = float(rates.get("llm_epic_expand_input_tokens") or 9000)
            exp_out = float(rates.get("llm_epic_expand_output_tokens") or 6000)
        elif prof == "longform":
            # Phase A ~200 beats / 16 per Haiku call + ~4 Phase B chapter calls
            phase_a = float(rates.get("longform_phase_a_scenes") or 200)
            chunk = float(rates.get("llm_expand_chunk_size") or 16)
            phase_b_calls = float(rates.get("longform_phase_b_expand_calls") or 4)
            expand_calls = math.ceil(phase_a / chunk) + phase_b_calls
            exp_in = float(rates.get("llm_expand_input_tokens") or 6000)
            exp_out = float(rates.get("llm_expand_output_tokens") or 3500)
        else:
            phase_a = float(rates.get("retention_phase_a_scenes") or 110)
            chunk = float(rates.get("llm_expand_chunk_size") or 16)
            phase_b_calls = float(rates.get("retention_phase_b_expand_calls") or 4)
            expand_calls = math.ceil(phase_a / chunk) + phase_b_calls
            exp_in = float(rates.get("llm_expand_input_tokens") or 6000)
            exp_out = float(rates.get("llm_expand_output_tokens") or 3500)

        retry_mult = float(rates.get("llm_retry_multiplier") or 1.25)
        expand_calls_eff = expand_calls * retry_mult

        exp_in_rate = float(rates.get("wavespeed_expand_usd_per_m_input") or 0.25)
        exp_out_rate = float(rates.get("wavespeed_expand_usd_per_m_output") or 1.25)
        per_expand = (exp_in / 1_000_000.0) * exp_in_rate + (
            exp_out / 1_000_000.0
        ) * exp_out_rate
        expand = expand_calls_eff * per_expand

        return {
            "total_usd": round(outline + expand, 6),
            "quantities": {
                "outline_model": rates.get("wavespeed_outline_model")
                or "deepseek/deepseek-v3.2",
                "expand_model": rates.get("wavespeed_expand_model")
                or "anthropic/claude-3-haiku",
                "outline_input_tokens": outline_in,
                "outline_output_tokens": outline_out,
                "outline_usd": round(outline, 6),
                "expand_calls_base": expand_calls,
                "expand_calls_with_retries": round(expand_calls_eff, 2),
                "expand_input_tokens_per_call": exp_in,
                "expand_output_tokens_per_call": exp_out,
                "expand_usd": round(expand, 6),
                "outline_usd_per_m_in_out": [outline_in_rate, outline_out_rate],
                "expand_usd_per_m_in_out": [exp_in_rate, exp_out_rate],
            },
        }


def _load_agents_settings() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
