"""Unit tests for Weird Human Biology script factory (no live network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.services.tts_sanitize import spell_numbers_for_vo
from src.services.weird_biology_research import (
    SourceItem,
    SourcePack,
    SourcePackInsufficient,
    _parse_pubmed_xml,
    build_source_pack,
    save_source_pack,
)
from src.services.weird_biology_script import (
    check_section_bands,
    format_script_sections_md,
    parse_script_output,
    word_count,
)
from src.services.weird_biology_claim_fix import (
    extract_unverified_claims,
    format_claims_block,
    parse_claim_fix_output,
)
from src.services.weird_biology_validate import (
    WeirdBiologyValidationError,
    parse_validation_report,
)


SAMPLE_PUBMED_XML = """<?xml version="1.0"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2023//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_230101.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <Journal>
          <Title>Journal of Goosebumps</Title>
          <JournalIssue><PubDate><Year>2019</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Piloerection mechanisms in cold exposure</ArticleTitle>
        <AuthorList>
          <Author><LastName>Smith</LastName><Initials>A</Initials></Author>
        </AuthorList>
        <Abstract>
          <AbstractText Label="Background">Goosebumps arise from arrector pili muscle contraction under sympathetic control in 48 healthy volunteers.</AbstractText>
          <AbstractText Label="Results">Mean latency was 2.5 seconds after cold onset in 90 percent of subjects.</AbstractText>
        </Abstract>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>87654321</PMID>
      <Article>
        <Journal>
          <Title>Neurophysiology Letters</Title>
          <JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Sympathetic pathways and cutaneous reflexes</ArticleTitle>
        <Abstract>
          <AbstractText>This study of 120 participants showed that emotional piloerection and cold-induced piloerection share a common sympathetic efferent pathway at the spinal level.</AbstractText>
        </Abstract>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


class TestPubMedParse(unittest.TestCase):
    def test_parse_pubmed_xml(self):
        items = _parse_pubmed_xml(SAMPLE_PUBMED_XML)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i.usable() for i in items))
        self.assertEqual(items[0].pmid, "12345678")
        self.assertIn("arrector pili", items[0].excerpt.lower())
        self.assertTrue(items[0].claims_candidates)
        self.assertIn("pubmed.ncbi.nlm.nih.gov/12345678", items[0].url)

    def test_source_pack_material_and_save(self):
        items = _parse_pubmed_xml(SAMPLE_PUBMED_XML)
        pack = SourcePack(
            topic="goosebumps",
            fetched_at="2026-01-01T00:00:00Z",
            items=items,
            min_usable=2,
        )
        text = pack.source_material_text()
        self.assertIn("PMID 12345678", text)
        self.assertIn("120 participants", text)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source_pack.json"
            save_source_pack(pack, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["usable_count"], 2)
            self.assertEqual(len(data["items"]), 2)


class TestScriptParseAndBands(unittest.TestCase):
    def _fake_sections(self, counts: dict[str, int]) -> str:
        parts = ["=== SCRIPT SECTIONS ==="]
        bodies = {}
        for name, n in counts.items():
            # Generate ~n words
            body = " ".join(["word"] * n)
            bodies[name] = body
            parts.append(f"## {name}\n\n{body}\n")
        parts.append("=== VO RAW ===")
        parts.append("\n\n".join(bodies[k] for k in counts))
        return "\n".join(parts)

    def test_parse_and_bands_ok(self):
        raw = self._fake_sections(
            {
                "SECTION 1: COLD HOOK": 100,
                "SECTION 2: CONTEXT & SETUP": 200,
                "SECTION 3: CORE DATA & EXPERIMENT": 500,
                "SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE": 375,
                "SECTION 5: IMPACTFUL OUTRO": 175,
            }
        )
        sections, vo = parse_script_output(raw)
        self.assertEqual(len(sections), 5)
        self.assertTrue(vo)
        checks = check_section_bands(sections)
        self.assertTrue(all(c.ok for c in checks))
        md = format_script_sections_md(sections)
        self.assertIn("## SECTION 1: COLD HOOK", md)

    def test_bands_fail(self):
        sections = {
            "SECTION 1: COLD HOOK": " ".join(["x"] * 50),
            "SECTION 2: CONTEXT & SETUP": " ".join(["x"] * 200),
            "SECTION 3: CORE DATA & EXPERIMENT": " ".join(["x"] * 500),
            "SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE": " ".join(["x"] * 375),
            "SECTION 5: IMPACTFUL OUTRO": " ".join(["x"] * 175),
        }
        checks = check_section_bands(sections)
        self.assertFalse(checks[0].ok)
        self.assertIn("expected 90-110", checks[0].detail)

    def test_word_count(self):
        self.assertEqual(word_count("twenty six percent"), 3)


class TestValidationParse(unittest.TestCase):
    def test_all_verified(self):
        ok, status, n = parse_validation_report(
            "ALL CLAIMS VERIFIED\n\n| Claim | Found | Quote | Risk |\n| a | Yes | q | |"
        )
        self.assertTrue(ok)
        self.assertEqual(n, 0)
        self.assertIn("ALL CLAIMS VERIFIED", status)

    def test_failed(self):
        ok, status, n = parse_validation_report(
            "VALIDATION FAILED — 3 UNVERIFIED CLAIMS\n\n| Claim | No | | High |"
        )
        self.assertFalse(ok)
        self.assertEqual(n, 3)
        self.assertIn("3", status)

    def test_ambiguous(self):
        with self.assertRaises(WeirdBiologyValidationError):
            parse_validation_report("Looks mostly fine to me.")


class TestClaimFixHelpers(unittest.TestCase):
    def test_extract_unverified_from_table(self):
        report = """VALIDATION FAILED — 2 UNVERIFIED CLAIMS

| Claim | Found in Source (Yes/No) | Source Quote (if Yes) | Risk Level |
|-------|--------------------------|----------------------|------------|
| 48 healthy volunteers | Yes | Mean latency… | |
| 90 percent of subjects | No | | High |
| University of Atlantis study | No | | High |
| [SOURCE NEEDED: exact dosage] | No | | High |
"""
        claims = extract_unverified_claims(report)
        self.assertEqual(len(claims), 3)
        self.assertTrue(any("90 percent" in c for c in claims))
        self.assertTrue(any("Atlantis" in c for c in claims))
        self.assertTrue(any("SOURCE NEEDED" in c for c in claims))
        self.assertFalse(any("48 healthy" in c for c in claims))

    def test_extract_source_needed_from_script(self):
        report = "VALIDATION FAILED — 1 UNVERIFIED CLAIMS\n\n(Local gate)"
        script = "Text with [SOURCE NEEDED: missing PMID] here."
        claims = extract_unverified_claims(report, script=script)
        self.assertEqual(claims, ["[SOURCE NEEDED: missing PMID]"])

    def test_format_claims_block_fallback(self):
        block = format_claims_block([], "VALIDATION FAILED — 1 UNVERIFIED CLAIMS\nbody")
        self.assertIn("No discrete claim rows", block)
        self.assertIn("VALIDATION FAILED", block)

    def test_parse_claim_fix_output_spells_numbers(self):
        bodies = {
            "SECTION 1: COLD HOOK": " ".join(["word"] * 100),
            "SECTION 2: CONTEXT & SETUP": "About 26 subjects joined.",
            "SECTION 3: CORE DATA & EXPERIMENT": " ".join(["word"] * 500),
            "SECTION 4: DILEMMA / PHILOSOPHICAL DEBATE": " ".join(["word"] * 375),
            "SECTION 5: IMPACTFUL OUTRO": " ".join(["word"] * 175),
        }
        # Keep section 2 short body as-is for spell check; pad other sections
        bodies["SECTION 1: COLD HOOK"] = " ".join(["hook"] * 100)
        bodies["SECTION 2: CONTEXT & SETUP"] = (
            "About 26 subjects joined in twenty nineteen. " + " ".join(["ctx"] * 190)
        )
        parts = ["=== SCRIPT SECTIONS ==="]
        for name, body in bodies.items():
            parts.append(f"## {name}\n\n{body}\n")
        parts.append("=== VO RAW ===")
        parts.append("\n\n".join(bodies.values()))
        md, vo = parse_claim_fix_output("\n".join(parts))
        self.assertIn("## SECTION 1: COLD HOOK", md)
        self.assertIn("twenty six", md)
        self.assertNotIn("26", md)
        self.assertIn("twenty six", vo)


class TestSpellNumbersForVo(unittest.TestCase):
    def test_twenty_six(self):
        out = spell_numbers_for_vo("There were 26 subjects.")
        self.assertIn("twenty six", out)
        self.assertNotIn("26", out)

    def test_percent(self):
        out = spell_numbers_for_vo("About 90% improved.")
        self.assertIn("ninety percent", out)

    def test_roman_iv(self):
        out = spell_numbers_for_vo("Type IV fibers fired.")
        self.assertIn("Type four", out)
        self.assertNotIn("IV", out)
        self.assertNotIn("Fourth", out)

    def test_year_still_works(self):
        out = spell_numbers_for_vo("In 2019 the lab published.")
        self.assertIn("twenty nineteen", out)
        self.assertNotIn("2019", out)


class TestBuildPackGate(unittest.TestCase):
    def test_insufficient_raises_with_pack(self):
        # Monkeypatch gather via empty items by calling build with mocked gather
        import src.services.weird_biology_research as mod

        original = mod.gather_sources

        def fake_gather(topic, **kwargs):
            return SourcePack(
                topic=topic,
                fetched_at="2026-01-01T00:00:00Z",
                items=[
                    SourceItem(
                        url="https://pubmed.ncbi.nlm.nih.gov/1/",
                        title="thin",
                        fetched_at="2026-01-01T00:00:00Z",
                        excerpt="too short",
                        source_hub="pubmed",
                        status="ok",
                    )
                ],
                min_usable=3,
            )

        mod.gather_sources = fake_gather  # type: ignore[assignment]
        try:
            with self.assertRaises(SourcePackInsufficient) as ctx:
                build_source_pack("x", min_usable=3)
            self.assertIsNotNone(ctx.exception.pack)
        finally:
            mod.gather_sources = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
