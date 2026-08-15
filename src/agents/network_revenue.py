"""Network revenue gate — affiliates / sponsors / licensing after 6+ monetized."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.channel_empire import network_revenue_status
from src.services.settings import CONFIG_DIR, ROOT

NETWORK_REVENUE_PATH = CONFIG_DIR / "network_revenue.json"


def load_network_revenue() -> dict[str, Any]:
    if not NETWORK_REVENUE_PATH.is_file():
        return {}
    try:
        data = json.loads(NETWORK_REVENUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def layer_allowed(layer: str) -> dict[str, Any]:
    """Return whether a revenue layer may run live."""
    status = network_revenue_status()
    cfg = load_network_revenue()
    layers = cfg.get("layers") if isinstance(cfg.get("layers"), dict) else {}
    entry = layers.get(layer) if isinstance(layers.get(layer), dict) else {}
    unlocked = bool(status.get("unlocked"))
    wants = bool(entry.get("enabled_when_unlocked", True)) if entry else True
    allowed = unlocked and wants and bool(entry)
    return {
        "layer": layer,
        "allowed": allowed,
        "unlocked": unlocked,
        "monetized_channels": status.get("monetized_channels"),
        "required": status.get("required"),
        "reason": (
            "ok"
            if allowed
            else (
                "network_revenue_locked"
                if not unlocked
                else ("layer_missing" if not entry else "layer_disabled")
            )
        ),
        "config": entry,
    }


def affiliate_allowed_for_channel(channel: str) -> dict[str, Any]:
    base = layer_allowed("affiliates")
    if not base.get("allowed"):
        return {**base, "channel": channel, "channel_allowed": False}
    cfg = base.get("config") or {}
    allowed_chs = [str(c) for c in (cfg.get("allowed_channels") or [])]
    ok = (channel or "").strip() in allowed_chs
    return {
        **base,
        "channel": channel,
        "channel_allowed": ok,
        "reason": "ok" if ok else "channel_not_on_affiliate_allowlist",
    }


def write_network_revenue_playbook() -> Path:
    status = network_revenue_status()
    cfg = load_network_revenue()
    path = ROOT / "output" / "ops" / "NETWORK_REVENUE_PLAYBOOK.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Network revenue playbook",
        "",
        f"_Status: unlocked={status.get('unlocked')} "
        f"({status.get('monetized_channels')}/{status.get('required')} monetized)_",
        "",
        status.get("message") or "",
        "",
        "## Layers (activate only when unlocked)",
        "",
    ]
    for name, entry in (cfg.get("layers") or {}).items():
        if not isinstance(entry, dict):
            continue
        lines.append(f"### {name}")
        lines.append(f"- priority: {entry.get('priority')}")
        if entry.get("tactics"):
            lines.append(f"- tactics: {', '.join(entry['tactics'])}")
        if entry.get("allowed_channels"):
            lines.append(f"- channels: {', '.join(entry['allowed_channels'])}")
        if entry.get("soft_brands"):
            lines.append(f"- brands: {', '.join(entry['soft_brands'])}")
        if entry.get("offer"):
            lines.append(f"- offer: {entry['offer']}")
        lines.append("")
    lines.append("## Checklist")
    for item in cfg.get("activation_checklist") or []:
        lines.append(f"- [ ] {item}")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
