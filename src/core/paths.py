"""Production output paths for the 9 business models.

Layout (MinIO / local mirror):
  production/{BusinessModel}/job_{id}/...

YouTube_Shorts has NO production folder — render is temporary and uploads
directly to YouTube (see DIRECT_UPLOAD_MODELS).
"""

from __future__ import annotations

from src.domain import BUSINESS_MODELS, DIRECT_UPLOAD_MODELS

PRODUCTION_ROOT = "production"


def persists_files(business_model: str) -> bool:
    """False for YouTube_Shorts (direct upload); True for all other models."""
    return business_model not in DIRECT_UPLOAD_MODELS


def model_folder(business_model: str) -> str:
    if business_model not in BUSINESS_MODELS:
        raise ValueError(f"Unknown business_model: {business_model}")
    return f"{PRODUCTION_ROOT}/{business_model}"


def job_prefix(business_model: str, job_id: int) -> str:
    """S3 key prefix for a job's durable artifacts."""
    if not persists_files(business_model):
        raise ValueError(
            f"{business_model} does not use production folders (direct upload only)"
        )
    return f"{model_folder(business_model)}/job_{job_id}"


def artifact_key(business_model: str, job_id: int, filename: str) -> str:
    return f"{job_prefix(business_model, job_id)}/{filename}"
