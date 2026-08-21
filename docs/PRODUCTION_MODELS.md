# Production models — 9 live business lines

Canonical list: `src.domain.BUSINESS_MODELS`. Each model has its own ARQ queue
and Compose worker. This is the production pipeline (no bookmark / test profile).

## Delivery layout

| Model | Format | Worker service | Durable output |
|-------|--------|----------------|----------------|
| YouTube_Shorts | video | `worker_youtube_shorts` | **None** — temp render → direct YouTube upload |
| Web_Series | video | `worker_web_series` | `production/Web_Series/job_{id}/` |
| Sleep_Stories | video | `worker_sleep_stories` | `production/Sleep_Stories/job_{id}/` |
| Online_Courses_Teachable | video | `worker_online_courses` | `production/Online_Courses_Teachable/job_{id}/` |
| Podcast_Audio | audio | `worker_podcast_audio` | `production/Podcast_Audio/job_{id}/` |
| Radio_FM | audio | `worker_radio_fm` | `production/Radio_FM/job_{id}/` |
| Audiobooks_ACX | audio | `worker_audiobooks_acx` | `production/Audiobooks_ACX/job_{id}/` |
| SEO_Blogs | text | `worker_seo_blogs` | `production/SEO_Blogs/job_{id}/` |
| EBooks_KDP | text | `worker_ebooks_kdp` | `production/EBooks_KDP/job_{id}/` |

Object keys live in MinIO (`S3_BUCKET`). Path helpers: `src.core.paths`.

Typical job folder contents (non-YouTube):

```text
production/{BusinessModel}/job_{id}/
  script.json
  final.mp4 | final_audio.wav | document.md | document.pdf
  audio/…   images/…   scenes/…   (video models, when rendered locally)
```

YouTube_Shorts artifacts are recorded as `youtube:{video_id}` (watch URL via API),
not as MinIO objects.

## Start

```bash
docker compose up -d --build
# Optional growth daemons (Airtable / Apify / watchdog):
docker compose --profile growth up -d
```

Pipeline routes by format family via `src.services.formats.get_adapter`.
External storefront publish (Teachable / ACX / KDP) remains a future hook;
files are produced under the per-model prefixes above today.
