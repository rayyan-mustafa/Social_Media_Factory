"""PublishModule — YouTube Data API private upload + AI disclosure (Plan C Module 5).

v1 locks:
- privacyStatus=private always on first upload
- Gate A must pass (or dry-run)
- AI / altered-content disclosure in description + containsSyntheticMedia
- public promote is a separate explicit step (blocked until Policy/Trends later)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.domain.models import GateAResult, GateBResult, PublishResult, ScriptResult
from src.services.gate_a import run_gate_a
from src.services.gate_retention import run_gate_r
from src.services.settings import ROOT, get_settings
from src.services.youtube_meta import load_youtube_meta

# Scope notes (YouTube Data API v3 + Analytics):
# - youtube.upload: videos.insert (upload) + limited metadata on own uploads
# - youtube.force-ssl: videos.update (status/privacy/publishAt schedule),
#   commentThreads.insert + pin, thumbnails, broader manage
# - yt-analytics.readonly: CTR / AVD / impressions for SMM scorecard
# youtube.upload alone is NOT enough for schedule arm or SMM pin (403 insufficient
# authentication scopes on videos?part=status / commentThreads).
# Refresh cannot upgrade scopes — re-run youtube_auth after AUTH_SCOPES changes.
# _build_youtube_client loads scopes embedded in the token (avoids invalid_scope).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
]
# Full auth for youtube_auth CLI — upload + schedule + pin + Analytics.
AUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
# Subset that schedule/pin need beyond bare upload.
MANAGE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube",  # accepted alternate
]
ANALYTICS_SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
# Shorts player "Related video" (link below channel handle). Not listed in the
# public discovery doc as of 2026-08; wired best-effort on insert/update.
SHORTS_RELATED_VIDEO_FIELD = "relatedVideoId"


class PublishModuleError(RuntimeError):
    pass


class PublishModule:
    def __init__(self, channel: str | None = None):
        get_settings.cache_clear()
        self.s = get_settings()
        from src.services.youtube_channel_auth import normalize_youtube_channel

        self.channel = normalize_youtube_channel(channel)

    def publish_private(
        self,
        *,
        final_path: Path | str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        thumbnail_path: Path | str | None = None,
        youtube_meta_dir: Path | str | None = None,
        script_path: Path | str | None = None,
        visual_manifest: Path | str | None = None,
        job_dir: Path | str | None = None,
        channel: str | None = None,
        dry_run: bool = False,
        allow_short: bool = False,
        allow_placeholders: bool = False,
        shorts_mode: bool = False,
        related_video_id: str | None = None,
    ) -> PublishResult:
        if channel is not None:
            from src.services.youtube_channel_auth import normalize_youtube_channel

            self.channel = normalize_youtube_channel(channel)
        final_path = Path(final_path)
        if not final_path.exists():
            raise PublishModuleError(f"final.mp4 missing: {final_path}")

        script: ScriptResult | None = None
        if script_path:
            sp = Path(script_path)
            if sp.exists():
                script = ScriptResult.model_validate(
                    json.loads(sp.read_text(encoding="utf-8"))
                )

        # Prefer explicit youtube_meta/ pack (title, description, tags, thumbnail).
        pack: dict[str, Any] = {}
        meta_dir = Path(youtube_meta_dir) if youtube_meta_dir else None
        if meta_dir is None and job_dir is not None:
            cand = Path(job_dir) / "youtube_meta"
            if cand.is_dir():
                meta_dir = cand
        if meta_dir is not None and meta_dir.is_dir():
            pack = load_youtube_meta(meta_dir)

        title = (
            title
            or pack.get("title")
            or (script.title if script else "")
            or final_path.stem
        ).strip()
        if not title:
            raise PublishModuleError("title required")

        body_desc = description if description is not None else pack.get("description")
        if body_desc is None and script is not None:
            body_desc = self._default_description(script)
        if body_desc is None:
            body_desc = title
        body_desc = self._ensure_ai_disclosure(body_desc)

        tag_list = tags if tags is not None else (pack.get("tags") or None)
        if not tag_list:
            tag_list = self._default_tags(script)

        thumb = Path(thumbnail_path) if thumbnail_path else None
        if thumb is None and pack.get("thumbnail_path"):
            thumb = Path(pack["thumbnail_path"])
        if thumb is not None and not thumb.exists():
            raise PublishModuleError(f"thumbnail missing: {thumb}")

        if shorts_mode:
            from src.services.gate_shorts import run_gate_s

            gate = run_gate_s(final_path, enforce_duration=True)
            if not gate.ok:
                raise PublishModuleError(
                    "Gate S FAILED — no Shorts upload:\n- " + "\n- ".join(gate.errors)
                )
        else:
            gate = run_gate_a(
                final_path,
                visual_manifest=visual_manifest,
                # allow_short forces skip; otherwise honor GATE_A_ENFORCE_DURATION
                enforce_duration=False if allow_short else None,
                allow_placeholders=allow_placeholders,
            )
            if not gate.ok:
                raise PublishModuleError(
                    "Gate A FAILED — no upload:\n- " + "\n- ".join(gate.errors)
                )

        # Gate R is advisory/soft unless GATE_R_ENFORCE=true (default false).
        # Soft mode still runs checks and stamps warnings; it must not hold or
        # block private upload when final.mp4 already exists.
        # Re-read .env at check time so long-lived farm PIDs with a stale
        # process GATE_R_ENFORCE=true still honor the current soft setting.
        from src.services.gate_r_env import gate_r_enforce_enabled

        gate_r_enforce = gate_r_enforce_enabled()
        if shorts_mode:
            gate_r_enforce = False
        dedupe_removed = None
        if script is not None:
            dedupe_removed = int((script.meta or {}).get("dedupe_removed") or 0)
        gate_r = run_gate_r(
            final_path=final_path,
            script_path=script_path,
            voice_manifest=job_dir / "audio" / "mixed" / "voice_manifest_mixed.json"
            if job_dir and (Path(job_dir) / "audio" / "mixed" / "voice_manifest_mixed.json").exists()
            else (Path(job_dir) / "audio" / "voice_manifest.json" if job_dir else None),
            job_dir=job_dir,
            enforce=gate_r_enforce and not allow_short,
            dedupe_removed=dedupe_removed,
        )
        if not gate_r.ok and gate_r_enforce:
            raise PublishModuleError(
                "Gate R FAILED — retention checks:\n- "
                + "\n- ".join(gate_r.errors)
            )

        # Gate B — Policy Agent (Plan C Module 6)
        from src.agents.policy_agent import PolicyAgent

        gate_b_enforce = bool(self.s.gate_b_enforce) and not allow_short
        try:
            gate_b_raw = PolicyAgent().run_gate_b(
                script=script,
                title=title,
                description=body_desc,
                ai_disclosure=True,
                made_for_kids=self.s.youtube_made_for_kids,
                refresh_if_stale=True,
            )
            gate_b = GateBResult(
                ok=gate_b_raw.ok,
                policy_version=gate_b_raw.policy_version,
                errors=list(gate_b_raw.errors),
                warnings=list(gate_b_raw.warnings),
                checks=dict(gate_b_raw.checks),
            )
        except Exception as exc:  # noqa: BLE001
            gate_b = GateBResult(
                ok=False,
                errors=[f"policy agent error: {exc}"],
            )
        if not gate_b.ok and gate_b_enforce:
            raise PublishModuleError(
                "Gate B FAILED — Policy Agent:\n- " + "\n- ".join(gate_b.errors)
            )

        out_dir = Path(job_dir) if job_dir else final_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        thumb_meta = {
            "thumbnail_path": str(thumb.resolve()) if thumb else None,
            "youtube_meta_dir": str(meta_dir.resolve()) if meta_dir else None,
        }

        if dry_run:
            result = PublishResult(
                ok=True,
                dry_run=True,
                privacy_status="private",
                video_id=None,
                watch_url=None,
                title=title,
                description=body_desc,
                tags=tag_list,
                final_path=str(final_path.resolve()),
                gate_a=gate,
                gate_b=gate_b,
                ai_disclosure=True,
                contains_synthetic_media=True,
                meta={
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "note": "dry_run — no YouTube API call",
                    "gate_r_ok": gate_r.ok,
                    "gate_r_warnings": gate_r.warnings,
                    "gate_r_errors": gate_r.errors,
                    "gate_b_ok": gate_b.ok,
                    "gate_b_warnings": gate_b.warnings,
                    "gate_b_errors": gate_b.errors,
                    SHORTS_RELATED_VIDEO_FIELD: (related_video_id or "").strip() or None,
                    **thumb_meta,
                },
            )
            manifest = out_dir / "publish_manifest.json"
            manifest.write_text(
                json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            result.publish_manifest_path = str(manifest.resolve())
            return result

        youtube = self._build_youtube_client()
        video_id = self._upload(
            youtube,
            final_path=final_path,
            title=title,
            description=body_desc,
            tags=tag_list,
            privacy=self.s.youtube_default_privacy or "private",
            related_video_id=related_video_id if shorts_mode else None,
        )
        related_ok = False
        related_err: str | None = None
        if shorts_mode and related_video_id and (related_video_id or "").strip():
            try:
                rel = self.set_shorts_related_video(
                    video_id,
                    (related_video_id or "").strip(),
                    youtube=youtube,
                )
                related_ok = bool(rel.get("ok"))
                related_err = rel.get("error")
            except Exception as exc:  # noqa: BLE001
                related_err = str(exc)[:300]
        thumb_ok = False
        thumb_err: str | None = None
        if thumb is not None:
            try:
                self._set_thumbnail(youtube, video_id=video_id, thumbnail_path=thumb)
                thumb_ok = True
            except Exception as exc:  # noqa: BLE001
                thumb_err = str(exc)
        watch = f"https://www.youtube.com/watch?v={video_id}"
        result = PublishResult(
            ok=True,
            dry_run=False,
            privacy_status=self.s.youtube_default_privacy or "private",
            video_id=video_id,
            watch_url=watch,
            title=title,
            description=body_desc,
            tags=tag_list,
            final_path=str(final_path.resolve()),
            gate_a=gate,
            gate_b=gate_b,
            ai_disclosure=True,
            contains_synthetic_media=True,
            meta={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "category_id": self.s.youtube_category_id,
                "gate_r_ok": gate_r.ok,
                "gate_r_warnings": gate_r.warnings,
                "gate_r_errors": gate_r.errors,
                "gate_b_ok": gate_b.ok,
                "gate_b_warnings": gate_b.warnings,
                "gate_b_errors": gate_b.errors,
                SHORTS_RELATED_VIDEO_FIELD: (related_video_id or "").strip() or None,
                "related_video_set": related_ok,
                "related_video_error": related_err,
                **thumb_meta,
                "thumbnail_uploaded": thumb_ok,
                "thumbnail_error": thumb_err,
            },
        )
        manifest = out_dir / "publish_manifest.json"
        manifest.write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        result.publish_manifest_path = str(manifest.resolve())
        return result

    def promote_public(self, video_id: str, *, force: bool = False) -> dict[str, Any]:
        """Explicit public promote — requires Policy Gate B + human unless force."""
        if not force:
            from src.agents.policy_agent import PolicyAgent

            gate_b = PolicyAgent().run_gate_b(
                title=video_id,
                description=self.s.youtube_ai_disclosure_text,
                ai_disclosure=True,
                refresh_if_stale=True,
            )
            if not gate_b.ok:
                raise PublishModuleError(
                    "Public promote blocked — Gate B:\n- "
                    + "\n- ".join(gate_b.errors)
                )
            raise PublishModuleError(
                "Public promote blocked (Plan C): human approve required after Gate B. "
                "Pass force=True only after you reviewed the private upload."
            )
        self.require_manage_scopes(op="promote_public / videos.update status")
        youtube = self._build_youtube_client()
        body = {"id": video_id, "status": {"privacyStatus": "public"}}
        resp = (
            youtube.videos()
            .update(part="status", body=body)
            .execute()
        )
        return {"video_id": video_id, "privacyStatus": "public", "raw": resp}

    def _default_description(self, script: ScriptResult) -> str:
        """Fallback body when youtube_meta is missing — same structure as packaging."""
        hook = script.hook.strip() if script.hook else script.title
        title = (script.title or "").strip()
        lines: list[str] = [hook]
        if title and title.lower() != hook.lower():
            lines.append(title)
        lines.extend(
            [
                "",
                "Explore this alternate-history documentary and decide where the "
                "timeline really turns.",
                "",
                "If you enjoy grounded what-if history, subscribe and share your "
                "counterfactual in the comments.",
                "",
                "Chapters:",
            ]
        )
        for ch in script.outline.chapters:
            lines.append(f"- {ch.title}")
        if script.outline.closer:
            closer = script.outline.closer.strip()
            if closer and "final visual" not in closer.lower():
                lines.append("")
                lines.append(closer)
        return "\n".join(lines).strip()

    def _default_tags(self, script: ScriptResult | None) -> list[str]:
        from src.services.youtube_meta import normalize_youtube_tags

        tags = [
            "history",
            "documentary",
            "alternate history",
            "what if history",
            "historical documentary",
        ]
        if script and script.topic:
            tags.append(script.topic[:40])
        if script and script.title:
            tags.append(script.title[:40])
        return normalize_youtube_tags(tags)

    def _ensure_ai_disclosure(self, description: str) -> str:
        from src.services.youtube_meta import append_youtube_ai_disclosure

        return append_youtube_ai_disclosure(
            description,
            self.s.youtube_ai_disclosure_text,
        )

    def _load_credentials(self):
        """Load/refresh OAuth credentials for this channel (scopes from token)."""
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise PublishModuleError(
                "YouTube libs missing. Run: "
                ".venv/bin/pip install google-api-python-client "
                "google-auth-oauthlib google-auth-httplib2"
            ) from exc

        from src.services.youtube_channel_auth import (
            youtube_client_secrets_path,
            youtube_token_path,
        )

        secrets = youtube_client_secrets_path(self.channel)
        token_path = youtube_token_path(self.channel)

        # Token alone is enough for upload/refresh (it embeds client_id/secret).
        # Client secrets file is only required for first-time youtube_auth.
        if not token_path.exists() and not secrets.exists():
            raise PublishModuleError(
                f"Missing YouTube OAuth client secrets: {secrets}\n"
                "Create a Google Cloud OAuth Desktop client, download JSON, save as that path, "
                f"then run: .venv/bin/python -m src.cli.youtube_auth --channel {self.channel}"
            )

        creds = None
        if token_path.exists():
            # Use scopes stored in the token so refresh never asks for more
            # than was granted (avoids invalid_scope after partial auth).
            creds = Credentials.from_authorized_user_file(str(token_path))
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                raise PublishModuleError(
                    f"No valid YouTube token for channel={self.channel} at {token_path}. "
                    f"Run: .venv/bin/python -m src.cli.youtube_auth --channel {self.channel}"
                )
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")

        return creds

    def _build_youtube_client(self):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise PublishModuleError(
                "YouTube libs missing. Run: "
                ".venv/bin/pip install google-api-python-client "
                "google-auth-oauthlib google-auth-httplib2"
            ) from exc

        creds = self._load_credentials()
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def has_analytics_scopes(self) -> bool:
        from src.services.youtube_analytics import token_has_analytics_scope

        return token_has_analytics_scope(self.token_scopes())

    @staticmethod
    def _shorts_related_video_payload(*, related_video_id: str) -> dict[str, str]:
        vid = (related_video_id or "").strip()
        if not vid:
            return {}
        return {SHORTS_RELATED_VIDEO_FIELD: vid}

    def set_shorts_related_video(
        self,
        short_video_id: str,
        related_video_id: str,
        *,
        youtube=None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Set Shorts player Related video (link below channel handle).

        Requires youtube.force-ssl on the channel token. Field is best-effort —
        not documented in the public Data API discovery schema yet.
        """
        short_id = (short_video_id or "").strip()
        related_id = (related_video_id or "").strip()
        if not short_id or not related_id:
            return {"ok": True, "skipped": True, "reason": "missing video id"}
        if channel is not None:
            from src.services.youtube_channel_auth import normalize_youtube_channel

            self.channel = normalize_youtube_channel(channel)
        self.require_manage_scopes(op="set_shorts_related_video / videos.update")
        yt = youtube or self._build_youtube_client()
        body: dict[str, Any] = {
            "id": short_id,
            **self._shorts_related_video_payload(related_video_id=related_id),
        }
        try:
            resp = yt.videos().update(part="snippet", body=body).execute()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "video_id": short_id,
                SHORTS_RELATED_VIDEO_FIELD: related_id,
                "error": str(exc)[:500],
            }
        return {
            "ok": True,
            "video_id": short_id,
            SHORTS_RELATED_VIDEO_FIELD: related_id,
            "raw": {"id": resp.get("id")},
        }

    def _upload(
        self,
        youtube,
        *,
        final_path: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy: str,
        related_video_id: str | None = None,
    ) -> str:
        from googleapiclient.http import MediaFileUpload

        body: dict[str, Any] = {
            "snippet": {
                "title": title[:100],
                "description": description[:4900],
                "tags": tags,
                "categoryId": str(self.s.youtube_category_id),
            },
            "status": {
                "privacyStatus": privacy if privacy in {"private", "unlisted", "public"} else "private",
                "selfDeclaredMadeForKids": bool(self.s.youtube_made_for_kids),
                # AI / altered content disclosure (YouTube Data API)
                "containsSyntheticMedia": True,
            },
        }
        rel_payload = self._shorts_related_video_payload(
            related_video_id=(related_video_id or "")
        )
        if rel_payload:
            body.update(rel_payload)
        media = MediaFileUpload(str(final_path), mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=media
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            # status may be None between chunks
            _ = status
        video_id = response.get("id")
        if not video_id:
            raise PublishModuleError(f"upload returned no id: {response}")
        return str(video_id)

    def update_packaging(
        self,
        video_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        thumbnail_path: Path | str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Patch an existing private/public video's snippet + optional thumbnail."""
        from src.services.youtube_meta import normalize_youtube_tags

        if channel is not None:
            from src.services.youtube_channel_auth import normalize_youtube_channel

            self.channel = normalize_youtube_channel(channel)
        youtube = self._build_youtube_client()
        out: dict[str, Any] = {"video_id": video_id}

        if title is not None or description is not None or tags is not None:
            # videos.update requires full snippet for the parts you send — fetch first
            existing = (
                youtube.videos()
                .list(part="snippet", id=video_id)
                .execute()
                .get("items")
                or []
            )
            if not existing:
                raise PublishModuleError(f"video not found: {video_id}")
            snip = dict(existing[0].get("snippet") or {})
            if title is not None:
                snip["title"] = title[:100]
            if description is not None:
                snip["description"] = self._ensure_ai_disclosure(description)[:4900]
            if tags is not None:
                snip["tags"] = normalize_youtube_tags(tags)
            # categoryId required on update
            if not snip.get("categoryId"):
                snip["categoryId"] = str(self.s.youtube_category_id)
            body = {"id": video_id, "snippet": snip}
            youtube.videos().update(part="snippet", body=body).execute()
            out["snippet_updated"] = True
            out["tags"] = snip.get("tags") or []
            out["title"] = snip.get("title")

        if thumbnail_path is not None:
            thumb = Path(thumbnail_path)
            if not thumb.exists():
                raise PublishModuleError(f"thumbnail missing: {thumb}")
            self._set_thumbnail(youtube, video_id=video_id, thumbnail_path=thumb)
            out["thumbnail_uploaded"] = True
            out["thumbnail_path"] = str(thumb.resolve())

        return out


    def token_scopes(self) -> list[str]:
        """Return scopes embedded in this channel's token (may be empty if missing)."""
        from src.services.youtube_channel_auth import youtube_token_path

        token_path = youtube_token_path(self.channel)
        if not token_path.exists():
            return []
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        scopes = data.get("scopes") or data.get("scope") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        return [str(s).strip() for s in scopes if str(s).strip()]

    def has_manage_scopes(self) -> bool:
        """True if token can schedule status / pin comments (force-ssl or youtube)."""
        have = set(self.token_scopes())
        return bool(have.intersection(MANAGE_SCOPES))

    def require_manage_scopes(self, *, op: str) -> None:
        if self.has_manage_scopes():
            return
        have = self.token_scopes() or ["(none)"]
        raise PublishModuleError(
            f"{op} needs youtube.force-ssl (or youtube) OAuth scope; "
            f"channel={self.channel} token currently has: {', '.join(have)}. "
            f"Re-auth: .venv/bin/python -m src.cli.youtube_auth --channel {self.channel} --print-url"
        )

    def schedule_publish_at(
        self,
        video_id: str,
        publish_at_iso: str,
        *,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Set private video to go public at publishAt (YouTube handles the flip).

        publish_at_iso: UTC timestamp like 2026-08-07T23:00:00Z
        Note: YouTube requires privacyStatus=private when publishAt is set.
        Requires AUTH_SCOPES (youtube.force-ssl) — youtube.upload alone → 403.
        """
        if channel is not None:
            from src.services.youtube_channel_auth import normalize_youtube_channel

            self.channel = normalize_youtube_channel(channel)
        self.require_manage_scopes(op="schedule_publish_at / videos.update status")
        youtube = self._build_youtube_client()
        body = {
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_at_iso,
                "selfDeclaredMadeForKids": bool(self.s.youtube_made_for_kids),
            },
        }
        resp = youtube.videos().update(part="status", body=body).execute()
        return {
            "video_id": video_id,
            "publishAt": publish_at_iso,
            "privacyStatus": "private",
            "raw": {"id": resp.get("id"), "status": resp.get("status")},
        }

    def _set_thumbnail(self, youtube, *, video_id: str, thumbnail_path: Path) -> None:
        from googleapiclient.http import MediaFileUpload

        suffix = thumbnail_path.suffix.lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/jpeg")
        media = MediaFileUpload(str(thumbnail_path), mimetype=mime, resumable=False)
        youtube.thumbnails().set(videoId=video_id, media_body=media).execute()


def run_oauth_console() -> Path:
    """Deprecated helper — use `python -m src.cli.youtube_auth` instead."""
    raise PublishModuleError("Use: .venv/bin/python -m src.cli.youtube_auth")
