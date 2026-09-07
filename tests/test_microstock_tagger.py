"""Metadata normalisation — the roadmap's anti-spam rules, enforced in code."""

from __future__ import annotations

from src.microstock.ai_tagger import _normalise_category, normalise


def test_keyword_stuffed_title_is_repaired():
    meta = normalise({"title": "icon vector graphic design flat asset cloud server"})
    assert "keyword-stuffed" in " ".join(meta.warnings)
    assert "vector" not in meta.title.lower()
    assert "cloud" in meta.title.lower()


def test_genuine_title_is_left_alone():
    meta = normalise({"title": "Isometric cloud server rack diagram", "keywords": []})
    assert meta.title == "Isometric cloud server rack diagram"
    assert not any("stuffed" in w for w in meta.warnings)


def test_title_is_truncated_to_max_words():
    meta = normalise({"title": " ".join(f"word{i}" for i in range(30))})
    assert len(meta.title.split()) == 10
    assert any("truncated" in w for w in meta.warnings)


def test_short_title_is_warned_not_dropped():
    meta = normalise({"title": "Cloud server"})
    assert meta.title == "Cloud server"
    assert any("only 2 words" in w for w in meta.warnings)


def test_trailing_boilerplate_is_stripped():
    assert "eps" not in normalise({"title": "Cloud server rack diagram - EPS10"}).title.lower()


def test_keywords_are_deduplicated_case_insensitively():
    meta = normalise({"title": "x", "keywords": ["Cloud", "cloud", " CLOUD ", "server"]})
    assert meta.keywords == ["cloud", "server"]


def test_keywords_are_clamped_to_maximum():
    meta = normalise({"title": "x", "keywords": [f"kw{i}" for i in range(100)]})
    assert len(meta.keywords) == 35
    assert any("trimmed" in w for w in meta.warnings)


def test_too_few_keywords_warns():
    assert any("min 25" in w for w in normalise({"title": "x", "keywords": ["a", "b"]}).warnings)


def test_comma_separated_keyword_string_is_accepted():
    meta = normalise({"title": "x", "keywords": "cloud, server, rack"})
    assert meta.keywords == ["cloud", "server", "rack"]


def test_category_matches_in_both_directions():
    assert _normalise_category("tech") == "Technology"
    assert _normalise_category("Technology and Computing") == "Technology"
    assert _normalise_category("BUSINESS") == "Business"


def test_unknown_category_falls_back_to_abstract():
    assert _normalise_category("qqq") == "Abstract"
    assert _normalise_category("") == "Abstract"


def test_mock_backend_produces_compliant_metadata(tmp_path):
    from src.microstock.ai_tagger import tag_image

    image = tmp_path / "cloud server rack.png"
    image.write_bytes(b"not really a png")
    meta = tag_image(image, backend="mock")
    assert meta.title and meta.keywords
    assert len(meta.keywords) <= 35
