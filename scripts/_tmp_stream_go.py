#!/usr/bin/env python3
"""One-shot VPS helper: check RTMP keys (presence only), pick viral VOD, prep playlists."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "new_yt_automation"
os.chdir(ROOT)


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def check_keys() -> dict:
    vals = parse_env(ROOT / ".env")
    report = {}
    for k in (
        "YT_LIVE_RTMP_KEY_NAPSTORIAN",
        "YT_LIVE_RTMP_KEY_NAPPING_HISTORIAN",
        "YT_LIVE_RTMP_URL_NAPSTORIAN",
        "YT_LIVE_RTMP_URL_NAPPING_HISTORIAN",
        "VOD_LOOP_ENABLED",
        "VOD_LOOP_BITRATE_K",
        "VOD_LOOP_ADAPTIVE",
    ):
        v = (vals.get(k) or "").strip()
        if "KEY" in k:
            report[k] = {"set": bool(v), "len": len(v)}
        elif "URL" in k:
            report[k] = {"set": bool(v), "rtmp": v.startswith("rtmp") if v else False}
        else:
            report[k] = v
    report["env_exists"] = (ROOT / ".env").exists()
    report["rtmp_related_keys"] = sorted(
        k for k in vals if "RTMP" in k or k.startswith("VOD_LOOP")
    )
    return report


def load_winners() -> list[dict]:
    p = ROOT / "output" / "ops" / "benchmarks.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    cw = d.get("channel_winners") if isinstance(d.get("channel_winners"), dict) else {}
    top = cw.get("top")
    if not isinstance(top, list):
        return []
    out = []
    for item in top:
        if isinstance(item, dict) and item.get("video_id"):
            out.append(item)
    return out


def find_channel_for_id(channel_id: str) -> str | None:
    # Search known config / oauth / ops for mapping without printing secrets.
    needles = {
        "napstorian": "napstorian",
        "napping_historian": "napping_historian",
        "napping-historian": "napping_historian",
    }
    candidates = list((ROOT / "config").rglob("*")) + list((ROOT / "output" / "ops").glob("*.json"))
    for p in candidates:
        if not p.is_file() or p.stat().st_size > 5_000_000:
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if channel_id and channel_id in t:
            low = t.lower()
            # Prefer nearby channel name
            for needle, name in needles.items():
                if needle in low:
                    return name
    # env mapping
    vals = parse_env(ROOT / ".env")
    for k, v in vals.items():
        if channel_id and channel_id in v:
            kl = k.lower()
            if "napstor" in kl:
                return "napstorian"
            if "historian" in kl:
                return "napping_historian"
    return None


def scan_jobs_for_video_ids(video_ids: list[str]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {vid: [] for vid in video_ids}
    jobs = ROOT / "output" / "jobs"
    if not jobs.exists():
        return hits
    for job_dir in jobs.iterdir():
        if not job_dir.is_dir():
            continue
        for mf in job_dir.rglob("*"):
            if not mf.is_file():
                continue
            if mf.suffix.lower() not in {".json", ".txt", ".md", ".yml", ".yaml"}:
                continue
            if mf.stat().st_size > 2_000_000:
                continue
            try:
                t = mf.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for vid in video_ids:
                if vid in t:
                    hits[vid].append(mf.as_posix())
    return hits


def write_playlist(channel: str, video_path: Path) -> Path:
    playlist = ROOT / "config" / "streaming" / f"playlist_{channel}.txt"
    # ffmpeg concat demuxer wants absolute path, single-quoted escaped
    abs_path = video_path.resolve().as_posix()
    # Escape single quotes for concat demuxer: ' -> '\''
    esc = abs_path.replace("'", r"'\''")
    playlist.write_text(f"file '{esc}'\n", encoding="utf-8")
    return playlist


def ensure_assets_dir() -> Path:
    d = ROOT / "output" / "streaming_assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_own_vod(video_id: str, out_dir: Path) -> Path | None:
    out = out_dir / f"{video_id}.mp4"
    if out.exists() and out.stat().st_size > 100_000:
        return out
    # yt-dlp preferred
    cmd = [
        "yt-dlp",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        str(out),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        print(json.dumps({"error": "yt-dlp_not_found"}))
        return None
    if r.returncode != 0 or not out.exists():
        print(
            json.dumps(
                {
                    "download_failed": True,
                    "video_id": video_id,
                    "rc": r.returncode,
                    "stderr_tail": (r.stderr or "")[-800:],
                }
            )
        )
        return None
    return out


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        print(json.dumps(check_keys(), indent=2))
        return 0

    if action == "pick":
        winners = load_winners()
        bench = json.loads((ROOT / "output" / "ops" / "benchmarks.json").read_text())
        cw = bench.get("channel_winners") or {}
        channel_id = cw.get("channel_id")
        mapped = find_channel_for_id(channel_id) if channel_id else None
        video_ids = [w["video_id"] for w in winners[:8]]
        hits = scan_jobs_for_video_ids(video_ids)
        # also title fuzzy against job folder names
        title_hits = []
        for w in winners[:5]:
            title = (w.get("title") or "").lower()
            tokens = [t for t in re.split(r"[^a-z0-9]+", title) if len(t) > 4][:6]
            for job in (ROOT / "output" / "jobs").iterdir():
                if not job.is_dir():
                    continue
                name = job.name.lower()
                score = sum(1 for t in tokens if t in name)
                final = job / "video" / "final.mp4"
                if score >= 2 and final.exists():
                    title_hits.append(
                        {
                            "video_id": w.get("video_id"),
                            "title": w.get("title"),
                            "views": w.get("views"),
                            "job": job.name,
                            "score": score,
                            "final": final.as_posix(),
                            "size": final.stat().st_size,
                        }
                    )
        print(
            json.dumps(
                {
                    "channel_id": channel_id,
                    "mapped_channel": mapped,
                    "winners": [
                        {
                            "video_id": w.get("video_id"),
                            "title": w.get("title"),
                            "views": w.get("views"),
                            "meta_hits": hits.get(w.get("video_id") or "", [])[:5],
                        }
                        for w in winners[:8]
                    ],
                    "title_hits": title_hits[:10],
                    "finals": [
                        {
                            "job": p.parent.parent.name,
                            "path": p.as_posix(),
                            "size": p.stat().st_size,
                        }
                        for p in sorted(
                            (ROOT / "output" / "jobs").glob("*/video/final.mp4"),
                            key=lambda x: x.stat().st_size,
                            reverse=True,
                        )[:15]
                    ],
                },
                indent=2,
            )
        )
        return 0

    if action == "prep":
        # args: prep <channel> <video_id> [optional local final path]
        channel = sys.argv[2]
        video_id = sys.argv[3]
        local = Path(sys.argv[4]) if len(sys.argv) > 4 else None
        assets = ensure_assets_dir()
        if local and local.exists() and local.stat().st_size > 100_000:
            video_path = local
            source = "local_final"
        else:
            video_path = download_own_vod(video_id, assets)
            source = "yt_dlp"
            if video_path is None:
                return 2
        playlist = write_playlist(channel, video_path)
        # ensure bitrate policy rung ~4000
        policy_path = ROOT / "output" / "ops" / "stream_bitrate_policy.json"
        if policy_path.exists():
            pol = json.loads(policy_path.read_text(encoding="utf-8"))
            ch = pol.setdefault("channels", {}).setdefault(channel, {})
            ch["rung"] = 2
            ch["bitrate_k"] = 4000
            ch["height"] = 720
            ch["last_action"] = "manual_stream_go"
            policy_path.write_text(json.dumps(pol, indent=2) + "\n", encoding="utf-8")
        # ensure VOD_LOOP_ENABLED in report only
        print(
            json.dumps(
                {
                    "ok": True,
                    "channel": channel,
                    "video_id": video_id,
                    "source": source,
                    "video_path": str(video_path),
                    "size": video_path.stat().st_size,
                    "playlist": str(playlist),
                    "playlist_body": playlist.read_text(encoding="utf-8").strip(),
                },
                indent=2,
            )
        )
        return 0

    print("usage: check|pick|prep", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
