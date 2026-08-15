# Cursor Build Prompt: Asset Fetcher Agent (Search + Download)

Paste this into Cursor to implement:

---

Build a Python module called `asset_fetcher.py` that searches approved public-domain sources by keyword and downloads candidate assets, ready to be passed into `asset_verifier.py`.

## Requirements

### 1. Input
A function `fetch_assets(keyword: str, asset_type: str, max_results: int = 10) -> list[dict]` where:
- `keyword`: search term (e.g. "Anne Boleyn portrait", "Tudor coronation")
- `asset_type`: "image" | "footage" | "audio"
- `max_results`: how many candidates to pull per source

### 2. Source integrations (build one function per source, called from `fetch_assets`)

**Wikimedia Commons** (images primarily)
- Use the Wikimedia API: `https://commons.wikimedia.org/w/api.php`
- Query with `action=query&list=search&srsearch={keyword}&srnamespace=6&format=json` (namespace 6 = File)
- For each result, fetch file info (`action=query&titles={title}&prop=imageinfo&iiprop=url|extmetadata`) to get direct URL + license metadata (`LicenseShortName`, `Restrictions`)

**Internet Archive** (footage/audio primarily)
- Use the Archive.org API: `https://archive.org/advancedsearch.php?q={keyword}&fl[]=identifier&fl[]=licenseurl&fl[]=mediatype&output=json`
- For each identifier, fetch item metadata: `https://archive.org/metadata/{identifier}` to pull `licenseurl` and file list
- Only keep items where `mediatype` matches requested `asset_type` and `licenseurl` indicates public domain/CC

**Library of Congress** (images/footage)
- Use LOC's free API: `https://www.loc.gov/search/?q={keyword}&fo=json`
- Pull `rights` field per result — only keep items marked public domain / no known restrictions

**Museum Open Access (Met, Rijksmuseum, Smithsonian)** — images only
- Met: `https://collectionapi.metmuseum.org/public/collection/v1/search?q={keyword}` then fetch object by ID for `isPublicDomain` flag and `primaryImage` URL
- Rijksmuseum: requires free API key — note this as a TODO with a placeholder function if no key is configured
- Smithsonian Open Access: `https://api.si.edu/openaccess/api/v1.0/search?q={keyword}&api_key={key}` — note as TODO if no key configured

### 3. Output format
Each result normalized into:
```python
{
  "source_url": str,        # direct file/page URL
  "asset_type": "image" | "footage" | "audio",
  "title": str,
  "metadata": {
    "license_tag": str,          # normalized: "public domain", "cc0", "cc-by", "unknown"
    "source_page_url": str,
    "digitization_note": str,    # any restoration/remaster language found in description
    "raw_source": str            # which API this came from, for debugging
  }
}
```

### 4. Rate limiting & politeness
- Add a simple delay (0.5-1s) between requests per source to avoid hammering free public APIs
- Wrap each source call in try/except — one source failing shouldn't crash the whole fetch; log and continue to next source

### 5. Download step
Separate function `download_asset(asset: dict, save_dir: str) -> str` that:
- Downloads the file from `source_url` to `save_dir`
- Saves a companion `.json` file alongside it with the full metadata dict (this is the "license proof" record asset_verifier.py and your policy agent will reference later)
- Returns the local file path

### 6. Pipeline glue
Write a `search_and_stage(keyword, asset_type, save_dir)` function that:
1. Calls `fetch_assets()`
2. Downloads each candidate via `download_asset()`
3. Passes each through `verify_asset()` (import from `asset_verifier.py`)
4. Routes approved assets to `save_dir/approved/`, manual_review/rejected to `save_dir/needs_review/`

### 7. Tests
- Mock API responses for each source (don't hit real APIs in tests)
- Confirm normalized output format is consistent across all sources
- Confirm a result with restoration language in its description gets flagged in `digitization_note`

---

Use `requests` for HTTP calls (already lightweight, no heavy deps). Keep API keys (Rijksmuseum, Smithsonian) in a `.env` file, never hardcoded — load with `python-dotenv`. If a key is missing, that source should just skip silently with a log line, not crash the fetch.
