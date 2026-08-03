import pytest
from src.services.scene_splitter import split_into_scenes

def test_sleep_story_pacing_logic():
    # Generate a ~100-word dummy script
    # We will use simple short clauses to make sure counting is easy.
    # E.g. "This is a short clause. " (5 words). 
    # 20 clauses * 5 = 100 words.
    
    clause = "This is a short clause. "
    text = clause * 20  # 100 words total
    
    # We want phase 1 to apply before word 50. 
    # Phase 1: 10-15 words per scene.
    # Phase 2: 150 words per scene.
    
    scenes = split_into_scenes(
        text=text,
        channel_type="sleep_story",
        test_phase_threshold=50
    )
    
    # Let's count words per scene to verify.
    word_counts = [len(scene.split()) for scene in scenes]
    print(f"Generated scenes word counts: {word_counts}")
    print(f"Total scenes: {len(scenes)}")
    print(f"Total words: {sum(word_counts)}")
    
    # Before word 50, each scene should be between 10-15 words.
    # Since each clause is 5 words, to get 10-15 we need 2 or 3 clauses (10 or 15 words).
    # After word 50, scenes should aim for 150 words. 
    # There are only 50 words left, so they should all be grouped into one final scene.
    
    # Analyze the scenes
    accumulated = 0
    phase_1_scenes = []
    phase_2_scenes = []
    
    for count in word_counts:
        # Note: The transition happens when accumulated crosses 50.
        # But if a scene was in progress, it finishes phase 1 rules.
        if accumulated < 50:
            phase_1_scenes.append(count)
        else:
            phase_2_scenes.append(count)
        accumulated += count
        
    for c in phase_1_scenes:
        assert 10 <= c <= 15, f"Phase 1 scene has {c} words, expected 10-15"
        
    # Phase 2 has 40 words left total because the 4th scene started at word 45, finished at 60 (phase 1 rules).
    assert len(phase_2_scenes) == 1, f"Phase 2 should just group the remaining words into 1 scene, got {len(phase_2_scenes)}"
    assert phase_2_scenes[0] == 40, f"Phase 2 scene should have exactly 40 words left, got {phase_2_scenes[0]}"

def test_history_pacing_logic():
    clause = "This is a short clause. "
    text = clause * 20  # 100 words total
    
    scenes = split_into_scenes(
        text=text,
        channel_type="history"
    )
    
    word_counts = [len(scene.split()) for scene in scenes]
    print(f"History generated scenes word counts: {word_counts}")
    
    # All scenes should be 10-15 words
    for c in word_counts:
        assert 10 <= c <= 15, f"History scene has {c} words, expected 10-15"

def test_override_pacing_logic():
    clause = "This is a short clause. "
    text = clause * 20  # 100 words total
    
    scenes = split_into_scenes(
        text=text,
        channel_type="some_other_type",
        override_words_per_scene=20
    )
    
    word_counts = [len(scene.split()) for scene in scenes]
    print(f"Override generated scenes word counts: {word_counts}")
    
    # With override=20, and clauses=5, we expect exactly 20 words (4 clauses) per scene
    for c in word_counts:
        assert c == 20, f"Override scene has {c} words, expected 20"
