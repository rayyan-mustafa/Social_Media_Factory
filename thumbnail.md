# Cursor Build Prompt: Thumbnail Generator Agent (High-CTR, A/B Ready)

Paste this into Cursor to implement:

---

Build a Python module called `thumbnail_generator.py` that extracts dramatic candidate frames from a video and composites multiple thumbnail variants for A/B testing.

## Requirements

### 1. Input
A function `generate_thumbnails(video_path: str, hook_text_options: list[str], output_dir: str, num_variants: int = 3) -> list[dict]` where:
- `video_path`: path to the source video file
- `hook_text_options`: list of short text overlays (3-5 words each) — can come from `hook_generator.py` output, reuse the "hook" field trimmed down
- `output_dir`: where composited thumbnails get saved
- `num_variants`: how many final thumbnail combos to produce

### 2. Stage A — Candidate frame extraction (ffmpeg-based)
- Use ffmpeg scene detection to pull high-motion/high-contrast frame candidates:
  `ffmpeg -i {video_path} -vf "select='gt(scene,0.3)',showinfo" -vsync vfr {output_dir}/candidates/frame_%03d.jpg`
- Also extract frames at moments flagged as emotionally intense (if transcript timestamps for hook moments are available from `hook_generator.py`, pull frames near those timestamps specifically — this ties thumbnail directly to the hook narrative)
- Score candidates using basic image analysis (not ML, just heuristics):
  - Face detection (OpenCV Haar cascade or a lightweight face detector) — prioritize frames with a clear, large face
  - Contrast score (standard deviation of pixel brightness) — prioritize higher contrast frames
  - Reject frames that are too dark/blurry (Laplacian variance blur check)

### 3. Stage B — Compositing (Pillow-based)
For each selected candidate frame + each hook_text option:
1. Apply a vignette (darken edges, keep center/face bright) using PIL ImageEnhance + a radial gradient mask
2. Boost contrast/saturation slightly (PIL ImageEnhance.Contrast, Color)
3. Overlay text:
   - Max 3-5 words (truncate/reject longer input, log a warning)
   - Bold sans-serif font (bundle a font file like Anton, Bebas Neue, or Montserrat Black — these read as "punchy" at small sizes)
   - Yellow or white fill with black outline/stroke (high contrast against most backgrounds) — implement via multiple stroke-offset draws or PIL's stroke_width if using a recent Pillow version
   - Position text in upper or lower third, avoiding face overlap — check face bounding box from Stage A and place text in the opposite zone
4. Save each combo as `{output_dir}/variants/thumb_{frame_id}_{text_id}.jpg`

### 4. Stage C — Variant selection for A/B test
- Output `num_variants` final picks (default 3), chosen for maximum diversity: different frame + different hook text + different color grade intensity per variant, not just random combos
- Return metadata per variant so it can be logged and tracked against actual CTR later

### 5. Output
```python
[
  {
    "variant_path": str,
    "source_frame": str,
    "hook_text_used": str,
    "face_detected": bool,
    "contrast_score": float
  },
  ...
]
```

### 6. CTR feedback loop (log-only, no auto-optimization yet)
- Write each published variant + its assigned video ID to `thumbnail_log.jsonl`
- Leave a placeholder function `record_ctr_result(video_id: str, variant_path: str, ctr: float)` — to be called later once YouTube Studio A/B test data or manual CTR check is available
- This log becomes the training data for eventually scoring which face-crop style / color grade / text pattern wins for YOUR audience specifically — don't skip logging even before you have a feedback mechanism wired up

### 7. Integration point
Runs after `hook_generator.py` produces hook text options and after the raw video file exists (post-edit, pre-publish). Feeds into your policy agent as a final pre-publish check (thumbnail also needs to pass copyright/asset rules from `asset_verifier.py` if the frame source or overlay art originated from an external asset rather than your own video).

### 8. Dependencies
`opencv-python` (face detection, blur/contrast scoring), `Pillow` (compositing/text), `ffmpeg-python` or subprocess calls to your existing ffmpeg setup. No paid APIs needed — this is fully self-hostable on your VPS/RunPod.

---

Test this against 2-3 of your actual past videos first. Manually review the output variants before trusting the heuristics — face detection + contrast scoring is a decent starting filter but won't perfectly replicate human judgment on "which frame looks most dramatic." Expect to tune the scene-detection threshold (the `0.3` value) per video style.
