# ADR-002: MinIO (S3 API) for artifacts

## Status
Accepted

## Context
Videos, audio, and images must survive container restarts and be shareable between API, workers, and optional RunPod jobs. Google Drive / local disk paths are not production patterns.

## Decision
Store all artifacts in **MinIO** via the S3 API (`boto3`), keyed as `jobs/{id}/…`, with SHA-256 checksums in `job_artifacts`.

## Consequences
- Pros: industry-standard object storage API; easy swap to AWS S3 / OVH Object Storage later.
- Cons: another Compose service to operate; disk must be sized for retention policy.
