# src/agents/leadgen/scorer.py
"""Scorer module for Lead‑Gen pipeline.
Applies rule‑based tags and computes a numeric score.
"""
from typing import List, Dict


def tag_lead(lead: Dict) -> List[str]:
    """Return list of tags for a lead based on its data.
    - no_site: missing website_url
    - social_only: only social media contact (not applicable here)
    - poor_site: website exists but appears low‑quality (simple heuristic)
    - skip: none of the above and lead is already strong.
    """
    tags = []
    website = lead.get("website_url")
    if not website:
        tags.append("no_site")
    else:
        # crude heuristic: if website length is short or contains "example" assume poor
        if len(website) < 10 or "example" in website.lower():
            tags.append("poor_site")
    # placeholder for social_only detection (could use phone prefix etc.)
    # For now, not implemented.
    if not tags:
        tags.append("skip")
    return tags


def compute_score(lead: Dict) -> float:
    """Compute a score from review count and rating.
    Simple weighted sum: rating * 2 + log(review_count+1).
    """
    rating = lead.get("rating") or 0
    review_count = lead.get("review_count") or 0
    try:
        import math
        score = rating * 2 + math.log(review_count + 1)
    except Exception:
        score = rating * 2
    return score


def score_leads(leads: List[Dict]) -> List[Dict]:
    """Apply tagging and scoring to each lead, add fields, and sort.
    Returns a new list sorted descending by score.
    """
    for lead in leads:
        lead["tags"] = tag_lead(lead)
        lead["score"] = compute_score(lead)
    # sort descending by score
    sorted_leads = sorted(leads, key=lambda x: x["score"], reverse=True)
    return sorted_leads
