"""Microstock vector engine — AI raster -> clean commercial SVG -> stock platforms.

Second revenue line, fully isolated from the YouTube farm: its own config
(``config/microstock/``), its own state (``output/microstock/``) and its own cron
beat. It *reuses* the farm's infrastructure (OpsStore, OpsLedger, CostGuardian,
settings/prompt loading, the vision rate gate) but shares no state with it, so a
failure here can never stall video production.

See GRAND_MASTER_STRATEGY in roadmap.txt for the commercial strategy.
"""

from __future__ import annotations

__all__ = ["paths", "config"]
