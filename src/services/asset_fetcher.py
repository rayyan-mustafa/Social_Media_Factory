"""Multi-source PD/OA asset search + download + verify staging.

Sources: Wikimedia Commons, Met Open Access, LOC, Internet Archive (license-gated).
Routes approved → save_dir/approved/; rejects → save_dir/needs_review/ (dead-letter).

Farm still policy (napstorian + napping_historian) — RMagine ladder:
  1. Per-scene Wiki + Met (see ``rmagine_scene_fetch``) — NO product hero cap.
  2. vision_judge each approved candidate.
  3. PASS → place as that scene's still.
  4. REJECT / no candidate → leave slot empty; Flux fills (never reuse weak PD).

``KEYWORD_STAGE_MAX`` / ``VISION_PASS_HEROES_MAX`` are soft defaults for single-keyword
legacy helpers only — they must NOT truncate per-scene archival placement.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from src.services.asset_verifier import verify_asset, write_job_attribution
from src.services.pd_clippings import license_is_commercial_ok

log = logging.getLogger(__name__)

# Wikimedia requires a descriptive UA with contact; bare tokens get 403.
USER_AGENT = (
    "NewYtAutomation/1.0 (VPS archival stills; contact=ops@localhost; "
    "+https://commons.wikimedia.org/wiki/Commons:API)"
)
# Soft defaults for legacy single-keyword stage helpers (not per-scene product caps).
KEYWORD_STAGE_MAX = 50
VISION_PASS_HEROES_MAX = 10_000  # effectively uncapped for apply/collect helpers
_API_PAGE_CAP = 50  # MediaWiki srlimit / Met objectID slice ceiling
# OOM guard: never pull Wiki originals / PDFs / TIFFs into RAM on 8GB VPS.
_ALLOWED_IMAGE_MIMES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})
_ALLOWED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_BLOCKED_SUFFIXES = frozenset(
    {".pdf", ".djvu", ".svg", ".tif", ".tiff", ".gif", ".webm", ".mp4", ".ogv"}
)
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024  # hard abort while streaming
_WIKI_THUMB_WIDTH = 1280
_DIGITIZATION_RE = re.compile(
    r"\b(restored|remastered|digitized by|colourised|colorized by)\b",
    re.IGNORECASE,
)


def _mime_allowed(mime: str | None) -> bool:
    m = (mime or "").split(";", 1)[0].strip().lower()
    return m in _ALLOWED_IMAGE_MIMES


def _url_suffix_ok(url: str) -> bool:
    suf = Path(url.split("?")[0]).suffix.lower()
    if not suf:
        return True  # Met CDNs sometimes omit extension; sniff after download
    if suf in _BLOCKED_SUFFIXES:
        return False
    if suf in _ALLOWED_IMAGE_SUFFIXES:
        return True
    return False


def _delay(sec: float = 0.6) -> None:
    time.sleep(max(0.0, sec))


def _client(timeout: float = 45.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def _met_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    retries: int = 3,
) -> httpx.Response:
    """GET Met Collection API with UA + backoff on Incapsula 403.

    Met sits behind Imperva; bursts / missing UA → HTML 403 iframe. Pace and
    retry instead of aborting the whole search.
    """
    last: httpx.Response | None = None
    for attempt in range(max(1, retries)):
        r = client.get(url, params=params)
        last = r
        if r.status_code != 403:
            return r
        wait = 1.5 * (attempt + 1)
        log.warning(
            "met HTTP 403 (Incapsula) attempt %s/%s — sleep %.1fs",
            attempt + 1,
            retries,
            wait,
        )
        _delay(wait)
    assert last is not None
    return last


def _digitization_note(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    m = _DIGITIZATION_RE.search(blob)
    return m.group(0) if m else ""


def _norm_license_from_wiki(ext: dict[str, Any]) -> str:
    short = ""
    for key in ("LicenseShortName", "UsageTerms", "License"):
        node = ext.get(key) or {}
        if isinstance(node, dict):
            short = str(node.get("value") or "").strip()
        else:
            short = str(node).strip()
        if short:
            break
    return short or "unknown"


def fetch_wikimedia(keyword: str, *, max_results: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with _client() as client:
            r = client.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": keyword,
                    "srnamespace": 6,
                    "srlimit": max(1, min(max_results, _API_PAGE_CAP)),
                    "format": "json",
                },
            )
            r.raise_for_status()
            hits = ((r.json().get("query") or {}).get("search")) or []
            _delay()
            for hit in hits:
                title = hit.get("title") or ""
                if not title:
                    continue
                info = client.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query",
                        "titles": title,
                        "prop": "imageinfo",
                        "iiprop": "url|size|mime|thumbmime|extmetadata",
                        "iiurlwidth": _WIKI_THUMB_WIDTH,
                        "format": "json",
                    },
                )
                info.raise_for_status()
                pages = ((info.json().get("query") or {}).get("pages")) or {}
                for page in pages.values():
                    iis = page.get("imageinfo") or []
                    if not iis:
                        continue
                    ii = iis[0]
                    mime = str(ii.get("mime") or ii.get("thumbmime") or "").lower()
                    if mime and not _mime_allowed(mime):
                        log.info(
                            "wikimedia skip non-raster %s mime=%s", title, mime
                        )
                        continue
                    # Prefer resized thumb — full originals routinely exceed 50–100MB.
                    thumb = str(ii.get("thumburl") or "").strip()
                    full = str(ii.get("url") or "").strip()
                    url = thumb or full
                    if not url or not _url_suffix_ok(url):
                        log.info("wikimedia skip bad url/suffix %s", title)
                        continue
                    size = ii.get("size")
                    try:
                        size_i = int(size) if size is not None else None
                    except (TypeError, ValueError):
                        size_i = None
                    # If we only have the full URL and it's already huge, skip.
                    if not thumb and size_i is not None and size_i > _MAX_DOWNLOAD_BYTES:
                        log.info(
                            "wikimedia skip oversized original %s size=%s",
                            title,
                            size_i,
                        )
                        continue
                    ext = ii.get("extmetadata") or {}
                    lic = _norm_license_from_wiki(ext)
                    desc = ""
                    if isinstance(ext.get("ImageDescription"), dict):
                        desc = str(ext["ImageDescription"].get("value") or "")
                    out.append(
                        {
                            "source_url": url,
                            "asset_type": "image",
                            "title": title,
                            "metadata": {
                                "license_tag": lic,
                                "source_page_url": (
                                    f"https://commons.wikimedia.org/wiki/{quote(title.replace(' ', '_'))}"
                                ),
                                "digitization_note": _digitization_note(desc, lic),
                                "raw_source": "wikimedia_commons",
                                "mime": mime or None,
                                "original_size": size_i,
                                "used_thumb": bool(thumb),
                            },
                        }
                    )
                _delay()
    except Exception as exc:  # noqa: BLE001
        log.warning("wikimedia fetch failed: %s", exc)
    return out[:max_results]


def fetch_met(keyword: str, *, max_results: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with _client() as client:
            r = _met_get(
                client,
                "https://collectionapi.metmuseum.org/public/collection/v1/search",
                params={"q": keyword, "hasImages": "true"},
            )
            if r.status_code == 403:
                log.warning("met search still 403 after retries for %r", keyword[:80])
                return out
            r.raise_for_status()
            ids = (r.json().get("objectIDs") or [])[
                : max(1, min(max_results, _API_PAGE_CAP))
            ]
            _delay(0.8)
            for oid in ids:
                try:
                    o = _met_get(
                        client,
                        f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}",
                    )
                    if o.status_code == 403:
                        log.warning("met object %s 403 — skip", oid)
                        _delay(1.0)
                        continue
                    o.raise_for_status()
                    obj = o.json()
                except Exception:  # noqa: BLE001
                    continue
                if not obj.get("isPublicDomain"):
                    continue
                # Prefer small derivative — primaryImage can be multi‑100MB scans.
                small = str(obj.get("primaryImageSmall") or "").strip()
                full = str(obj.get("primaryImage") or "").strip()
                img = small or full
                if not img:
                    continue
                if not _url_suffix_ok(img):
                    continue
                title = obj.get("title") or f"Met {oid}"
                out.append(
                    {
                        "source_url": img,
                        "asset_type": "image",
                        "title": title,
                        "metadata": {
                            "license_tag": "Met Open Access",
                            "source_page_url": obj.get("objectURL")
                            or f"https://www.metmuseum.org/art/collection/search/{oid}",
                            "digitization_note": _digitization_note(
                                str(obj.get("creditLine") or ""),
                                str(obj.get("repository") or ""),
                            ),
                            "raw_source": "met_open_access",
                            "met_object_id": oid,
                            "used_thumb": bool(small),
                        },
                    }
                )
                _delay(0.7)
    except Exception as exc:  # noqa: BLE001
        log.warning("met fetch failed: %s", exc)
    return out[:max_results]


def fetch_loc(keyword: str, *, max_results: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with _client() as client:
            r = client.get(
                "https://www.loc.gov/search/",
                params={"q": keyword, "fo": "json", "c": max(1, min(max_results, 20))},
            )
            r.raise_for_status()
            results = r.json().get("results") or []
            for item in results:
                rights = " ".join(
                    str(x) for x in (item.get("rights") or []) if x
                ).lower()
                if rights and not (
                    "public domain" in rights
                    or "no known" in rights
                    or "cc0" in rights
                ):
                    # Skip unclear / restricted
                    if not license_is_commercial_ok(rights):
                        continue
                img = ""
                for key in ("image_url", "url"):
                    val = item.get(key)
                    if isinstance(val, list) and val:
                        img = str(val[0])
                        break
                    if isinstance(val, str) and val.startswith("http"):
                        img = val
                        break
                if not img:
                    continue
                title = item.get("title") or "LOC item"
                if isinstance(title, list):
                    title = title[0] if title else "LOC item"
                out.append(
                    {
                        "source_url": img,
                        "asset_type": "image",
                        "title": str(title),
                        "metadata": {
                            "license_tag": rights or "public domain",
                            "source_page_url": item.get("id") or item.get("url") or img,
                            "digitization_note": _digitization_note(
                                str(item.get("description") or "")
                            ),
                            "raw_source": "loc",
                        },
                    }
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("loc fetch failed: %s", exc)
    return out[:max_results]


def fetch_archive(
    keyword: str, *, asset_type: str = "footage", max_results: int = 5
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    mediatype = "movies" if asset_type == "footage" else ("audio" if asset_type == "audio" else "image")
    try:
        with _client() as client:
            r = client.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": keyword,
                    "fl[]": ["identifier", "licenseurl", "mediatype", "title"],
                    "rows": max(1, min(max_results, 10)),
                    "page": 1,
                    "output": "json",
                },
            )
            r.raise_for_status()
            docs = ((r.json().get("response") or {}).get("docs")) or []
            for doc in docs:
                lic_url = str(doc.get("licenseurl") or "").lower()
                if lic_url and not (
                    "publicdomain" in lic_url.replace("-", "")
                    or "cc0" in lic_url
                    or "/publicdomain/" in lic_url
                ):
                    if not license_is_commercial_ok(lic_url):
                        continue
                ident = doc.get("identifier")
                if not ident:
                    continue
                page = f"https://archive.org/details/{ident}"
                out.append(
                    {
                        "source_url": page,
                        "asset_type": asset_type if asset_type in {"footage", "audio", "image"} else "footage",
                        "title": doc.get("title") or ident,
                        "metadata": {
                            "license_tag": "public domain" if "publicdomain" in lic_url.replace("-", "") else (lic_url or "unknown"),
                            "source_page_url": page,
                            "digitization_note": "",
                            "raw_source": "internet_archive",
                            "identifier": ident,
                        },
                    }
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("archive fetch failed: %s", exc)
    return out[:max_results]


def fetch_assets(
    keyword: str,
    asset_type: str = "image",
    max_results: int = 10,
    *,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search approved sources; one source failing does not abort others."""
    asset_type = (asset_type or "image").lower()
    want = sources or ["wikimedia", "met", "loc"]
    if asset_type in {"footage", "audio"} and "archive" not in want:
        want = [*want, "archive"]
    results: list[dict[str, Any]] = []
    per = max(1, max_results)
    if "wikimedia" in want and asset_type == "image":
        results.extend(fetch_wikimedia(keyword, max_results=per))
    if "met" in want and asset_type == "image":
        results.extend(fetch_met(keyword, max_results=per))
    if "loc" in want and asset_type == "image":
        results.extend(fetch_loc(keyword, max_results=per))
    if "archive" in want:
        results.extend(fetch_archive(keyword, asset_type=asset_type, max_results=min(5, per)))
    # Dedupe by source_url
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in results:
        u = row.get("source_url") or ""
        if not u or u in seen:
            continue
        seen.add(u)
        row["asset_type"] = asset_type if asset_type != "image" else row.get("asset_type", "image")
        deduped.append(row)
        if len(deduped) >= max_results * 2:
            break
    return deduped[: max_results * 2]


def download_asset(asset: dict[str, Any], save_dir: str | Path) -> str:
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    url = str(asset.get("source_url") or "")
    if not url.startswith("http"):
        raise ValueError(f"bad source_url: {url!r}")
    # Archive detail pages are not direct files — skip binary download
    if "archive.org/details/" in url:
        meta_path = save_dir / "archive_meta_only.json"
        meta_path.write_text(json.dumps(asset, indent=2), encoding="utf-8")
        return str(meta_path)

    if not _url_suffix_ok(url):
        raise ValueError(f"blocked non-raster asset url: {url[:120]}")

    meta = dict(asset.get("metadata") or {})
    mime_hint = str(meta.get("mime") or "").lower()
    if mime_hint and not _mime_allowed(mime_hint):
        raise ValueError(f"blocked mime {mime_hint}: {url[:120]}")

    suffix = Path(url.split("?")[0]).suffix.lower() or ".jpg"
    if len(suffix) > 8 or suffix not in _ALLOWED_IMAGE_SUFFIXES:
        suffix = ".jpg"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(asset.get("title") or "asset"))[:60]
    dest = save_dir / f"{safe}{suffix}"
    n = 0
    while dest.exists():
        n += 1
        dest = save_dir / f"{safe}_{n}{suffix}"

    # Stream to disk with hard byte budget — never r.content on Wiki originals.
    written = 0
    try:
        with _client(timeout=120.0) as client:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                cl = r.headers.get("Content-Length")
                try:
                    cl_i = int(cl) if cl else 0
                except ValueError:
                    cl_i = 0
                if cl_i > _MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Content-Length {cl_i} exceeds {_MAX_DOWNLOAD_BYTES} byte cap"
                    )
                ctype = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype and ctype.startswith("image/") and not _mime_allowed(ctype):
                    raise ValueError(f"blocked Content-Type {ctype}")
                with dest.open("wb") as fh:
                    for chunk in r.iter_bytes(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > _MAX_DOWNLOAD_BYTES:
                            raise ValueError(
                                f"download exceeded {_MAX_DOWNLOAD_BYTES} byte cap"
                            )
                        fh.write(chunk)
    except Exception:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    if written < 64:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"download too small ({written}B): {url[:120]}")

    side = dest.with_suffix(dest.suffix + ".json")
    if dest.suffix == ".json":
        side = Path(str(dest) + ".meta.json")
    side.write_text(
        json.dumps(
            {
                **asset,
                "local_path": str(dest),
                "downloaded": True,
                "bytes": written,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(dest)


def search_and_stage(
    keyword: str,
    asset_type: str,
    save_dir: str | Path,
    *,
    max_results: int = KEYWORD_STAGE_MAX,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch → download → verify → route approved / needs_review."""
    save_dir = Path(save_dir)
    approved_dir = save_dir / "approved"
    review_dir = save_dir / "needs_review"
    approved_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)

    candidates = fetch_assets(keyword, asset_type, max_results=max_results, sources=sources)
    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for asset in candidates:
        try:
            path = download_asset(asset, review_dir)  # stage temp then move
        except Exception as exc:  # noqa: BLE001
            rejected.append({**asset, "error": str(exc)[:200]})
            continue
        verdict = verify_asset(
            str(asset.get("source_url") or ""),
            str(asset.get("asset_type") or asset_type),
            dict(asset.get("metadata") or {}),
        )
        local = Path(path)
        meta = {
            **asset,
            "local_path": str(local),
            "verify": verdict,
        }
        if verdict.get("status") == "approved":
            dest = approved_dir / local.name
            if local.resolve() != dest.resolve():
                # Capture sidecar path before move (download_asset writes foo.jpg.json).
                side_src = local.with_suffix(local.suffix + ".json")
                if not side_src.is_file():
                    side_src = Path(str(local) + ".meta.json")
                # Move on disk — never read_bytes() (OOM on multi‑MB stills).
                try:
                    local.replace(dest)
                except OSError:
                    shutil.move(str(local), str(dest))
                side_dst = dest.with_suffix(dest.suffix + ".json")
                meta["local_path"] = str(dest)
                side_dst.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                try:
                    if side_src.is_file() and side_src.resolve() != side_dst.resolve():
                        side_src.unlink(missing_ok=True)
                except OSError:
                    pass
            meta["local_path"] = str(dest)
            approved.append(meta)
        else:
            side = local.with_suffix(local.suffix + ".json")
            side.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            rejected.append(meta)

    attribution = write_job_attribution(
        approved_dir,
        [
            {
                "title": a.get("title"),
                "license_tag": (a.get("metadata") or {}).get("license_tag"),
                "source_page_url": (a.get("metadata") or {}).get("source_page_url"),
                "source_url": a.get("source_url"),
            }
            for a in approved
        ],
    )

    summary = {
        "keyword": keyword,
        "asset_type": asset_type,
        "n_candidates": len(candidates),
        "n_approved": len(approved),
        "n_rejected": len(rejected),
        "approved": approved,
        "rejected": rejected,
        "attribution_path": str(attribution) if attribution else None,
    }
    (save_dir / "stage_summary.json").write_text(
        json.dumps(
            {k: v for k, v in summary.items() if k not in {"approved", "rejected"}}
            | {
                "approved_paths": [a.get("local_path") for a in approved],
                "rejected_n": len(rejected),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return summary


def collect_vision_pass_assets(
    scene_text: str,
    approved_assets: list[dict[str, Any]],
    *,
    historical_figure: str | None = None,
    max_pass: int = VISION_PASS_HEROES_MAX,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Judge approved assets; return vision-PASS locals only.

    FAIL / API-miss never enter ``passed`` and must not be composed — callers
    leave those scene slots empty so Flux fills (``ai_fill_gaps`` /
    ``flux_on_vision_reject``). Digitization/license rejects are already
    filtered before this stage.
    """
    from src.services.vision_judge import validate_image_with_vision_llm

    results: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    # No product truncator: judge all approved unless caller sets a soft max_pass.
    soft_cap = int(max_pass) if max_pass is not None else VISION_PASS_HEROES_MAX
    if soft_cap <= 0:
        soft_cap = VISION_PASS_HEROES_MAX
    for a in approved_assets:
        if len(passed) >= soft_cap:
            break
        ref = str(a.get("local_path") or a.get("path") or a.get("source_url") or "")
        if not ref:
            continue
        v = validate_image_with_vision_llm(
            ref, scene_text, historical_figure, settings=settings
        )
        row = {**a, "vision": v}
        results.append(row)
        if v.get("status") == "PASS":
            passed.append(row)
        else:
            rejected.append(row)
    return {
        "passed": passed,
        "rejected": rejected,
        "judged": results,
        "n_pass": len(passed),
        "n_rejected": len(rejected),
        "n_judged": len(results),
        "ai_fill_gaps": True,
        "flux_on_vision_reject": True,
        "reuse_weak_pd": False,
    }


def apply_vision_pass_heroes_to_job(
    job_dir: Path | str,
    pass_assets: list[dict[str, Any]],
    *,
    script: dict[str, Any] | None = None,
    width: int = 1280,
    height: int = 720,
    max_heroes: int = VISION_PASS_HEROES_MAX,
) -> dict[str, Any]:
    """Place vision-PASS stills as scene heroes (Flux fills remaining gaps).

    Only PASS assets may be passed here — vision REJECT must never become a
    scene still. Prefer ``scene_index`` on each asset (per-scene RMagine path);
    otherwise fall back to chapter anchors. **No product hero cap** — ``max_heroes``
    is a soft safety only (default uncapped). Call before
    ``synthesize_script(resume=True)`` so Flux skips PASS indices.
    """
    from src.services.pd_clippings import _chapter_anchor_indices, fit_still

    job_dir = Path(job_dir)
    images = job_dir / "images"
    images.mkdir(parents=True, exist_ok=True)
    hero_dir = images / "fetched_heroes"
    hero_dir.mkdir(parents=True, exist_ok=True)

    soft_cap = int(max_heroes) if max_heroes is not None else VISION_PASS_HEROES_MAX
    if soft_cap <= 0:
        soft_cap = VISION_PASS_HEROES_MAX

    usable: list[dict[str, Any]] = []
    for a in pass_assets:
        if len(usable) >= soft_cap:
            break
        src = Path(str(a.get("local_path") or a.get("path") or ""))
        if not src.is_file():
            continue
        usable.append(a)

    if not usable:
        return {"ok": False, "fetched_hero_count": 0, "reason": "no_pass_locals"}

    if script is None:
        sp = job_dir / "script" / "script.json"
        if sp.is_file():
            try:
                script = json.loads(sp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                script = None

    # Prefer explicit scene_index from per-scene fetch; else chapter anchors.
    if all(a.get("scene_index") is not None for a in usable):
        pairs = [(a, int(a["scene_index"])) for a in usable]
    else:
        anchors = _chapter_anchor_indices(script, len(usable))
        pairs = list(zip(usable, anchors))

    applied: list[dict[str, Any]] = []
    for asset, scene_idx in pairs:
        src = Path(str(asset.get("local_path") or asset.get("path")))
        fitted = hero_dir / f"hero_{int(scene_idx):03d}.jpg"
        try:
            fit_still(src, fitted, width=width, height=height)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetched hero fit failed %s: %s", src, exc)
            continue
        dest = images / f"scene_{int(scene_idx):03d}.jpg"
        # Backup Flux/AI original when replacing an existing still
        if dest.is_file():
            backup = images / "flux_backup"
            backup.mkdir(parents=True, exist_ok=True)
            bak = backup / dest.name
            if not bak.exists():
                try:
                    shutil.copyfile(dest, bak)
                except OSError:
                    pass
        try:
            shutil.copyfile(fitted, dest)
        except OSError as exc:
            log.warning("fetched hero write failed %s: %s", dest, exc)
            continue
        applied.append(
            {
                "scene_index": int(scene_idx),
                "local_path": str(fitted.resolve()),
                "replaced": str(dest.resolve()),
                "title": asset.get("title"),
                "source_url": asset.get("source_url"),
                "license_tag": (asset.get("metadata") or {}).get("license_tag"),
                "vision_confidence": (asset.get("vision") or {}).get("confidence"),
                "backend": "asset_fetcher_vision",
            }
        )

    # Stamp visual_manifest when present (post-Flux path); pre-Flux is fine without it.
    vm_path = images / "visual_manifest.json"
    if vm_path.is_file() and applied:
        try:
            vm = json.loads(vm_path.read_text(encoding="utf-8"))
            meta = vm.setdefault("meta", {})
            if isinstance(meta, dict):
                meta["fetched_heroes"] = True
                meta["fetched_hero_count"] = len(applied)
                meta["fetched_heroes_list"] = applied
            by_idx = {int(a["scene_index"]): a for a in applied}
            for sc in vm.get("scenes") or []:
                if not isinstance(sc, dict):
                    continue
                try:
                    idx = int(sc.get("index"))
                except (TypeError, ValueError):
                    continue
                if idx in by_idx:
                    sc["fetched_hero"] = True
                    sc["backend"] = "asset_fetcher_vision"
            vm_path.write_text(
                json.dumps(vm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            log.warning("visual_manifest fetched-hero stamp failed: %s", exc)

    report = {
        "ok": True,
        "fetched_heroes": True,
        "fetched_hero_count": len(applied),
        "heroes": applied,
        "hero_dir": str(hero_dir.resolve()),
        "additive": True,
        "ai_fill_gaps": True,
        "flux_on_vision_reject": True,
        "reuse_weak_pd": False,
        "allowed_sources": ["wikimedia", "met", "loc", "archive"],
        "commercial_use_only": True,
        "no_pexels": True,
    }
    (hero_dir / "apply_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    stamp = job_dir / "fetched_heroes.json"
    stamp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Soft-merge into pd_clippings stamp so SOP "heroes present" sees additive PD/OA.
    pd_stamp = job_dir / "pd_clippings.json"
    try:
        existing: dict[str, Any] = {}
        if pd_stamp.is_file():
            existing = json.loads(pd_stamp.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        existing["ok"] = True
        existing["pd_clippings"] = True
        existing["fetched_heroes"] = True
        existing["fetched_hero_count"] = len(applied)
        prev = list(existing.get("heroes") or [])
        existing["heroes"] = prev + applied
        existing["pd_clip_count"] = len(existing["heroes"])
        pd_stamp.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        log.warning("pd_clippings merge for fetched heroes failed: %s", exc)

    return report
