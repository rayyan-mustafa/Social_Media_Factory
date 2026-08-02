"""YouTube Data API v3 uploader using stored OAuth refresh token (headless)."""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeUploader:
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

    def bootstrap_local(self) -> None:
        """One-time interactive auth (local only — not for VPS)."""
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets), SCOPES)
        creds = flow.run_local_server(port=0)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        logger.info("youtube_token_saved", extra={"path": str(self.token_file)})

    def upload(
        self,
        file_path: Path,
        title: str,
        description: str,
        tags: list[str] | None = None,
        category_id: str = "27",
    ) -> str:
        creds = self._load_credentials()
        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": (tags or [])[:20],
                "categoryId": category_id,
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
        return video_id

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
