# Weird Biology Pipeline Diagnosis: Image-to-Scene Matching

**Date**: 2026-08-18  
**Scenario**: Video topic "Why Nobody Smiled In Old Photos" with scene "Nobody can hold a smile that long"  
**Issue**: Can the pipeline retrieve contextually appropriate images (historical photos) for each scene?

---

## **Executive Summary**

🔴 **CRITICAL GAP IDENTIFIED**: Your current weird_biology pipeline **cannot fetch contextual background images** for scenes. It relies on **hardcoded procedural backgrounds** (solid teal/cream colors) regardless of scene content.

| Aspect | Current State | Requirement for "Old Photos" Topic |
|--------|---------------|-----------------------------------|
| Background images | ❌ Hardcoded procedural colors | ✅ Historical photography backgrounds |
| Scene→Image matching | ❌ Absent | ✅ Match "old photo studio" → fetch era-appropriate photos |
| Visual query generation | ❌ None | ✅ Generate visual queries like "Victorian photography studio" |
| Image fetching APIs | ❌ None integrated | ✅ Wikimedia, Pexels, or historical archives |
| Scene-context awareness | ❌ Limited (keyword-only) | ✅ Analyze scene text to infer visual needs |

**Verdict**: ⚠️ **Needs tuning. Current logic is insufficient for this use case.**

---

## **Current Pipeline Architecture**

### Flow: Script → Timeline → Shots → Render

```
INPUT: "Why Nobody Smiled In Old Photos"
   ↓
[Script Stage] WeirdBiologyScriptWriter
   → Generates 5 sections: Hook, Setup, Core Data, Debate, Outro
   → Output: vo_raw.txt + script_sections_md
   ❌ No visual_query field added
   ↓
[TTS Stage] WeirdBiologyTTS
   → Synthesizes full VO with timing metadata
   → Output: full_vo.wav (e.g., 120 seconds)
   ↓
[Visual Timeline Stage] build_visual_timeline()
   → Splits VO into ~2-4s beats (32-50 phrases)
   → Creates VisualBeat objects: {index, text, section, duration_s, ...}
   ❌ No visual_query field created
   ↓
[Shot Planning] WeirdBiologyShotPlanner.plan_shots()
   → LLM analyzes each beat text
   → Returns: {pose, emotion, props, kinetic_text, xray, layout, camera}
   ❌ No visual_query field generated
   ✅ Keyword rules: if "door" in text → layout="crib_door_scene"
   ✅ But: layout only switches color palette (teal→cream), not images
   ↓
[Rendering] Two backends available:
   
   A) SVG Stickman (Default)
      → src/services/weird_biology_stickman.py
      → Procedural SVG drawing
      → Background: Solid color from palette (teal or cream)
      → ❌ No image fetching
      
   B) PNG Compositor
      → src/services/weird_biology_compositor.py
      → Pre-cached PNG assets from assets/weird_biology/
      → Background: hardcoded "nursery_room.png" (procedurally generated)
      → ❌ No image fetching
   ↓
OUTPUT: final.mp4
   → Stickman figures on solid color background
   → No contextual historical photos
```

---

## **Detailed Problem Breakdown**

### 1. **No Visual Query Generation**

**Current State**:
- Script sections have `text` only (e.g., "During Victorian times, people rarely smiled...")
- Shot planner receives: `{index, text, section, duration_s, word_count}`
- Shot planner outputs: `{pose, emotion, props, kinetic_text, xray, layout, camera}`
- **Missing**: `visual_query` field that describes what background should be retrieved

**Example for Your Topic**:
```json
// Current (missing visual_query):
{
  "beat_index": 5,
  "text": "Photography studios of the 1800s required subjects to hold still for minutes.",
  "section": 3,
  "pose": "think",
  "emotion": "worry",
  "layout": "default"
}

// What's needed:
{
  "beat_index": 5,
  "text": "Photography studios of the 1800s required subjects to hold still for minutes.",
  "section": 3,
  "pose": "think",
  "emotion": "worry",
  "layout": "default",
  "visual_query": "Victorian photography studio interior 1860s daguerreotype"  // ← MISSING
}
```

**Impact**: Without `visual_query`, the compositor can't search for or fetch images matching the scene.

---

### 2. **Background Selection Is Layout-Based, Not Content-Based**

**Current Logic** (in `weird_biology_shot_plan.py`, `_rule_based_shots()`):
```python
if "door" in low or "enter" in low:
    layout = "crib_door_scene"  # Switch to cream palette
else:
    layout = "default"  # Stay teal/slate
```

**What this does**: Changes the CSS-like color palette for procedural drawing.
- Default: Teal (#3E6B6B) walls + slate (#4A5F6A) background
- Crib scene: Cream (#F2DAAE) walls + beach-yellow floor

**What it doesn't do**: Fetch any actual images.

---

### 3. **No Image Fetching Integration**

**Gaps in Current Stack**:

| Component | Should Integrate | Currently Does |
|-----------|------------------|-----------------|
| `weird_biology_script.py` | ❌ Extract subject matter (e.g., "photography") | ✅ Only generates VO text |
| `weird_biology_scenes.py` | ❌ Add `visual_query` to each beat | ✅ Only splits text into phrases |
| `weird_biology_shot_plan.py` | ❌ Generate `visual_query` from beat text | ✅ Only plans pose/props via keyword rules |
| `weird_biology_stickman.py` | ❌ Fetch background image | ✅ Draws solid color rectangles |
| `weird_biology_compositor.py` | ❌ Fetch background image | ✅ Uses hardcoded nursery_room.png |

**Available but Unused**: 
- `src/services/media_retrieval.py` (exists in project, as shown in semantic search)
  - Has `MediaRetrievalService.fetch_best_image_url(query)` 
  - Uses Wikimedia Commons + CLIP ranking
  - **Never called** from weird_biology pipeline

---

### 4. **Scene-to-Image Consistency**

**For "Why Nobody Smiled In Old Photos"**, here's what should happen:

| Scene | Current | What's Needed |
|-------|---------|--------------|
| "In Victorian times, photography studios were dark..." | Stickman on teal background | Stickman over actual historical studio photo |
| "Exposure times lasted 5-10 minutes..." | Stickman on teal background | Stickman over daguerreotype or wet-plate studio scene |
| "The human face cannot hold a smile that long..." | Stickman on teal background | Stickman over historical portraits showing stern expressions |
| "Modern smiling conventions emerged later..." | Stickman on cream background (if rules mention "old") | Stickman over historical progression: stern → subtle → wide smile |

**Consistency Check**: ✅ Would the images stay topically consistent?
- Yes, if `visual_query` is derived from beat text (e.g., extract "Victorian studio" → search for that)
- No, if randomly assigned

**Current State**: 🔴 All backgrounds are the same solid colors. No consistency to maintain.

---

## **Root Causes**

### **1. No LLM Field for Visual Query Generation**
The shot planner LLM prompt doesn't ask for or return a `visual_query`. 

See: [weird_biology_shot_plan.py, `_USER_TMPL`](src/services/weird_biology_shot_plan.py#L40-L80)
```python
_USER_TMPL = """Plan stickman shots for these narration beats.
Rules:
- pose: one of {poses}
- emotion: neutral|surprise|calm|worry|smile
- props: 0–3 from {props}
- kinetic_text: short coral pop-in ONLY for stats/numbers
- ...
Return JSON:
{{"shots":[{{"beat_index":0,"pose":"stand","emotion":"neutral", ...}}]}}
"""
```

**Missing**: `visual_query` is not in the rules or return schema.

### **2. Compositor Hardcodes Backgrounds**
Both rendering backends use fixed, non-contextual backgrounds:

**SVG Stickman** ([weird_biology_stickman.py](src/services/weird_biology_stickman.py#L1500-L1540)):
```python
# Draw solid color background based on layout
if layout == "crib_door_scene":
    bg = palette["bg_slate"]  # cream
else:
    bg = palette["bg_teal"]   # teal
layers.append(f'<rect width="{W}" height="{H}" fill="{bg}"/>')  # Solid color only
```

**PNG Compositor** ([weird_biology_compositor.py](src/services/weird_biology_compositor.py#L103-L110)):
```python
bg = load_asset("backgrounds/nursery_room.png")  # Hardcoded single file
if bg.size != (rig.width, rig.height):
    bg = bg.resize((rig.width, rig.height), Image.Resampling.LANCZOS)
paste_layer(canvas, bg, (0, 0))  # Always the same background
```

### **3. No Scene-Context Analysis**
Shot planner only does keyword pattern matching (looking for "door", "crib", "baby"). 

It doesn't:
- Extract topic/subject from the full script
- Infer visual needs from sentence semantics
- Generate descriptive visual queries

---

## **Recommendations for Tuning**

### **PRIORITY 1: Add Visual Query Generation (High Impact, Feasible)**

**Step 1a**: Update shot planner LLM prompt to include `visual_query` field

File: `src/services/weird_biology_shot_plan.py`, update `_USER_TMPL`:

```python
_USER_TMPL = """Plan stickman shots for these narration beats.
Rules:
- pose: one of {poses}
- emotion: neutral|surprise|calm|worry|smile
- props: 0–3 from {props}
- kinetic_text: short coral pop-in ONLY for stats/numbers
- visual_query: SHORT (5-10 words) search query for background image.
  Examples: "Victorian photography studio", "x-ray chest anatomy", "crib nursery room"
  IMPORTANT: visual_query should match beat content; use scene keywords, era, setting, objects
- xray: true only when revealing internal biology
- layout: default|crib_door_scene
- camera: wide|face_zoom

Return JSON:
{{"shots":[{{"beat_index":0, "pose":"stand", "emotion":"neutral",
  "props":[], "kinetic_text":null, "visual_query":"historical daguerreotype studio",
  "xray":false, "internal":"nerve", "camera":"wide", "layout":"default", "bubble_bars":0}}]}}

BEATS:
{beats_json}
"""
```

**Step 1b**: Update `_normalize_shot()` to validate and store `visual_query`:

```python
def _normalize_shot(raw: dict[str, Any], beat_index: int) -> dict[str, Any]:
    # ... existing code ...
    vq = raw.get("visual_query")
    if vq is not None:
        vq = str(vq).strip() or None
    # Fallback: if no visual_query, generate from beat section
    if not vq:
        vq = f"generic {raw.get('layout')} scene"  # Fallback
    return {
        "beat_index": beat_index,
        "pose": pose,
        # ... other fields ...
        "visual_query": vq,  # ← Add this
    }
```

**Step 1c**: Update VisualBeat or Shot data model to carry `visual_query`

File: `src/services/weird_biology_stickman.py` or new model:

```python
@dataclass
class ShotSpec:
    beat_index: int
    pose: str
    emotion: str
    props: list[str]
    kinetic_text: str | None
    xray: bool
    internal: str
    camera: str
    layout: str
    bubble_bars: int
    visual_query: str = ""  # ← Add this field
```

---

### **PRIORITY 2: Integrate MediaRetrievalService (High Impact, Medium Effort)**

**Step 2a**: Modify compositor to fetch images based on `visual_query`

File: `src/services/weird_biology_compositor.py`:

```python
from src.services.media_retrieval import MediaRetrievalService

async def composite_scene_with_background(
    rig: RigSpec,
    visual_query: str = None
) -> Image.Image:
    """Composite rig layers onto background fetched from visual_query."""
    
    canvas = Image.new("RGBA", (rig.width, rig.height), (0, 0, 0, 255))
    
    # Fetch background image if visual_query provided
    if visual_query:
        media_service = MediaRetrievalService(use_clip=True)
        try:
            url = await media_service.fetch_best_image_url(visual_query)
            if url:
                bg_path = Path(tempfile.gettempdir()) / f"bg_{hash(visual_query)}.jpg"
                media_service.download_image(url, bg_path)
                bg = Image.open(bg_path).convert("RGB")
                if bg.size != (rig.width, rig.height):
                    bg = bg.resize((rig.width, rig.height), Image.Resampling.LANCZOS)
                paste_layer(canvas, bg, (0, 0))
            else:
                # Fallback: procedural background
                _draw_procedural_bg(canvas, rig)
        except Exception as e:
            logger.warning(f"Background fetch failed for '{visual_query}': {e}. Using fallback.")
            _draw_procedural_bg(canvas, rig)
    else:
        # No visual_query: use procedural fallback
        _draw_procedural_bg(canvas, rig)
    
    # Composite character layers on top
    for spec in sorted(rig.layers, key=lambda s: s.z):
        part = load_asset(spec.asset)
        part = _rotate_layer(part, spec.rotation)
        paste_layer(canvas, part, (spec.x, spec.y))
    
    return canvas.convert("RGB")

def _draw_procedural_bg(canvas: Image.Image, rig: RigSpec) -> None:
    """Fallback: draw solid color background as currently done."""
    # Original hardcoded background logic
    bg = load_asset("backgrounds/nursery_room.png")
    if bg.size != (rig.width, rig.height):
        bg = bg.resize((rig.width, rig.height), Image.Resampling.LANCZOS)
    paste_layer(canvas, bg, (0, 0))
```

**Step 2b**: Update render pipeline to pass `visual_query` through

File: `src/services/weird_biology_compose.py`:

```python
async def render_beat_clip(
    shot: ShotSpec,  # Now includes visual_query
    beat: VisualBeat,
    audio_path: Path,
    output_path: Path,
    # ...
) -> Path:
    """Render one beat with context-aware background."""
    # ... existing setup ...
    
    for frame_idx in range(n_frames):
        rig = RigSpec.from_shot(shot, frame_index=frame_idx, n_frames=n_frames)
        
        # Fetch background based on visual_query
        frame_img = await composite_scene_with_background(rig, shot.visual_query)
        
        frame_path = frames_dir / f"frame_{frame_idx:04d}.png"
        frame_img.save(frame_path)
    
    # ... rest of ffmpeg concat ...
```

---

### **PRIORITY 3: Enhance Visual Query Generation with NLP (Medium Priority, More Effort)**

For improved scene-to-image matching, add semantic extraction:

```python
def extract_visual_query_from_beat(beat_text: str, section: int, topic: str) -> str:
    """Generate visual_query by analyzing beat text + topic context."""
    
    # 1. Topic-aware prefix
    topic_prefixes = {
        "photography": "historical photography",
        "smile": "portrait",
        "face": "human face anatomy",
        # ... etc ...
    }
    
    # 2. Extract noun phrases from beat (simple NLP)
    nouns = extract_nouns(beat_text)  # e.g., ["studio", "photograph", "expression"]
    
    # 3. Section-specific modifiers
    section_modifiers = {
        1: "historical",
        2: "vintage",
        3: "scientific",
        4: "modern",
        5: "contemporary"
    }
    
    # 4. Combine: "historical Victorian photography studio"
    visual_query = f"{section_modifiers.get(section, '')} {' '.join(nouns[:2])}".strip()
    
    return visual_query[:50]  # Cap at 50 chars for API
```

---

### **PRIORITY 4: Cache & Fallback Strategy (Medium Priority)**

To ensure reliability:

```python
# In MediaRetrievalService or wrapper:
class WeirdBiologyBackgroundFetcher:
    def __init__(self):
        self.media_service = MediaRetrievalService(use_clip=True)
        self.cache = {}  # visual_query → image URL cache
        self.fallback_palette = {
            "default": "#3E6B6B",  # teal
            "crib_door_scene": "#F2DAAE"  # cream
        }
    
    async def get_background(self, visual_query: str, layout: str = "default") -> Image.Image:
        """Fetch background or return procedural fallback."""
        
        # 1. Check cache
        if visual_query in self.cache:
            try:
                return Image.open(self.cache[visual_query])
            except:
                pass
        
        # 2. Try fetch
        try:
            url = await self.media_service.fetch_best_image_url(visual_query)
            if url:
                self.cache[visual_query] = url
                # Download and return
                return await self._download_and_prepare(url)
        except Exception as e:
            logger.warning(f"Background fetch failed: {e}")
        
        # 3. Fallback: procedural
        return self._create_procedural_bg(layout)
```

---

## **Testing & Validation for "Old Photos" Topic**

Once tuned, test with:

```bash
python -m src.cli.generate_weird_biology_video \
  --topic "Why Nobody Smiled In Old Photos" \
  --out-dir output/old_photos_test/ \
  --enable-visual-query-fetch \
  --visual-query-debug
```

**Validation Checklist**:
- [ ] Beat 1 (Hook): `visual_query` → "historical portrait" → fetches era-appropriate photo
- [ ] Beat 3-5 (Core Data): `visual_query` → "Victorian photography studio" → fetches studio scene
- [ ] Beat 7 (Debate): `visual_query` → "modern vs historical smile expressions" → fetches comparative images
- [ ] All backgrounds are contextually consistent (no random modern photos for 1800s topic)
- [ ] Stickman figures composite cleanly over fetched images (not distorted)
- [ ] Fallback to procedural bg if fetch fails (no black frames)

---

## **Summary: Diagnosis & Path Forward**

### **Current State** ❌
- Pipeline generates stickman animations on solid color backgrounds
- No mechanism to fetch contextual images per scene
- No `visual_query` field in the pipeline
- "Old Photos" topic gets same teal/cream backgrounds as any other topic

### **Needs Tuning** ⚠️
1. **Add `visual_query` generation** in shot planner (1-2 hour effort)
2. **Integrate MediaRetrievalService** into compositor (2-4 hour effort)
3. **Enhance extraction logic** for better semantic matching (optional, 4-6 hours)
4. **Implement caching & fallback** for reliability (1-2 hours)

### **Post-Tuning** ✅
- Pipeline generates stickman animations over **contextually appropriate historical photos**
- Beat: "Victorian studios" → actual historical studio interiors
- Beat: "Stern expressions" → historical portraits showing actual stern faces
- Consistency maintained across all beats in the topic

**Effort**: ~8-12 hours for MVP (visual_query + media integration)  
**ROI**: Dramatic visual quality improvement for documentary topics like history, science, old photos.

