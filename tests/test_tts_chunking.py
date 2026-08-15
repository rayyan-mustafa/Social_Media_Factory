"""Unit tests for phoneme-aware sentence packing (hybrid Kokoro TTS)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.services.tts_chunking import (
    group_scenes_into_chapter_chunks,
    pack_sentences_by_phoneme_limit,
    split_sentences,
)


class TestSplitSentences(unittest.TestCase):
    def test_splits_on_sentence_endings_only(self):
        text = (
            "Troops landed at Gravesend, then marched inland. "
            "Elizabeth waited at Windsor! Did London fall?"
        )
        parts = split_sentences(text)
        self.assertEqual(len(parts), 3)
        self.assertIn("Gravesend, then marched inland.", parts[0])
        self.assertTrue(parts[1].endswith("!"))
        self.assertTrue(parts[2].endswith("?"))

    def test_does_not_split_on_semicolon(self):
        text = "Trade shifted; silver flowed into London. Resistance grew."
        parts = split_sentences(text)
        self.assertEqual(len(parts), 2)
        self.assertIn(";", parts[0])


class TestPackSentencesByPhonemeLimit(unittest.TestCase):
    def test_packs_under_cap_without_comma_split(self):
        # Fake phoneme counter: 1 phoneme per character (deterministic).
        text = (
            "Short one. "
            "Another short sentence here. "
            "A longer sentence with commas, and more words, still one unit."
        )

        def char_len(t: str) -> int:
            return len(t)

        batches = pack_sentences_by_phoneme_limit(
            text, max_phonemes=80, phoneme_len=char_len
        )
        self.assertGreaterEqual(len(batches), 2)
        for b in batches:
            self.assertLessEqual(char_len(b), 80)
        # Comma-bearing sentence stays intact as one string (or alone in a batch).
        joined = " ".join(batches)
        self.assertIn("commas, and more words, still one unit.", joined)
        # Never produced a batch that ends mid-clause at a comma-only break
        # of the third sentence into two parts.
        comma_sentence = (
            "A longer sentence with commas, and more words, still one unit."
        )
        self.assertTrue(
            any(comma_sentence in b for b in batches),
            f"comma sentence was split across batches: {batches}",
        )

    def test_single_oversized_sentence_word_splits(self):
        sent = "Alpha bravo charlie delta echo foxtrot."

        def wordish(t: str) -> int:
            return len(t.split()) * 10

        batches = pack_sentences_by_phoneme_limit(
            sent, max_phonemes=25, phoneme_len=wordish
        )
        self.assertGreater(len(batches), 1)
        self.assertEqual(" ".join(batches), sent)

    def test_empty(self):
        self.assertEqual(pack_sentences_by_phoneme_limit(""), [])


class TestGroupChapterChunks(unittest.TestCase):
    def test_one_chunk_per_chapter(self):
        scenes = [
            SimpleNamespace(index=0, text="A.", chapter_id=1, word_count=1),
            SimpleNamespace(index=1, text="B.", chapter_id=1, word_count=1),
            SimpleNamespace(index=2, text="C.", chapter_id=2, word_count=1),
        ]
        chunks = group_scenes_into_chapter_chunks(scenes)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].scene_indices, [0, 1])
        self.assertEqual(chunks[1].scene_indices, [2])

    def test_word_budget_subchunks(self):
        scenes = [
            SimpleNamespace(index=i, text=f"S{i}.", chapter_id=1, word_count=50)
            for i in range(5)
        ]
        chunks = group_scenes_into_chapter_chunks(scenes, target_chunk_words=120)
        self.assertGreater(len(chunks), 1)
        all_idx = [i for c in chunks for i in c.scene_indices]
        self.assertEqual(all_idx, list(range(5)))


if __name__ == "__main__":
    unittest.main()
