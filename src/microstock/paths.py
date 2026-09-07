"""Single source of truth for every microstock filesystem location.

Every other module imports its directories from here — nothing else builds paths
by hand, so relocating the engine is a one-file change.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config" / "microstock"
PROMPTS_DIR = CONFIG_DIR / "prompts"

SETTINGS_PATH = CONFIG_DIR / "settings.json"
NICHES_PATH = CONFIG_DIR / "niches.json"
PLATFORMS_PATH = CONFIG_DIR / "platforms.json"

OUTPUT_DIR = ROOT / "output" / "microstock"
RAW_PNG_DIR = OUTPUT_DIR / "raw_png"
TRACED_SVG_DIR = OUTPUT_DIR / "traced_svg"
CLEAN_SVG_DIR = OUTPUT_DIR / "clean_svg"
REJECTED_DIR = OUTPUT_DIR / "rejected"
OUTBOX_DIR = OUTPUT_DIR / "outbox"
OPS_DIR = OUTPUT_DIR / "ops"

# Ops state files (OpsStore owns jobs/events/ledger; these are microstock-specific)
ASSET_LEDGER_PATH = OPS_DIR / "asset_ledger.json"
BRIEF_STOCK_PATH = OPS_DIR / "brief_stock.json"
SALES_BENCHMARKS_PATH = OPS_DIR / "sales_benchmarks.json"
BEAT_LAST_PATH = OPS_DIR / "beat_last.json"
SPEND_JSONL_PATH = OPS_DIR / "spend.jsonl"

_MANAGED_DIRS = (
    OUTPUT_DIR,
    RAW_PNG_DIR,
    TRACED_SVG_DIR,
    CLEAN_SVG_DIR,
    REJECTED_DIR,
    OUTBOX_DIR,
    OPS_DIR,
)


def ensure_dirs() -> None:
    """Create every managed output directory. Idempotent, safe to call per beat."""
    for path in _MANAGED_DIRS:
        path.mkdir(parents=True, exist_ok=True)


def outbox_for(platform: str, batch_id: str) -> Path:
    """Tier-B staging directory for a manual-upload platform batch."""
    return OUTBOX_DIR / platform / batch_id
