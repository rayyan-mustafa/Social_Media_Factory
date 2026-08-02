"""S3-compatible object storage (MinIO)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import boto3
from botocore.client import Config

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)


class ObjectStorage:
    def __init__(self) -> None:
        settings = get_settings()
        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4"),
        )

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            logger.info("creating_bucket", extra={"bucket": self.bucket})
            self._client.create_bucket(Bucket=self.bucket)

    @staticmethod
    def checksum_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def upload_file(self, local_path: Path, key: str, content_type: str | None = None) -> str:
        if content_type:
            self._client.upload_file(
                str(local_path),
                self.bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        else:
            self._client.upload_file(str(local_path), self.bucket, key)
        logger.info("s3_upload", extra={"key": key})
        return key

    def download_file(self, key: str, local_path: Path) -> Path:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, key, str(local_path))
        return local_path

    def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def delete_prefix(self, prefix: str) -> int:
        """Deletes all objects in the bucket that match the given prefix."""
        deleted_count = 0
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            if "Contents" in page:
                objects_to_delete = [{"Key": obj["Key"]} for obj in page["Contents"]]
                self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": objects_to_delete, "Quiet": True},
                )
                deleted_count += len(objects_to_delete)
        logger.info("s3_delete_prefix", extra={"prefix": prefix, "deleted_count": deleted_count})
        return deleted_count
