"""RunPod REST v2 + GraphQL client for ephemeral on-demand pods.

Uses RUNPOD_API_KEY from settings/.env. Prefer create → work → terminate;
never leave pods idle.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from src.services.settings import get_settings

logger = logging.getLogger(__name__)

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"


class RunPodClientError(RuntimeError):
    pass


class RunPodClient:
    def __init__(self, api_key: str | None = None):
        key = (api_key or "").strip()
        if not key:
            key = (get_settings().runpod_api_key or os.getenv("RUNPOD_API_KEY") or "").strip()
        if not key:
            raise RunPodClientError("RUNPOD_API_KEY missing")
        self.api_key = key
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def list_pods(self) -> list[dict[str, Any]]:
        r = httpx.get(f"{REST_BASE}/pods", headers=self._headers, timeout=60.0)
        self._raise(r, "list pods")
        data = r.json()
        if isinstance(data, list):
            return data
        return list(data.get("items") or data.get("pods") or [])

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        r = httpx.get(f"{REST_BASE}/pods/{pod_id}", headers=self._headers, timeout=60.0)
        self._raise(r, f"get pod {pod_id}")
        return r.json()

    def create_pod(
        self,
        *,
        name: str,
        gpu_type_id: str,
        image_name: str | None = None,
        template_id: str | None = None,
        cloud_type: str = "COMMUNITY",
        gpu_count: int = 1,
        container_disk_gb: int = 40,
        volume_gb: int | None = None,
        volume_mount_path: str = "/workspace",
        ports: list[str] | None = None,
        env: dict[str, str] | None = None,
        data_center_ids: list[str] | None = None,
        network_volume_id: str | None = None,
        docker_args: str | None = None,
    ) -> dict[str, Any]:
        """Create an on-demand GPU pod via REST v2 (falls back to GraphQL if needed)."""
        if not image_name and not template_id:
            raise RunPodClientError("image_name or template_id required")

        body: dict[str, Any] = {
            "name": name,
            "cloudType": cloud_type.upper(),
            "gpuTypeIds": [gpu_type_id],
            "gpuCount": int(gpu_count),
            "containerDiskInGb": int(container_disk_gb),
        }
        if image_name:
            body["imageName"] = image_name
        if template_id:
            body["templateId"] = template_id
        if volume_gb is not None and volume_gb > 0:
            body["volumeInGb"] = int(volume_gb)
            body["volumeMountPath"] = volume_mount_path
        if ports:
            body["ports"] = ports
        if env:
            body["env"] = env
        if data_center_ids:
            body["dataCenterIds"] = data_center_ids
        if network_volume_id:
            body["networkVolumeId"] = network_volume_id
            body["volumeMountPath"] = volume_mount_path
        # dockerArgs is GraphQL-only. REST create silently drops it, so when a
        # start-command override is required (ckpt bootstrap / yanwk entrypoint)
        # skip REST and go straight to GraphQL.
        if docker_args:
            return self._create_pod_graphql(
                name=name,
                gpu_type_id=gpu_type_id,
                image_name=image_name,
                template_id=template_id,
                cloud_type=cloud_type,
                gpu_count=gpu_count,
                container_disk_gb=container_disk_gb,
                volume_gb=volume_gb,
                volume_mount_path=volume_mount_path,
                ports=ports,
                env=env,
                data_center_ids=data_center_ids,
                network_volume_id=network_volume_id,
                docker_args=docker_args,
            )

        r = httpx.post(f"{REST_BASE}/pods", headers=self._headers, json=body, timeout=120.0)
        if r.status_code >= 400:
            # GraphQL fallback when REST rejects networkVolumeId / shape quirks
            logger.warning(
                "REST create pod HTTP %s — trying GraphQL: %s",
                r.status_code,
                r.text[:400],
            )
            return self._create_pod_graphql(
                name=name,
                gpu_type_id=gpu_type_id,
                image_name=image_name,
                template_id=template_id,
                cloud_type=cloud_type,
                gpu_count=gpu_count,
                container_disk_gb=container_disk_gb,
                volume_gb=volume_gb,
                volume_mount_path=volume_mount_path,
                ports=ports,
                env=env,
                data_center_ids=data_center_ids,
                network_volume_id=network_volume_id,
                docker_args=docker_args,
            )
        return r.json()

    def _create_pod_graphql(
        self,
        *,
        name: str,
        gpu_type_id: str,
        image_name: str | None,
        template_id: str | None,
        cloud_type: str,
        gpu_count: int,
        container_disk_gb: int,
        volume_gb: int | None,
        volume_mount_path: str,
        ports: list[str] | None,
        env: dict[str, str] | None,
        data_center_ids: list[str] | None,
        network_volume_id: str | None,
        docker_args: str | None = None,
    ) -> dict[str, Any]:
        inp: dict[str, Any] = {
            "name": name,
            "cloudType": cloud_type.upper(),
            "gpuTypeId": gpu_type_id,
            "gpuCount": int(gpu_count),
            "containerDiskInGb": int(container_disk_gb),
            "startSsh": True,
            "volumeMountPath": volume_mount_path,
        }
        if image_name:
            inp["imageName"] = image_name
        if template_id:
            inp["templateId"] = template_id
        if volume_gb is not None and volume_gb > 0 and not network_volume_id:
            inp["volumeInGb"] = int(volume_gb)
        if ports:
            inp["ports"] = ",".join(ports)
        if env:
            inp["env"] = [{"key": k, "value": v} for k, v in env.items()]
        if data_center_ids:
            inp["dataCenterId"] = data_center_ids[0]
        if network_volume_id:
            inp["networkVolumeId"] = network_volume_id
        if docker_args:
            inp["dockerArgs"] = docker_args

        query = """
        mutation PodFindAndDeployOnDemand($input: PodFindAndDeployOnDemandInput!) {
          podFindAndDeployOnDemand(input: $input) {
            id
            name
            desiredStatus
            imageName
            machineId
            machine { podHostId }
            runtime { ports { ip isIpPublic privatePort publicPort type } }
          }
        }
        """
        data = self.graphql(query, {"input": inp})
        pod = (data.get("data") or {}).get("podFindAndDeployOnDemand")
        if not pod:
            errs = data.get("errors") or data
            raise RunPodClientError(f"GraphQL create pod failed: {errs}")
        return pod

    def stop_pod(self, pod_id: str) -> dict[str, Any]:
        r = httpx.post(
            f"{REST_BASE}/pods/{pod_id}/stop", headers=self._headers, timeout=60.0
        )
        if r.status_code >= 400:
            # GraphQL stop
            self.graphql(
                """
                mutation($id: String!) {
                  podStop(input: {podId: $id}) { id desiredStatus }
                }
                """,
                {"id": pod_id},
            )
            return {"id": pod_id, "desiredStatus": "EXITED"}
        return r.json() if r.content else {"id": pod_id}

    def terminate_pod(self, pod_id: str) -> None:
        """Permanently delete a pod (stops billing)."""
        r = httpx.delete(f"{REST_BASE}/pods/{pod_id}", headers=self._headers, timeout=60.0)
        if r.status_code < 400:
            return
        data = self.graphql(
            """
            mutation($id: String!) {
              podTerminate(input: {podId: $id})
            }
            """,
            {"id": pod_id},
        )
        if data.get("errors"):
            raise RunPodClientError(f"terminate {pod_id} failed: {data['errors']}")

    def wait_until_running(
        self,
        pod_id: str,
        *,
        timeout_s: float = 600.0,
        poll_s: float = 5.0,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_pod(pod_id)
            status = (
                last.get("desiredStatus")
                or last.get("status")
                or (last.get("runtime") or {}).get("uptimeInSeconds")
            )
            runtime = last.get("runtime") or {}
            ports = runtime.get("ports") or last.get("ports") or []
            # Ready when we have a public proxy / IP
            if ports or str(status).upper() in {"RUNNING", "ACTIVE"}:
                if ports or self.proxy_base_url(last, http_port=8188):
                    return last
            desired = str(last.get("desiredStatus") or "").upper()
            if desired in {"EXITED", "TERMINATED", "FAILED"}:
                raise RunPodClientError(f"pod {pod_id} entered {desired}: {last}")
            time.sleep(poll_s)
        raise RunPodClientError(f"pod {pod_id} not ready within {timeout_s}s; last={last}")

    def proxy_base_url(self, pod: dict[str, Any], *, http_port: int = 8188) -> str | None:
        """Build https://{pod_id}-{port}.proxy.runpod.net for HTTP services."""
        pod_id = pod.get("id") or pod.get("podId")
        if not pod_id:
            return None
        runtime = pod.get("runtime") or {}
        ports = runtime.get("ports") or pod.get("ports") or []
        for p in ports:
            if not isinstance(p, dict):
                continue
            priv = int(p.get("privatePort") or p.get("private_port") or 0)
            typ = str(p.get("type") or "").lower()
            if priv == http_port or (typ == "http" and priv):
                public = p.get("publicPort") or p.get("public_port")
                ip = p.get("ip")
                if ip and public and p.get("isIpPublic"):
                    return f"http://{ip}:{public}"
        # Standard RunPod HTTP proxy
        return f"https://{pod_id}-{http_port}.proxy.runpod.net"

    def wait_http_ready(
        self,
        base_url: str,
        *,
        path: str = "/",
        timeout_s: float = 900.0,
        poll_s: float = 5.0,
        ok_statuses: tuple[int, ...] = (200, 401),
    ) -> None:
        # Note: do NOT treat 404 as ready — RunPod proxies often return 404
        # before the container HTTP server is listening (false-positive "ready").
        deadline = time.time() + timeout_s
        url = base_url.rstrip("/") + path
        last_err = ""
        while time.time() < deadline:
            try:
                r = httpx.get(url, timeout=15.0, follow_redirects=True)
                if r.status_code in ok_statuses:
                    return
                last_err = f"HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            time.sleep(poll_s)
        raise RunPodClientError(f"HTTP not ready at {url}: {last_err}")

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        r = httpx.post(
            GRAPHQL_URL,
            headers=self._headers,
            json={"query": query, "variables": variables or {}},
            timeout=120.0,
        )
        if r.status_code >= 400:
            raise RunPodClientError(f"GraphQL HTTP {r.status_code}: {r.text[:600]}")
        return r.json()

    def probe_gpu_stock(
        self,
        gpu_type_id: str,
        *,
        cloud_type: str = "COMMUNITY",
        gpu_count: int = 1,
    ) -> dict[str, Any]:
        """Best-effort stock probe via GraphQL — never creates a pod.

        Returns ``{gpu_type_id, cloud_type, stock_status, price_usd_hr, raw}``.
        ``stock_status`` is High/Medium/Low/None (None/null → out of stock).
        """
        secure = str(cloud_type or "COMMUNITY").strip().upper() == "SECURE"
        query = """
        query($id: String!, $secure: Boolean!, $count: Int!) {
          gpuTypes(input: { id: $id }) {
            id
            displayName
            lowestPrice(input: { gpuCount: $count, secureCloud: $secure }) {
              stockStatus
              uninterruptablePrice
              availableGpuCounts
            }
          }
        }
        """
        data = self.graphql(
            query,
            {"id": gpu_type_id, "secure": secure, "count": int(gpu_count)},
        )
        if data.get("errors"):
            raise RunPodClientError(f"probe_gpu_stock failed: {data['errors']}")
        gts = (data.get("data") or {}).get("gpuTypes") or []
        lp = (gts[0].get("lowestPrice") if gts else None) or {}
        status = lp.get("stockStatus")
        if status is None:
            status = "None"
        return {
            "gpu_type_id": gpu_type_id,
            "cloud_type": str(cloud_type or "COMMUNITY").strip().upper(),
            "stock_status": str(status),
            "price_usd_hr": lp.get("uninterruptablePrice"),
            "available_gpu_counts": lp.get("availableGpuCounts"),
            "raw": gts[0] if gts else None,
        }

    @staticmethod
    def _raise(r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            raise RunPodClientError(f"{what}: HTTP {r.status_code}: {r.text[:600]}")
