"""Shot planner for Weird Biology.

Tries ``openrouter/free`` twice, then WaveSpeed ``google/gemini-2.5-flash-lite``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.services.llm import LLMClient, LLMError, parse_json_object
from src.services.openrouter import OpenRouterClient
from src.services.settings import get_settings
from src.services.whb_llm_router import (
    FREE_MODEL,
    STAGE_SHOT_PLAN,
    WAVESPEED_FALLBACK_MODEL,
    WhbFreeFirstRouter,
)
from src.services.weird_biology_scenes import VisualTimeline, ensure_core_data_xray_slot
from src.services.weird_biology_stickman import POSES, PROPS, ShotSpec
from src.services.weird_biology_prop_registry import get_planner_props
from src.services.weird_biology_style import (
    STYLE_BLUE_BENCH,
    STYLE_OUTDOOR_SPLIT,
    STYLE_WARM_CREAM,
    STYLE_WARM_TAN_SOFA,
    planner_model,
)

PLANNER_MODEL = WAVESPEED_FALLBACK_MODEL

_SYSTEM = (
    "You plan Weird Human Biology stickman shots. Reply with ONLY valid JSON. "
    "Keep plans short. Default layout: clinical teal/slate; crib_door_scene uses warm "
    "cream reference palette. Coral text only for stats/bubble bars. "
    "CRITICAL: The video topic is the primary context — every shot must visually match "
    "the topic. Use props and poses that reinforce what the topic is about. "
    "For mosquito/bite topics: ankles/welts beats → react+worry+wide; "
    "warm/nearby/animal beats → stand+neutral+wide (show animal scene); "
    "cheese/feet/smell beats → stand+worry+wide (equals-sign comparison); "
    "carbon-dioxide/heat/infrared beats → xray=true+vessel. "
    "For old-photo topics: face_zoom on stare/expression beats; worry for strain/hold beats."
)

_USER_TMPL = """Video topic: {topic}

Plan stickman shots for these narration beats.
Rules:
- ALWAYS ground shots in the video topic above — poses, props, and kinetic text must relate to it
- pose: one of {poses}
- emotion: neutral|surprise|calm|worry|smile
- props: 0–3 from {props} (minimal environments; use crib/door/bubble/room_corner for nursery beats)
- kinetic_text: short coral pop-in ONLY for stats/numbers/emphasis, else null
- bubble_bars: 0–3 coral bars in a speech bubble (accent only); prefer with crib/baby beats
- layout: default|crib_door_scene (crib_door_scene = baby+adult dual cast with crib/door)
- xray: true only when revealing internal biology (nerve|vessel|muscle)
- camera: wide|face_zoom
- At least one beat in section 3 MUST have xray=true (prefer beat_index={xray_hint})
- layout default: teal/slate world; crib_door_scene: warm cream nursery (not teal walls)
- coral only for kinetic/bubble bars (not full-scene palette)
- props names only from the list
- For topics about photography/cameras/smiling: use face_zoom camera for smile/reaction shots
- For topics about history/old photos: use shrug or react poses, show strain/effort in emotions
- MOSQUITO TOPIC RULES (apply when topic mentions mosquito/bite/malaria):
  * Beats with ankles/welts/lump/itch/bite → react pose, worry emotion, wide camera
  * Beats with warm/nearby/bird/frog/cattle/cow/animal → stand pose, neutral emotion, wide camera + heat_waves prop
  * Beats with cheese/limburger/feet/foot/smell/bacteria → stand pose, worry emotion, wide camera + smell_cloud+equals_sign+cheese_wedge props
  * Beats with carbon dioxide/plume/infrared/heat sensing → xray=true, internal=vessel
  * Beats with study/percent/100x/tournament → kinetic_text with the key stat, wide camera
- NEW BIOLOGY PROP RULES:
  * sun/light/sneeze beats → sun_ray prop + sneeze_burst prop
  * rain/petrichor/geosmin beats → rain_drops prop
  * bacteria/gut/appendix/microbiome beats → bacteria_blob prop
  * warm/heat/temperature/animal beats → heat_waves prop
  * cheese/limburger/smell/feet comparison beats → smell_cloud + equals_sign + cheese_wedge props
- SHORT PUNCHY BEATS (<6 words) that reveal a counter-intuitive fact → face_zoom camera, max emotion
- "You/Your" direct-address beats → react pose, worry emotion (make viewer feel personally targeted)

Return JSON:
{{"shots":[{{"beat_index":0,"pose":"stand","emotion":"neutral","props":[],"kinetic_text":null,"xray":false,"internal":"nerve","camera":"wide","layout":"default","bubble_bars":0}}]}}

BEATS:
{beats_json}
"""


def _topic_keywords(topic: str) -> frozenset[str]:
    """Extract lower-case word tokens from topic for rule-based topic-awareness."""
    return frozenset(re.findall(r"[a-z]+", (topic or "").lower()))


# ── Mosquito topic beat-keyword groups ─────────────────────────────────────
_MOSQUITO_BITE_KW = frozenset({
    "ankles", "ankle", "welts", "welt", "lump", "lumps", "itch", "bite", "bites",
    "bitten", "ear", "you", "your", "constantly", "annoying",
})
_MOSQUITO_ANIMAL_KW = frozenset({
    "warm", "nearby", "bird", "birds", "frog", "frogs", "cattle", "cow",
    "animal", "animals", "forest", "wherever", "whatever",
})
_MOSQUITO_SMELL_KW = frozenset({
    "cheese", "limburger", "feet", "foot", "toes", "smell", "odor", "odour",
    "bacteria", "acids", "carboxylic", "exhaust", "colony", "sebum", "oils",
})
_MOSQUITO_BIO_KW = frozenset({
    "carbon", "dioxide", "co2", "plume", "infrared", "heat", "radiation",
    "receptor", "detector", "nerve", "sensor", "sense",
})
_MOSQUITO_STAT_KW = frozenset({
    "hundred", "times", "percent", "study", "tournament", "subject", "33",
    "64", "volunteers", "2022", "2024", "282", "million", "610",
})

# ── Human biology topic beat-keyword groups ──────────────────────────────────
# Sneeze / photic sneeze reflex
_SNEEZE_TRIGGER_KW = frozenset({
    "sun", "light", "bright", "sneeze", "sneezing", "sneezed", "look", "looking",
})
_SNEEZE_BIO_KW = frozenset({
    "optic", "trigeminal", "nerve", "wiring", "cross", "signals", "neurons",
    "reflex", "neurological", "brain", "pathway",
})
# Water pruning / fingers
_PRUNE_SURFACE_KW = frozenset({
    "wrinkle", "wrinkled", "prune", "pruned", "pruning", "finger", "fingers",
    "skin", "ridges", "water", "wet",
})
_PRUNE_BIO_KW = frozenset({
    "nervous", "system", "brain", "controlled", "active", "grip", "traction",
    "osmosis", "not", "channel",
})
# Emotional tears
_TEARS_EMOTION_KW = frozenset({
    "cry", "crying", "cried", "tears", "tear", "weep", "weeping", "sob", "sobbing",
    "sad", "sad", "emotional", "feelings", "feel",
})
_TEARS_CHEM_KW = frozenset({
    "hormone", "hormones", "cortisol", "stress", "chemistry", "protein",
    "leucine", "manganese", "chemical", "contain",
})
# Appendix
_APPENDIX_DANGER_KW = frozenset({
    "appendix", "appendicitis", "burst", "rupture", "useless", "ticking", "bomb",
    "remove", "removed",
})
_APPENDIX_BIO_KW = frozenset({
    "bacteria", "gut", "microbiome", "reboot", "safe", "house", "biofilm",
    "plague", "disease", "immune",
})
# Crowded teeth / jaw
_TEETH_CROWDED_KW = frozenset({
    "wisdom", "teeth", "tooth", "crowded", "crooked", "jaw", "braces",
    "orthodontist", "straight",
})
_TEETH_EVOLUTION_KW = frozenset({
    "ancient", "ancestors", "shrink", "shrunk", "shrank", "smaller",
    "soft", "cooked", "food", "diet",
})
# Yawn
_YAWN_ACT_KW = frozenset({
    "yawn", "yawning", "yawned", "mouth", "jaw", "open", "wide",
})
_YAWN_CONTAGIOUS_KW = frozenset({
    "contagious", "spread", "see", "watching", "empathy", "social", "group",
    "tribe", "warning",
})
_YAWN_BIO_KW = frozenset({
    "cool", "cooling", "brain", "temperature", "blood", "vessel", "stretch",
    "oxygen",
})
# Motion sickness
_MOTION_SICK_KW = frozenset({
    "sick", "nausea", "nauseous", "vomit", "throw", "queasy", "dizzy",
    "motion", "sickness",
})
_MOTION_CONFLICT_KW = frozenset({
    "ear", "inner", "eyes", "eye", "disagree", "conflict", "mismatch",
    "signals", "mixed",
})
_MOTION_POISON_KW = frozenset({
    "poison", "poisoned", "toxin", "assumes", "brain", "mistake", "error",
    "evolutionary",
})
# Goosebumps
_GOOSE_TRIGGER_KW = frozenset({
    "goosebump", "goosebumps", "skin", "hair", "raise", "raised", "raised",
    "cold", "chills", "fear", "scared",
})
_GOOSE_ANCESTOR_KW = frozenset({
    "furry", "fur", "ancestor", "ancestors", "bigger", "predator", "predators",
    "adrenaline", "vestigial", "ancient",
})
# Hypnic jerk
_HYPNIC_FALL_KW = frozenset({
    "jerk", "twitch", "twitches", "jolt", "falling", "asleep", "startle",
    "violent", "wake", "wakes", "awake",
})
_HYPNIC_BIO_KW = frozenset({
    "brain", "misinterprets", "dying", "death", "sleep", "consciousness",
    "muscle", "relax", "signal",
})
# Brain freeze
_BRAIN_FREEZE_TRIGGER_KW = frozenset({
    "cold", "ice", "cream", "slushie", "frozen", "eat", "drink", "cold",
    "palate", "mouth",
})
_BRAIN_FREEZE_PAIN_KW = frozenset({
    "pain", "ache", "freeze", "forehead", "temples", "headache", "stab",
    "pressure", "sphenopalatine",
})
_BRAIN_FREEZE_BIO_KW = frozenset({
    "vessel", "vessels", "dilate", "constrict", "blood", "cranial", "rapid",
    "receptor", "nerve",
})
# Rain smell
_RAIN_SMELL_KW = frozenset({
    "rain", "smell", "petrichor", "geosmin", "soil", "earth", "wet",
    "detect", "nose", "sniff",
})
_RAIN_STAT_KW = frozenset({
    "trillion", "parts", "billion", "shark", "blood", "sensitive", "sensitivity",
    "detect", "concentration",
})
# Tickle
_TICKLE_SELF_KW = frozenset({
    "tickle", "tickling", "tickled", "yourself", "self", "own", "cannot",
    "can't", "predict",
})
_TICKLE_BIO_KW = frozenset({
    "cerebellum", "predicts", "movement", "movements", "dampens", "cancels",
    "sensory", "response",
})
# Fingerprints
_PRINT_GRIP_KW = frozenset({
    "fingerprint", "fingerprints", "ridges", "grip", "friction", "rough",
    "wet", "surface", "traction",
})
_PRINT_UNIQUE_KW = frozenset({
    "unique", "identical", "twins", "dna", "different", "crime", "identify",
    "whorls", "loops",
})
# Naked ape / body hair
_NAKED_HAIR_KW = frozenset({
    "hair", "fur", "naked", "hairless", "lost", "lose", "body",
    "primate", "ape", "bare",
})
_NAKED_SWEAT_KW = frozenset({
    "sweat", "sweating", "cool", "cooling", "heat", "run", "running",
    "persistence", "endurance", "hunt",
})
# Chin
_CHIN_UNIQUE_KW = frozenset({
    "chin", "only", "unique", "humans", "human", "chimpanzee", "neanderthal",
    "jaw", "mandible",
})
_CHIN_THEORY_KW = frozenset({
    "theory", "theories", "stress", "chewing", "bite", "force", "sexual",
    "selection", "speech",
})


def _rule_based_shots(tl: VisualTimeline) -> list[dict[str, Any]]:
    """Deterministic fallback if LLM fails — now topic-aware."""
    xray_i = ensure_core_data_xray_slot(tl)
    topic_kw = _topic_keywords(tl.topic)

    # Topic-level signals that override section defaults
    topic_is_photo = bool(topic_kw & {"photo", "photos", "photograph", "camera", "smile", "smiled", "smiling", "portrait"})
    topic_is_history = bool(topic_kw & {"old", "history", "historical", "ancient", "century", "victorian"})
    topic_is_pain = bool(topic_kw & {"pain", "hurt", "injury", "wound", "ache"})
    topic_is_sleep = bool(topic_kw & {"sleep", "dream", "nap", "tired", "yawn"})
    topic_is_food = bool(topic_kw & {"eat", "food", "digest", "stomach", "gut"})
    topic_is_mosquito = bool(topic_kw & {
        "mosquito", "mosquitoes", "bite", "bites", "malaria", "insect", "insects",
    })
    # New human-biology topic flags
    topic_is_sneeze   = bool(topic_kw & {"sneeze", "sneezing", "photic", "reflex"})
    topic_is_prune    = bool(topic_kw & {"prune", "pruning", "fingers", "wrinkle"})
    topic_is_tears    = bool(topic_kw & {"cry", "tears", "tear", "weep", "emotional"})
    topic_is_appendix = bool(topic_kw & {"appendix", "appendicitis", "gut", "bacteria"})
    topic_is_teeth    = bool(topic_kw & {"teeth", "tooth", "crowded", "jaw", "wisdom"})
    topic_is_yawn     = bool(topic_kw & {"yawn", "yawning", "contagious"})
    topic_is_motion   = bool(topic_kw & {"motion", "sickness", "nausea", "dizzy"})
    topic_is_goose    = bool(topic_kw & {"goosebumps", "goosebump", "vestigial"})
    topic_is_hypnic   = bool(topic_kw & {"hypnic", "jerk", "twitch", "twitches"})
    topic_is_freeze   = bool(topic_kw & {"freeze", "brain", "frozen", "palate"})
    topic_is_rain     = bool(topic_kw & {"rain", "petrichor", "geosmin", "smell"})
    topic_is_tickle   = bool(topic_kw & {"tickle", "tickling", "yourself", "cerebellum"})
    topic_is_prints   = bool(topic_kw & {"fingerprints", "fingerprint", "ridges"})
    topic_is_naked    = bool(topic_kw & {"naked", "hairless", "sweat", "body", "hair"})
    topic_is_chin     = bool(topic_kw & {"chin", "chimpanzee", "neanderthal"})

    shots: list[dict[str, Any]] = []
    for b in tl.beats:
        pose = "stand"
        emotion = "neutral"
        props: list[str] = []
        kinetic = None
        camera = "wide"
        xray = False
        internal = "nerve"
        low = b.text.lower()

        if b.section == 1:
            pose = "look_at_arms"
            emotion = "surprise"
        elif b.section == 2:
            pose = "think"
        elif b.section == 3:
            pose = "point"
            if re.search(r"\d|percent|study|nerve|muscle|brain", low):
                kinetic = re.sub(r"[^A-Za-z0-9 %]", "", b.text)[:28] or None
            if xray_i is not None and b.index == xray_i:
                xray = True
                internal = "nerve"
        elif b.section == 4:
            pose = "react"
            emotion = "worry"
        else:
            pose = "stand"
            emotion = "calm"
            camera = "face_zoom"

        # ── Topic-aware overrides (beat-level) ──────────────────────────────
        beat_kw = frozenset(re.findall(r"[a-z]+", low))
        if topic_is_photo:
            # Beats mentioning smile/hold/long → show strain/react
            if beat_kw & {"smile", "smiling", "grin", "hold", "held", "long", "minutes"}:
                pose = "react"
                emotion = "worry"
                camera = "face_zoom"
            # Beats mentioning photo/camera/portrait → face zoom reaction
            elif beat_kw & {"photo", "camera", "photograph", "portrait", "picture", "sit", "pose", "posed"}:
                pose = "shrug"
                emotion = "neutral"
                camera = "face_zoom"
            # Short stare/expression beats → tight face zoom (the stare IS the shot)
            elif beat_kw & {"stare", "staring", "expression", "actually", "was", "look"}:
                pose = "face_zoom"
                emotion = "worry"
                camera = "face_zoom"
        if topic_is_history and b.section <= 2:
            emotion = "calm"
        if topic_is_pain:
            emotion = "worry"
        if topic_is_sleep and beat_kw & {"sleep", "dream", "nap", "tired"}:
            pose = "think"
        if topic_is_food and beat_kw & {"eat", "food", "digest", "stomach"}:
            pose = "look_at_arms"

        # ── New human-biology topic overrides (beat-level) ───────────────────
        if topic_is_sneeze:
            if beat_kw & _SNEEZE_TRIGGER_KW:
                pose = "react"
                emotion = "surprise"
                camera = "face_zoom"   # sneeze face IS the shot
                props = ["sun_ray", "sneeze_burst"]  # sun trigger + spray burst
            elif beat_kw & _SNEEZE_BIO_KW:
                xray = True
                internal = "nerve"
                pose = "point"
                camera = "wide"
        elif topic_is_prune:
            if beat_kw & _PRUNE_SURFACE_KW:
                pose = "look_at_arms"  # character staring at wrinkled hands
                emotion = "surprise"
                camera = "wide"
            elif beat_kw & _PRUNE_BIO_KW:
                xray = True
                internal = "nerve"
                pose = "point"
                camera = "wide"
        elif topic_is_tears:
            if beat_kw & _TEARS_EMOTION_KW:
                pose = "react"
                emotion = "worry"
                camera = "face_zoom"   # close on the crying face
                props = []
            elif beat_kw & _TEARS_CHEM_KW:
                xray = True
                internal = "vessel"
                pose = "point"
                camera = "wide"
                kinetic = "STRESS HORMONES" if "hormone" in beat_kw else kinetic
        elif topic_is_appendix:
            if beat_kw & _APPENDIX_DANGER_KW:
                pose = "react"
                emotion = "worry"
                camera = "wide"
                props = ["thermometer", "bacteria_blob"]  # danger + gut life
            elif beat_kw & _APPENDIX_BIO_KW:
                xray = True
                internal = "vessel"
                pose = "point"
                camera = "wide"
                props = ["bacteria_blob"]  # gut bacteria visualized
        elif topic_is_teeth:
            if beat_kw & _TEETH_CROWDED_KW:
                pose = "face_zoom"     # open-mouth teeth close-up
                emotion = "worry"
                camera = "face_zoom"
                props = []
            elif beat_kw & _TEETH_EVOLUTION_KW:
                pose = "shrug"
                emotion = "neutral"
                camera = "wide"
        elif topic_is_yawn:
            if beat_kw & _YAWN_ACT_KW:
                pose = "react"         # wide-open jaw
                emotion = "calm"       # drowsy, not scared
                camera = "face_zoom"
                props = []
            elif beat_kw & _YAWN_CONTAGIOUS_KW:
                pose = "look_at_arms"
                emotion = "surprise"
                camera = "wide"
            elif beat_kw & _YAWN_BIO_KW:
                xray = True
                internal = "vessel"
                pose = "point"
                camera = "wide"
                props = ["heat_waves"]  # brain cooling = heat dispersal
        elif topic_is_motion:
            if beat_kw & _MOTION_SICK_KW:
                pose = "react"
                emotion = "worry"
                camera = "face_zoom"   # sick-face close-up
                props = []
            elif beat_kw & _MOTION_CONFLICT_KW:
                pose = "shrug"
                emotion = "surprise"
                camera = "wide"
                xray = True
                internal = "nerve"
            elif beat_kw & _MOTION_POISON_KW:
                pose = "react"
                emotion = "worry"
                camera = "wide"
                props = ["brain_icon"]
        elif topic_is_goose:
            if beat_kw & _GOOSE_TRIGGER_KW:
                pose = "look_at_arms"  # character staring at own arms
                emotion = "surprise"
                camera = "wide"
                props = []
            elif beat_kw & _GOOSE_ANCESTOR_KW:
                pose = "arms_up"       # puffed-up ancestor display
                emotion = "surprise"
                camera = "wide"
        elif topic_is_hypnic:
            if beat_kw & _HYPNIC_FALL_KW:
                pose = "react"
                emotion = "surprise"
                camera = "face_zoom"   # the jerk IS the face
                props = []
            elif beat_kw & _HYPNIC_BIO_KW:
                xray = True
                internal = "nerve"
                pose = "point"
                camera = "wide"
        elif topic_is_freeze:
            if beat_kw & _BRAIN_FREEZE_TRIGGER_KW:
                pose = "react"
                emotion = "worry"
                camera = "face_zoom"
                props = ["thermometer"]  # cold signal
            elif beat_kw & _BRAIN_FREEZE_PAIN_KW:
                pose = "react"
                emotion = "worry"
                camera = "face_zoom"
                kinetic = "BRAIN FREEZE" if "freeze" in beat_kw else kinetic
            elif beat_kw & _BRAIN_FREEZE_BIO_KW:
                xray = True
                internal = "vessel"
                pose = "point"
                camera = "wide"
        elif topic_is_rain:
            if beat_kw & _RAIN_SMELL_KW:
                pose = "look_at_arms"  # sniffing the air
                emotion = "surprise"
                camera = "wide"
                props = ["rain_drops"]  # rain context
            elif beat_kw & _RAIN_STAT_KW:
                pose = "point"
                camera = "wide"
                props = ["rain_drops"]
                nums = re.findall(r"\d+\.?\d*|trillion|billion", b.text.lower())
                if nums:
                    kinetic = f"{nums[0].upper()} PARTS PER TRILLION"
        elif topic_is_tickle:
            if beat_kw & _TICKLE_SELF_KW:
                pose = "shrug"
                emotion = "surprise"
                camera = "face_zoom"
                props = []
            elif beat_kw & _TICKLE_BIO_KW:
                xray = True
                internal = "nerve"
                pose = "point"
                camera = "wide"
        elif topic_is_prints:
            if beat_kw & _PRINT_GRIP_KW:
                pose = "look_at_arms"  # examining fingers
                emotion = "surprise"
                camera = "wide"
            elif beat_kw & _PRINT_UNIQUE_KW:
                pose = "point"
                emotion = "neutral"
                camera = "wide"
        elif topic_is_naked:
            if beat_kw & _NAKED_HAIR_KW:
                pose = "look_at_arms"  # staring at own bare skin
                emotion = "surprise"
                camera = "wide"
            elif beat_kw & _NAKED_SWEAT_KW:
                pose = "walk"          # running / persistence hunt
                emotion = "neutral"
                camera = "wide"
                xray = True
                internal = "vessel"    # show blood vessels / heat
                props = ["heat_waves"]  # sweating = heat dissipation
        elif topic_is_chin:
            if beat_kw & _CHIN_UNIQUE_KW:
                pose = "face_zoom"     # close on the chin
                emotion = "neutral"
                camera = "face_zoom"
                props = []
            elif beat_kw & _CHIN_THEORY_KW:
                pose = "shrug"
                emotion = "surprise"
                camera = "wide"

        # ── Mosquito topic overrides (beat-level) ────────────────────────────
        if topic_is_mosquito:
            if beat_kw & _MOSQUITO_BITE_KW:
                # Ankles/welts/you → panic reaction, show body with bite marks
                pose = "react"
                emotion = "worry"
                camera = "wide"
                props = ["arrow"]  # arrow pointing at bites
            elif beat_kw & _MOSQUITO_ANIMAL_KW:
                # Warm/nearby/animal scene → neutral wide show + heat waves
                pose = "stand"
                emotion = "neutral"
                camera = "wide"
                props = ["heat_waves"]  # warm blooded creature
            elif beat_kw & _MOSQUITO_SMELL_KW:
                # Cheese/feet/bacteria → equals-sign comparison composition
                pose = "stand"
                emotion = "worry"
                camera = "wide"
                props = ["smell_cloud", "cheese_wedge"]  # full comparison scene
            elif beat_kw & _MOSQUITO_BIO_KW:
                # CO2/infrared/heat → xray reveal
                xray = True
                internal = "vessel"
                pose = "point"
                camera = "wide"
            elif beat_kw & _MOSQUITO_STAT_KW:
                # Numbers/study → kinetic text pop-in
                nums = re.findall(r"\d+\.?\d*\s*(?:percent|times|million|%)?|\d+", b.text)
                if nums:
                    kinetic = f"{nums[0].strip().upper()}"
                pose = "point"
                camera = "wide"
        # ────────────────────────────────────────────────────────────────────

        layout = "default"
        bubble_bars = 0
        if "door" in low or "enter" in low:
            props = ["door"]
            pose = "enter_door"
        if any(k in low for k in ("crib", "baby", "infant", "nursery", "cradle")):
            layout = "crib_door_scene"
            props = ["crib", "door", "room_corner", "bubble"]
            bubble_bars = 3
            pose = "arms_up"
            emotion = "surprise"
        # ── Determine scene style + bg_mode from topic type ────────────────────
        # Mosquito animal/outdoor beats → outdoor split (white sky + green ground)
        # Mosquito sofa/indoor beats  → warm tan + teal sofa
        # Comparison/Limburger beats  → blue bench scene
        # Everything else             → warm cream (default new look)
        beat_style = STYLE_WARM_CREAM
        beat_bg    = "default"

        if topic_is_mosquito:
            if beat_kw & _MOSQUITO_ANIMAL_KW:
                beat_style = STYLE_OUTDOOR_SPLIT
                beat_bg    = "outdoor_split"
            elif beat_kw & _MOSQUITO_SMELL_KW:
                beat_style = STYLE_BLUE_BENCH
                beat_bg    = "bench"
            else:
                beat_style = STYLE_WARM_TAN_SOFA
                beat_bg    = "sofa"
        elif topic_is_naked and beat_kw & _NAKED_SWEAT_KW:
            beat_style = STYLE_OUTDOOR_SPLIT
            beat_bg    = "outdoor_split"
        elif topic_is_rain and beat_kw & _RAIN_SMELL_KW:
            beat_style = STYLE_OUTDOOR_SPLIT
            beat_bg    = "outdoor_split"
        elif (topic_is_sneeze or topic_is_prune or topic_is_tears or
              topic_is_appendix or topic_is_teeth or topic_is_yawn or
              topic_is_goose or topic_is_tickle or topic_is_prints or
              topic_is_chin or topic_is_motion or topic_is_hypnic or
              topic_is_freeze or topic_is_rain):
            beat_style = STYLE_WARM_CREAM
            beat_bg    = "default"

        shots.append(
            {
                "beat_index": b.index,
                "pose": pose,
                "emotion": emotion,
                "props": props,
                "kinetic_text": kinetic,
                "xray": xray,
                "internal": internal,
                "camera": camera,
                "layout": layout,
                "bubble_bars": bubble_bars,
                "style": beat_style,
                "bg_mode": beat_bg,
            }
        )
    return _enforce_xray(shots, tl)


def _enforce_xray(shots: list[dict[str, Any]], tl: VisualTimeline) -> list[dict[str, Any]]:
    if any(s.get("xray") for s in shots):
        return shots
    hint = ensure_core_data_xray_slot(tl)
    if hint is None and shots:
        hint = shots[len(shots) // 2]["beat_index"]
    for s in shots:
        if int(s.get("beat_index", -1)) == hint:
            s["xray"] = True
            s["internal"] = s.get("internal") or "nerve"
            break
    return shots


def _normalize_shot(raw: dict[str, Any], beat_index: int) -> dict[str, Any]:
    pose = str(raw.get("pose") or "stand").lower()
    if pose not in POSES:
        pose = "stand"
    props = raw.get("props") or []
    if isinstance(props, str):
        props = [props]
    props = [str(p).lower() for p in props if str(p).lower() in PROPS][:3]
    internal = str(raw.get("internal") or "nerve").lower()
    if internal not in {"nerve", "vessel", "muscle"}:
        internal = "nerve"
    camera = str(raw.get("camera") or "wide").lower()
    if camera not in {"wide", "face_zoom"}:
        camera = "wide"
    kt = raw.get("kinetic_text")
    if kt is not None:
        kt = str(kt).strip() or None
    layout = str(raw.get("layout") or "default").lower()
    if layout not in {"default", "crib_door_scene"}:
        layout = "default"
    try:
        bubble_bars = int(raw.get("bubble_bars") or 0)
    except (TypeError, ValueError):
        bubble_bars = 0
    bubble_bars = max(0, min(5, bubble_bars))
    return {
        "beat_index": beat_index,
        "pose": pose,
        "emotion": str(raw.get("emotion") or "neutral").lower(),
        "props": props,
        "kinetic_text": kt,
        "xray": bool(raw.get("xray")),
        "internal": internal,
        "camera": camera,
        "layout": layout,
        "bubble_bars": bubble_bars,
    }


def _parse_shots_payload(text: str) -> dict[str, Any]:
    data = parse_json_object(text)
    if not isinstance(data.get("shots"), list):
        raise LLMError("missing shots array")
    return data


class WeirdBiologyShotPlanner:
    def __init__(
        self,
        *,
        model: str | None = None,
        llm: LLMClient | None = None,
        openrouter: OpenRouterClient | None = None,
        router: WhbFreeFirstRouter | None = None,
    ):
        self.settings = get_settings()
        self.model = (model or planner_model() or PLANNER_MODEL).strip()
        self._llm = llm
        self._openrouter = openrouter
        self._router = router

    @property
    def llm(self) -> LLMClient:
        return self._get_llm()

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.settings, default_model=self.model)
        return self._llm

    def _get_router(self) -> WhbFreeFirstRouter:
        if self._router is None:
            self._router = WhbFreeFirstRouter(
                self.settings,
                channel="weird_human_biology",
                openrouter=self._openrouter,
            )
        return self._router

    def _fallback_shots_llm(self, system: str, user: str) -> dict[str, Any]:
        llm = self._get_llm()
        try:
            data = llm.chat_json(
                system=system,
                user=user,
                temperature=0.3,
                model=self.model,
                timeout_s=120.0,
                same_model_retries=1,
                fallback_model=None,
            )
        except LLMError:
            text = llm.chat_text(
                system=system,
                user=user,
                temperature=0.3,
                model=self.model,
                timeout_s=120.0,
            )
            data = parse_json_object(text)
        if not isinstance(data.get("shots"), list):
            raise LLMError("missing shots array")
        return data

    def plan(
        self,
        tl: VisualTimeline,
        *,
        batch_size: int = 24,
    ) -> dict[str, Any]:
        """Return shot_plan dict with shots aligned to beats."""
        if not tl.beats:
            return {"model": self.model, "shots": [], "source": "empty"}

        # Try LLM in batches; on failure use rules for that batch
        all_shots: dict[int, dict[str, Any]] = {}
        source = "llm"
        models_used: list[str] = []
        xray_hint = ensure_core_data_xray_slot(tl)
        for start in range(0, len(tl.beats), batch_size):
            batch = tl.beats[start : start + batch_size]
            beats_payload = [
                {
                    "beat_index": b.index,
                    "section": b.section,
                    "text": b.text[:220],
                    "duration_s": round(b.duration_s, 2),
                }
                for b in batch
            ]
            topic_str = (tl.topic or "Weird Human Biology").strip()
            prop_context = " ".join(
                [topic_str, *(str(item.get("text") or "") for item in beats_payload)]
            )
            user = _USER_TMPL.format(
                topic=topic_str,
                poses=", ".join(POSES),
                props=", ".join(get_planner_props(prop_context)),
                xray_hint=xray_hint if xray_hint is not None else "middle of section 3",
                beats_json=json.dumps(beats_payload, ensure_ascii=False),
            )
            try:
                data, meta = self._get_router().call(
                    system=_SYSTEM,
                    user=user,
                    parse=_parse_shots_payload,
                    fallback=lambda u=user: self._fallback_shots_llm(_SYSTEM, u),
                    fallback_model=self.model,
                    stage=STAGE_SHOT_PLAN,
                    temperature=0.3,
                    timeout_s=120.0,
                )
                models_used.append(meta.model_used)
                raw_shots = data.get("shots") if isinstance(data, dict) else None
                if not isinstance(raw_shots, list):
                    raise LLMError("missing shots array")
                by_idx = {
                    int(s.get("beat_index")): s
                    for s in raw_shots
                    if isinstance(s, dict) and s.get("beat_index") is not None
                }
                for b in batch:
                    raw = by_idx.get(b.index) or {
                        "beat_index": b.index,
                        "pose": "stand",
                    }
                    all_shots[b.index] = _normalize_shot(raw, b.index)
            except Exception:  # noqa: BLE001
                source = "llm+rules_fallback"
                for s in _rule_based_shots(
                    VisualTimeline(
                        beats=batch,
                        full_vo_duration_s=tl.full_vo_duration_s,
                        topic=tl.topic,
                    )
                ):
                    all_shots[int(s["beat_index"])] = s

        ordered = [all_shots[b.index] for b in tl.beats if b.index in all_shots]
        # Fill any gaps
        for b in tl.beats:
            if b.index not in all_shots:
                ordered.append(
                    _normalize_shot({"beat_index": b.index, "pose": "stand"}, b.index)
                )
        ordered.sort(key=lambda s: int(s["beat_index"]))
        ordered = _enforce_xray(ordered, tl)
        return {
            "model": models_used[-1] if models_used else self.model,
            "fallback_model": self.model,
            "free_model": FREE_MODEL,
            "source": source,
            "shots": ordered,
        }


def shots_to_specs(plan: dict[str, Any]) -> list[ShotSpec]:
    return [
        ShotSpec.from_dict(s, seed=int(s.get("beat_index") or i))
        for i, s in enumerate(plan.get("shots") or [])
    ]
