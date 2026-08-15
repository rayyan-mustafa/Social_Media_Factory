"""Farm-safe pre-ingest asset license gate (rules only, no AI).

Digitization / restored / remastered → rejected for auto (AI fill), not a human queue.
Pexels / Pixabay / Unsplash are NOT whitelisted.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.agents.store import OPS_DIR
from src.services.pd_clippings import license_is_commercial_ok
from src.services.settings import CONFIG_DIR, ROOT

APPROVED_DOMAINS_PATH = CONFIG_DIR / "approved_domains.json"
ASSET_LOG = OPS_DIR / "asset_log.jsonl"
NEEDS_REVIEW = OPS_DIR / "needs_review.jsonl"

_DIGITIZATION_RE = re.compile(
    r"\b(restored|remastered|digitized by|colourised|colorized by)\b",
    re.IGNORECASE,
)

_LICENSE_NORMALIZE = {
    "public domain": "public domain",
    "pd": "public domain",
    "pd-art": "public domain",
    "cc0": "cc0",
    "cc-zero": "cc0",
    "cc by": "cc-by",
    "cc-by": "cc-by",
    "cc by-sa": "cc-by-sa",
    "cc-by-sa": "cc-by-sa",
    "met open access": "cc0",
    "open access": "cc0",
    "open license": "cc0",
}

_ALLOWED_TAGS = frozenset({"public domain", "cc0", "cc-by", "cc-by-sa"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_approved_domains(path: Path | None = None) -> list[str]:
    p = path or APPROVED_DOMAINS_PATH
    if not p.is_file():
        return [
            "commons.wikimedia.org",
            "upload.wikimedia.org",
            "metmuseum.org",
            "images.metmuseum.org",
            "collectionapi.metmuseum.org",
            "loc.gov",
            "archive.org",
        ]
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(x).strip().lower() for x in data if str(x).strip()]
    return []


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def domain_whitelisted(url: str, domains: list[str] | None = None) -> bool:
    host = domain_of(url)
    if not host:
        return False
    allow = domains if domains is not None else load_approved_domains()
    for d in allow:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def normalize_license_tag(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if license_is_commercial_ok(text):
        # Prefer coarse tags for verifier output
        compact = re.sub(r"[^a-z0-9]+", "", text)
        if "ccbysa" in compact or "by-sa" in text.replace(" ", ""):
            return "cc-by-sa"
        if "ccby" in compact and "cc0" not in compact:
            return "cc-by"
        if "cc0" in compact or "openaccess" in compact or "metopenaccess" in compact:
            return "cc0"
        return "public domain"
    for key, tag in _LICENSE_NORMALIZE.items():
        if key in text:
            return tag
    return ""


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def verify_asset(
    source_url: str,
    asset_type: str,
    metadata: dict[str, Any] | None = None,
    *,
    domains: list[str] | None = None,
    log: bool = True,
) -> dict[str, Any]:
    """Rules-based gate. Digitization trap → rejected (auto AI-fill), not manual_review."""
    meta = dict(metadata or {})
    asset_type = (asset_type or "image").strip().lower()
    if asset_type not in {"image", "footage", "audio"}:
        asset_type = "image"

    domain_ok = domain_whitelisted(source_url, domains)
    lic_raw = meta.get("license_tag") or meta.get("license") or ""
    lic_tag = normalize_license_tag(str(lic_raw))
    # Also accept commercial-ok strings even if normalize is empty
    license_ok = bool(lic_tag) and lic_tag in _ALLOWED_TAGS
    if not license_ok and license_is_commercial_ok(str(lic_raw)):
        lic_tag = normalize_license_tag(str(lic_raw)) or "public domain"
        license_ok = True

    dig_note = str(meta.get("digitization_note") or "")
    digitization_flag = bool(_DIGITIZATION_RE.search(dig_note))
    requires_audio_strip = asset_type == "footage"

    mime = str(meta.get("mime") or "").split(";", 1)[0].strip().lower()
    mime_blocked = bool(
        mime
        and asset_type == "image"
        and mime
        not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}
    )

    status = "approved"
    reasons: list[str] = []
    if not domain_ok:
        status = "rejected"
        reasons.append(f"domain not whitelisted: {domain_of(source_url) or '(none)'}")
    if not license_ok:
        status = "rejected"
        reasons.append(f"license not farm-safe: {lic_raw!r}")
    if digitization_flag:
        # Plan: reject for auto (safer than Claude manual_review queue)
        status = "rejected"
        reasons.append("digitization/restoration trap — auto-reject → AI fill")
    if mime_blocked:
        status = "rejected"
        reasons.append(f"non-raster mime blocked: {mime}")

    # Block stock domains even if somehow listed
    host = domain_of(source_url)
    if any(x in host for x in ("pexels.com", "pixabay.com", "unsplash.com", "mixkit.co")):
        status = "rejected"
        reasons.append(f"stock domain blocked: {host}")

    reason = "; ".join(reasons) if reasons else "ok"
    result = {
        "status": status,
        "checks": {
            "domain_whitelisted": domain_ok,
            "license_tag_present": bool(str(lic_raw).strip()),
            "license_tag_normalized": lic_tag,
            "license_ok": license_ok,
            "digitization_flag": digitization_flag,
            "requires_audio_strip": requires_audio_strip,
            "mime": mime or None,
            "mime_ok": not mime_blocked,
        },
        "reason": reason,
        "source_url": source_url,
        "asset_type": asset_type,
    }

    if log:
        row = {
            "ts": _now(),
            "source_url": source_url,
            "asset_type": asset_type,
            "status": status,
            "checks": result["checks"],
            "reason": reason,
            "metadata": {
                k: meta.get(k)
                for k in ("title", "license_tag", "source_page_url", "digitization_note", "raw_source")
                if k in meta
            },
        }
        _append_jsonl(ASSET_LOG, row)
        if status != "approved":
            _append_jsonl(NEEDS_REVIEW, row)

    return result


def write_job_attribution(
    job_dir: Path | str,
    assets: list[dict[str, Any]],
    *,
    filename: str = "attribution.md",
) -> Path | None:
    """Write BY/SA (and any used OA) credits for a job. Returns path or None if empty."""
    lines: list[str] = ["# Asset attribution", ""]
    n = 0
    for a in assets:
        lic = normalize_license_tag(
            str(a.get("license_tag") or a.get("license") or "")
        )
        if lic not in {"cc-by", "cc-by-sa", "cc0", "public domain"}:
            continue
        title = a.get("title") or "Untitled"
        page = a.get("source_page_url") or a.get("source_url") or ""
        lines.append(f"- **{title}** — {lic} — {page}")
        n += 1
    if n == 0:
        return None
    out = Path(job_dir) / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
