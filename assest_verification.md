# Cursor Build Prompt: Asset Verification Agent

Paste this into Cursor to implement:

---

Build a Python module called `asset_verifier.py` for a content pipeline. Purpose: verify public-domain/licensed visual and footage assets BEFORE they enter the media library, to avoid YouTube copyright/Content ID issues.

## Requirements

### 1. Input
A function `verify_asset(source_url: str, asset_type: str, metadata: dict) -> dict` where:
- `source_url`: URL the asset was pulled from
- `asset_type`: "image" | "footage" | "audio"
- `metadata`: dict with any known fields (title, license_tag, source_page_url, digitization_note)

### 2. Approved domain whitelist (config file `approved_domains.json`)
```json
[
  "commons.wikimedia.org",
  "loc.gov",
  "nationalarchives.gov.uk",
  "bl.uk",
  "rijksmuseum.nl",
  "metmuseum.org",
  "nationalgallery.org.uk",
  "europeana.eu",
  "si.edu",
  "archive.org",
  "britishpathe.com",
  "pexels.com",
  "pixabay.com",
  "unsplash.com"
]
```

### 3. Verification logic — checklist to run per asset
1. **Domain check**: parse `source_url`, confirm domain is in `approved_domains.json`. Fail if not.
2. **License tag check**: `metadata['license_tag']` must be non-empty and match one of: "public domain", "cc0", "cc-by", "open license". Fail if missing or doesn't match.
3. **Digitization/restoration flag**: if `metadata['digitization_note']` mentions "restored", "remastered", or "digitized by [named studio]", mark as `manual_review` (not auto-fail) — this is the "old film transfer might have separate copyright" trap.
4. **Audio strip check** (only if `asset_type == "footage"`): flag `requires_audio_strip: true` in output — downstream ffmpeg step must mute/replace embedded audio before use.
5. **Logging**: every check writes a record to `asset_log.jsonl` (append-only) with: timestamp, source_url, asset_type, each check's pass/fail, final status, and a saved screenshot/license-proof path if available.

### 4. Output
Return dict:
```python
{
  "status": "approved" | "rejected" | "manual_review",
  "checks": {
    "domain_whitelisted": bool,
    "license_tag_present": bool,
    "digitization_flag": bool,
    "requires_audio_strip": bool
  },
  "reason": str  # human-readable explanation of the status
}
```

### 5. Integration point
This runs as a **pre-ingestion gate** — call `verify_asset()` when an asset is first pulled into the media library (before it's added to any video/newsletter/ebook asset folder), NOT at final video export. Wire it in as a step before assets are saved to the pipeline's asset storage directory.

### 6. Manual review queue
Any asset with status `manual_review` or `rejected` should be written to a separate `needs_review.jsonl` file instead of the main approved asset library, so a human can glance at it later without blocking the pipeline.

### 7. Tests
Write basic unit tests covering:
- Approved domain + valid license tag → "approved"
- Non-whitelisted domain → "rejected"
- Whitelisted domain, missing license tag → "rejected"
- Whitelisted domain, valid license, but "restored" in digitization_note → "manual_review"
- Footage asset → confirm `requires_audio_strip: True` always set

---

Keep it dependency-light (standard library + `urllib.parse` for domain checks). No external API calls needed for this module — it's a rules-based gate, not an AI classifier.
