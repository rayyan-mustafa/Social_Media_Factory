# ADR-003: Kokoro TTS instead of ElevenLabs

## Status
Accepted

## Context
The prototype already used Kokoro. Paid SaaS TTS increases per-video cost and hides ML integration skills.

## Decision
Default voice synthesis is **self-hosted Kokoro**. ElevenLabs is out of scope for v1.

## Consequences
- Pros: $0 TTS marginal cost; demonstrates local model serving; stronger Backend+ML story.
- Cons: voice quality/ops burden on the worker image; GPU optional for throughput.
