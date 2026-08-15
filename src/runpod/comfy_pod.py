"""ComfyUI HTTP client for ephemeral stills pods (Flux workflow)."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from src.runpod.client import RunPodClientError

logger = logging.getLogger(__name__)

# RunPod proxy HTML interstitial while the container HTTP server is cold.
_TRANSIENT_PROMPT_STATUSES = frozenset({502, 503, 504})
_WAITING_SERVICE_MARKERS = (
    "waiting for service to respond",
    "runpod",
)


def _is_transient_comfy_response(status_code: int, body: str) -> bool:
    if status_code in _TRANSIENT_PROMPT_STATUSES:
        return True
    low = (body or "").lower()
    return any(m in low for m in _WAITING_SERVICE_MARKERS) and status_code >= 400


class ComfyPodClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 600.0,
        prompt_retries: int = 8,
        prompt_retry_base_s: float = 3.0,
        prompt_retry_max_s: float = 45.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.prompt_retries = max(1, int(prompt_retries))
        self.prompt_retry_base_s = max(0.5, float(prompt_retry_base_s))
        self.prompt_retry_max_s = max(self.prompt_retry_base_s, float(prompt_retry_max_s))

    def wait_service_ready(
        self,
        *,
        timeout_s: float = 120.0,
        poll_s: float = 3.0,
        path: str = "/system_stats",
    ) -> None:
        """Poll Comfy until it answers (skip RunPod 'Waiting for service' HTML)."""
        deadline = time.time() + max(5.0, float(timeout_s))
        url = f"{self.base_url}{path}"
        last_err = ""
        while time.time() < deadline:
            try:
                r = httpx.get(url, timeout=15.0, follow_redirects=True)
                body = (r.text or "")[:300]
                if r.status_code == 200 and not _is_transient_comfy_response(
                    r.status_code, body
                ):
                    # Reject HTML interstitial even on odd 200s
                    if "<html" in body.lower() and "waiting for service" in body.lower():
                        last_err = "HTML waiting interstitial"
                    else:
                        return
                elif _is_transient_comfy_response(r.status_code, body):
                    last_err = f"HTTP {r.status_code} transient"
                else:
                    # Comfy may 404 some paths while up — try /prompt OPTIONS-less GET root
                    if r.status_code in (401, 404) and path != "/":
                        path = "/"
                        url = f"{self.base_url}/"
                        last_err = f"HTTP {r.status_code} on {path}"
                        continue
                    last_err = f"HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            time.sleep(poll_s)
        raise RunPodClientError(
            f"Comfy not ready at {self.base_url} ({last_err})"
        )

    def queue_prompt(self, workflow: dict[str, Any], *, client_id: str | None = None) -> str:
        cid = client_id or uuid.uuid4().hex
        payload = {"prompt": workflow, "client_id": cid}
        last_err: Exception | None = None
        delay = self.prompt_retry_base_s
        for attempt in range(1, self.prompt_retries + 1):
            try:
                r = httpx.post(
                    f"{self.base_url}/prompt",
                    json=payload,
                    timeout=120.0,
                )
            except httpx.HTTPError as exc:
                last_err = RunPodClientError(f"Comfy /prompt transport: {exc}")
                logger.warning(
                    "Comfy /prompt transport attempt %s/%s: %s",
                    attempt,
                    self.prompt_retries,
                    exc,
                )
                time.sleep(delay)
                delay = min(self.prompt_retry_max_s, delay * 1.7)
                continue

            if r.status_code < 400:
                data = r.json()
                prompt_id = data.get("prompt_id") or data.get("promptId")
                if not prompt_id:
                    raise RunPodClientError(f"Comfy /prompt missing prompt_id: {data}")
                return str(prompt_id)

            body = r.text or ""
            err = RunPodClientError(
                f"Comfy /prompt HTTP {r.status_code}: {body[:500]}"
            )
            if (
                attempt < self.prompt_retries
                and _is_transient_comfy_response(r.status_code, body)
            ):
                last_err = err
                logger.warning(
                    "Comfy /prompt transient HTTP %s attempt %s/%s — backoff %.1fs",
                    r.status_code,
                    attempt,
                    self.prompt_retries,
                    delay,
                )
                # Re-probe readiness between retries when RunPod shows waiting page.
                try:
                    self.wait_service_ready(timeout_s=min(90.0, delay * 4))
                except RunPodClientError:
                    time.sleep(delay)
                delay = min(self.prompt_retry_max_s, delay * 1.7)
                continue
            raise err

        assert last_err is not None
        raise last_err

    def wait_history(
        self, prompt_id: str, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        deadline = time.time() + float(timeout_s or self.timeout_s)
        last: dict[str, Any] = {}
        while time.time() < deadline:
            r = httpx.get(f"{self.base_url}/history/{prompt_id}", timeout=60.0)
            if r.status_code >= 400:
                body = r.text or ""
                if _is_transient_comfy_response(r.status_code, body):
                    time.sleep(2.0)
                    continue
                raise RunPodClientError(
                    f"Comfy /history HTTP {r.status_code}: {body[:400]}"
                )
            hist = r.json()
            entry = hist.get(prompt_id) if isinstance(hist, dict) else None
            if entry:
                return entry
            # Some builds return flat
            if isinstance(hist, dict) and hist.get("outputs"):
                return hist
            last = hist if isinstance(hist, dict) else {"raw": hist}
            time.sleep(1.5)
        raise RunPodClientError(f"Comfy prompt {prompt_id} timed out; last={last}")

    def fetch_first_image(self, history_entry: dict[str, Any]) -> bytes:
        outputs = history_entry.get("outputs") or {}
        for _node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            images = node_out.get("images") or []
            for img in images:
                if not isinstance(img, dict):
                    continue
                filename = img.get("filename")
                if not filename:
                    continue
                params = {
                    "filename": filename,
                    "subfolder": img.get("subfolder") or "",
                    "type": img.get("type") or "output",
                }
                r = httpx.get(
                    f"{self.base_url}/view", params=params, timeout=120.0
                )
                if r.status_code >= 400:
                    raise RunPodClientError(
                        f"Comfy /view HTTP {r.status_code}: {r.text[:300]}"
                    )
                return r.content
        raise RunPodClientError(f"No images in Comfy history: {str(history_entry)[:400]}")

    def generate_image_bytes(self, workflow: dict[str, Any]) -> bytes:
        prompt_id = self.queue_prompt(workflow)
        hist = self.wait_history(prompt_id)
        return self.fetch_first_image(hist)

    def generate_to_path(self, workflow: dict[str, Any], out_path: Path | str) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.generate_image_bytes(workflow))
        return out
