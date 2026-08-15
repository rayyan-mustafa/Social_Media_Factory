"""Curated public-domain hero stills for hybrid Flux compose.

Downloads rights-clear Wikimedia Commons + Met Open Access (CC0) images into a
job, resizes to farm still size (1280×720), and swaps them into selected scene
slots. Optional curated denser pack targets **20–30** bank images.

**Primary farm still path (both channels):** ``rmagine_scene_fetch`` —
every scene tries Wiki+Met (per-scene query distill + vision); PASS places
archival still; REJECT/no-candidate → Flux. **No product cap** on how many
scenes may get Wiki+Met PASSes.
"""

from __future__ import annotations

import json
import logging
import shutil
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# Keep in sync with asset_fetcher.USER_AGENT — Met Incapsula 403s bare/token UAs.
USER_AGENT = (
    "NewYtAutomation/1.0 (VPS archival stills; contact=ops@localhost; "
    "+https://commons.wikimedia.org/wiki/Commons:API)"
)
MET_COLLECTION_API = "https://collectionapi.metmuseum.org/public/collection/v1"
TARGET_HERO_MIN = 20
# Curated denser-pack soft target only (optional bank). Per-scene Wiki+Met
# placement is uncapped via rmagine_scene_fetch (every scene attempts archival).
TARGET_HERO_MID = 25
TARGET_HERO_MAX = 30
# Napstorian PD motion inserts (still→soft push / drop-ins).
TARGET_MOTION_MIN_NAPSTORIAN = 10
TARGET_MOTION_MID_NAPSTORIAN = 12
TARGET_MOTION_MAX_NAPSTORIAN = 15
# Historian: soft/slow Ken-Burns-compatible pans only (History Calling calm).
TARGET_MOTION_MIN_HISTORIAN = 4
TARGET_MOTION_MAX_HISTORIAN = 6

# Curated Tudor / Scottish / Mary Tudor × James V heroes.
# Sources allowed here: Wikimedia Commons (PD/PD-Art/CC0) + Met Open Access (CC0).
# Every entry must carry an explicit commercial-safe license string (gate rejects
# NC/ND/fair-use/unstated/Pexels-as-PD/"check Commons").
SCOTTISH_KING_CURATED: list[dict[str, Any]] = [
    {
        "id": "mary_i_portrait",
        "title": "Mary I of England",
        "commons_file": "Mary I by Master John.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "commercial_ok": True,
    },
    {
        "id": "james_v_portrait",
        "title": "James V of Scotland",
        "commons_file": "Portrait of James V of Scotland (1512 - 1542).jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "James V of Scotland Portrait.png",
        "commercial_ok": True,
    },
    {
        "id": "henry_viii_portrait",
        "title": "Henry VIII",
        "commons_file": "After Hans Holbein the Younger - Portrait of Henry VIII - Google Art Project.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "commercial_ok": True,
    },
    {
        "id": "catherine_of_aragon",
        "title": "Catherine of Aragon",
        "commons_file": "Catalina de Aragón, por un artista anónimo.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Catherine of Aragon (1485-1536).jpg",
        "commercial_ok": True,
    },
    {
        "id": "anne_boleyn_portrait",
        "title": "Anne Boleyn",
        "commons_file": "Anne boleyn.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Anne Boleyn.jpg",
        "commercial_ok": True,
    },
    {
        "id": "elizabeth_i_portrait",
        "title": "Elizabeth I (coronation)",
        "commons_file": "Elizabeth I in coronation robes.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "commercial_ok": True,
    },
    {
        "id": "henry_vii_portrait",
        "title": "Henry VII of England",
        "commons_file": "Henry VII of England, by unknown artist.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "commercial_ok": True,
    },
    {
        "id": "tudor_map",
        "title": "British Isles map (historical)",
        "commons_file": "Map of the British Isles (1578).jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "1631 Blaeu Map of the British Isles (England, Scotland, Ireland) - Geographicus - BritanniaeHiberniae-blaeu-1631.jpg",
        "commercial_ok": True,
    },
    {
        "id": "scotland_map",
        "title": "Scotland / British Isles map",
        "commons_file": "1631 Blaeu Map of the British Isles (England, Scotland, Ireland) - Geographicus - BritanniaeHiberniae-blaeu-1631.jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Map of the British Isles by Abraham Ortelius (Bonhams lot 175, 14 June 2017).jpg",
        "commercial_ok": True,
    },
    {
        "id": "letter_seal",
        "title": "Wax seal / letter",
        "commons_file": "Wax Seal of the Priory of St Clement (Dominicans, Blackfriars, or Friars Preachers), Leicester. From Leicestershire Architectural and Archaeological Society (1884).jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Medieval seal matrix.jpg",
        "commercial_ok": True,
    },
    {
        "id": "edinburgh_castle",
        "title": "Edinburgh Castle engraving",
        "commons_file": "Edinburgh Castle by Thomas Keith.jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Edinburgh Castle from Grass Market.jpg",
        "commercial_ok": True,
    },
    {
        "id": "thomas_more_holbein",
        "title": "Thomas More (Holbein)",
        "commons_file": "Hans Holbein, the Younger - Sir Thomas More - Google Art Project.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "commercial_ok": True,
    },
    {
        "id": "margaret_tudor",
        "title": "Margaret Tudor",
        "commons_file": "Portrait of Margaret Tudor, Queen of Scotland, OB. 1541 (4671646).jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Margaret Tudor.jpg",
        "commercial_ok": True,
    },
    {
        "id": "holyrood",
        "title": "Holyrood Palace (historical view)",
        "commons_file": "Holyrood Palace from Calton Hill.jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Palace of Holyroodhouse.jpg",
        "commercial_ok": True,
    },
    {
        "id": "jane_seymour_portrait",
        "title": "Jane Seymour",
        "commons_file": "Hans Holbein the Younger - Jane Seymour, Queen of England - Google Art Project.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Jane Seymour Holbein.jpg",
        "commercial_ok": True,
    },
    {
        "id": "edward_vi_portrait",
        "title": "Edward VI of England",
        "commons_file": "Circle of William Scrots Edward VI of England.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Edward VI of England c. 1546.jpg",
        "commercial_ok": True,
    },
    {
        "id": "thomas_cromwell_portrait",
        "title": "Thomas Cromwell (Holbein)",
        "commons_file": "Cromwell,Thomas(1EEssex)01.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Hans Holbein d. J. 047.jpg",
        "commercial_ok": True,
    },
    {
        "id": "cardinal_wolsey",
        "title": "Cardinal Wolsey",
        "commons_file": "Cardinal Wolsey (c.1475-1530).jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Sampson Strong's portrait of Cardinal Wolsey at Christ Church (1610).jpg",
        "commercial_ok": True,
    },
    {
        "id": "mary_queen_of_scots",
        "title": "Mary, Queen of Scots",
        "commons_file": "Mary Queen of Scots by Francois Clouet.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Mary, Queen of Scots.jpg",
        "commercial_ok": True,
    },
    {
        "id": "lady_jane_grey",
        "title": "Lady Jane Grey",
        "commons_file": "Streatham Portrait of Lady Jane Grey.jpg",
        "license": "PD-Art (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "The Execution of Lady Jane Grey.jpg",
        "commercial_ok": True,
    },
    {
        "id": "hampton_court",
        "title": "Hampton Court Palace (historical view)",
        "commons_file": "Hampton Court Palace, the East Front.jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Hampton Court Palace 1818.jpg",
        "commercial_ok": True,
    },
    {
        "id": "tower_of_london",
        "title": "Tower of London engraving",
        "commons_file": "Tower of London from the Thames.jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Tower of London engraved by Wenceslaus Hollar.jpg",
        "commercial_ok": True,
    },
    {
        "id": "ortelius_britain",
        "title": "Ortelius map of Britain",
        "commons_file": "Map of the British Isles by Abraham Ortelius (Bonhams lot 175, 14 June 2017).jpg",
        "license": "PD (Wikimedia Commons)",
        "source": "wikimedia_commons",
        "fallback_commons_file": "Ortelius - Britannia.jpg",
        "commercial_ok": True,
    },
]

# Met Open Access = CC0 for designated public-domain works (isPublicDomain=true).
# Verified object IDs (collection API); fetch path re-checks isPublicDomain + image.
MET_OPEN_ACCESS_CURATED: list[dict[str, Any]] = [
    {
        "id": "met_henry_viii_field_armor",
        "title": "Field Armor of King Henry VIII",
        "met_object_id": 23936,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_henry_viii_armor_garniture",
        "title": "Armor Garniture, probably of Henry VIII",
        "met_object_id": 22741,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_holbein_wedigh",
        "title": "Hermann von Wedigh III (Holbein)",
        "met_object_id": 436658,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_holbein_erasmus",
        "title": "Erasmus of Rotterdam (Holbein)",
        "met_object_id": 459080,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_holbein_royal_livery",
        "title": "Portrait of a Man in Royal Livery (Holbein)",
        "met_object_id": 436660,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_dinteville_allegory",
        "title": "Moses and Aaron before Pharaoh (Dinteville allegory, 1537)",
        "met_object_id": 437217,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_giles_capel_helm",
        "title": "Foot-Combat Helm of Sir Giles Capel",
        "met_object_id": 21997,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_burgonet",
        "title": "Burgonet (Met OA armor)",
        "met_object_id": 22263,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_parade_gauntlet",
        "title": "Gauntlet for the Right Hand (Met OA)",
        "met_object_id": 26587,
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
]

# Default denser pack: Commons stills + Met OA (dedupe by id; prefer listed order).
DEFAULT_PD_HERO_CURATED: list[dict[str, Any]] = [
    *SCOTTISH_KING_CURATED,
    *MET_OPEN_ACCESS_CURATED,
]


@dataclass
class PdHero:
    id: str
    title: str
    path: str
    commons_file: str
    license: str
    source: str
    scene_index: int | None = None
    url: str | None = None
    source_url: str | None = None
    commercial_ok: bool = True
    youtube_monetization_safe: bool = True
    met_object_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Keep url + source_url aligned for older readers
        if not d.get("source_url") and d.get("url"):
            d["source_url"] = d["url"]
        if not d.get("url") and d.get("source_url"):
            d["url"] = d["source_url"]
        return d


def commons_file_page_url(filename: str) -> str:
    enc = urllib.parse.quote(filename.replace(" ", "_"))
    return f"https://commons.wikimedia.org/wiki/File:{enc}"


def commons_thumb_url(filename: str, *, width: int = 1600) -> str:
    """Build a Commons Special:FilePath URL (follows to current file)."""
    enc = urllib.parse.quote(filename.replace(" ", "_"))
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{enc}?width={width}"


def met_object_page_url(object_id: int) -> str:
    return f"https://www.metmuseum.org/art/collection/search/{int(object_id)}"


def fetch_url(url: str, dest: Path, *, timeout: float = 60.0) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if len(data) < 800:
        raise RuntimeError(f"download too small ({len(data)} B): {url}")
    dest.write_bytes(data)
    return dest


# Back-compat alias
download_url = fetch_url


def fetch_json(url: str, *, timeout: float = 45.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object from {url}")
    return payload


def fetch_met_object(object_id: int, *, timeout: float = 45.0) -> dict[str, Any]:
    """GET Met Collection API object record."""
    url = f"{MET_COLLECTION_API}/objects/{int(object_id)}"
    return fetch_json(url, timeout=timeout)


def met_open_access_image_url(obj: dict[str, Any]) -> str | None:
    """Return primary image URL only when Met marks the work public domain."""
    if not obj.get("isPublicDomain"):
        return None
    for key in ("primaryImage", "primaryImageSmall"):
        url = str(obj.get(key) or "").strip()
        if url.startswith("http"):
            return url
    return None


def write_asset_license_sidecar(
    asset_path: Path,
    *,
    license: str,
    source: str,
    source_url: str,
    title: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Stamp commercial_ok + source_url + license next to a fitted asset."""
    lic = license_gate_or_raise(license, label=asset_path.name)
    side = Path(asset_path).with_suffix(Path(asset_path).suffix + ".license.json")
    payload: dict[str, Any] = {
        "title": title or Path(asset_path).stem,
        "license": lic,
        "commercial_ok": True,
        "youtube_monetization_safe": True,
        "source": source,
        "source_url": source_url,
    }
    if extra:
        payload.update(extra)
    side.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return side


def fit_still(src: Path, dest: Path, *, width: int = 1280, height: int = 720) -> Path:
    """Center-crop / letterbox to exact farm still size.

    Memory-safe: rejects non-rasters / huge files, uses draft decode + thumbnail
    so Wiki multi‑100MB scans never expand to 400MB+ RGB buffers.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    suf = src.suffix.lower()
    if suf in {".pdf", ".djvu", ".svg", ".tif", ".tiff"}:
        raise ValueError(f"fit_still refuses non-raster: {src.name}")
    try:
        st = src.stat().st_size
    except OSError as exc:
        raise ValueError(f"fit_still missing source: {src}") from exc
    if st > 25 * 1024 * 1024:
        raise ValueError(f"fit_still source too large ({st}B): {src.name}")

    Image.MAX_IMAGE_PIXELS = 40_000_000
    with Image.open(src) as raw:
        # Downscale in decoder when possible, then convert.
        raw.draft("RGB", (width * 2, height * 2))
        img = raw.convert("RGB")
        # Bound working set before LANCZOS upscale/crop.
        max_edge = max(width, height) * 3
        if max(img.size) > max_edge:
            img.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)
        tw, th = width, height
        scale = max(tw / img.width, th / img.height)
        nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        left = max(0, (nw - tw) // 2)
        top = max(0, (nh - th) // 2)
        img = img.crop((left, top, left + tw, top + th))
        if img.size != (tw, th):
            canvas = Image.new("RGB", (tw, th), (12, 14, 18))
            canvas.paste(img, ((tw - img.width) // 2, (th - img.height) // 2))
            img = canvas
        img.save(dest, format="JPEG", quality=92, optimize=True)
    return dest


def _hero_source_url(item: dict[str, Any], *, commons_file: str = "", met_id: int | None = None) -> str:
    explicit = str(item.get("source_url") or item.get("url") or "").strip()
    if explicit:
        return explicit
    if met_id is not None:
        return met_object_page_url(int(met_id))
    if commons_file:
        return commons_file_page_url(commons_file)
    return ""


def _stamp_hero_license(fitted: Path, hero: PdHero) -> None:
    try:
        write_asset_license_sidecar(
            fitted,
            license=hero.license,
            source=hero.source,
            source_url=str(hero.source_url or hero.url or ""),
            title=hero.title,
            extra={
                "id": hero.id,
                "commons_file": hero.commons_file or None,
                "met_object_id": hero.met_object_id,
            },
        )
    except ValueError as exc:
        logger.warning("hero license sidecar refused %s: %s", hero.id, exc)


def fetch_met_open_access_still(
    object_id: int,
    dest_raw: Path,
    *,
    timeout: float = 60.0,
) -> tuple[Path, dict[str, Any], str]:
    """Download a Met Open Access still; refuse if not public-domain / no image."""
    obj = fetch_met_object(object_id, timeout=timeout)
    img_url = met_open_access_image_url(obj)
    if not img_url:
        raise RuntimeError(
            f"MET object {object_id} not Open Access / no primary image "
            f"(isPublicDomain={obj.get('isPublicDomain')!r})"
        )
    fetch_url(img_url, dest_raw, timeout=timeout)
    return dest_raw, obj, img_url


def fetch_curated_heroes(
    out_dir: Path,
    *,
    curated: list[dict[str, Any]] | None = None,
    width: int = 1280,
    height: int = 720,
    reuse_fitted: bool = True,
    pause_s: float = 1.25,
    max_heroes: int = TARGET_HERO_MAX,
) -> list[PdHero]:
    """Download curated Wikimedia + Met Open Access stills into ``out_dir``.

    Aims for ``TARGET_HERO_MIN``–``TARGET_HERO_MAX`` commercial-safe heroes.
    Skips entries that fail the license gate. Non-blocking if fewer available.
    """
    import time

    out_dir = Path(out_dir)
    raw_dir = out_dir / "raw"
    fit_dir = out_dir / "fitted"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fit_dir.mkdir(parents=True, exist_ok=True)

    cap = max(1, min(int(max_heroes or TARGET_HERO_MAX), TARGET_HERO_MAX))
    heroes: list[PdHero] = []
    blocked: list[dict[str, Any]] = []

    for item in curated or DEFAULT_PD_HERO_CURATED:
        if len(heroes) >= cap:
            break
        hid = str(item.get("id") or "").strip()
        if not hid:
            continue
        lic_raw = str(item.get("license") or "").strip()
        source = str(item.get("source") or "").strip() or (
            "met_open_access" if item.get("met_object_id") else "wikimedia_commons"
        )
        # Quantity increase path: only Wikimedia + Met Open Access.
        if source not in {"wikimedia_commons", "met_open_access"}:
            blocked.append({"id": hid, "reason": "source_not_allowed", "source": source})
            continue
        if item.get("commercial_ok") is False or not license_is_commercial_ok(lic_raw):
            blocked.append({"id": hid, "reason": "license_not_commercial_ok", "license": lic_raw})
            logger.warning("skipping hero %s (license gate): %s", hid, lic_raw)
            continue
        try:
            lic = license_gate_or_raise(lic_raw, label=hid)
        except ValueError as exc:
            blocked.append({"id": hid, "reason": str(exc)})
            continue

        fitted = fit_dir / f"{hid}.jpg"
        met_id = item.get("met_object_id")
        met_id_i = int(met_id) if met_id is not None else None
        commons = str(item.get("commons_file") or "")
        src_url = _hero_source_url(item, commons_file=commons, met_id=met_id_i)

        if reuse_fitted and fitted.is_file() and fitted.stat().st_size > 2000:
            hero = PdHero(
                id=hid,
                title=str(item.get("title") or hid),
                path=str(fitted.resolve()),
                commons_file=commons,
                license=lic,
                source=source,
                url=src_url,
                source_url=src_url,
                commercial_ok=True,
                youtube_monetization_safe=True,
                met_object_id=met_id_i,
            )
            _stamp_hero_license(fitted, hero)
            heroes.append(hero)
            continue

        saved: Path | None = None
        used_file = commons
        used_url = src_url
        last_err: Exception | None = None

        if met_id_i is not None:
            dest_raw = raw_dir / f"{hid}.jpg"
            try:
                time.sleep(pause_s)
                saved, obj, img_url = fetch_met_open_access_still(met_id_i, dest_raw)
                used_url = met_object_page_url(met_id_i)
                title_override = str(obj.get("title") or "").strip()
                if title_override and not item.get("title"):
                    item = {**item, "title": title_override}
                # Prefer page URL for provenance; keep CDN in sidecar extra via stamp
                item = {**item, "_met_image_url": img_url}
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("MET PD fetch failed %s (%s): %s", hid, met_id_i, exc)
                blocked.append({"id": hid, "reason": f"met_fetch_failed:{exc}"[:200]})
        else:
            files = [commons] if commons else []
            fb = item.get("fallback_commons_file")
            if fb:
                files.append(str(fb))
            for fname in files:
                if not fname:
                    continue
                url = commons_thumb_url(fname)
                ext = ".jpg"
                low = fname.lower()
                if low.endswith(".png"):
                    ext = ".png"
                elif low.endswith(".svg"):
                    continue
                dest_raw = raw_dir / f"{hid}{ext}"
                try:
                    time.sleep(pause_s)
                    fetch_url(url, dest_raw)
                    saved = dest_raw
                    used_file = fname
                    used_url = commons_file_page_url(fname)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    logger.warning("PD fetch failed %s: %s", fname, exc)

        if saved is None:
            if last_err is not None and met_id_i is None:
                blocked.append({"id": hid, "reason": f"fetch_failed:{last_err}"[:200]})
            logger.warning("skipping hero %s: %s", hid, last_err)
            continue
        try:
            fit_still(saved, fitted, width=width, height=height)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fit failed %s: %s", hid, exc)
            blocked.append({"id": hid, "reason": f"fit_failed:{exc}"[:200]})
            continue

        hero = PdHero(
            id=hid,
            title=str(item.get("title") or hid),
            path=str(fitted.resolve()),
            commons_file=used_file,
            license=lic,
            source=source,
            url=used_url,
            source_url=used_url,
            commercial_ok=True,
            youtube_monetization_safe=True,
            met_object_id=met_id_i,
        )
        _stamp_hero_license(fitted, hero)
        heroes.append(hero)

    sidecar = out_dir / "pd_heroes.json"
    sidecar.write_text(
        json.dumps(
            {
                "count": len(heroes),
                "target_min": TARGET_HERO_MIN,
                "target_max": TARGET_HERO_MAX,
                "heroes": [h.to_dict() for h in heroes],
                "blocked": blocked,
                "allowed_sources": ["wikimedia_commons", "met_open_access"],
                "commercial_use_only": True,
                "youtube_monetization_safe_required": True,
                "notes": (
                    "Curated Wikimedia Commons + Met Open Access (CC0) stills. "
                    f"Target {TARGET_HERO_MIN}–{TARGET_HERO_MAX} when available; "
                    "each asset stamped commercial_ok + source_url + license."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return heroes


def _chapter_anchor_indices(
    script: dict[str, Any] | None,
    n_want: int,
    *,
    max_index: int | None = None,
    min_index: int = 3,
) -> list[int]:
    """Pick scene indices for PD still/motion inserts.

    When ``max_index`` is set (e.g. SOP 10m head uses scenes 0–68), anchors stay
    inside that window so short production cuts actually receive the motion inserts.
    """

    def _in_window(idx: int) -> bool:
        if idx < max(0, int(min_index)):
            return False
        if max_index is not None and idx > int(max_index):
            return False
        return True

    if not script:
        start = max(5, int(min_index))
        raw = list(range(start, start + n_want * 8, 8))
        return [a for a in raw if _in_window(a)][:n_want]

    first: dict[int, int] = {}
    scenes: list[int] = []
    for sc in script.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        try:
            idx = int(sc.get("index"))
            cid = int(sc.get("chapter_id") or 0)
        except (TypeError, ValueError):
            continue
        if max_index is not None and idx > int(max_index):
            continue
        scenes.append(idx)
        if cid and cid not in first and _in_window(idx):
            first[cid] = idx
    # Skip hook chapter id=1; take chapter starts then mid fills
    anchors = [first[c] for c in sorted(first) if c != 1]
    if len(anchors) < n_want and scenes:
        step = max(1, len(scenes) // (n_want + 1))
        for i in range(1, n_want + 1):
            if i * step < len(scenes):
                cand = scenes[i * step]
                if _in_window(cand):
                    anchors.append(cand)
    # Even spacing fallback when chapter starts are sparse inside the window
    if len(anchors) < n_want and scenes:
        window = [s for s in scenes if _in_window(s)]
        if window:
            for i in range(n_want):
                pos = int(round((i + 1) * (len(window) / (n_want + 1))))
                pos = min(max(0, pos), len(window) - 1)
                anchors.append(window[pos])
    # Dedupe preserve order
    out: list[int] = []
    seen: set[int] = set()
    for a in anchors:
        if a not in seen and _in_window(a):
            out.append(a)
            seen.add(a)
        if len(out) >= n_want:
            break
    return out


def apply_pd_heroes_to_job(
    job_dir: Path,
    *,
    heroes: list[PdHero] | None = None,
    script: dict[str, Any] | None = None,
    width: int = 1280,
    height: int = 720,
) -> dict[str, Any]:
    """Swap fitted PD stills into job ``images/scene_XXX.jpg`` + visual_manifest.

    Backs up originals to ``images/flux_backup/``. Non-blocking if no heroes.
    """
    job_dir = Path(job_dir)
    images = job_dir / "images"
    if not images.is_dir():
        raise FileNotFoundError(f"images dir missing: {images}")

    pd_dir = job_dir / "images" / "pd_heroes"
    if heroes is None:
        heroes = fetch_curated_heroes(pd_dir, width=width, height=height)
    if not heroes:
        return {"ok": False, "pd_clip_count": 0, "reason": "no_heroes_downloaded"}

    if script is None:
        sp = job_dir / "script" / "script.json"
        if sp.is_file():
            script = json.loads(sp.read_text(encoding="utf-8"))

    anchors = _chapter_anchor_indices(script, len(heroes))
    backup = images / "flux_backup"
    backup.mkdir(parents=True, exist_ok=True)

    applied: list[dict[str, Any]] = []
    for hero, scene_idx in zip(heroes, anchors):
        dest = images / f"scene_{int(scene_idx):03d}.jpg"
        if not dest.is_file():
            continue
        bak = backup / dest.name
        if not bak.exists():
            shutil.copy2(dest, bak)
        shutil.copy2(hero.path, dest)
        hero.scene_index = int(scene_idx)
        applied.append(
            {
                **hero.to_dict(),
                "scene_index": int(scene_idx),
                "replaced": str(dest.resolve()),
                "backup": str(bak.resolve()),
            }
        )

    # Patch visual_manifest paths stay the same; stamp meta.
    vm_path = images / "visual_manifest.json"
    if vm_path.is_file():
        try:
            vm = json.loads(vm_path.read_text(encoding="utf-8"))
            meta = vm.setdefault("meta", {})
            if isinstance(meta, dict):
                meta["pd_clippings"] = True
                meta["pd_clip_count"] = len(applied)
                meta["pd_heroes"] = applied
            # Mark swapped scenes in scene entries
            by_idx = {int(a["scene_index"]): a for a in applied}
            for sc in vm.get("scenes") or []:
                if not isinstance(sc, dict):
                    continue
                try:
                    idx = int(sc.get("index"))
                except (TypeError, ValueError):
                    continue
                if idx in by_idx:
                    sc["pd_hero"] = True
                    sc["pd_hero_id"] = by_idx[idx].get("id")
                    sc["backend"] = "pd_clippings"
            vm_path.write_text(
                json.dumps(vm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("visual_manifest PD stamp failed: %s", exc)

    report = {
        "ok": True,
        "pd_clippings": True,
        "pd_clip_count": len(applied),
        "target_min": TARGET_HERO_MIN,
        "target_max": TARGET_HERO_MAX,
        "heroes": applied,
        "pd_dir": str(pd_dir.resolve()),
        "allowed_sources": ["wikimedia_commons", "met_open_access"],
        "commercial_use_only": True,
        "youtube_monetization_safe_required": True,
    }
    (pd_dir / "apply_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Job-level stamp for SMM SOP
    stamp = job_dir / "pd_clippings.json"
    stamp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# PD motion clips — napstorian denser (10–15); historian soft pans (4–6)
# ---------------------------------------------------------------------------
# Tudor eras have almost no true contemporary film. We synthesize short
# camera-moves from **already commercial-safe** PD/PD-Art/CC0 stills, or accept
# human drop-ins that ship a matching ``.license.json`` with commercial_ok=true.
# Napstorian: denser documentary inserts. Historian: soft/slow Ken-Burns pans
# only (History Calling calm — no action montages).
#
# HARD RULE: no NC/ND/fair-use/unstated licenses — YouTube monetization safe only.

_LICENSE_REJECT_TOKENS = (
    "by-nc",
    "by-nd",
    "nc-sa",
    "nc/",
    " noncommercial",
    "non-commercial",
    "fair use",
    "fairuse",
    "all rights reserved",
    "copyrighted",
    "rights reserved",
    "no derivatives",
    "nd ",
    " nd",
    "pexels",  # not PD — do not label as PD
    "mixkit",
    "verify drop-in",
    "check commons",
    "check file",
    "unstated",
    "unknown",
)

_LICENSE_ALLOW_TOKENS = (
    "pd-art",
    "public domain",
    "public_domain",
    " pd ",
    "pd/",
    "(pd)",
    "cc0",
    "cc-zero",
    "creative commons zero",
    "us government",
    "us-gov",
    "work of the united states government",
    "pdm",
    "met open access",
    "open access",
)


def license_is_commercial_ok(license_text: str | None) -> bool:
    """Return True only for clearly commercial-safe license strings.

    Allows: Public Domain / PD-Art / CC0 / US government / Met Open Access (CC0).
    Rejects: NC, ND, fair use, unstated, modern stock labeled as PD.
    """
    import re

    raw = str(license_text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    compact = re.sub(r"[^a-z0-9]+", "", low)

    reject_compact = (
        "bync",
        "bynd",
        "ncsa",
        "noncommercial",
        "fairuse",
        "allrightsreserved",
        "copyrighted",
        "noderivatives",
        "pexels",
        "mixkit",
        "verifydropin",
        "checkcommons",
        "checkfile",
        "unstated",
        "unknown",
        "rightsreserved",
    )
    for bad in reject_compact:
        if bad in compact:
            return False
    # Word-boundary-ish NC/ND (avoid matching "france")
    if re.search(r"(^|[^a-z])nc([^a-z]|$)", low) and "france" not in low:
        if "public domain" not in low and "pd-art" not in low and "cc0" not in low:
            # "NC" alone in Creative Commons sense
            if "creative commons" in low or "cc by" in low or "by-nc" in low.replace(" ", ""):
                return False

    if "publicdomain" in compact or "pdart" in compact:
        return True
    if "cc0" in compact or "creativecommonszero" in compact:
        return True
    if "usgovernment" in compact or "unitedstatesgovernment" in compact:
        return True
    # Met Open Access designated works are CC0
    if "metopenaccess" in compact or (
        "openaccess" in compact and ("met" in compact or "cc0" in compact or "museum" in compact)
    ):
        return True
    # "PD …" / "PD-Art" / "PD (Wikimedia…)"
    if compact.startswith("pd"):
        return True
    if low.startswith("pd ") or low.startswith("pd-") or low.startswith("pd/"):
        return True
    return False


def license_gate_or_raise(license_text: str | None, *, label: str = "asset") -> str:
    """Refuse assets that are not commercial-safe."""
    lic = str(license_text or "").strip()
    if not license_is_commercial_ok(lic):
        raise ValueError(
            f"license gate refused {label}: {lic!r} "
            "(need PD / PD-Art / CC0 / US-gov / Met Open Access; "
            "no NC/ND/fair-use/unstated)"
        )
    return lic


def _motion_clip_record(
    *,
    id: str,
    title: str,
    path: str,
    source: str,
    license: str,
    kind: str,
    commons_file: str | None = None,
    still_path: str | None = None,
    source_url: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lic = license_gate_or_raise(license, label=id)
    url = source_url
    if not url and commons_file:
        url = commons_thumb_url(str(commons_file))
    out: dict[str, Any] = {
        "id": id,
        "title": title,
        "path": path,
        "source": source,
        "license": lic,
        "kind": kind,
        "commercial_ok": True,
        "youtube_monetization_safe": True,
        "source_url": url or "",
    }
    if commons_file:
        out["commons_file"] = commons_file
    if still_path:
        out["still_path"] = still_path
    if extra:
        out.update(extra)
    return out


def _read_dropin_license(video_path: Path) -> dict[str, Any] | None:
    """Require sidecar ``name.license.json`` (or ``licenses.json`` map) for drop-ins."""
    sidecar = video_path.with_suffix(video_path.suffix + ".license.json")
    if not sidecar.is_file():
        sidecar = video_path.with_name(video_path.stem + ".license.json")
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None
    # Optional directory manifest
    for parent in (video_path.parent, video_path.parent.parent):
        man = parent / "licenses.json"
        if not man.is_file():
            continue
        try:
            data = json.loads(man.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            entry = data.get(video_path.name) or data.get(video_path.stem)
            if isinstance(entry, dict):
                return entry
    return None


# Only stills with explicit PD / PD-Art / Met OA in curated hero pack.
NAPSTORIAN_PD_MOTION_CURATED: list[dict[str, Any]] = [
    {
        "id": "mary_i_motion",
        "title": "Mary I portrait (slow push-in)",
        "hero_id": "mary_i_portrait",
        "commons_file": "Mary I by Master John.jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "james_v_motion",
        "title": "James V of Scotland portrait (slow push-in)",
        "hero_id": "james_v_portrait",
        "commons_file": "Portrait of James V of Scotland (1512 - 1542).jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "henry_viii_motion",
        "title": "Henry VIII portrait (slow push-in)",
        "hero_id": "henry_viii_portrait",
        "commons_file": "After Hans Holbein the Younger - Portrait of Henry VIII - Google Art Project.jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "catherine_aragon_motion",
        "title": "Catherine of Aragon portrait (slow push-in)",
        "hero_id": "catherine_of_aragon",
        "commons_file": "Catalina de Aragón, por un artista anónimo.jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "anne_boleyn_motion",
        "title": "Anne Boleyn portrait (slow push-in)",
        "hero_id": "anne_boleyn_portrait",
        "commons_file": "Anne boleyn.jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "elizabeth_i_motion",
        "title": "Elizabeth I coronation (slow push-in)",
        "hero_id": "elizabeth_i_portrait",
        "commons_file": "Elizabeth I in coronation robes.jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "map_british_isles_motion",
        "title": "British Isles map 1578 (slow push)",
        "hero_id": "tudor_map",
        "commons_file": "Map of the British Isles (1578).jpg",
        "kind": "still_motion",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "scotland_map_motion",
        "title": "Scotland / British Isles map (slow push)",
        "hero_id": "scotland_map",
        "commons_file": "1631 Blaeu Map of the British Isles (England, Scotland, Ireland) - Geographicus - BritanniaeHiberniae-blaeu-1631.jpg",
        "kind": "still_motion",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "thomas_more_motion",
        "title": "Thomas More Holbein (slow push-in)",
        "hero_id": "thomas_more_holbein",
        "commons_file": "Hans Holbein, the Younger - Sir Thomas More - Google Art Project.jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "margaret_tudor_motion",
        "title": "Margaret Tudor portrait (slow push-in)",
        "hero_id": "margaret_tudor",
        "commons_file": "Portrait of Margaret Tudor, Queen of Scotland, OB. 1541 (4671646).jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "edinburgh_castle_motion",
        "title": "Edinburgh Castle engraving (slow push)",
        "hero_id": "edinburgh_castle",
        "commons_file": "Edinburgh Castle by Thomas Keith.jpg",
        "kind": "still_motion",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "met_henry_armor_motion",
        "title": "Henry VIII field armor (Met OA slow push)",
        "hero_id": "met_henry_viii_field_armor",
        "met_object_id": 23936,
        "kind": "still_motion",
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "met_armor_garniture_motion",
        "title": "Henry VIII armor garniture (Met OA slow push)",
        "hero_id": "met_henry_viii_armor_garniture",
        "met_object_id": 22741,
        "kind": "still_motion",
        "license": "CC0 (Met Open Access)",
        "source": "met_open_access",
        "commercial_ok": True,
    },
    {
        "id": "jane_seymour_motion",
        "title": "Jane Seymour portrait (slow push-in)",
        "hero_id": "jane_seymour_portrait",
        "commons_file": "Hans Holbein the Younger - Jane Seymour, Queen of England - Google Art Project.jpg",
        "kind": "still_motion",
        "license": "PD-Art (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "letter_seal_motion",
        "title": "Wax seal / letter (slow push)",
        "hero_id": "letter_seal",
        "commons_file": "Wax Seal of the Priory of St Clement (Dominicans, Blackfriars, or Friars Preachers), Leicester. From Leicestershire Architectural and Archaeological Society (1884).jpg",
        "kind": "still_motion",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
]

# Soft/slow pans only for napping_historian (maps, manuscripts, calm portraits).
HISTORIAN_PD_MOTION_CURATED: list[dict[str, Any]] = [
    {
        "id": "hist_map_british_isles",
        "title": "British Isles map (soft pan)",
        "hero_id": "tudor_map",
        "commons_file": "Map of the British Isles (1578).jpg",
        "kind": "still_motion_soft",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "hist_scotland_map",
        "title": "Scotland map (soft pan)",
        "hero_id": "scotland_map",
        "commons_file": "1631 Blaeu Map of the British Isles (England, Scotland, Ireland) - Geographicus - BritanniaeHiberniae-blaeu-1631.jpg",
        "kind": "still_motion_soft",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "hist_letter_seal",
        "title": "Wax seal / letter (soft pan)",
        "hero_id": "letter_seal",
        "commons_file": "Wax Seal of the Priory of St Clement (Dominicans, Blackfriars, or Friars Preachers), Leicester. From Leicestershire Architectural and Archaeological Society (1884).jpg",
        "kind": "still_motion_soft",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "hist_edinburgh_castle",
        "title": "Edinburgh Castle (soft pan)",
        "hero_id": "edinburgh_castle",
        "commons_file": "Edinburgh Castle by Thomas Keith.jpg",
        "kind": "still_motion_soft",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "hist_holyrood",
        "title": "Holyrood Palace (soft pan)",
        "hero_id": "holyrood",
        "commons_file": "Holyrood Palace from Calton Hill.jpg",
        "kind": "still_motion_soft",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
    {
        "id": "hist_ortelius_britain",
        "title": "Ortelius Britain map (soft pan)",
        "hero_id": "ortelius_britain",
        "commons_file": "Map of the British Isles by Abraham Ortelius (Bonhams lot 175, 14 June 2017).jpg",
        "kind": "still_motion_soft",
        "license": "PD (Wikimedia Commons)",
        "commercial_ok": True,
    },
]


def _run_ffmpeg(cmd: list[str], *, label: str) -> None:
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"ffmpeg {label} failed: {err}")


def synthesize_motion_from_still(
    still: Path,
    dest: Path,
    *,
    duration_s: float = 8.0,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    soft: bool = False,
    zoom_end: float | None = None,
) -> Path:
    """Build a short documentary push-in from a PD still (no external video needed).

    ``soft=True`` (historian / History Calling calm) uses a gentler Ken-Burns
    zoom (~1.06) instead of the denser napstorian push (~1.18).
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dur = max(3.0, min(12.0, float(duration_s)))
    frames = max(1, int(round(dur * fps)))
    if zoom_end is not None:
        z_end = float(zoom_end)
    else:
        z_end = 1.06 if soft else 1.18
    z_end = max(1.02, min(1.25, z_end))
    z_step = max(0.00005, (z_end - 1.0) / frames)
    vf = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(1.0+on*{z_step:.6f},{z_end})':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={width}x{height}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-loop",
        "1",
        "-i",
        str(still),
        "-vf",
        vf,
        "-t",
        f"{dur:.3f}",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        str(dest),
    ]
    _run_ffmpeg(cmd, label=f"pd_motion_still {still.name}")
    return dest


def prepare_motion_clip(
    src: Path,
    dest: Path,
    *,
    max_duration_s: float = 10.0,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
) -> Path:
    """Trim/scale an existing PD/open video into a short 1280×720 insert."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dur = max(3.0, min(12.0, float(max_duration_s)))
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},setsar=1,format=yuv420p"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0",
        "-i",
        str(src),
        "-t",
        f"{dur:.3f}",
        "-vf",
        vf,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        str(dest),
    ]
    _run_ffmpeg(cmd, label=f"pd_motion_trim {src.name}")
    return dest


def _local_pd_motion_drop_ins(job_dir: Path) -> list[Path]:
    roots = [
        job_dir / "clips" / "pd",
        job_dir / "images" / "pd_motion" / "raw",
        Path(__file__).resolve().parents[2] / "assets" / "pd_motion",
    ]
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir()):
            if p.name.startswith("."):
                continue
            if p.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"} and p.stat().st_size > 2000:
                out.append(p)
    return out


def fetch_or_build_pd_motion_clips(
    out_dir: Path,
    *,
    curated: list[dict[str, Any]] | None = None,
    job_dir: Path | None = None,
    min_clips: int = TARGET_MOTION_MIN_NAPSTORIAN,
    max_clips: int = TARGET_MOTION_MAX_NAPSTORIAN,
    duration_s: float = 8.0,
    width: int = 1280,
    height: int = 720,
    soft: bool = False,
) -> list[dict[str, Any]]:
    """Prepare commercial-safe PD motion clips under ``out_dir/fitted``.

    Napstorian default **10–15**; historian soft mode **4–6** (caller sets caps).
    """
    out_dir = Path(out_dir)
    fit_dir = out_dir / "fitted"
    raw_dir = out_dir / "raw"
    fit_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    clips: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    pack = curated
    if pack is None:
        pack = HISTORIAN_PD_MOTION_CURATED if soft else NAPSTORIAN_PD_MOTION_CURATED

    # 1) Local drop-ins — ONLY with commercial_ok license sidecar
    for i, src in enumerate(_local_pd_motion_drop_ins(job_dir) if job_dir else []):
        if len(clips) >= max_clips:
            break
        meta = _read_dropin_license(src)
        if not meta:
            blocked.append(
                {
                    "path": str(src),
                    "reason": "missing_license_sidecar",
                    "need": f"{src.name}.license.json with commercial_ok=true",
                }
            )
            logger.warning(
                "pd motion drop-in blocked (no license sidecar): %s", src.name
            )
            continue
        lic = str(meta.get("license") or "")
        if meta.get("commercial_ok") is False or not license_is_commercial_ok(lic):
            blocked.append(
                {"path": str(src), "reason": "license_not_commercial_ok", "license": lic}
            )
            logger.warning(
                "pd motion drop-in blocked (license): %s (%s)", src.name, lic
            )
            continue
        dest = fit_dir / f"dropin_{i:02d}.mp4"
        try:
            if not dest.is_file() or dest.stat().st_size < 2000:
                prepare_motion_clip(
                    src, dest, max_duration_s=duration_s, width=width, height=height
                )
            clips.append(
                _motion_clip_record(
                    id=f"dropin_{i:02d}",
                    title=str(meta.get("title") or src.stem),
                    path=str(dest.resolve()),
                    source="local_dropin",
                    license=lic,
                    kind="video",
                    source_url=str(meta.get("source_url") or meta.get("url") or ""),
                    extra={"dropin_src": str(src.resolve())},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("pd motion drop-in failed %s: %s", src, exc)
            blocked.append({"path": str(src), "reason": str(exc)[:200]})

    # 2) Curated still→motion from verified PD/PD-Art stills only
    for item in pack:
        if len(clips) >= max_clips:
            break
        cid = str(item["id"])
        lic = str(item.get("license") or "")
        if not license_is_commercial_ok(lic) or not item.get("commercial_ok", True):
            blocked.append({"id": cid, "reason": "curated_license_rejected", "license": lic})
            continue
        dest = fit_dir / f"{cid}.mp4"
        commons = str(item.get("commons_file") or "")
        met_oid = item.get("met_object_id")
        met_oid_i = int(met_oid) if met_oid is not None else None
        motion_source = str(item.get("source") or "pd_still_motion")
        item_soft = soft or str(item.get("kind") or "").endswith("_soft")
        src_url = _hero_source_url(item, commons_file=commons, met_id=met_oid_i)
        if dest.is_file() and dest.stat().st_size > 2000:
            try:
                rec = _motion_clip_record(
                    id=cid,
                    title=str(item.get("title") or cid),
                    path=str(dest.resolve()),
                    source=motion_source,
                    license=lic,
                    kind="still_motion_soft" if item_soft else "still_motion",
                    commons_file=commons or None,
                    source_url=src_url,
                    extra={"met_object_id": met_oid_i} if met_oid_i else None,
                )
                clips.append(rec)
                write_asset_license_sidecar(
                    dest,
                    license=lic,
                    source=motion_source,
                    source_url=str(rec.get("source_url") or src_url),
                    title=str(item.get("title") or cid),
                    extra={"id": cid, "met_object_id": met_oid_i},
                )
            except ValueError as exc:
                blocked.append({"id": cid, "reason": str(exc)})
            continue

        still: Path | None = None
        hero_id = str(item.get("hero_id") or cid.replace("_motion", ""))
        if job_dir is not None:
            for a in (
                job_dir / "images" / "pd_heroes" / "fitted" / f"{hero_id}.jpg",
                job_dir / "images" / "pd_heroes" / "fitted" / f"{cid}.jpg",
            ):
                if a.is_file() and a.stat().st_size > 2000:
                    still = a
                    break
        if still is None:
            try:
                raw = raw_dir / f"{cid}.jpg"
                if met_oid_i is not None:
                    fetch_met_open_access_still(met_oid_i, raw)
                    src_url = met_object_page_url(met_oid_i)
                elif commons:
                    fetch_url(commons_thumb_url(commons), raw)
                    src_url = commons_file_page_url(commons)
                else:
                    continue
                still = fit_still(
                    raw, raw_dir / f"{cid}_fit.jpg", width=width, height=height
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("pd motion still fetch failed %s: %s", cid, exc)
                blocked.append({"id": cid, "reason": f"fetch_failed:{exc}"[:200]})
                continue
        try:
            synthesize_motion_from_still(
                still,
                dest,
                duration_s=duration_s,
                width=width,
                height=height,
                soft=item_soft,
            )
            rec = _motion_clip_record(
                id=cid,
                title=str(item.get("title") or cid),
                path=str(dest.resolve()),
                source=motion_source,
                license=lic,
                kind="still_motion_soft" if item_soft else "still_motion",
                commons_file=commons or None,
                still_path=str(still),
                source_url=src_url,
                extra={"met_object_id": met_oid_i} if met_oid_i else None,
            )
            clips.append(rec)
            write_asset_license_sidecar(
                dest,
                license=lic,
                source=motion_source,
                source_url=str(rec.get("source_url") or src_url),
                title=str(item.get("title") or cid),
                extra={"id": cid, "met_object_id": met_oid_i, "still_path": str(still)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("pd motion synthesize failed %s: %s", cid, exc)
            blocked.append({"id": cid, "reason": str(exc)[:200]})

    if len(clips) < min_clips:
        logger.info(
            "pd motion only prepared %s/%s commercial-safe clips (non-blocking)",
            len(clips),
            min_clips,
        )
    sidecar = out_dir / "pd_motion_clips.json"
    sidecar.write_text(
        json.dumps(
            {
                "count": len(clips),
                "clips": clips,
                "blocked": blocked,
                "min_clips": min_clips,
                "max_clips": max_clips,
                "soft": soft,
                "channel_policy": "historian_soft" if soft else "napstorian_dense",
                "commercial_use_only": True,
                "youtube_monetization_safe_required": True,
                "allowed_licenses": [
                    "Public Domain",
                    "PD-Art",
                    "CC0",
                    "US government",
                    "Met Open Access",
                ],
                "rejected_licenses": [
                    "CC BY-NC",
                    "CC BY-ND",
                    "CC BY-NC-SA",
                    "fair use",
                    "unstated",
                    "Pexels/Mixkit-as-PD",
                ],
                "notes": (
                    "Napstorian: 10–15 commercial-safe PD motion inserts "
                    "(Wikimedia/Met still→motion). Historian: 4–6 soft/slow "
                    "Ken-Burns-compatible pans only (History Calling calm — "
                    "no action montages). Drop-ins need *.license.json with "
                    "commercial_ok=true. Non-blocking if fewer pass the license gate."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return clips[:max_clips]


def _stamp_visual_manifest_pd_motion(
    vm_path: Path,
    applied: list[dict[str, Any]],
    *,
    clear_other_motion: bool = True,
) -> None:
    """Write PD motion video_path stamps onto a visual manifest."""
    if not vm_path.is_file() or not applied:
        return
    try:
        vm = json.loads(vm_path.read_text(encoding="utf-8"))
        by_idx = {int(a["scene_index"]): a for a in applied}
        for sc in vm.get("scenes") or []:
            if not isinstance(sc, dict):
                continue
            try:
                idx = int(sc.get("index"))
            except (TypeError, ValueError):
                continue
            if idx in by_idx:
                sc["video_path"] = by_idx[idx]["scene_clip_path"]
                sc["is_video_scene"] = True
                sc["pd_motion"] = True
                sc["pd_motion_id"] = by_idx[idx].get("id")
                sc["backend"] = sc.get("backend") or "pd_motion"
            elif clear_other_motion and sc.get("pd_motion"):
                sc.pop("video_path", None)
                sc.pop("is_video_scene", None)
                sc.pop("pd_motion", None)
                sc.pop("pd_motion_id", None)
                if sc.get("backend") == "pd_motion":
                    sc.pop("backend", None)
        meta = vm.setdefault("meta", {})
        if isinstance(meta, dict):
            meta["pd_motion_clips"] = True
            meta["pd_motion_count"] = len(applied)
            meta["pd_motion_channel"] = "napstorian"
            meta["pd_motion_scene_indices"] = sorted(by_idx)
            meta["commercial_use_only"] = True
            meta["youtube_monetization_safe_required"] = True
        vm_path.write_text(
            json.dumps(vm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("visual_manifest PD motion stamp failed (%s): %s", vm_path, exc)


def apply_pd_motion_clips_to_job(
    job_dir: Path,
    *,
    channel: str = "napstorian",
    min_clips: int | None = None,
    max_clips: int | None = None,
    duration_s: float = 8.0,
    width: int = 1280,
    height: int = 720,
    force: bool = False,
    max_scene_index: int | None = None,
    visual_manifest_names: list[str] | None = None,
    soft: bool | None = None,
) -> dict[str, Any]:
    """Attach PD motion clips to scenes (sets scene video_path).

    Napstorian: **10–15** denser inserts. Historian: **4–6** soft/slow pans only.
    Non-blocking. Unknown channels → skipped.

    ``max_scene_index`` keeps anchors inside a short production head (e.g. 68 for
    SOP ~10m). Also stamps ``visual_manifest_sop_10m.json`` when present.
    """
    from src.services.youtube_channel_auth import normalize_youtube_channel

    job_dir = Path(job_dir)
    ch = normalize_youtube_channel(channel)
    if ch not in {"napstorian", "napping_historian"}:
        return {
            "ok": False,
            "skipped": True,
            "reason": "unsupported_channel",
            "channel": ch,
            "pd_motion_count": 0,
        }

    historian = ch == "napping_historian"
    use_soft = bool(soft) if soft is not None else historian
    if min_clips is None:
        min_clips = (
            TARGET_MOTION_MIN_HISTORIAN if historian else TARGET_MOTION_MIN_NAPSTORIAN
        )
    if max_clips is None:
        max_clips = (
            TARGET_MOTION_MAX_HISTORIAN if historian else TARGET_MOTION_MAX_NAPSTORIAN
        )

    images = job_dir / "images"
    if not images.is_dir():
        return {"ok": False, "reason": "images_dir_missing", "pd_motion_count": 0}

    # Auto-detect SOP 10m window when that manifest exists and caller didn't pin.
    sop_vm = images / "visual_manifest_sop_10m.json"
    if max_scene_index is None and sop_vm.is_file():
        try:
            sop = json.loads(sop_vm.read_text(encoding="utf-8"))
            idxs = [
                int(s["index"])
                for s in (sop.get("scenes") or [])
                if isinstance(s, dict) and "index" in s
            ]
            if idxs:
                max_scene_index = max(idxs)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            max_scene_index = None

    motion_dir = images / "pd_motion"
    clips = fetch_or_build_pd_motion_clips(
        motion_dir,
        job_dir=job_dir,
        curated=HISTORIAN_PD_MOTION_CURATED if use_soft else None,
        min_clips=int(min_clips),
        max_clips=int(max_clips),
        duration_s=duration_s,
        width=width,
        height=height,
        soft=use_soft,
    )
    if not clips:
        return {"ok": False, "reason": "no_motion_clips", "pd_motion_count": 0}

    script = None
    sp = job_dir / "script" / "script.json"
    if sp.is_file():
        try:
            script = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            script = None
    anchors = _chapter_anchor_indices(
        script, len(clips), max_index=max_scene_index, min_index=3
    )
    # Prefer mid/late anchors so cold-open still uses Flux/KB energy
    anchors = [a for a in anchors if a >= 3] or anchors

    applied: list[dict[str, Any]] = []
    for clip, scene_idx in zip(clips, anchors):
        scene_clip = images / f"scene_{int(scene_idx):03d}_clip.mp4"
        src = Path(clip["path"])
        if not src.is_file():
            continue
        if force or not scene_clip.is_file() or scene_clip.stat().st_size < 2000:
            shutil.copy2(src, scene_clip)
        applied.append(
            {
                **clip,
                "scene_index": int(scene_idx),
                "scene_clip_path": str(scene_clip.resolve()),
            }
        )

    # Drop orphan scene_*_clip.mp4 so EditModule does not exceed the insert budget
    keep = {int(a["scene_index"]) for a in applied}
    for orphan in images.glob("scene_*_clip.mp4"):
        try:
            idx = int(orphan.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if idx not in keep:
            try:
                orphan.unlink()
            except OSError:
                pass

    names = visual_manifest_names or [
        "visual_manifest.json",
        "visual_manifest_sop_10m.json",
    ]
    for name in names:
        _stamp_visual_manifest_pd_motion(images / name, applied)

    report = {
        "ok": True,
        "pd_motion_clips": True,
        "pd_motion_count": len(applied),
        "channel": ch,
        "soft": use_soft,
        "clips": applied,
        "motion_dir": str(motion_dir.resolve()),
        "max_scene_index": max_scene_index,
        "commercial_use_only": True,
        "youtube_monetization_safe_required": True,
    }
    (motion_dir / "apply_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Merge into job pd_clippings stamp when present
    stamp = job_dir / "pd_clippings.json"
    base: dict[str, Any] = {}
    if stamp.is_file():
        try:
            base = json.loads(stamp.read_text(encoding="utf-8"))
            if not isinstance(base, dict):
                base = {}
        except (OSError, json.JSONDecodeError):
            base = {}
    base.update(
        {
            "pd_motion_clips": True,
            "pd_motion_count": len(applied),
            "pd_motion": applied,
            "pd_motion_soft": use_soft,
            "pd_motion_channel": ch,
            "commercial_use_only": True,
            "youtube_monetization_safe_required": True,
        }
    )
    if "pd_clippings" not in base:
        base["pd_clippings"] = True
    stamp.write_text(json.dumps(base, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def maybe_apply_pd_motion_for_compose(
    job_dir: Path,
    *,
    channel: str | None = None,
) -> dict[str, Any]:
    """Gate PD motion by settings + channel. Safe no-op when disabled."""
    from src.services.settings import get_settings
    from src.services.youtube_channel_auth import normalize_youtube_channel

    s = get_settings()
    if not bool(getattr(s, "compose_pd_motion_clips_enabled", True)):
        return {"ok": False, "skipped": True, "reason": "compose_pd_motion_clips_disabled"}
    ch = normalize_youtube_channel(
        channel
        or getattr(s, "compose_channel_default", None)
        or "napstorian"
    )
    if ch not in {"napstorian", "napping_historian"}:
        return {"ok": False, "skipped": True, "reason": "unsupported_channel", "channel": ch}

    historian = ch == "napping_historian"
    if historian and not bool(
        getattr(s, "compose_pd_motion_historian_enabled", True)
    ):
        return {
            "ok": False,
            "skipped": True,
            "reason": "historian_pd_motion_disabled",
            "channel": ch,
        }

    if historian:
        min_clips = int(
            getattr(s, "compose_pd_motion_historian_min_clips", TARGET_MOTION_MIN_HISTORIAN)
            or TARGET_MOTION_MIN_HISTORIAN
        )
        max_clips = int(
            getattr(s, "compose_pd_motion_historian_max_clips", TARGET_MOTION_MAX_HISTORIAN)
            or TARGET_MOTION_MAX_HISTORIAN
        )
    else:
        min_clips = int(
            getattr(s, "compose_pd_motion_min_clips", TARGET_MOTION_MIN_NAPSTORIAN)
            or TARGET_MOTION_MIN_NAPSTORIAN
        )
        max_clips = int(
            getattr(s, "compose_pd_motion_max_clips", TARGET_MOTION_MAX_NAPSTORIAN)
            or TARGET_MOTION_MAX_NAPSTORIAN
        )

    return apply_pd_motion_clips_to_job(
        job_dir,
        channel=ch,
        min_clips=min_clips,
        max_clips=max_clips,
        duration_s=float(getattr(s, "compose_pd_motion_duration_s", 8.0) or 8.0),
        width=int(getattr(s, "image_width", 1280) or 1280),
        height=int(getattr(s, "image_height", 720) or 720),
        soft=historian,
    )
