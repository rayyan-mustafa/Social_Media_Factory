# src/agents/leadgen/__init__.py
"""Lead‑Gen package entry point.
Provides a `run_pipeline` function that coordinates the stages:
scrape → score → enrich → draft → track → report.
All modules are imported locally to keep the package self‑contained.
"""

import yaml
from .run import hunt_leads, write_run


def run_pipeline(config_path: str = "config/leadgen.yaml"):
    """Run the full lead‑generation pipeline.

    The configuration file should contain at least the following keys:
    - region_id: identifier for the region configuration
    - vertical_id: identifier for the vertical configuration
    - city: city name used for the place search
    - limit: maximum number of leads to keep (default 25)
    - use_google_places: bool – if ``False`` forces Nominatim only
    - sync_sheets: bool – whether to write results to Google Sheets
    """
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    region_id = cfg.get("region_id", "default_region")
    vertical_id = cfg.get("vertical_id", "default_vertical")
    city = cfg.get("city", "Austin")
    limit = cfg.get("limit", 25)
    use_google = cfg.get("use_google_places", True)
    sync_sheets = cfg.get("sync_sheets", False)

    # Perform the hunt – allow Nominatim fallback when Google Places is disabled
    hunt = hunt_leads(
        region_id=region_id,
        vertical_id=vertical_id,
        city=city,
        limit=limit,
        allow_nominatim_fallback=not use_google,
    )

    # Persist results, optionally syncing to Google Sheets
    write_run(
        hunt,
        write_sheet=sync_sheets,
    )

    print("Lead‑gen pipeline completed.")
