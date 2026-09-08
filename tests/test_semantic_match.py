"""Local ranking, and the judgement about when it is trustworthy alone.

The point is cost: candidate selection was ~50% of a run's model spend, and it
is a *ranking* step over candidates that already survived sourcing. When the
providers' own captions make the answer obvious, paying a vision model to
agree is waste. When they do not, the model is worth every cent -- so the
gate here is deliberately conservative.

Nothing in this file affects `image_review`, which still sees every shipped
image.
"""

from __future__ import annotations

import pytest

from core.semantic_match import (
    CLEAR_MARGIN,
    HIGH_CONFIDENCE,
    TOO_WEAK_TO_JUDGE,
    describe,
    rank,
    rank_items,
    relevance,
    tokens_of,
)


# --- tokenising -------------------------------------------------------------

def test_content_words_survive_and_filler_does_not():
    assert tokens_of("a photo of the dark hospital corridor") == [
        "dark", "hospital", "corridor"]


def test_short_words_and_stopwords_are_dropped():
    assert tokens_of("in on at it is the") == []


def test_an_empty_brief_has_no_tokens():
    assert tokens_of("") == []
    assert tokens_of(None) == []


# --- scoring ----------------------------------------------------------------

def test_a_caption_naming_the_subject_scores_high():
    tokens = tokens_of("dark hospital corridor")
    assert relevance("a dark hospital corridor at night", tokens) > 0.9


def test_an_unrelated_caption_scores_zero():
    tokens = tokens_of("dark hospital corridor")
    assert relevance("a bowl of fresh fruit on a table", tokens) == 0.0


def test_partial_coverage_scores_partially():
    tokens = tokens_of("dark hospital corridor")
    score = relevance("a hospital reception desk", tokens)
    assert 0.0 < score < 0.6


def test_adjacent_words_beat_scattered_ones():
    """"fishing boat" together names one thing; apart it often names two."""
    tokens = tokens_of("fishing boat")
    together = relevance("a small fishing boat at dawn", tokens)
    apart = relevance("a boat show, with fishing gear on the far stand", tokens)
    assert together > apart


def test_a_score_never_exceeds_one():
    tokens = tokens_of("ocean waves")
    assert relevance("ocean waves ocean waves ocean waves", tokens) <= 1.0


def test_no_tokens_means_no_confidence():
    assert relevance("anything at all", []) == 0.0


# --- ranking ----------------------------------------------------------------

def test_the_best_description_wins():
    ranking = rank(
        ["a bowl of fruit",
         "a dark hospital corridor with peeling paint",
         "a city street"],
        "dark hospital corridor",
    )
    assert ranking.winner_index == 1


def test_an_empty_shortlist_has_no_winner():
    ranking = rank([], "anything")
    assert ranking.winner_index == -1
    assert ranking.best == 0.0


# --- the confidence judgement ----------------------------------------------

def test_one_clear_match_is_decided_locally():
    """The case worth saving: obvious winner, obvious losers."""
    ranking = rank(
        ["a dark hospital corridor with peeling paint",
         "a bowl of fruit",
         "a city street at noon"],
        "dark hospital corridor",
    )
    assert ranking.confident is True
    assert ranking.best >= HIGH_CONFIDENCE
    assert ranking.margin >= CLEAR_MARGIN


def test_two_equally_good_matches_go_to_the_model():
    """Choosing between them is a visual judgement, not a lexical one."""
    ranking = rank(
        ["a dark hospital corridor with peeling paint",
         "a dark hospital corridor at night"],
        "dark hospital corridor",
    )
    assert ranking.confident is False
    assert ranking.margin < CLEAR_MARGIN


def test_a_uniformly_wrong_shortlist_goes_to_the_model():
    """"None of these fit" is a verdict the local scorer cannot give."""
    ranking = rank(["a bowl of fruit", "a city street"], "dark hospital corridor")
    assert ranking.too_weak is True
    assert ranking.confident is False
    assert ranking.best < TOO_WEAK_TO_JUDGE


def test_a_strong_but_contested_match_is_not_trusted():
    ranking = rank(
        ["ocean waves crashing on rocks", "ocean waves on the rocks"],
        "ocean waves rocks",
    )
    assert ranking.best >= HIGH_CONFIDENCE
    assert ranking.confident is False, "a contested pick was taken locally"


def test_the_reason_explains_the_decision():
    confident = rank(["a dark hospital corridor", "a bowl of fruit"],
                     "dark hospital corridor")
    weak = rank(["a bowl of fruit"], "dark hospital corridor")
    assert "local match" in confident.reason()
    assert "reviewer" in weak.reason()


# --- provider results -------------------------------------------------------

class _Item:
    def __init__(self, title="", query="", attribution="", source_page=""):
        self.title = title
        self.query = query
        self.attribution = attribution
        self.source_page = source_page


def test_a_providers_own_words_are_gathered():
    text = describe(_Item(title="Dark hospital corridor",
                          query="hospital", attribution="A. Photographer"))
    assert "dark hospital corridor" in text
    assert "photographer" in text


def test_items_can_be_ranked_directly():
    ranking = rank_items(
        [_Item(title="a bowl of fruit"),
         _Item(title="a dark hospital corridor with peeling paint")],
        "dark hospital corridor",
    )
    assert ranking.winner_index == 1


def test_an_item_with_no_description_scores_nothing():
    ranking = rank_items([_Item()], "dark hospital corridor")
    assert ranking.best == 0.0
    assert ranking.confident is False


# --- the gate is untouched --------------------------------------------------

def test_this_only_short_circuits_ranking_never_review():
    """The local path returns a chosen file; it never returns an approval."""
    import inspect

    from core import image_sourcer

    source = inspect.getsource(image_sourcer._select_photo_candidate)
    local_block = source[source.index("candidate_descriptions and"):]
    local_block = local_block[: local_block.index("_CANDIDATE_SELECTION_CALLS")]
    # It picks a path. It does not set approved, and does not touch the gate.
    assert "return winner" in local_block
    assert "approved" not in local_block


def test_the_shortcut_needs_real_captions():
    """Without provider descriptions there is nothing to rank on, and the
    model is asked as before."""
    import inspect

    from core import image_sourcer

    source = inspect.getsource(image_sourcer._select_photo_candidate)
    assert "len(candidate_descriptions) == len(candidate_paths)" in source
