"""Horror Stories in Arabic or English, with bilingual visual search.

Two separate concerns, deliberately kept apart:

  * script language decides narration, captions and the finished video;
  * visual search always runs in BOTH scripts, because the language a story
    is told in has nothing to do with the language its photographs are
    catalogued under. Flight 19's record is entirely English; a Saudi case's
    record is largely Arabic. Either video may need either archive.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from core.bilingual_queries import (
    arabic_terms,
    build_bilingual_queries,
    is_single_script,
    latin_terms,
    script_of,
)
from core.utils import available_languages, load_channel_config

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HORROR_JSON = json.loads(
    (REPO_ROOT / "config" / "channels" / "horror_stories.json")
    .read_text(encoding="utf-8")
)


# --- language variants ------------------------------------------------------

def test_horror_offers_arabic_and_english():
    assert set(available_languages(HORROR_JSON)) == {"ar", "en"}


def test_arabic_is_the_default():
    assert load_channel_config("horror_stories").language == "ar"
    assert available_languages(HORROR_JSON)[0] == "ar"


@pytest.mark.parametrize("code,voice_locale", [("ar", "ar-SA"), ("en", "en-US")])
def test_language_moves_narration_and_voice_together(code, voice_locale):
    """A half-applied variant writes English and reads it aloud in Arabic."""
    cfg = load_channel_config("horror_stories", language=code)
    assert cfg.language == code
    assert cfg.voice.language == voice_locale


def test_english_variant_rewrites_the_script_instructions():
    en = load_channel_config("horror_stories", language="en")
    assert "Write English horror narration" in en.script_style.instructions
    assert "Modern Standard Arabic" not in en.script_style.instructions


def test_arabic_variant_keeps_the_arabic_instructions():
    ar = load_channel_config("horror_stories", language="ar")
    assert "Modern Standard Arabic" in ar.script_style.instructions


@pytest.mark.parametrize("code", ["ar", "en"])
def test_truth_rules_survive_in_both_languages(code):
    """The verified/reported/unverified contract is language-independent."""
    cfg = load_channel_config("horror_stories", language=code)
    for token in ("[VERIFIED]", "[REPORTED]", "[UNVERIFIED]"):
        assert token in cfg.script_style.instructions, (code, token)
    assert "openly fiction" in cfg.script_style.instructions


@pytest.mark.parametrize("code", ["ar", "en"])
def test_safety_and_sourcing_settings_are_identical_in_both(code):
    cfg = load_channel_config("horror_stories", language=code)
    # Both language variants source their visuals the same way: what changes
    # between ar and en is the words, never the sourcing or safety policy.
    assert cfg.image_sourcing.prefer_generated_visuals is True
    assert cfg.image_sourcing.web_photos_only is False
    assert cfg.rendering_defaults.max_visual_hold_seconds == 5.0
    assert cfg.video.resolution == [1080, 1920]


def test_english_cta_is_in_english():
    en = load_channel_config("horror_stories", language="en")
    rules = " ".join(en.business_strategy.cta_rules)
    assert "part two" in rules.lower()
    assert "الجزء الثاني" not in rules


def test_arabic_cta_is_in_arabic():
    ar = load_channel_config("horror_stories", language="ar")
    assert "الجزء الثاني" in " ".join(ar.business_strategy.cta_rules)


def test_an_unsupported_language_is_refused():
    with pytest.raises(ValueError, match="does not offer script language"):
        load_channel_config("horror_stories", language="fr")


def test_football_news_is_unaffected():
    cfg = load_channel_config("football_news")
    assert cfg.language == "ar"
    assert cfg.language_variants == {}
    # And asking it for a language it never declared is refused, not silently
    # applied.
    with pytest.raises(ValueError):
        load_channel_config("football_news", language="en")


def test_no_language_argument_leaves_the_base_config_untouched():
    base = load_channel_config("horror_stories")
    explicit = load_channel_config("horror_stories", language="ar")
    assert base.language == explicit.language == "ar"


# --- script detection -------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Flight 19 TBM Avenger", "latin"),
        ("الرحلة 19 مثلث برمودا", "arabic"),
        ("الرحلة 19 archival photograph", "mixed"),
        ("1945", "none"),
    ],
)
def test_script_detection(text, expected):
    assert script_of(text) == expected


def test_mixed_script_queries_are_rejected():
    """Search engines tokenise the scripts separately; mixing matches nothing."""
    assert is_single_script("الرحلة 19 archival photograph") is False
    assert is_single_script("Flight 19 archival photograph") is True
    assert is_single_script("مثلث برمودا صورة أرشيفية") is True


# --- term extraction --------------------------------------------------------

def test_latin_terms_keep_proper_nouns_and_drop_filler():
    terms = latin_terms("Flight 19 TBM Avenger photograph of the Fort Lauderdale")
    assert "Flight" in terms and "Avenger" in terms and "Lauderdale" in terms
    for filler in ("photograph", "of", "the"):
        assert filler not in [t.lower() for t in terms], filler


def test_arabic_terms_are_extracted_separately():
    terms = arabic_terms("مثلث برمودا الرحلة 19")
    assert "برمودا" in terms
    assert all(not re.search(r"[A-Za-z]", t) for t in terms)


# --- bilingual query building ----------------------------------------------

def _flight19_queries(**over):
    params = dict(
        keywords="Flight 19 TBM Avenger Fort Lauderdale 1945",
        title="اختفاء الرحلة 19: كيف تلاشت 5 طائرات حربية في مثلث برمودا؟",
        narration="في 5 ديسمبر 1945 اختفت خمس قاذفات",
        entities=["Flight 19", "Bermuda Triangle"],
    )
    params.update(over)
    return build_bilingual_queries(**params)


def test_english_queries_lead():
    """Historical material is indexed under its original names."""
    queries = _flight19_queries()
    assert queries, "no queries produced"
    assert script_of(queries[0]) == "latin"
    assert "Flight" in queries[0]


def test_arabic_queries_are_still_produced():
    """Regional sources hold photographs English archives never catalogued."""
    queries = _flight19_queries()
    assert any(script_of(q) == "arabic" for q in queries)


def test_no_query_mixes_scripts():
    """The exact defect from the last run: 'الرحلة 19 archival photograph'."""
    for q in _flight19_queries():
        assert is_single_script(q), q


def test_the_arabic_title_never_contaminates_an_english_query():
    for q in _flight19_queries():
        if script_of(q) == "latin":
            assert "الرحلة" not in q


def test_an_english_only_story_still_produces_queries():
    queries = build_bilingual_queries(
        keywords="Mary Celeste ghost ship 1872",
        title="The Mary Celeste",
        entities=["Mary Celeste"],
    )
    assert queries
    assert all(is_single_script(q) for q in queries)
    assert any("Celeste" in q for q in queries)


def test_an_arabic_only_story_still_produces_queries():
    queries = build_bilingual_queries(
        keywords="بئر برهوت اليمن",
        title="أسطورة بئر برهوت",
        entities=["بئر برهوت"],
    )
    assert queries
    assert all(is_single_script(q) for q in queries)
    assert any(script_of(q) == "arabic" for q in queries)


def test_queries_stay_search_sized():
    for q in _flight19_queries(keywords="a " * 200):
        assert len(q) <= 120, len(q)


def test_no_duplicate_queries():
    queries = _flight19_queries()
    assert len(queries) == len(set(q.lower() for q in queries))


def test_empty_input_produces_no_queries():
    assert build_bilingual_queries() == []


def test_search_language_is_independent_of_script_language():
    """An English video still searches Arabic, and vice versa."""
    same = build_bilingual_queries(
        keywords="Flight 19 Avenger",
        title="اختفاء الرحلة 19",
        entities=["Flight 19"],
    )
    scripts = {script_of(q) for q in same}
    assert "latin" in scripts and "arabic" in scripts
