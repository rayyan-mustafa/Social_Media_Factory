"""RMagine query brain — museum-friendly Wiki/Met search phases.

Port of Ai_studio_prototyping ``distillQueryPhases`` + keyword/figure maps.
Fully headless — no Studio UI / Pexels.
"""

from __future__ import annotations

import re
from typing import Any

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "but",
        "or",
        "for",
        "nor",
        "on",
        "at",
        "to",
        "from",
        "by",
        "with",
        "about",
        "as",
        "into",
        "like",
        "through",
        "after",
        "over",
        "between",
        "out",
        "against",
        "during",
        "without",
        "before",
        "under",
        "around",
        "among",
        "within",
        "wide",
        "shot",
        "close",
        "up",
        "closeup",
        "pan",
        "zoom",
        "tilt",
        "aerial",
        "drone",
        "view",
        "scene",
        "showing",
        "silhouette",
        "video",
        "footage",
        "photo",
        "image",
        "picture",
        "motion",
        "slow",
        "fast",
        "background",
        "foreground",
        "camera",
        "angle",
        "perspective",
        "tracking",
        "static",
        "of",
        "in",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "can",
        "could",
        "it",
        "its",
        "they",
        "their",
        "them",
        "he",
        "his",
        "him",
        "she",
        "her",
        "we",
        "our",
        "us",
        "you",
        "your",
        "that",
        "this",
        "these",
        "those",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "why",
        "how",
        "what",
        "some",
        "any",
        "all",
        "every",
        "each",
        "no",
        "none",
        "one",
        "two",
        "three",
        "wearing",
        "dressed",
        "embroidered",
        "richly",
        "calm",
        "dignified",
        "expression",
        "looking",
        "standing",
        "sitting",
        "holding",
        "smiling",
        "crying",
        "evoking",
        "tension",
        "portrait",
        "beautiful",
        "beautifully",
        "gorgeous",
        "detailed",
        "highly",
        "photograph",
        "photographs",
        "photography",
        "classic",
        "inside",
        "interior",
        "exterior",
        "ambient",
        "mood",
        "describing",
        "depicting",
        "depicts",
        "depict",
        "represent",
        "representing",
        "represented",
        "rendered",
        "rendering",
        "render",
        "illustration",
        "style",
        "photorealistic",
        "realistic",
        "cinematic",
        "indoor",
        "outdoor",
        "digital",
        "art",
        "vector",
        "composition",
        "framing",
        "lighting",
        "focus",
        "textures",
    }
)

_GENERIC_SOLO = frozenset(
    {
        "face",
        "eyes",
        "eye",
        "man",
        "woman",
        "people",
        "person",
        "house",
        "building",
        "street",
        "road",
        "city",
        "town",
        "color",
        "background",
        "foreground",
        "portrait",
        "landscape",
        "view",
        "picture",
        "photo",
        "image",
        "interior",
        "exterior",
        "office",
        "place",
        "places",
        "thing",
        "things",
        "history",
        "historical",
        "old",
        "ancient",
        "modern",
        "new",
        "young",
        "vintage",
    }
)

_MULTI_WORD_FIGURES = (
    "catherine of aragon",
    "katherine of aragon",
    "anne boleyn",
    "jane seymour",
    "anne of cleves",
    "catherine howard",
    "katherine howard",
    "catherine parr",
    "katherine parr",
    "thomas cromwell",
    "thomas wolsey",
    "cardinal wolsey",
    "thomas cranmer",
    "thomas more",
    "elizabeth i",
    "mary i",
    "mary tudor",
    "mary i of england",
    "magna carta",
    "hampton court palace",
    "hampton court",
    "tower of london",
    "westminster abbey",
    "windsor castle",
    "lady jane grey",
    "jane grey",
    "henry viii",
    # Wave 1+ empire niches (art / science / empires / money / philosophy)
    "leonardo da vinci",
    "vincent van gogh",
    "claude monet",
    "rembrandt van rijn",
    "marcus aurelius",
    "isaac newton",
    "marie curie",
    "michael faraday",
    "galileo galilei",
    "louis pasteur",
    "nikola tesla",
    "john d rockefeller",
    "andrew carnegie",
    "east india company",
    "standard oil",
    "bank of england",
    "fall of constantinople",
)

_SINGLE_WORD_FIGURES = {
    "cromwell": "thomas cromwell",
    "wolsey": "thomas wolsey",
    "cranmer": "thomas cranmer",
    "boleyn": "anne boleyn",
    "seymour": "jane seymour",
    "parr": "catherine parr",
    "tudor": "tudor",
    "aragon": "catherine of aragon",
    "cleves": "anne of cleves",
    "shakespeare": "shakespeare",
    "rembrandt": "rembrandt van rijn",
    "vermeer": "johannes vermeer",
    "caravaggio": "caravaggio",
    "titian": "titian",
    "newton": "isaac newton",
    "curie": "marie curie",
    "faraday": "michael faraday",
    "galileo": "galileo galilei",
    "pasteur": "louis pasteur",
    "tesla": "nikola tesla",
    "rockefeller": "john d rockefeller",
    "carnegie": "andrew carnegie",
    "seneca": "seneca",
    "epictetus": "epictetus",
    "confucius": "confucius",
}

_FIGURE_CANONICAL = {
    "catherine of aragon": "Catherine of Aragon",
    "katherine of aragon": "Catherine of Aragon",
    "anne boleyn": "Anne Boleyn",
    "jane seymour": "Jane Seymour",
    "anne of cleves": "Anne of Cleves",
    "catherine howard": "Catherine Howard",
    "katherine howard": "Catherine Howard",
    "catherine parr": "Catherine Parr",
    "katherine parr": "Catherine Parr",
    "henry viii": "Henry VIII",
    "thomas cromwell": "Thomas Cromwell",
    "thomas wolsey": "Thomas Wolsey",
    "cardinal wolsey": "Thomas Wolsey",
    "thomas cranmer": "Thomas Cranmer",
    "thomas more": "Thomas More",
    "elizabeth i": "Elizabeth I",
    "mary i": "Mary I of England",
    "mary tudor": "Mary I of England",
    "mary i of england": "Mary I of England",
    "lady jane grey": "Lady Jane Grey",
    "jane grey": "Lady Jane Grey",
    "leonardo da vinci": "Leonardo da Vinci",
    "vincent van gogh": "Vincent van Gogh",
    "claude monet": "Claude Monet",
    "rembrandt van rijn": "Rembrandt",
    "johannes vermeer": "Johannes Vermeer",
    "caravaggio": "Caravaggio",
    "titian": "Titian",
    "marcus aurelius": "Marcus Aurelius",
    "isaac newton": "Isaac Newton",
    "marie curie": "Marie Curie",
    "michael faraday": "Michael Faraday",
    "galileo galilei": "Galileo Galilei",
    "louis pasteur": "Louis Pasteur",
    "nikola tesla": "Nikola Tesla",
    "john d rockefeller": "John D. Rockefeller",
    "andrew carnegie": "Andrew Carnegie",
    "seneca": "Seneca",
    "epictetus": "Epictetus",
    "confucius": "Confucius",
}

# After this many portrait placements of the same figure, force symbolic B-roll queries.
PORTRAIT_REUSE_LIMIT = 3


def sanitize_query_for_museum(raw_query: str) -> str:
    text = (raw_query or "").replace("_", " ")
    text = re.sub(r"\b(not|is|about|choice|secret|hidden)\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def get_search_keywords(text: str) -> str:
    if not text:
        return ""
    sanitized = sanitize_query_for_museum(text)
    lower = re.sub(
        r"\b(null|undefined|none|curated historical b-roll)\b",
        "",
        sanitized.lower(),
        flags=re.I,
    )
    lower = re.sub(r"\s+", " ", lower).strip()
    if not lower:
        return ""

    for needle, canon in (
        ("catherine of aragon", "Catherine of Aragon"),
        ("katherine of aragon", "Catherine of Aragon"),
        ("anne boleyn", "Anne Boleyn"),
        ("jane seymour", "Jane Seymour"),
        ("anne of cleves", "Anne of Cleves"),
        ("catherine howard", "Catherine Howard"),
        ("katherine howard", "Catherine Howard"),
        ("catherine parr", "Catherine Parr"),
        ("katherine parr", "Catherine Parr"),
        ("henry viii", "Henry VIII"),
        ("henry the eighth", "Henry VIII"),
        ("king henry", "Henry VIII"),
        ("thomas cromwell", "Thomas Cromwell"),
        ("thomas wolsey", "Thomas Wolsey"),
        ("cardinal wolsey", "Thomas Wolsey"),
        ("thomas cranmer", "Thomas Cranmer"),
        ("thomas more", "Thomas More"),
        ("elizabeth i", "Elizabeth I"),
        ("queen elizabeth", "Elizabeth I"),
        ("mary i", "Mary I of England"),
        ("queen mary", "Mary I of England"),
        ("mary tudor", "Mary I of England"),
    ):
        if needle in lower:
            return canon

    words = re.sub(r"[^\w\s]", "", lower).split()
    keywords = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    return " ".join(keywords[:4])


def get_required_proper_nouns(text: str) -> list[str]:
    if not text:
        return []
    lower = text.lower()
    found: list[str] = []
    for target in _MULTI_WORD_FIGURES:
        if re.search(rf"\b{re.escape(target)}\b", lower):
            found.append(target)
    for target, resolved in _SINGLE_WORD_FIGURES.items():
        if re.search(rf"\b{re.escape(target)}\b", lower):
            if not any(target in f for f in found):
                found.append(resolved)
    if re.search(r"\bhenry\b", lower, flags=re.I):
        if re.search(r"\bviii\b", lower, flags=re.I) or re.search(
            r"\bthe eighth\b", lower, flags=re.I
        ):
            found.append("henry viii")
        elif "henry" not in found:
            found.append("henry")
    # stable unique
    out: list[str] = []
    seen: set[str] = set()
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def extract_historical_figure(text: str) -> str | None:
    """Best canonical figure name for vision identity checks."""
    nouns = get_required_proper_nouns(text)
    for n in nouns:
        canon = _FIGURE_CANONICAL.get(n.lower())
        if canon:
            return canon
        if n.lower() not in {"tudor", "henry", "monarch"}:
            return " ".join(w.capitalize() for w in n.split())
    return None


def is_eligible_search_query(query: str) -> bool:
    if not query or not str(query).strip():
        return False
    cleaned = query.strip().lower()
    if len(cleaned) <= 1:
        return False
    if " " not in cleaned and (cleaned in _GENERIC_SOLO or cleaned in _STOP_WORDS):
        return False
    return True


def _era_anchors(lower: str) -> tuple[str, str]:
    if any(x in lower for x in ("washington", "delaware", "continental army")):
        return "American Revolution", "18th century"
    if any(x in lower for x in ("napoleon", "waterloo", "french revolution")):
        return "French Revolution", "19th century"
    if any(x in lower for x in ("lincoln", "civil war", "gettysburg")):
        return "American Civil War", "19th century"
    if any(
        x in lower
        for x in (
            "rembrandt",
            "vermeer",
            "caravaggio",
            "titian",
            "van gogh",
            "monet",
            "louvre",
            "painting",
            "masterpiece",
            "canvas",
        )
    ):
        return "European painting", "17th century"
    if any(
        x in lower
        for x in (
            "newton",
            "curie",
            "faraday",
            "galileo",
            "pasteur",
            "tesla",
            "laboratory",
            "telescope",
            "microscope",
        )
    ):
        return "history of science", "19th century"
    if any(
        x in lower
        for x in (
            "rockefeller",
            "carnegie",
            "standard oil",
            "factory",
            "railroad",
            "industrial",
        )
    ):
        return "Gilded Age", "19th century"
    if any(
        x in lower
        for x in ("bank", "gold standard", "coinage", "mint", "ledger", "bretton")
    ):
        return "monetary history", "19th century"
    if any(
        x in lower
        for x in ("stoic", "marcus aurelius", "seneca", "epictetus", "plato", "athens")
    ):
        return "classical philosophy", "ancient Rome"
    if any(x in lower for x in ("maya", "angkor", "khmer", "mali", "inca", "axum")):
        return "world civilization", "premodern"
    if any(x in lower for x in ("rome", "caesar", "roman", "carthage")):
        return "Roman", "ancient Rome"
    if any(x in lower for x in ("ottoman", "byzantine", "constantinople")):
        return "Ottoman", "16th century"
    if any(x in lower for x in ("mongol", "genghis")):
        return "Mongol", "13th century"
    if any(x in lower for x in ("qing", "ming", "forbidden city")):
        return "imperial China", "18th century"
    return "Tudor", "16th century"


def distill_query_phases(
    query: str,
    *,
    force_symbolic_broll: bool = False,
) -> dict[str, str]:
    """Return phase1..phase4 museum-friendly search strings (RMagine port)."""
    museum_sanitized = sanitize_query_for_museum(query)
    raw = (museum_sanitized or "")
    raw = re.sub(
        r"\b(null|undefined|none|curated historical b-roll)\b",
        "",
        raw,
        flags=re.I,
    )
    raw = re.sub(
        r"\b(close up|closeup|macro|shot|view of|hand on|woman's hand|"
        r"over the shoulder|showing|featuring|depicting)\b",
        "",
        raw,
        flags=re.I,
    )
    raw = re.sub(r"\s+", " ", raw).strip()
    lower = raw.lower()
    required = [] if force_symbolic_broll else get_required_proper_nouns(raw)
    capitalized = [
        " ".join(w.capitalize() for w in n.split()) for n in required
    ]
    era_prefix, era_century = _era_anchors(lower)

    artifact1 = artifact2 = artifact3 = ""
    if capitalized and not force_symbolic_broll:
        figure = capitalized[0]
        artifact1 = figure
        artifact2 = f"{figure} portrait"
        artifact3 = f"{era_prefix} court painting"
    elif any(
        x in lower
        for x in (
            "quill",
            "document",
            "confession",
            "letter",
            "manuscript",
            "paper",
            "writing",
        )
    ):
        artifact1 = f"{era_prefix} manuscript"
        artifact2 = f"{era_century} letter"
        artifact3 = "quill manuscript"
    elif any(x in lower for x in ("signet", "ring", "choice", "seal", "stamp")):
        artifact1 = "Gold Signet Ring"
        artifact2 = f"{era_prefix} seal"
        artifact3 = f"{era_century} ring"
    elif any(
        x in lower for x in ("chest", "wooden", "box", "trunk", "dusty", "storage")
    ):
        artifact1 = f"{era_prefix} chest"
        artifact2 = f"{era_century} wooden chest"
        artifact3 = f"{era_prefix} furniture"
    elif any(x in lower for x in ("key", "lock", "cushion", "velvet")):
        artifact1 = "antique key"
        artifact2 = f"{era_century} key"
        artifact3 = f"{era_prefix} key"
    elif any(
        x in lower
        for x in ("abdomen", "pregnant", "pregnancy", "lady", "woman")
    ):
        artifact1 = f"{era_prefix} lady portrait"
        artifact2 = f"{era_century} court painting"
        artifact3 = f"{era_prefix} court painting"
    elif any(
        x in lower
        for x in (
            "painting",
            "canvas",
            "masterpiece",
            "fresco",
            "altarpiece",
            "portrait oil",
        )
    ):
        artifact1 = f"{era_prefix} painting"
        artifact2 = "oil on canvas portrait"
        artifact3 = "museum painting"
    elif any(
        x in lower
        for x in (
            "astrolabe",
            "telescope",
            "microscope",
            "sextant",
            "laboratory",
            "flask",
            "instrument",
        )
    ):
        artifact1 = "scientific instrument"
        artifact2 = f"{era_century} laboratory instrument"
        artifact3 = "brass scientific instrument"
    elif any(x in lower for x in ("coin", "currency", "mint", "gold bar", "banknote")):
        artifact1 = "historical coin"
        artifact2 = f"{era_century} coinage"
        artifact3 = "gold coin museum"
    elif any(x in lower for x in ("ruin", "temple", "aqueduct", "fortress wall")):
        artifact1 = f"{era_prefix} ruins"
        artifact2 = "ancient temple ruins"
        artifact3 = "archaeological site"
    elif any(x in lower for x in ("tapestry", "weaving", "wall hanging")):
        artifact1 = f"{era_prefix} tapestry"
        artifact2 = f"{era_century} tapestry"
        artifact3 = f"{era_prefix} court painting"
    elif any(x in lower for x in ("crown", "throne", "scepter", "regalia")):
        artifact1 = f"{era_prefix} crown"
        artifact2 = f"{era_prefix} throne"
        artifact3 = f"{era_century} royal regalia"
    elif any(x in lower for x in ("gold", "money", "coin", "wealth", "treasure")):
        artifact1 = f"{era_prefix} coins"
        artifact2 = f"{era_century} coin"
        artifact3 = f"{era_prefix} treasure"
    elif any(
        x in lower for x in ("battle", "war", "soldier", "army", "conflict")
    ):
        artifact1 = f"{era_prefix} battle painting"
        artifact2 = f"{era_century} armor"
        artifact3 = f"{era_prefix} weapon"
    elif any(
        x in lower
        for x in (
            "death",
            "execution",
            "murder",
            "kill",
            "blood",
            "tower",
            "scaffold",
            "axe",
        )
    ):
        artifact1 = "Tower of London"
        artifact2 = f"{era_prefix} execution painting"
        artifact3 = f"{era_century} tower"
    elif any(
        x in lower
        for x in (
            "church",
            "abbey",
            "monk",
            "pope",
            "cathedral",
            "monastery",
            "religion",
        )
    ):
        artifact1 = "Westminster Abbey"
        artifact2 = f"{era_prefix} abbey"
        artifact3 = f"{era_century} monastery"
    elif any(
        x in lower
        for x in (
            "castle",
            "palace",
            "corridor",
            "room",
            "hall",
            "wall",
            "interior",
        )
    ):
        artifact1 = "Hampton Court Palace"
        artifact2 = f"{era_prefix} hall"
        artifact3 = f"{era_prefix} architecture"
    else:
        clean = get_search_keywords(raw) or " ".join(raw.split()[:2])
        brief = " ".join(clean.split()[:3])
        artifact1 = f"{brief} {era_prefix}".strip()
        artifact2 = f"{brief} {era_century}".strip()
        artifact3 = f"{era_prefix} painting"

    phase4 = f"{era_prefix} architecture"
    if any(x in lower for x in ("castle", "palace", "corridor")):
        phase4 = "Hampton Court Palace"
    elif any(x in lower for x in ("church", "abbey", "cathedral")):
        phase4 = "Westminster Abbey"

    if force_symbolic_broll and capitalized:
        # Prefer artifact / place queries over another portrait of the same face.
        artifact1 = artifact3 or phase4
        artifact2 = phase4
        artifact3 = f"{era_prefix} painting"

    return {
        "phase1": artifact1.strip(),
        "phase2": artifact2.strip(),
        "phase3": artifact3.strip(),
        "phase4": phase4.strip(),
    }


def scene_search_bundle(
    *,
    text: str = "",
    visual_prompt: str = "",
    force_symbolic_broll: bool = False,
) -> dict[str, Any]:
    """Build query phases + historical_figure for one farm scene."""
    blob = f"{visual_prompt or ''} {text or ''}".strip()
    figure = None if force_symbolic_broll else extract_historical_figure(blob)
    phases = distill_query_phases(blob or "historical painting", force_symbolic_broll=force_symbolic_broll)
    return {
        "historical_figure": figure,
        "phases": phases,
        "phase_list": [
            phases["phase1"],
            phases["phase2"],
            phases["phase3"],
            phases["phase4"],
        ],
        "force_symbolic_broll": force_symbolic_broll,
        "source_text": blob[:400],
    }
