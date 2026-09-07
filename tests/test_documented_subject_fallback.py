"""An unsourceable slot falls back to the real subject inside it.

The image review gate kept rejecting correct archival photographs because the
slot had asked for a staged scene around the subject:

    "image 2.3 fails to show the specific action requested (holding cash)"
    "image 2.1 ... showing only two bills instead of stacks"

The archive holds one photograph of the Cooper money: the decayed Tina Bar
bundles. It cannot also show hands holding stacks. Stripping the staging and
searching for the subject finds the real photograph instead of dropping the
beat or inventing a picture.
"""

import pytest

from core.image_sourcer import _documented_subject_queries


def _first(keywords: str) -> str:
    variants = _documented_subject_queries(keywords)
    assert variants, f"no fallback produced for {keywords!r}"
    return variants[0]


# --- staged actions are stripped ------------------------------------------

@pytest.mark.parametrize(
    "keywords,expected_gone",
    [
        ("hands holding stacks of 1971 twenty dollar bills", "holding"),
        ("close-up of the clip-on tie", "close-up"),
        ("photograph of the Boeing 727 airstair", "photograph of"),
        ("an investigator examining the case file", "examining"),
        ("a man holding the ransom money", "holding"),
        ("scene showing the parachute rigging", "showing"),
    ],
)
def test_staging_is_removed(keywords, expected_gone):
    assert expected_gone.lower() not in _first(keywords).lower()


def test_the_real_subject_survives():
    out = _first("hands holding stacks of 1971 twenty dollar bills")
    assert "twenty dollar bills" in out.lower()


def test_a_year_is_kept_because_the_era_matters():
    """Stripping the year would trade one gate failure for another.

    The gate rejected a 2013-series bill in a 1971 story, so the era is the one
    qualifier that must survive simplification.
    """
    assert "1971" in _first("hands holding stacks of 1971 twenty dollar bills")
    assert "1971" in _first("close-up of the 1971 Northwest Orient Boeing 727")


def test_small_counts_are_still_stripped():
    """Only years survive; "two", "12" and friends are staging, not subject."""
    assert "2 " not in _first("2 parachutes on the tarmac")
    assert "12" not in _first("12 evidence bags")


@pytest.mark.parametrize(
    "keywords",
    [
        "stacks of banknotes",
        "piles of evidence bags",
        "bundles of ransom money",
        "several parachutes",
        "a single clip-on tie",
    ],
)
def test_quantities_are_removed(keywords):
    out = _first(keywords).lower()
    for q in ("stacks of", "piles of", "bundles of", "several", "a single"):
        assert q not in out


# --- atmosphere is stripped ------------------------------------------------

@pytest.mark.parametrize(
    "keywords,gone",
    [
        ("Portland airport at night", "at night"),
        ("Columbia River in the fog", "in the fog"),
        ("Boeing 727 dramatic lighting", "dramatic lighting"),
        ("Lewis River aerial view", "aerial view"),
    ],
)
def test_atmosphere_is_removed(keywords, gone):
    variants = _documented_subject_queries(keywords)
    assert any(gone.lower() not in v.lower() for v in variants)


# --- the ladder narrows ----------------------------------------------------

def test_variants_get_progressively_simpler():
    variants = _documented_subject_queries(
        "close-up of hands holding stacks of 1971 twenty dollar bills at night"
    )
    assert len(variants) >= 2
    lengths = [len(v.split()) for v in variants]
    assert lengths == sorted(lengths, reverse=True), lengths


def test_a_tail_noun_phrase_is_offered_last():
    variants = _documented_subject_queries(
        "an investigator examining the recovered ransom money at dusk"
    )
    assert len(variants[-1].split()) <= 3


# --- inert where it should be ---------------------------------------------

def test_an_already_concrete_subject_yields_nothing_or_itself():
    """A clean archival request needs no rescue."""
    variants = _documented_subject_queries("FBI evidence photograph of the clip-on tie")
    assert all(v.lower() != "" for v in variants)


def test_empty_input_is_safe():
    assert _documented_subject_queries("") == []
    assert _documented_subject_queries(None) == []


def test_no_variant_is_blank():
    for kw in ("stacks of", "close-up of", "hands holding"):
        assert all(v.strip() for v in _documented_subject_queries(kw))


def test_original_is_never_returned_as_a_variant():
    kw = "Boeing 727 Northwest Orient 1971"
    assert kw not in _documented_subject_queries(kw)


# --- the bare title is not a rescue ----------------------------------------

from core.image_sourcer import _relaxed_query_tiers  # noqa: E402
from core.utils import Script, ScriptSection, VisualSlot  # noqa: E402


def _tiers(*, web_photos_only: bool):
    script = Script(
        title="Can a necktie solve the D.B. Cooper mystery?",
        video_type="narrative",
        sections=[ScriptSection(id=1, narration="n", slots=[])],
    )
    desc = {
        "slot": VisualSlot(
            visual="google_photo",
            keywords="FBI evidence photograph of the recovered ransom money",
            prompt="The recovered ransom money.",
        ),
        "section": script.sections[0],
        "sub_idx": 0,
    }
    return _relaxed_query_tiers(desc, script, web_photos_only=web_photos_only)


def test_bare_title_is_dropped_on_a_web_photo_channel():
    """A slot needing the ransom money was widened to the title and got a plane.

    The gate called it "a complete subject mismatch, showing an airplane
    instead of the requested ransom money" -- correct, and caused entirely by
    searching the story title for a specific beat.
    """
    title = "can a necktie solve the d.b. cooper mystery?"
    assert all(t.lower() != title for t in _tiers(web_photos_only=True))


def test_bare_title_is_kept_for_other_channels():
    title = "can a necktie solve the d.b. cooper mystery?"
    assert any(t.lower() == title for t in _tiers(web_photos_only=False))


def test_the_slot_subject_still_leads_the_ladder():
    tiers = _tiers(web_photos_only=True)
    assert tiers, "the ladder must not be emptied"
    assert "ransom money" in tiers[0].lower()
