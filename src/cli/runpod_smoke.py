"""Smoke-test ephemeral RunPod pods (create → ready → generate → terminate).

Never leaves pods running. Farm must stay OFF — this CLI always terminates.

Industrial stills proof (single-flight)::

  .venv/bin/python -m src.cli.runpod_smoke --stills --count 2

Policy (Rayyan/CTO):
  - Healthy-run ceiling: RUNPOD_POD_MAX_MINUTES=60 (safety only)
  - Kill immediately on error
  - Kill immediately once stills are on local disk (do not wait out TTL)
  - Circuit breaker + bash -n preflight before create
  - Success writes output/ops/runpod_stills_smoke_ok.json (does NOT re-arm farm)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runpod.capacity import (  # noqa: E402
    CapacityDeferError,
    apply_secure_only_to_spec,
    assert_capacity_allows_stills,
)
from src.runpod.client import RunPodClient, RunPodClientError  # noqa: E402
from src.runpod.comfy_pod import ComfyPodClient  # noqa: E402
from src.runpod.guards import (  # noqa: E402
    RunPodGuardError,
    SMOKE_LOCK_PATH,
    assert_circuit_allows_create,
    assert_vps_still_paths,
    preflight_docker_args,
    record_create_failure,
    write_smoke_failure,
    write_smoke_ok,
)
from src.runpod.lifecycle import (  # noqa: E402
    STILLS_BOOTSTRAP_DOCKER_ARGS,
    EphemeralPodSession,
    PodSpec,
    parse_gpu_fallback_csv,
    stills_pod_spec_from_settings,
    voice_pod_spec_from_settings,
)
from src.services.settings import get_settings  # noqa: E402

app = typer.Typer(add_completion=False, no_args_is_help=True)

_SMOKE_PROMPT = (
    "Painterly Tudor documentary still, Henry VIII in Hampton Court gallery, "
    "soft window light, historical illustration style, no text"
)

# Shared base64-wrapped bootstrap (see lifecycle.docker_args_from_script).
_BOOTSTRAP_DOCKER_ARGS = STILLS_BOOTSTRAP_DOCKER_ARGS

# Healthy-run ceiling for proof (weights download + 2 stills). Not a dwell time.
_SMOKE_MAX_MINUTES = 60.0


def _stills_bootstrap_spec(base: PodSpec) -> PodSpec:
    """No network volume; download ckpt onto large container disk.

    GPU chain (prefer cheap Community): A5000 Comm → 3090 Comm → A5000 Secure → A40 Secure.
    """
    fallbacks = [
        (base.gpu_type_id, base.cloud_type),
        *list(base.gpu_fallbacks or []),
    ]
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for g, c in [
        ("NVIDIA RTX A5000", "COMMUNITY"),
        ("NVIDIA GeForce RTX 3090", "COMMUNITY"),
        *fallbacks,
        ("NVIDIA RTX A5000", "SECURE"),
        ("NVIDIA A40", "SECURE"),
    ]:
        key = (str(g).strip(), str(c or "COMMUNITY").strip().upper())
        if key in seen or not key[0]:
            continue
        seen.add(key)
        ordered.append(key)
    primary_gpu, primary_cloud = ordered[0]
    rest = ordered[1:]
    return PodSpec(
        name_prefix=base.name_prefix,
        gpu_type_id=primary_gpu,
        cloud_type=primary_cloud,
        image_name=base.image_name or "yanwk/comfyui-boot:cu126-slim",
        template_id=None,  # template can ignore dockerArgs
        container_disk_gb=max(80, int(base.container_disk_gb or 40)),
        volume_gb=20,
        volume_mount_path="/workspace",
        ports=list(base.ports) or ["8188/http"],
        env={
            **dict(base.env or {}),
            "CLI_ARGS": "--listen 0.0.0.0 --port 8188",
        },
        data_center_ids=None,
        network_volume_id=None,
        http_port=base.http_port,
        ready_path="/",  # yanwk UI; system_stats may lag first boot
        # Cap waits by healthy-run ceiling (TTL also enforces).
        ready_timeout_s=min(max(float(base.ready_timeout_s), 1800.0), 55 * 60.0),
        boot_timeout_s=min(float(base.boot_timeout_s), 55 * 60.0),
        gpu_fallbacks=rest,
        wait_http=base.wait_http,
        require_network_volume=False,
        docker_args=_BOOTSTRAP_DOCKER_ARGS,
        max_minutes=_SMOKE_MAX_MINUTES,
    )


def _kill_yt_stills(client: RunPodClient) -> list[str]:
    """Terminate any leftover yt-stills* pods (single-flight safety)."""
    killed: list[str] = []
    for p in client.list_pods():
        pid = p.get("id") or p.get("podId")
        name = str(p.get("name") or "")
        if pid and name.startswith("yt-stills"):
            try:
                client.terminate_pod(str(pid))
                killed.append(str(pid))
                typer.echo(f"preflight kill leftover: {pid} ({name})", err=True)
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"WARN terminate leftover {pid}: {exc}", err=True)
    return killed


def _kill_yt_ephemeral(client: RunPodClient) -> list[str]:
    killed: list[str] = []
    for p in client.list_pods():
        pid = p.get("id") or p.get("podId")
        name = str(p.get("name") or "")
        if pid and (name.startswith("yt-voice") or name.startswith("yt-stills")):
            try:
                client.terminate_pod(str(pid))
                killed.append(str(pid))
                typer.echo(f"kill leftover: {pid} ({name})", err=True)
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"WARN terminate leftover {pid}: {exc}", err=True)
    return killed


def _acquire_smoke_lock() -> None:
    """Max 1 stills smoke at a time (file lock)."""
    SMOKE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SMOKE_LOCK_PATH.exists():
        try:
            meta = json.loads(SMOKE_LOCK_PATH.read_text(encoding="utf-8"))
            age = time.time() - float(meta.get("pid_started") or 0)
            # Stale lock > 2h → steal (prior crash)
            if age < 7200:
                raise RunPodGuardError(
                    f"Another stills smoke holds {SMOKE_LOCK_PATH} "
                    f"(pid={meta.get('pid')}, age_s={age:.0f}). Single-flight only."
                )
        except RunPodGuardError:
            raise
        except Exception:  # noqa: BLE001
            pass
    SMOKE_LOCK_PATH.write_text(
        json.dumps({"pid": os.getpid(), "pid_started": time.time()}, indent=2) + "\n",
        encoding="utf-8",
    )


def _release_smoke_lock() -> None:
    try:
        if SMOKE_LOCK_PATH.exists():
            SMOKE_LOCK_PATH.unlink()
    except OSError:
        pass


@app.command("list")
def list_pods() -> None:
    client = RunPodClient()
    pods = client.list_pods()
    typer.echo(json.dumps(pods, indent=2, default=str))


@app.command("smoke")
def smoke(
    voice: bool = typer.Option(False, "--voice", help="Smoke CosyVoice pod (Ada primary)"),
    stills: bool = typer.Option(False, "--stills", help="Smoke Comfy stills pod"),
    both: bool = typer.Option(False, "--both", help="Smoke voice then stills"),
    skip_http: bool = typer.Option(
        False, "--skip-http", help="Only wait until RUNNING; skip HTTP ready"
    ),
    stills_count: int = typer.Option(
        0,
        "--stills-count",
        help="If >0 with --stills: run N timed Flux generations (real workflow)",
    ),
    count: int = typer.Option(
        0,
        "--count",
        help="Alias for --stills-count (preferred: --stills --count 2)",
    ),
    force_gpu: str = typer.Option(
        "",
        "--force-gpu",
        help='Override stills primary GPU, e.g. "NVIDIA A40"',
    ),
    force_cloud: str = typer.Option(
        "",
        "--force-cloud",
        help="Override stills primary cloud (COMMUNITY|SECURE)",
    ),
    force_fallbacks: str = typer.Option(
        "",
        "--force-fallbacks",
        help="Override stills GPU fallbacks CSV (GPU:CLOUD,...)",
    ),
    bootstrap_ckpt: bool = typer.Option(
        False,
        "--bootstrap-ckpt",
        help=(
            "Force container-disk Flux bootstrap (no network volume). "
            "Default stills proof already bootstraps when netvol is empty."
        ),
    ),
    out_dir: Path = typer.Option(
        Path("output/runpod_smoke_stills"),
        "--out-dir",
        help="Where to write timed still JPEGs when --count > 0",
    ),
    skip_capacity: bool = typer.Option(
        False,
        "--skip-capacity",
        help="Skip runpod_stills_benchmark_v1 pre-flight (not recommended)",
    ),
) -> None:
    if both:
        voice = stills = True
    if not voice and not stills:
        raise typer.BadParameter("Pass --voice, --stills, or --both")

    n_stills = max(int(stills_count or 0), int(count or 0))
    get_settings.cache_clear()
    # Enforce healthy-run ceiling for this process (settings + session TTL).
    os.environ.setdefault("RUNPOD_POD_MAX_MINUTES", str(int(_SMOKE_MAX_MINUTES)))
    results: dict[str, object] = {
        "policy": {
            "max_minutes_ceiling": _SMOKE_MAX_MINUTES,
            "kill_on_error": True,
            "kill_on_success_after_download": True,
            "farm_rearm": False,
        }
    }

    client = RunPodClient()
    lock_held = False
    try:
        if stills and n_stills > 0:
            _acquire_smoke_lock()
            lock_held = True
            results["pre_killed"] = _kill_yt_stills(client)
            try:
                assert_circuit_allows_create()
            except RunPodGuardError as exc:
                results["stills"] = {"ok": False, "error": str(exc), "gate": "circuit"}
                write_smoke_failure(error=str(exc))
                raise typer.Exit(code=2) from exc

        if voice:
            results["voice"] = _smoke_one(
                "voice", voice_pod_spec_from_settings(), skip_http
            )

        if stills:
            try:
                cap = assert_capacity_allows_stills(
                    client=client, skip=skip_capacity
                )
                if cap is not None:
                    results["capacity"] = cap.to_dict()
            except CapacityDeferError as exc:
                results["capacity"] = exc.result.to_dict()
                results["stills"] = {
                    "ok": False,
                    "error": str(exc),
                    "gate": "capacity",
                }
                write_smoke_failure(error=str(exc))
                raise typer.Exit(code=3) from exc

            spec = _stills_spec_override(
                force_gpu=force_gpu.strip() or None,
                force_cloud=force_cloud.strip() or None,
                force_fallbacks=force_fallbacks.strip() or None,
            )
            # Production path already uses container bootstrap when netvol empty;
            # --bootstrap-ckpt forces that path even if a volume id is set.
            if bootstrap_ckpt or not spec.network_volume_id:
                spec = _stills_bootstrap_spec(spec)
            else:
                spec.max_minutes = _SMOKE_MAX_MINUTES
            if (
                not skip_capacity
                and isinstance(results.get("capacity"), dict)
                and results["capacity"].get("decision") == "proceed_secure_only"
            ):
                apply_secure_only_to_spec(spec)

            results["stills"] = _smoke_one(
                "stills",
                spec,
                skip_http,
                stills_count=n_stills,
                out_dir=out_dir,
                write_gate=(n_stills > 0),
            )
    finally:
        leftover = _kill_yt_ephemeral(client)
        results["leftover_terminated"] = leftover
        results["pods_remaining"] = [
            {"id": p.get("id") or p.get("podId"), "name": p.get("name")}
            for p in client.list_pods()
        ]
        if lock_held:
            _release_smoke_lock()

    typer.echo(json.dumps(results, indent=2, default=str))
    stills_res = results.get("stills")
    if isinstance(stills_res, dict) and n_stills > 0 and not stills_res.get("ok"):
        raise typer.Exit(code=1)


def _stills_spec_override(
    *,
    force_gpu: str | None,
    force_cloud: str | None,
    force_fallbacks: str | None,
) -> PodSpec:
    spec = stills_pod_spec_from_settings()
    if force_gpu:
        spec.gpu_type_id = force_gpu
    if force_cloud:
        spec.cloud_type = force_cloud.upper()
    if force_fallbacks is not None and force_fallbacks != "":
        spec.gpu_fallbacks = parse_gpu_fallback_csv(force_fallbacks)
    elif force_gpu:
        if "A40" in force_gpu.upper() and not force_fallbacks:
            spec.gpu_fallbacks = [
                ("NVIDIA A40", "SECURE"),
            ]
            if (force_cloud or "COMMUNITY").upper() == "SECURE":
                spec.gpu_fallbacks = [("NVIDIA A40", "COMMUNITY")]
    return spec


def _build_smoke_workflow(*, seed: int) -> dict:
    import copy

    s = get_settings()
    path = Path(s.comfy_workflow_path)
    if not path.is_absolute():
        path = ROOT / path
    workflow = json.loads(path.read_text(encoding="utf-8"))
    wf = copy.deepcopy(workflow)
    if "6" in wf and "inputs" in wf["6"]:
        wf["6"]["inputs"]["text"] = _SMOKE_PROMPT
    if "33" in wf and "inputs" in wf["33"]:
        wf["33"]["inputs"]["text"] = ""
    if "27" in wf and "inputs" in wf["27"]:
        wf["27"]["inputs"]["width"] = int(s.image_width)
        wf["27"]["inputs"]["height"] = int(s.image_height)
        wf["27"]["inputs"]["batch_size"] = 1
    if "30" in wf and "inputs" in wf["30"]:
        wf["30"]["inputs"]["ckpt_name"] = s.comfy_ckpt_name
    if "31" in wf and "inputs" in wf["31"]:
        wf["31"]["inputs"]["seed"] = int(seed)
        wf["31"]["inputs"]["steps"] = int(s.comfy_steps)
        wf["31"]["inputs"]["cfg"] = 1
        wf["31"]["inputs"]["sampler_name"] = "euler"
        wf["31"]["inputs"]["scheduler"] = "simple"
    if "35" in wf and "inputs" in wf["35"]:
        wf["35"]["inputs"]["guidance"] = float(s.comfy_guidance)
    return wf


def _time_stills(comfy: ComfyPodClient, *, count: int, out_dir: Path) -> dict:
    """Generate stills and save to the **VPS workspace** (project ROOT), not the pod disk."""
    out_dir = Path(out_dir)
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    else:
        out_dir = out_dir.resolve()
    # Must land under the VPS project tree.
    try:
        out_dir.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise RunPodGuardError(
            f"out_dir must be under VPS workspace {ROOT.resolve()}; got {out_dir}"
        ) from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    times: list[float] = []
    paths: list[str] = []
    errors: list[str] = []
    for i in range(count):
        seed = 42_000 + i
        wf = _build_smoke_workflow(seed=seed)
        dest = out_dir / f"smoke_{i:03d}.jpg"
        t0 = time.perf_counter()
        try:
            raw = comfy.generate_image_bytes(wf)
            try:
                from io import BytesIO

                from PIL import Image

                img = Image.open(BytesIO(raw)).convert("RGB")
                img.save(dest, format="JPEG", quality=92, optimize=True)
            except Exception:
                dest.write_bytes(raw)
            if not dest.exists() or dest.stat().st_size < 100:
                raise RuntimeError(f"still not on VPS disk or too small: {dest}")
            dt = time.perf_counter() - t0
            times.append(dt)
            paths.append(str(dest.resolve()))
            typer.echo(f"still[{i}] {dt:.2f}s -> VPS {dest}", err=True)
        except Exception as exc:  # noqa: BLE001
            dt = time.perf_counter() - t0
            errors.append(f"still[{i}] after {dt:.1f}s: {exc}")
            typer.echo(f"still[{i}] FAILED after {dt:.1f}s: {exc}", err=True)
            break
    warm = times[1:] if len(times) > 1 else times
    median = sorted(warm)[len(warm) // 2] if warm else (times[0] if times else None)
    return {
        "requested": count,
        "completed": len(times),
        "wall_s": times,
        "warm_wall_s": warm,
        "median_warm_s": median,
        "first_still_s": times[0] if times else None,
        "paths": paths,
        "vps_out_dir": str(out_dir),
        "errors": errors,
    }


def _smoke_one(
    kind: str,
    spec: PodSpec,
    skip_http: bool,
    *,
    stills_count: int = 0,
    out_dir: Path | None = None,
    write_gate: bool = False,
) -> dict:
    out: dict = {"kind": kind, "ok": False, "killed_on_success": False}
    if skip_http:
        spec.wait_http = False
        spec.boot_timeout_s = min(float(spec.boot_timeout_s), 300.0)
    # Preflight before create (also done inside EphemeralPodSession).
    try:
        preflight_docker_args(spec.docker_args)
    except RunPodGuardError as exc:
        out["error"] = str(exc)
        if write_gate:
            write_smoke_failure(error=str(exc))
        return out

    t_enter = time.perf_counter()
    sess: EphemeralPodSession | None = None
    try:
        sess = EphemeralPodSession(spec, max_minutes=_SMOKE_MAX_MINUTES if kind == "stills" else None)
        sess.__enter__()
        cold_s = time.perf_counter() - t_enter
        out["pod_id"] = sess.pod_id
        out["base_url"] = sess.base_url
        out["selected_gpu"] = sess.selected_gpu_type_id
        out["selected_cloud"] = sess.selected_cloud_type
        out["attempts"] = sess.attempts
        out["cold_start_s"] = round(cold_s, 2)
        out["max_minutes_ceiling"] = sess._resolved_max_minutes()
        out["ok"] = True
        if kind == "stills" and stills_count > 0 and sess.base_url:
            comfy = ComfyPodClient(sess.base_url)
            out["generations"] = _time_stills(
                comfy,
                count=stills_count,
                out_dir=out_dir or Path("output/runpod_smoke_stills"),
            )
            gens = out["generations"]
            completed = int(gens.get("completed") or 0)
            if gens.get("errors") or completed < stills_count:
                out["ok"] = False
                out["error"] = "; ".join(gens.get("errors") or ["incomplete stills"])
            else:
                # SUCCESS only when JPGs exist on the VPS workspace (not pod disk).
                try:
                    vps_paths = assert_vps_still_paths(list(gens.get("paths") or []))
                except RunPodGuardError as vps_exc:
                    out["ok"] = False
                    out["error"] = str(vps_exc)
                else:
                    out["vps_paths"] = [str(p) for p in vps_paths]
                    # Kill rented GPU only AFTER files are on our VPS.
                    sess.terminate_now(reason="stills_on_vps_workspace")
                    out["killed_on_success"] = True
                    typer.echo(
                        "kill-on-success: VPS JPGs verified → pod terminated immediately",
                        err=True,
                    )
                    if write_gate:
                        write_smoke_ok(
                            stills_count=completed,
                            pod_id=out.get("pod_id"),
                            gpu=out.get("selected_gpu"),
                            cloud=out.get("selected_cloud"),
                            paths=[str(p) for p in vps_paths],
                            cold_start_s=out.get("cold_start_s"),
                            extra={
                                "generations": {
                                    "wall_s": gens.get("wall_s"),
                                    "median_warm_s": gens.get("median_warm_s"),
                                    "vps_out_dir": gens.get("vps_out_dir"),
                                },
                                "max_minutes_ceiling": out.get("max_minutes_ceiling"),
                            },
                        )
                        out["smoke_ok_path"] = "output/ops/runpod_stills_smoke_ok.json"
        elif write_gate and stills_count <= 0:
            # Ready-only smoke does not open the farm gate.
            out["note"] = "ready-only — smoke_ok not written (need --count N)"
    except (RunPodClientError, RunPodGuardError) as exc:
        out["error"] = str(exc)
        out["ok"] = False
        out["cold_start_s"] = round(time.perf_counter() - t_enter, 2)
        if write_gate:
            write_smoke_failure(error=str(exc))
            # Circuit already recorded by EphemeralPodSession on enter failure;
            # record again only if we never entered.
            if sess is None:
                try:
                    record_create_failure(error=str(exc))
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["ok"] = False
        out["cold_start_s"] = round(time.perf_counter() - t_enter, 2)
        if write_gate:
            write_smoke_failure(error=out["error"])
    finally:
        # Always terminate (error or success). Success already called terminate_now.
        if sess is not None:
            try:
                if out.get("ok") and not out.get("killed_on_success") and stills_count > 0:
                    # Partial failure mid-generate — kill immediately.
                    sess.terminate_now(reason="stills_error_or_incomplete")
                sess.__exit__(None, None, None)
            except Exception as term_exc:  # noqa: BLE001
                typer.echo(f"WARN session exit: {term_exc}", err=True)
            out["terminated_on_exit"] = True
    return out


if __name__ == "__main__":
    # Allow: python -m src.cli.runpod_smoke --voice
    if len(sys.argv) > 1 and sys.argv[1] not in {"list", "smoke", "--help", "-h"}:
        sys.argv.insert(1, "smoke")
    app()
