"""Tests for TTS text sanitization."""

from __future__ import annotations

import unittest

from src.services.tts_sanitize import sanitize_for_tts, sanitize_scene_text

ANNE_VISUAL_LEAK = (
    "The final visual should be a slow zoom on the famous National Portrait Gallery "
    "painting of Anne Boleyn, but subtly altered in the reflection of her pendant to "
    "show an older, crowned Elizabeth I, leaving the audience with the haunting image "
    "of a legacy fully realized."
)


class TestTtsSanitize(unittest.TestCase):
    def test_drops_visual_only_sentence(self):
        cleaned, warnings = sanitize_scene_text(ANNE_VISUAL_LEAK)
        self.assertEqual(cleaned, "")
        self.assertTrue(any("visual" in w.lower() for w in warnings))

    def test_sanitize_for_tts_visual_leak(self):
        self.assertEqual(sanitize_for_tts(ANNE_VISUAL_LEAK), "")

    def test_year_1536(self):
        cleaned, _ = sanitize_scene_text("The year is 1536.")
        self.assertIn("fifteen thirty-six", cleaned)
        self.assertNotIn("1536", cleaned)

    def test_year_with_comma(self):
        cleaned, _ = sanitize_scene_text("In 1,536 the court trembled.")
        self.assertIn("fifteen thirty-six", cleaned)

    def test_year_1538(self):
        cleaned, _ = sanitize_scene_text("By 1538 the succession was uncertain.")
        self.assertIn("fifteen thirty-eight", cleaned)

    def test_henry_viii(self):
        cleaned, _ = sanitize_scene_text("King Henry VIII paced the hall.")
        self.assertIn("Henry the Eighth", cleaned)
        self.assertNotIn("VIII", cleaned)

    def test_edward_vi(self):
        cleaned, _ = sanitize_scene_text("The young Edward VI waited in the wings.")
        self.assertIn("Edward the Sixth", cleaned)
        self.assertNotIn("VI", cleaned)

    def test_visual_prompt_leakage_prefix(self):
        text = (
            "a painterly still of the dimly lit Tudor bedchamber, with Anne Boleyn "
            "lying pale and sweating on the ornate bed"
        )
        cleaned, warnings = sanitize_scene_text(text)
        self.assertEqual(cleaned, "")
        self.assertTrue(warnings)

    def test_same_recurring_prefix(self):
        cleaned, warnings = sanitize_scene_text(
            "same recurring Anne Boleyn: slender dark-eyed Tudor noblewoman"
        )
        self.assertEqual(cleaned, "")
        self.assertTrue(warnings)

    def test_preserves_normal_narration(self):
        text = "The air in the royal bedchamber was heavy with the scent of fever and fear."
        cleaned, warnings = sanitize_scene_text(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(warnings, [])

    def test_mixed_sentence_strips_visual_clause(self):
        text = (
            "Anne survived the fever. The final visual should be a slow zoom on her portrait."
        )
        cleaned, warnings = sanitize_scene_text(text)
        self.assertIn("Anne survived the fever", cleaned)
        self.assertNotIn("slow zoom", cleaned.lower())

    def test_spell_numbers_for_vo_plain(self):
        from src.services.tts_sanitize import spell_numbers_for_vo

        self.assertIn("twenty six", spell_numbers_for_vo("Count 26 now."))
        out = spell_numbers_for_vo("Stage IV response.")
        self.assertIn("Stage four", out)
        self.assertNotIn("Fourth", out)


if __name__ == "__main__":
    unittest.main()
