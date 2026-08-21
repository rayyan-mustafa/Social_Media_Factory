# ADR-001: ARQ + Redis instead of Celery

## Status
Accepted

## Context
The pipeline needs durable async job execution with retries, without the operational weight of Celery + RabbitMQ for a single-VPS control plane.

## Decision
Use **ARQ** on **Redis** for worker tasks (`run_pipeline`).

## Consequences
- Pros: small dependency surface, native async, simple ops on one VPS.
- Cons: fewer enterprise plugins than Celery; fine for v1.
- Migration path: Celery/Temporal if multi-tenant scale appears.
