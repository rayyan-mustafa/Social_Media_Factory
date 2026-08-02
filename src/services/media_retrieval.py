"""Wikimedia Commons retrieval + optional CLIP re-ranking + Chroma cache."""

from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path

import aiohttp
import requests
from PIL import Image

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)

USER_AGENT = "YouTubeAutomationEngine/0.1 (portfolio; contact=local-dev)"


class MediaRetrievalService:
    def __init__(self, use_clip: bool = True) -> None:
        settings = get_settings()
        self.use_clip = use_clip
        self.chroma_dir = settings.chroma_persist_dir
        self._device = "cpu"
        self._clip_model = None
        self._clip_preprocess = None
        self._collection = None

    def _ensure_clip(self) -> None:
        if self._clip_model is not None:
            return
        import clip  # type: ignore
        import torch

        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._clip_model, self._clip_preprocess = clip.load("ViT-B/32", device=self._device)
        logger.info("clip_loaded", extra={"device": self._device})

    def _ensure_chroma(self) -> None:
        if self._collection is not None:
            return
        import chromadb
        from chromadb.utils import embedding_functions

        Path(self.chroma_dir).mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=self.chroma_dir)
        self._collection = client.get_or_create_collection(
            name="media_cache",
            embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            ),
        )

    async def _search_wikimedia(self, query: str, limit: int = 5) -> list[str]:
        url = "https://commons.wikimedia.org/w/api.php"
        params: dict[str, str | int] = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url",
        }
        headers = {"User-Agent": USER_AGENT}
        async with (
            aiohttp.ClientSession(headers=headers) as session,
            session.get(url, params=params) as resp,
        ):
            data = await resp.json()
        pages = data.get("query", {}).get("pages", {})
        urls: list[str] = []
        for page in pages.values():
            info = page.get("imageinfo") or []
            if info and "url" in info[0]:
                urls.append(info[0]["url"])
        return urls

    def _cache_lookup(self, query: str) -> str | None:
        try:
            self._ensure_chroma()
            assert self._collection is not None
            res = self._collection.query(query_texts=[query], n_results=1)
            if res["ids"][0] and res["distances"][0][0] < 0.15:
                return res["metadatas"][0][0].get("url")
        except Exception:
            logger.exception("chroma_lookup_failed")
        return None

    def _cache_store(self, query: str, url: str) -> None:
        try:
            self._ensure_chroma()
            assert self._collection is not None
            self._collection.add(
                ids=[str(uuid.uuid4())],
                documents=[query],
                metadatas=[{"url": url}],
            )
        except Exception:
            logger.exception("chroma_store_failed")

    def _rank_with_clip(self, query: str, candidates: list[str]) -> str:
        import clip  # type: ignore
        import torch

        self._ensure_clip()
        assert self._clip_model is not None and self._clip_preprocess is not None
        text = clip.tokenize([query]).to(self._device)
        scored: list[tuple[str, float]] = []
        for c_url in candidates:
            try:
                img_r = requests.get(c_url, timeout=8, headers={"User-Agent": USER_AGENT})
                img_r.raise_for_status()
                img = self._clip_preprocess(Image.open(BytesIO(img_r.content)).convert("RGB"))
                img = img.unsqueeze(0).to(self._device)
                with torch.no_grad():
                    score = torch.cosine_similarity(
                        self._clip_model.encode_image(img),
                        self._clip_model.encode_text(text),
                    ).item()
                scored.append((c_url, score))
            except Exception:
                continue
        if not scored:
            return candidates[0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    async def fetch_best_image_url(self, query: str, limit: int = 5) -> str | None:
        cached = self._cache_lookup(query)
        if cached:
            logger.info("media_cache_hit", extra={"query": query})
            return cached

        candidates = await self._search_wikimedia(query, limit=limit)
        if not candidates:
            logger.warning("media_no_candidates", extra={"query": query})
            return None

        if self.use_clip:
            try:
                best = self._rank_with_clip(query, candidates)
            except Exception:
                logger.exception("clip_rank_failed_fallback")
                best = candidates[0]
        else:
            best = candidates[0]

        self._cache_store(query, best)
        return best

    def download_image(self, url: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        output_path.write_bytes(resp.content)
        return output_path

    def placeholder_image(self, output_path: Path, size: tuple[int, int] = (1280, 720)) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (32, 32, 40)).save(output_path, format="JPEG")
        return output_path
