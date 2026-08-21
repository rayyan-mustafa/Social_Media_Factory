import re

WPM = 150
WORDS_PER_SECOND = WPM / 60.0

def _get_target_bounds(channel_type: str, total_words: int, threshold: int, override: int | None) -> tuple[int, int]:
    """Returns (min_words, max_words) based on pacing rules."""
    if override:
        return override, override

    if channel_type == "sleep_story":
        if total_words < threshold:
            # Phase 1: 4-6 seconds -> 10-15 words
            return 10, 15
        else:
            # Phase 2: 60 seconds -> 150 words
            return 150, 150
    
    # Default (history, etc.): 4-6 seconds -> 10-15 words
    return 10, 15

def split_into_scenes(
    text: str,
    channel_type: str,
    test_phase_threshold: int | None = None,
    override_words_per_scene: int | None = None
) -> list[str]:
    threshold = test_phase_threshold if test_phase_threshold is not None else 1500
    
    # Tokenize the text into sentences/clauses.
    # Keep punctuation with the clause.
    clauses = [c for c in re.split(r'(?<=[.!?,\;:\]\)])\s+', text.strip()) if c.strip()]
    
    scenes = []
    current_scene_words = []
    current_scene_word_count = 0
    total_word_count = 0
    
    target_min, target_max = _get_target_bounds(
        channel_type, total_word_count, threshold, override_words_per_scene
    )

    for clause in clauses:
        words = clause.split()
        word_count = len(words)
        
        # If adding this clause puts us over target_max, we should snap.
        # But we must snap to the *nearest* sentence boundary within the target range if possible.
        # If we are already >= target_min, we can definitely cut before adding this clause.
        # If cutting before this clause puts us closer to the ideal range than cutting after, we cut before.
        # However, the rule says "snap every scene boundary to the nearest sentence or clause end within the target word range"
        # "even if that makes the scene a few words shorter/longer than the ideal range"
        
        if current_scene_word_count > 0:
            dist_without = abs((target_max + target_min) / 2.0 - current_scene_word_count)
            dist_with = abs((target_max + target_min) / 2.0 - (current_scene_word_count + word_count))
            
            # If we are already past or at target_max, we definitely should cut.
            if current_scene_word_count >= target_max:
                scenes.append(" ".join(current_scene_words))
                current_scene_words = []
                current_scene_word_count = 0
                # re-evaluate bounds since total_word_count hasn't advanced for this new clause yet
                target_min, target_max = _get_target_bounds(
                    channel_type, total_word_count, threshold, override_words_per_scene
                )
            # If adding this clause crosses the target_max, snap to whichever is closer to the target ideal range.
            elif (current_scene_word_count + word_count) > target_max:
                if dist_without < dist_with or current_scene_word_count >= target_min:
                    scenes.append(" ".join(current_scene_words))
                    current_scene_words = []
                    current_scene_word_count = 0
                    target_min, target_max = _get_target_bounds(
                        channel_type, total_word_count, threshold, override_words_per_scene
                    )

        current_scene_words.append(clause)
        current_scene_word_count += word_count
        total_word_count += word_count
        
        # If exactly matched max or crossed max after addition, we can cut immediately 
        # (though the loop logic already checks before addition). Let's just let the next iteration or the end handle it.

    if current_scene_words:
        scenes.append(" ".join(current_scene_words))

    return scenes
