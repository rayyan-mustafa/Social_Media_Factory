"""YouTube Tier A Distributor."""

import os
from pathlib import Path
from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.core.config import get_settings
from src.core.logging import get_logger
from src.db import Job
from src.db.repository import get_distribution, record_distribution
from src.domain import ArtifactKind
from src.services.distributors.base import Distributor, PublishResult

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeDistributor(Distributor):
    publish_mode: Literal["auto", "manual_review"] = "auto"
    platform: str = "youtube"

    def __init__(self) -> None:
        settings = get_settings()
        self.client_secrets = Path(settings.youtube_client_secrets_file)
        self.token_file = Path(settings.youtube_token_file)
        self.privacy = settings.youtube_privacy_status

    def _load_credentials(self) -> Credentials:
        creds: Credentials | None = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.token_file.write_text(creds.to_json(), encoding="utf-8")
            return creds
        raise FileNotFoundError(
            "Valid YouTube OAuth token not found. Run scripts/youtube_auth_bootstrap.py "
            "once on a machine with a browser, then copy youtube_token.json to /secrets."
        )

    async def publish(
        self, 
        session: AsyncSession, 
        job: Job, 
        artifacts: dict[ArtifactKind, list[Path]], 
        work_dir: Path
    ) -> PublishResult:
        
        # Idempotency check: has this already been successfully uploaded or is it pending approval?
        existing_dist = await get_distribution(session, job.id, self.platform)
        if existing_dist and existing_dist.status in ("success", "pending_approval"):
            logger.info("youtube_upload_idempotency_skip", extra={"job_id": job.id, "video_id": existing_dist.external_id})
            return PublishResult(
                status=existing_dist.status, # type: ignore
                platform=self.platform,
                external_id=existing_dist.external_id,
                manifest_path=existing_dist.manifest_path
            )

        video_paths = artifacts.get(ArtifactKind.VIDEO)
        if not video_paths:
            raise ValueError("No video artifact found for YouTube upload.")
        file_path = video_paths[0]
        
        creds = self._load_credentials()
        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {
                "title": job.topic[:100],
                "description": (job.script_json.get("title", "") if job.script_json else "")[:5000],
                "tags": [job.niche][:20],
                "categoryId": "27",
            },
            "status": {
                "privacyStatus": self.privacy,
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(file_path), chunksize=1024 * 1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("youtube_upload_progress", extra={"pct": int(status.progress() * 100)})
                
        video_id = response["id"]
        logger.info("youtube_upload_done", extra={"video_id": video_id})
        
        await record_distribution(
            session=session,
            job_id=job.id,
            platform=self.platform,
            status="success",
            external_id=video_id
        )

        return PublishResult(
            status="success",
            platform=self.platform,
            external_id=video_id
        )

    def check_processing_status(self, video_id: str) -> str:
        """Returns the processing status of a YouTube video (e.g. 'processing', 'succeeded', 'failed')."""
        creds = self._load_credentials()
        youtube = build("youtube", "v3", credentials=creds)
        request = youtube.videos().list(part="processingDetails", id=video_id)
        response = request.execute()
        
        items = response.get("items", [])
        if not items:
            return "not_found"
            
        processing_details = items[0].get("processingDetails", {})
        status = processing_details.get("processingStatus", "unknown")
        return status
