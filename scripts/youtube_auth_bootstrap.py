"""One-time local YouTube OAuth bootstrap.

Run on a machine with a browser, then copy youtube_token.json to the VPS /secrets.
"""

from pathlib import Path

from src.core.config import get_settings
from src.services.youtube_uploader import YouTubeUploader


def main() -> None:
    settings = get_settings()
    secrets = Path("secrets")
    secrets.mkdir(exist_ok=True)
    # Prefer local paths for bootstrap
    uploader = YouTubeUploader()
    uploader.client_secrets = Path(settings.youtube_client_secrets_file)
    if not uploader.client_secrets.exists():
        uploader.client_secrets = secrets / "client_secret.json"
    uploader.token_file = secrets / "youtube_token.json"
    if not uploader.client_secrets.exists():
        raise SystemExit(
            f"Place Google OAuth client_secret.json at {uploader.client_secrets}"
        )
    uploader.bootstrap_local()
    print(f"Saved refresh token to {uploader.token_file}")


if __name__ == "__main__":
    main()
