"""Fetch and log source packs for Weird Human Biology scripts.

Deterministic research (not Claude tool-use): PubMed E-utilities primary,
Europe PMC + allowlisted article pages secondary. Google Scholar is skipped.
"""

from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "yt-long-factory-weird-biology/1.0 "
    "(research; contact=local-ops; +https://localhost)"
)
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EUROPE_PMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

ALLOWLIST_HOSTS = frozenset(
    {
        "pubmed.ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
        "www.nih.gov",
        "nih.gov",
        "www.smithsonianmag.com",
        "smithsonianmag.com",
        "www.nature.com",
        "nature.com",
        "www.scientificamerican.com",
        "scientificamerican.com",
        "www.sciencedirect.com",
        "sciencedirect.com",
        "europepmc.org",
        "www.europepmc.org",
    }
)

SKIP_HOSTS = frozenset({"scholar.google.com", "scholar.google.co.uk"})

DEFAULT_MIN_USABLE = 3
DEFAULT_PUBMED_RETMAX = 8
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class SourceItem:
    url: str
    title: str
    fetched_at: str
    excerpt: str
    source_hub: str
    claims_candidates: list[str] = field(default_factory=list)
    pmid: str | None = None
    status: str = "ok"  # ok | skipped | error
    error: str | None = None

    def usable(self) -> bool:
        return self.status == "ok" and len((self.excerpt or "").strip()) >= 80


@dataclass
class SourcePack:
    topic: str
    fetched_at: str
    items: list[SourceItem]
    skipped: list[dict[str, str]] = field(default_factory=list)
    min_usable: int = DEFAULT_MIN_USABLE

    def usable_items(self) -> list[SourceItem]:
        return [i for i in self.items if i.usable()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "fetched_at": self.fetched_at,
            "min_usable": self.min_usable,
            "usable_count": len(self.usable_items()),
            "items": [asdict(i) for i in self.items],
            "skipped": list(self.skipped),
        }

    def source_material_text(self) -> str:
        """Flatten usable excerpts for script + validator prompts."""
        blocks: list[str] = []
        for i, item in enumerate(self.usable_items(), start=1):
            head = f"[{i}] {item.title}"
            if item.pmid:
                head += f" (PMID {item.pmid})"
            head += f"\nURL: {item.url}\nHub: {item.source_hub}\n"
            body = item.excerpt.strip()
            blocks.append(head + body)
        return "\n\n---\n\n".join(blocks)


class SourcePackInsufficient(RuntimeError):
    """Raised when too few usable sources were fetched."""

    def __init__(self, message: str, *, pack: SourcePack | None = None):
        super().__init__(message)
        self.pack = pack


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_html(raw: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", raw or ""))
    return _WS_RE.sub(" ", text).strip()


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in SKIP_HOSTS or host.endswith(".scholar.google.com"):
        return False
    if host in ALLOWLIST_HOSTS:
        return True
    return host.endswith(".nih.gov") or host.endswith(".ncbi.nlm.nih.gov")


def _claim_snippets(text: str, *, limit: int = 8) -> list[str]:
    """Pull short lines that look like fact-bearing statements."""
    out: list[str] = []
    for chunk in re.split(r"(?<=[.!?])\s+", text):
        c = chunk.strip()
        if len(c) < 40:
            continue
        if re.search(r"\d|percent|%|study|found|showed|participants|subjects", c, re.I):
            out.append(c[:280])
        if len(out) >= limit:
            break
    return out


def _client(timeout_s: float = 45.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout_s,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        follow_redirects=True,
    )


def pubmed_search_ids(topic: str, *, retmax: int = DEFAULT_PUBMED_RETMAX) -> list[str]:
    term = f"{topic.strip()} AND (mechanism OR physiology OR study)"
    params = {
        "db": "pubmed",
        "term": term,
        "retmax": str(retmax),
        "retmode": "json",
        "sort": "relevance",
    }
    with _client() as client:
        r = client.get(PUBMED_ESEARCH, params=params)
        r.raise_for_status()
        data = r.json()
    ids = data.get("esearchresult", {}).get("idlist") or []
    return [str(x) for x in ids]


def pubmed_fetch_abstracts(pmids: list[str]) -> list[SourceItem]:
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    with _client(timeout_s=60.0) as client:
        r = client.get(PUBMED_EFETCH, params=params)
        r.raise_for_status()
        xml_text = r.text
    return _parse_pubmed_xml(xml_text)


def _parse_pubmed_xml(xml_text: str) -> list[SourceItem]:
    """Parse PubMed XML into SourceItems (testable without network)."""
    items: list[SourceItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("PubMed XML parse failed: %s", exc)
        return items

    fetched_at = _now_iso()
    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        pmid = (pmid_el.text or "").strip() if pmid_el is not None else ""
        title_el = article.find(".//ArticleTitle")
        title = _strip_html(
            "".join(title_el.itertext()) if title_el is not None else ""
        ) or f"PMID {pmid}"
        abstract_bits: list[str] = []
        for abs_el in article.findall(".//Abstract/AbstractText"):
            label = abs_el.attrib.get("Label") or abs_el.attrib.get("NlmCategory")
            body = _strip_html("".join(abs_el.itertext()))
            if not body:
                continue
            if label:
                abstract_bits.append(f"{label}: {body}")
            else:
                abstract_bits.append(body)
        journal_el = article.find(".//Journal/Title")
        journal = (journal_el.text or "").strip() if journal_el is not None else ""
        year_el = article.find(".//PubDate/Year")
        year = (year_el.text or "").strip() if year_el is not None else ""
        authors: list[str] = []
        for a in article.findall(".//Author")[:5]:
            last = (a.findtext("LastName") or "").strip()
            init = (a.findtext("Initials") or "").strip()
            if last:
                authors.append(f"{last} {init}".strip())
        meta_line = " | ".join(
            x for x in [", ".join(authors), journal, year] if x
        )
        excerpt = "\n".join(
            x for x in [meta_line, "\n".join(abstract_bits)] if x
        ).strip()
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
        if not excerpt:
            items.append(
                SourceItem(
                    url=url,
                    title=title,
                    fetched_at=fetched_at,
                    excerpt="",
                    source_hub="pubmed",
                    pmid=pmid or None,
                    status="error",
                    error="empty abstract",
                )
            )
            continue
        items.append(
            SourceItem(
                url=url,
                title=title,
                fetched_at=fetched_at,
                excerpt=excerpt,
                source_hub="pubmed",
                pmid=pmid or None,
                claims_candidates=_claim_snippets(excerpt),
                status="ok",
            )
        )
    return items


def europe_pmc_search(topic: str, *, page_size: int = 5) -> list[SourceItem]:
    params = {
        "query": f"{topic} mechanism OR physiology",
        "format": "json",
        "pageSize": str(page_size),
        "resultType": "core",
    }
    fetched_at = _now_iso()
    try:
        with _client() as client:
            r = client.get(EUROPE_PMC_SEARCH, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Europe PMC search failed: %s", exc)
        return [
            SourceItem(
                url=EUROPE_PMC_SEARCH,
                title="Europe PMC search",
                fetched_at=fetched_at,
                excerpt="",
                source_hub="europepmc",
                status="error",
                error=str(exc)[:240],
            )
        ]

    items: list[SourceItem] = []
    for hit in (data.get("resultList") or {}).get("result") or []:
        title = _strip_html(str(hit.get("title") or "Europe PMC result"))
        abstract = _strip_html(str(hit.get("abstractText") or ""))
        pmid = str(hit.get("pmid") or "").strip() or None
        pmcid = str(hit.get("pmcid") or "").strip()
        doi = str(hit.get("doi") or "").strip()
        if pmcid:
            url = f"https://europepmc.org/article/PMC/{pmcid.replace('PMC', '')}"
        elif pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        elif doi:
            url = f"https://doi.org/{doi}"
        else:
            url = "https://europepmc.org/"
        authors = str(hit.get("authorString") or "")
        journal = str(hit.get("journalTitle") or "")
        year = str(hit.get("pubYear") or "")
        meta = " | ".join(x for x in [authors, journal, year] if x)
        excerpt = "\n".join(x for x in [meta, abstract] if x).strip()
        if len(excerpt) < 80:
            continue
        items.append(
            SourceItem(
                url=url,
                title=title,
                fetched_at=fetched_at,
                excerpt=excerpt,
                source_hub="europepmc",
                pmid=pmid,
                claims_candidates=_claim_snippets(excerpt),
                status="ok",
            )
        )
    return items


def fetch_allowlisted_page(url: str, *, source_hub: str | None = None) -> SourceItem:
    fetched_at = _now_iso()
    hub = source_hub or (urlparse(url).hostname or "web")
    if not _host_allowed(url):
        return SourceItem(
            url=url,
            title="",
            fetched_at=fetched_at,
            excerpt="",
            source_hub=hub,
            status="skipped",
            error="host not allowlisted or Google Scholar",
        )
    try:
        with _client(timeout_s=30.0) as client:
            r = client.get(url)
            r.raise_for_status()
            raw_html = r.text
        text = _strip_html(raw_html)
        excerpt = text[:6000]
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
        if m:
            title = _strip_html(m.group(1))[:200]
        if not title:
            title = url
        if len(excerpt) < 80:
            return SourceItem(
                url=url,
                title=title,
                fetched_at=fetched_at,
                excerpt=excerpt,
                source_hub=hub,
                status="error",
                error="page text too short",
            )
        return SourceItem(
            url=url,
            title=title,
            fetched_at=fetched_at,
            excerpt=excerpt,
            source_hub=hub,
            claims_candidates=_claim_snippets(excerpt),
            status="ok",
        )
    except Exception as exc:  # noqa: BLE001
        return SourceItem(
            url=url,
            title="",
            fetched_at=fetched_at,
            excerpt="",
            source_hub=hub,
            status="error",
            error=str(exc)[:240],
        )


def _dedupe_items(items: list[SourceItem]) -> list[SourceItem]:
    seen: set[str] = set()
    out: list[SourceItem] = []
    for it in items:
        key = (it.pmid or "").strip() or it.url.strip().rstrip("/")
        key = key.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def gather_sources(
    topic: str,
    *,
    extra_urls: list[str] | None = None,
    pubmed_retmax: int = DEFAULT_PUBMED_RETMAX,
    min_usable: int = DEFAULT_MIN_USABLE,
) -> SourcePack:
    """Fetch sources without enforcing the usable-count gate."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")

    skipped: list[dict[str, str]] = [
        {
            "hub": "google_scholar",
            "reason": "bot walls / ToS — skipped by design; use PubMed/NCBI instead",
            "url": f"https://scholar.google.com/scholar?q={quote_plus(topic)}",
        }
    ]
    items: list[SourceItem] = []

    try:
        pmids = pubmed_search_ids(topic, retmax=pubmed_retmax)
        items.extend(pubmed_fetch_abstracts(pmids))
    except Exception as exc:  # noqa: BLE001
        log.warning("PubMed research failed: %s", exc)
        items.append(
            SourceItem(
                url=PUBMED_ESEARCH,
                title="PubMed search",
                fetched_at=_now_iso(),
                excerpt="",
                source_hub="pubmed",
                status="error",
                error=str(exc)[:240],
            )
        )

    items.extend(europe_pmc_search(topic))

    for url in extra_urls or []:
        u = (url or "").strip()
        if not u:
            continue
        if "scholar.google" in u:
            skipped.append(
                {"hub": "google_scholar", "reason": "skipped by design", "url": u}
            )
            continue
        items.append(fetch_allowlisted_page(u))

    items = _dedupe_items(items)
    return SourcePack(
        topic=topic,
        fetched_at=_now_iso(),
        items=items,
        skipped=skipped,
        min_usable=min_usable,
    )


def build_source_pack(
    topic: str,
    *,
    min_usable: int = DEFAULT_MIN_USABLE,
    extra_urls: list[str] | None = None,
    pubmed_retmax: int = DEFAULT_PUBMED_RETMAX,
) -> SourcePack:
    """Research a topic and return a source pack. Raises if insufficient."""
    pack = gather_sources(
        topic,
        extra_urls=extra_urls,
        pubmed_retmax=pubmed_retmax,
        min_usable=min_usable,
    )
    usable = pack.usable_items()
    if len(usable) < min_usable:
        gaps = [
            f"{i.source_hub}:{i.status}:{i.error or i.title[:60]}"
            for i in pack.items
            if not i.usable()
        ]
        raise SourcePackInsufficient(
            "SOURCE PACK INSUFFICIENT: "
            f"need {min_usable} usable sources, got {len(usable)}. "
            f"Gaps: {'; '.join(gaps[:12]) or 'none'}",
            pack=pack,
        )
    return pack


def save_source_pack(pack: SourcePack, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(pack.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
