"""Candidate selection must reject artwork and lookalikes, not just wrong subjects.

The D.B. Cooper run reached the image review gate with correctly-authored slots
and still failed on what search returned:

    "image 1.2 shows prop $100 bills instead of the requested $20 ransom money,
     and image 3.5 shows an anime cover instead of a real FBI case folder."

The slots were right. The filter that picks a winner from the candidates had no
rule against artwork or replica objects.
"""

import pytest

from prompts import pexels_candidate_selection_prompt


def _rules(**kwargs) -> str:
    """The rendered prompt, whitespace-collapsed.

    The prompt is wrapped for readability, so a phrase can straddle a newline
    ("the real ransom\\nmoney"). Collapsing runs of whitespace lets these
    assert on meaning rather than on where the source happens to wrap.
    """
    defaults = dict(keywords="k", prompt="p", num_images=3, narration="n")
    defaults.update(kwargs)
    return " ".join(pexels_candidate_selection_prompt(**defaults).lower().split())


# --- artwork and merchandise ----------------------------------------------

@pytest.mark.parametrize(
    "term",
    [
        "fan art", "anime", "manga", "cartoon", "illustration", "painting",
        "render", "digital art", "movie poster", "podcast", "meme",
        "merchandise",
    ],
)
def test_artwork_and_merchandise_are_named_as_rejects(term):
    assert term in _rules(), f"{term!r} is not called out as a reject"


def test_cover_art_is_rejected():
    rules = _rules()
    assert "cover" in rules
    for medium in ("book", "album", "dvd", "magazine"):
        assert medium in rules, medium


def test_the_anime_cover_failure_is_addressed():
    """The exact shape that beat the filter: artwork standing in for a document."""
    rules = _rules()
    assert "anime" in rules
    assert "artwork" in rules


# --- archival drawings stay allowed ---------------------------------------

def test_authentic_drawn_artefacts_remain_acceptable():
    """A composite sketch is drawn and is still a real case record."""
    rules = _rules()
    assert "composite sketch" in rules
    assert "wanted poster" in rules
    assert "newspaper" in rules


def test_the_drawn_exception_is_scoped_to_genuine_artefacts():
    """It must not read as a blanket allowance for drawings."""
    rules = _rules()
    assert "acceptable only when" in rules or "acceptable only" in rules
    assert "rather than someone's rendering" in rules


# --- lookalikes and replicas ----------------------------------------------

@pytest.mark.parametrize("term", ["prop", "replica", "novelty", "toy"])
def test_replica_objects_are_rejected(term):
    assert term in _rules(), f"{term!r} is not called out"


def test_the_prop_money_failure_is_addressed():
    """Denomination has to be honoured, not approximated."""
    rules = _rules()
    assert "denomination" in rules
    assert "ransom money" in rules


# --- broken pages ----------------------------------------------------------

@pytest.mark.parametrize(
    "term",
    ["access restricted", "access denied", "403", "404", "paywall",
     "captcha", "cookie", "login"],
)
def test_error_and_block_screens_are_rejected(term):
    assert term in _rules(), f"{term!r} is not called out as a reject"


def test_the_access_restricted_failure_is_addressed():
    """The one genuine reject in the last run was a blocked-page screenshot."""
    rules = _rules()
    assert "access restricted" in rules
    assert "never acceptable" in rules


def test_era_mismatch_is_rejected():
    rules = _rules()
    assert "era" in rules or "decade" in rules


# --- existing behaviour preserved -----------------------------------------

def test_watermark_tie_breaker_is_still_present():
    rules = _rules()
    assert "watermark" in rules
    assert "tie-breaker" in rules


def test_subject_relevance_rules_are_unchanged():
    """The new rules are additive; relevance is still the primary test."""
    rules = _rules()
    assert "what exactly is being talked about at this moment" in rules
    assert "reject the whole set if none of them show the named subject" in rules


def test_football_specific_rule_survives():
    """Other channels' rules must not have been displaced."""
    assert "association football" in _rules()


def test_schema_is_intact():
    out = pexels_candidate_selection_prompt(
        keywords="k", prompt="p", num_images=3, narration="n"
    )
    assert '"winner_index"' in out
    assert '"approved"' in out
    assert "1-based" in out
