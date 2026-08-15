"""Settings + prompt loading for ScriptModule."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"
OUTPUT_DIR = ROOT / "output" / "scripts"

_COMMENT_LINE = re.compile(r"^\s*#")
_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "https://llm.wavespeed.ai/v1"
    llm_api_key: str = ""  # fallback; prefer WAVESPEED_API_KEY
    wavespeed_api_key: str = ""
    # Dual models (WaveSpeed OpenAI-compatible)
    llm_outline_model: str = "deepseek/deepseek-v3.2"
    llm_expand_model: str = "anthropic/claude-3-haiku"
    # Cross-pair failover on JSON parse exhaustion (sleep policy).
    llm_outline_fallback_model: str = "anthropic/claude-3-haiku"
    llm_expand_fallback_model: str = "deepseek/deepseek-v3.2"
    llm_model: str = "deepseek/deepseek-v3.2"  # legacy default / outline fallback
    llm_reasoning_enabled: bool = False  # WaveSpeed models — not OpenRouter reasoning
    # Global json_object mode (expand/haiku may not support — DeepSeek auto-enables).
    llm_json_response_format: bool = False
    llm_send_temperature: bool = True
    llm_timeout_s: float = 120.0
    llm_max_retries: int = 2
    # Retries AFTER first parse failure on the same model (sleep: 2 → 3 attempts/model).
    llm_json_same_model_retries: int = 2
    # Legacy alias — preferred path uses same_model_retries + failover.
    llm_json_parse_retries: int = 3

    target_seconds_per_scene: float = 3.0
    min_scenes: int = 170
    max_scenes: int = 270
    target_scenes: int = 210
    target_duration_min: int = 20
    script_max_retries: int = 2

    # VoiceModule — default kokoro (local $0). GPU voice is opt-in.
    # kokoro | elevenlabs | cosyvoice_runpod | fish_runpod
    tts_backend: str = "kokoro"
    elevenlabs_api_key: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_voice_id: str = ""
    kokoro_voice: str = "am_michael"
    kokoro_speed: float = 0.90
    kokoro_lang: str = "en-us"
    kokoro_model_path: str = str(ROOT / "models" / "kokoro" / "kokoro-v1.0.onnx")
    kokoro_voices_path: str = str(ROOT / "models" / "kokoro" / "voices-v1.0.bin")
    voice_min_scene_seconds: float = 2.0
    voice_max_scene_seconds: float = 150.0  # epic Phase B ~120s; longform ~60s

    # Ephemeral CosyVoice — separate pod from stills. Primary: RTX 2000 Ada Secure
    # (~$0.24/hr; Community maxCount=0). Fallbacks only when Ada stock empty.
    runpod_voice_gpu_type_id: str = "NVIDIA RTX 2000 Ada Generation"
    runpod_voice_cloud_type: str = "SECURE"
    runpod_voice_template_id: str = "204bfa9j8i"
    runpod_voice_image: str = "neosun/cosyvoice:v3.4.0"
    runpod_voice_container_disk_gb: int = 30
    runpod_voice_volume_gb: int = 10
    runpod_voice_volume_mount: str = "/data/voices"
    runpod_voice_ports: str = "8188/http"
    runpod_voice_http_port: int = 8188
    runpod_voice_ready_path: str = "/health"
    runpod_voice_ready_timeout_s: float = 900.0
    runpod_voice_synth_timeout_s: float = 300.0
    runpod_voice_data_centers: str = "EU-RO-1,EUR-IS-1"  # live Ada Secure stock
    runpod_voice_network_volume_id: str = ""
    # GPU:CLOUD CSV after primary — A40 Secure only (skip flaky A40 Community)
    runpod_voice_gpu_fallbacks: str = "NVIDIA A40:SECURE"
    cosyvoice_model_dir: str = "pretrained_models/Fun-CosyVoice3-0.5B"
    cosyvoice_voice_id: str = "default"

    # VisualModule / Stills
    # mock | runpod (=serverless) | runpod_serverless | runpod_pod | api
    # Intended production: runpod_pod (A40 Secure ephemeral). Farm stays off.
    image_backend: str = "mock"
    visuals_backend: str = ""  # optional alias; if set, overrides image_backend
    image_width: int = 1280
    image_height: int = 720
    image_max_retries_per_scene: int = 2
    image_min_bytes: int = 8_000  # after resize JPEG can be smaller
    # Cinematic Tudor stills: richer midtones than stickman; reject void/blank
    image_min_mean_luma: float = 28.0
    image_min_bright_frac: float = 0.04  # min fraction of pixels with max(RGB) > 40
    image_max_mean_luma: float = 230.0  # reject overexposed / blank white frames
    character_bible_path: str = str(CONFIG_DIR / "character_bible.json")

    # RunPod serverless / public stills (legacy warm workers)
    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""
    # comfyui = Hub ComfyUI Flux workflow; simple = {prompt,width,height}
    runpod_worker_type: str = "comfyui"  # comfyui|simple
    runpod_prompt_field: str = "prompt"
    runpod_width_field: str = "width"
    runpod_height_field: str = "height"
    runpod_payload_mode: str = "input_object"  # flat|input_object
    runpod_base_url: str = "https://api.runpod.ai/v2"
    comfy_workflow_path: str = str(CONFIG_DIR / "comfy_flux_workflow.json")
    comfy_ckpt_name: str = "flux1-dev-fp8.safetensors"
    comfy_steps: int = 20
    comfy_guidance: float = 4.5

    # Ephemeral stills — A40 Secure ONLY (no A5000/3090 Community/Secure walk).
    runpod_stills_gpu_type_id: str = "NVIDIA A40"
    runpod_stills_cloud_type: str = "SECURE"
    runpod_stills_template_id: str = "0hlycynxue"
    runpod_stills_image: str = "yanwk/comfyui-boot:cu126-slim"
    runpod_stills_container_disk_gb: int = 80  # Flux FP8 ~17GB + Comfy + headroom
    runpod_stills_volume_gb: int = 20
    runpod_stills_volume_mount: str = "/workspace"
    runpod_stills_ports: str = "8188/http"
    runpod_stills_http_port: int = 8188
    runpod_stills_ready_path: str = "/"
    # Flux download (~17GB) + image pull + Comfy boot — do not give up mid-download.
    runpod_stills_ready_timeout_s: float = 3600.0
    runpod_stills_data_centers: str = ""
    # No network volume (deleted 2026-08-07) — weights re-download per pod.
    runpod_stills_network_volume_id: str = ""
    # Empty = primary only (A40 Secure). Do not rehydrate old Community walk.
    runpod_stills_gpu_fallbacks: str = ""
    # wait_until_running must cover image pull; 600s was killing pods mid-pull.
    runpod_pod_boot_timeout_s: float = 1800.0
    # Hard wall TTL ceiling for a *healthy* ephemeral stills/voice pod (minutes).
    # Env RUNPOD_POD_MAX_MINUTES. Default 60 for stills + smoke proof.
    # Failure → kill immediately; stills on local disk → kill immediately.
    # 60 is a ceiling, never a minimum dwell.
    runpod_pod_max_minutes: float = 60.0

    # Pre-flight capacity benchmark (runpod_stills_benchmark_v1) before stills
    # pod create. GraphQL stock only — never creates a probe pod.
    # With A40 Secure-only factory: A40 Secure stock → GREEN (intended path).
    # Mixed Community factories: GREEN=Community; YELLOW=Secure only; RED=none.
    runpod_capacity_probe: bool = True
    # Required for Secure-only stills / one-off YELLOW proceed.
    runpod_allow_secure: bool = True
    # Soft schedule advice by default; true → hard-block outside windows.
    runpod_require_window: bool = False
    # Preferred UTC windows (PKT = UTC+5): primary 12–16 PKT, backup 08–11 PKT.
    runpod_primary_window_utc: str = "07:00-11:00"
    runpod_backup_window_utc: str = "03:00-06:00"

    # Capacity watchdog — cron one-shot probe → learn → GREEN-only auto-start.
    # Farm is always green-light only (YELLOW/RED never start factory).
    # Daily job-count caps DISABLED (0): sheet approved=TRUE ideas drive volume.
    # CostGuardian $ ceilings still apply. Optional >0 = emergency brake only.
    runpod_watchdog: bool = True
    runpod_auto_start: bool = True
    runpod_auto_max_jobs_per_day: int = 0
    runpod_auto_max_jobs_per_day_hard: int = 0
    runpod_auto_opportunistic: bool = True
    runpod_auto_job_cooldown_min: int = 3
    runpod_auto_opportunistic_min_green_streak: int = 1
    runpod_auto_opportunistic_min_stock: str = "Low"
    runpod_probe_interval_in_window_min: int = 10
    runpod_probe_interval_out_window_min: int = 10
    runpod_probe_out_window_jitter_min: int = 0
    runpod_learn_min_samples: int = 50
    # Continuous seasonal learning — rolling year of probes.
    runpod_learn_lookback_days: int = 365
    # Mature-before-promote: repeated GREEN days / samples / rate.
    runpod_learn_mature_min_days: int = 5
    runpod_learn_mature_min_samples: int = 8
    runpod_learn_mature_min_green_rate: float = 0.35
    # When true AND mature gates pass, learned windows override schedule defaults.
    runpod_learn_auto_apply: bool = False

    # Managed image API (optional fallback — leave empty if using RunPod)
    image_api_base_url: str = ""
    image_api_key: str = ""
    image_api_prompt_field: str = "prompt"

    # Plan C §7.1 sparse AI video (default 5 clips)
    enable_ai_video_clips: bool = False
    max_ai_video_clips_per_job: int = 5
    ai_video_seconds_per_clip: float = 5.0
    # Cheapest public path: RunPod Pruna Video (~$0.02/s @ 720p → ~$0.10 / 5s)
    video_endpoint_id: str = "p-video"
    video_api_key: str = ""  # falls back to RUNPOD_API_KEY
    video_resolution: str = "720p"
    video_draft: bool = False  # Pruna draft mode (~75% cheaper, preview quality)

    # EditModule / VPS FFmpeg only (Plan C §6)
    compose_backend: str = "vps_ffmpeg"
    compose_fps: int = 30
    compose_preset: str = "veryfast"
    compose_crf: int = 23
    ken_burns_zoom_end: float = 1.12  # Netflix-doc default (channel may soften)
    ken_burns_zoom_end_phase_b: float = 1.16  # stronger motion on long Phase B holds
    compose_chapter_crossfade_s: float = 0.3
    compose_dramatic_hold_s: float = 0.2
    compose_max_still_s: float = 10.0  # split Ken Burns when scene holds longer
    compose_subclip_crossfade_s: float = 0.0  # keep 0 so audio stays in sync; hard cut OK
    compose_chapter_end_pause_ms: int = 200  # voice_prosody chapter-boundary pad
    compose_date_overlay_enabled: bool = True
    compose_date_overlay_s: float = 2.5  # lower-third year burn on long holds
    # Code 2D infographic / pattern-interrupt cards (PIL → FFmpeg overlay).
    # Honors config/visual_modalities.json profile "infographic".enabled.
    # Netflix Turning Point pacing: ~one teaching beat every 60–90s.
    compose_infographic_overlays_enabled: bool = True
    compose_infographic_overlay_s: float = 3.5
    compose_infographic_min_cards: int = 8
    compose_infographic_max_cards: int = 20
    compose_infographic_target_interval_s: float = 75.0
    # $0 premium documentary chrome (PIL/FFmpeg). Default ON for all farm paths.
    # Env: COMPOSE_PREMIUM_OVERLAYS=1|true|0|false (also COMPOSE_PREMIUM_OVERLAYS_ENABLED)
    compose_premium_overlays_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPOSE_PREMIUM_OVERLAYS_ENABLED",
            "COMPOSE_PREMIUM_OVERLAYS",
            "compose_premium_overlays_enabled",
        ),
    )
    compose_fog_drift_enabled: bool = True
    compose_vignette_enabled: bool = True
    compose_letterbox_enabled: bool = True
    compose_progress_rail_enabled: bool = True
    compose_year_stamp_enabled: bool = True
    compose_lower_thirds_enabled: bool = True
    compose_cold_open_title_enabled: bool = True
    compose_pulse_marker_enabled: bool = True
    compose_channel_default: str = "napstorian"
    # PD/open motion inserts (Commons/Met still→motion or drop-ins).
    # Napstorian: 10–15 denser inserts. Historian: 4–6 soft Ken-Burns pans
    # (History Calling calm). COMPOSE_PD_MOTION_CLIPS=0 disables all channels.
    compose_pd_motion_clips_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPOSE_PD_MOTION_CLIPS",
            "COMPOSE_PD_MOTION_CLIPS_ENABLED",
            "compose_pd_motion_clips_enabled",
        ),
    )
    compose_pd_motion_min_clips: int = Field(
        default=10,
        validation_alias=AliasChoices(
            "COMPOSE_PD_MOTION_MIN_CLIPS",
            "compose_pd_motion_min_clips",
        ),
    )
    compose_pd_motion_max_clips: int = Field(
        default=15,
        validation_alias=AliasChoices(
            "COMPOSE_PD_MOTION_MAX_CLIPS",
            "compose_pd_motion_max_clips",
        ),
    )
    compose_pd_motion_historian_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPOSE_PD_MOTION_HISTORIAN",
            "COMPOSE_PD_MOTION_HISTORIAN_ENABLED",
            "compose_pd_motion_historian_enabled",
        ),
    )
    compose_pd_motion_historian_min_clips: int = Field(
        default=4,
        validation_alias=AliasChoices(
            "COMPOSE_PD_MOTION_HISTORIAN_MIN_CLIPS",
            "compose_pd_motion_historian_min_clips",
        ),
    )
    compose_pd_motion_historian_max_clips: int = Field(
        default=6,
        validation_alias=AliasChoices(
            "COMPOSE_PD_MOTION_HISTORIAN_MAX_CLIPS",
            "compose_pd_motion_historian_max_clips",
        ),
    )
    compose_pd_motion_duration_s: float = Field(
        default=8.0,
        validation_alias=AliasChoices(
            "COMPOSE_PD_MOTION_DURATION_S",
            "compose_pd_motion_duration_s",
        ),
    )
    # Cinematic SFX (Mixkit/CC0 pack under assets/sfx/). Master + per-channel.
    # napstorian default ON; napping_historian default OFF (calm sleep).
    # COMPOSE_SFX=0 disables all channels.
    compose_sfx: bool = Field(
        default=True,
        validation_alias=AliasChoices("COMPOSE_SFX", "compose_sfx"),
    )
    compose_sfx_napstorian: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPOSE_SFX_NAPSTORIAN",
            "compose_sfx_napstorian",
        ),
    )
    compose_sfx_historian: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "COMPOSE_SFX_HISTORIAN",
            "COMPOSE_SFX_NAPPING_HISTORIAN",
            "compose_sfx_historian",
        ),
    )
    compose_sfx_dir: str = str(ROOT / "assets" / "sfx")
    compose_sfx_ambient_bed: bool = True
    compose_sfx_whoosh_db: float = -14.0
    compose_sfx_impact_db: float = -11.0
    compose_sfx_plate_db: float = -15.0
    compose_sfx_infographic_db: float = -18.0
    compose_sfx_ambient_db: float = -28.0
    # Netflix-doc density: whoosh on every plate + soft hits on mid-video beats.
    compose_sfx_max_infographic_whooshes: int = 24
    compose_sfx_beat_interval_s: float = 70.0
    compose_sfx_beat_db: float = -20.0
    edit_allow_placeholders: bool = False
    loudness_normalize: bool = True
    audio_bed_enabled: bool = True
    # Historian keyword one-shots (quill/door/horse) — default OFF for calm sleep.
    audio_bed_sfx_historian: bool = False
    keep_scene_clips: bool = True  # set false later to prune after concat

    # PublishModule / YouTube Data API (Module 5) — private first
    # Dual Brand Accounts: napstorian → youtube_token_path (legacy);
    # napping_historian → config/youtube_token_napping_historian.json
    # (or YOUTUBE_TOKEN_PATH_NAPPING_HISTORIAN). Never cross-wipe tokens.
    youtube_client_secrets_path: str = str(ROOT / "config" / "youtube_client_secrets.json")
    youtube_token_path: str = str(ROOT / "config" / "youtube_token.json")
    youtube_category_id: str = "27"  # Education
    youtube_default_privacy: str = "private"
    youtube_ai_disclosure_text: str = (
        "Visuals and narration in this video are AI-assisted. "
        "The script is original and human-written. "
        "Altered or synthetic media is disclosed per YouTube guidelines."
    )
    youtube_made_for_kids: bool = False
    # Gate A duration band defaults (longform 15–25 min). Prefer profile_gate_a_band()
    # for checks — epic uses ~80–100 min via retention_profiles.json.
    gate_a_min_duration_s: float = 900.0  # 15 min
    gate_a_max_duration_s: float = 1500.0  # 25 min
    gate_a_min_file_bytes: int = 100_000
    gate_a_enforce_duration: bool = False

    # YouTube packaging — OpenRouter metadata + Seedream thumbnails
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    # Multimodal judge for Wiki/Met stills (must NOT be openrouter/free)
    # Multimodal judge for Wiki/Met stills — LOCKED to openrouter/free (Rayyan).
    openrouter_vision_model: str = "openrouter/free"
    openrouter_reasoning_enabled: bool = True
    thumbnail_image_submit_url: str = (
        "https://api.wavespeed.ai/api/v3/bytedance/seedream-v5.0-lite"
    )

    # === Autonomous ops agents ===
    database_url: str = ""  # optional postgresql://… (file store used if empty)
    redis_url: str = "redis://localhost:6379"
    google_sheet_id: str = ""
    google_sheets_credentials: str = str(CONFIG_DIR / "google_sheets_service_account.json")
    youtube_api_key: str = ""  # Data API key for Trends search (optional)
    gate_b_enforce: bool = True
    ops_ledger_email: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    monthly_budget_usd: float = 25.0
    publish_timezone: str = "Asia/Karachi"
    publish_hour_local: int = 4
    publish_cadence_days: int = 1
    publish_schedule_path: str = str(CONFIG_DIR / "publish_schedule.json")


from src.services.retention_profile import merge_profile_into_script_settings


def load_script_settings() -> dict:
    path = CONFIG_DIR / "script_settings.json"
    base: dict = {}
    if path.exists():
        base = json.loads(path.read_text(encoding="utf-8"))
    return merge_profile_into_script_settings(base)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Env (.env) wins. File config fills pacing knobs only when env leaves defaults."""
    s = Settings()
    file_cfg = load_script_settings()
    if not file_cfg:
        return s

    # Only apply file values when they differ from class defaults AND env didn't set them.
    # Simpler rule for v1: file overrides class defaults; explicit env still wins because
    # Settings() already applied env. So we only fill from file if still at class default
    # — actually env-applied values equal to default are indistinguishable. Prefer:
    # file for pacing numbers unless corresponding env var is present.
    updates: dict = {}
    mapping = {
        "target_seconds_per_scene": "TARGET_SECONDS_PER_SCENE",
        "min_scenes": "MIN_SCENES",
        "max_scenes": "MAX_SCENES",
        "target_scenes": "TARGET_SCENES",
        "target_duration_min": "TARGET_DURATION_MIN",
        "script_max_retries": "SCRIPT_MAX_RETRIES",
    }
    file_key = {
        "target_seconds_per_scene": "target_seconds_per_scene",
        "min_scenes": "min_scenes",
        "max_scenes": "max_scenes",
        "target_scenes": "target_scenes",
        "target_duration_min": "target_duration_min",
        "script_max_retries": "max_retries",
    }
    for attr, env_name in mapping.items():
        if os.getenv(env_name) is not None:
            continue
        fk = file_key[attr]
        if fk in file_cfg:
            updates[attr] = file_cfg[fk]
    if updates:
        return s.model_copy(update=updates)
    return s


def load_prompt(name: str, *, prompts_dir: str | Path | None = None) -> str:
    """Load an editable prompt file; strip #-comment lines only.

    When ``prompts_dir`` is set (profile ``prompts_dir``), resolve from that
    folder; otherwise use ``config/prompts/``. Absolute or repo-relative paths OK.
    """
    if prompts_dir is not None:
        base = Path(prompts_dir)
        if not base.is_absolute():
            base = ROOT / base
    else:
        base = PROMPTS_DIR
    path = base / name
    if not path.exists():
        # Fall back to default prompts so longform-only files still resolve
        fallback = PROMPTS_DIR / name
        if prompts_dir is not None and fallback.exists() and fallback != path:
            path = fallback
        else:
            raise FileNotFoundError(f"Prompt file missing: {path}")
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if _COMMENT_LINE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        raise ValueError(f"Prompt file is empty after stripping comments: {path}")
    return text


def render_prompt(template: str, values: dict[str, str | int | float]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Missing prompt placeholder: {{{{{key}}}}}")
        return str(values[key])

    return _PLACEHOLDER.sub(repl, template)


def load_style_hint() -> str:
    path = CONFIG_DIR / "style_prompt.txt"
    if not path.exists():
        return "cinematic gallery still"
    lines = [
        ln for ln in path.read_text(encoding="utf-8").splitlines() if not _COMMENT_LINE.match(ln)
    ]
    return " ".join(ln.strip() for ln in lines if ln.strip()) or "cinematic gallery still"


def ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR
