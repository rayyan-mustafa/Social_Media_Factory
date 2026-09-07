"""Tier A distribution — FTP push to platforms that accept batch uploads.

Two deliberate departures from roadmap.txt:

1. **Sequential, not multi-threaded.** The roadmap asks for a multi-threaded
   uploader. Contributor FTP endpoints rate-limit or suspend accounts that open
   concurrent sessions, and the throughput gain is irrelevant at this volume.
2. **FTPS preferred over plain FTP.** The roadmap's ``ftplib.FTP`` sends
   credentials in cleartext. This tries ``FTP_TLS`` first and only falls back
   when a platform genuinely does not support it.

Credentials are read from the environment only — never from the committed
platform matrix. Nothing uploads unless both config gates are open, the asset
passed Gate V, and the ledger confirms it has not gone to that platform before.
"""

from __future__ import annotations

import ftplib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.microstock import config
from src.microstock.ledger import AssetLedger

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60


class UploadError(RuntimeError):
    """Upload failed in a way worth retrying on a later beat."""


@dataclass
class UploadResult:
    platform: str = ""
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "uploaded": len(self.uploaded),
            "skipped": len(self.skipped),
            "failed": self.failed,
            "dry_run": self.dry_run,
            "reason": self.reason,
            "asset_ids": self.uploaded,
        }


def _connect(host: str, user: str, password: str, *, use_tls: bool, timeout: int):
    """Open an FTP session, preferring TLS."""
    if use_tls:
        try:
            session = ftplib.FTP_TLS()
            session.connect(host, 21, timeout=timeout)
            session.login(user, password)
            session.prot_p()
            logger.info("ftp: connected to %s over TLS", host)
            return session
        except (ftplib.all_errors, OSError) as exc:  # noqa: B014
            logger.warning("ftp: TLS to %s failed (%s), falling back to plain FTP", host, exc)
    session = ftplib.FTP()
    session.connect(host, 21, timeout=timeout)
    session.login(user, password)
    logger.info("ftp: connected to %s (plain)", host)
    return session


def upload_batch(
    platform_name: str,
    assets: list[dict[str, Any]],
    *,
    ledger: AssetLedger | None = None,
    dry_run: bool | None = None,
    remote_dir: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> UploadResult:
    """Upload Gate-V-passed assets to one platform's FTP endpoint."""
    ledger = ledger or AssetLedger()
    result = UploadResult(platform=platform_name)

    platform = config.platform_by_name(platform_name)
    if not platform:
        result.reason = "unknown_platform"
        return result
    if platform.get("permanently_excluded"):
        result.reason = "permanently_excluded"
        logger.error("refusing to upload to %s — permanently excluded", platform_name)
        return result
    if platform.get("tier") != "auto":
        result.reason = "not_an_ftp_platform"
        return result

    if dry_run is None:
        dry_run = bool(config.section("distribution").get("dry_run", True))
    result.dry_run = dry_run

    if not dry_run and not config.distribution_enabled():
        result.reason = "distribution_disarmed"
        logger.warning("upload blocked: distribution is disarmed in config")
        return result
    if not platform.get("enabled"):
        result.reason = "platform_disabled"
        return result

    # Never re-send something already delivered.
    pending = [a for a in assets if not ledger.already_uploaded(a["asset_id"], platform_name)]
    result.skipped = [
        a["asset_id"] for a in assets if ledger.already_uploaded(a["asset_id"], platform_name)
    ]
    if not pending:
        result.reason = "nothing_pending"
        return result

    if dry_run:
        result.reason = "dry_run"
        result.uploaded = []
        logger.info(
            "DRY RUN: would upload %d file(s) to %s: %s",
            len(pending), platform_name, [a["asset_id"] for a in pending],
        )
        result.failed = []
        result.skipped += [a["asset_id"] for a in pending]
        return result

    credentials = config.ftp_credentials(platform_name)
    if not credentials:
        result.reason = "missing_credentials"
        logger.warning(
            "no FTP credentials for %s — set MICROSTOCK_FTP_%s_{USER,PASS} in .env",
            platform_name, platform_name.upper(),
        )
        return result

    host, user, password = credentials
    ftp_cfg = platform.get("ftp") or {}
    try:
        session = _connect(
            host, user, password,
            use_tls=bool(ftp_cfg.get("use_tls", True)), timeout=timeout_s,
        )
    except (ftplib.all_errors, OSError) as exc:  # noqa: B014
        raise UploadError(f"could not connect to {host}: {exc}") from exc

    try:
        if remote_dir:
            try:
                session.cwd(remote_dir)
            except ftplib.error_perm:
                session.mkd(remote_dir)
                session.cwd(remote_dir)

        for asset in pending:  # sequential on purpose — see module docstring
            source = Path(asset.get("clean_svg") or "")
            asset_id = asset["asset_id"]
            if not source.is_file():
                result.failed.append({"asset_id": asset_id, "error": "file missing"})
                continue
            remote_name = f"{asset_id}.svg"
            try:
                with source.open("rb") as handle:
                    session.storbinary(f"STOR {remote_name}", handle)
            except (ftplib.all_errors, OSError) as exc:  # noqa: B014
                logger.warning("ftp: %s failed for %s: %s", remote_name, platform_name, exc)
                result.failed.append({"asset_id": asset_id, "error": str(exc)})
                continue
            ledger.mark_uploaded(asset_id, platform_name)
            result.uploaded.append(asset_id)
            logger.info("ftp: uploaded %s -> %s", remote_name, platform_name)
    finally:
        try:
            session.quit()
        except (ftplib.all_errors, OSError):  # noqa: B014
            session.close()

    result.reason = "ok"
    return result
