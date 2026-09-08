"""A regenerated image must not repeat the rejected one.

Observed across three real runs: the same beat came back as "a fireplace
screen instead of a security desk" every time, because regeneration rebuilt
the prompt from the brief that had just been rejected. The reviewer had said
what it wanted, and that feedback was being discarded.
"""

from __future__ import annotations

import pytest

from core.image_sourcer import _corrected_brief


def _desc(prompt="", keywords="", suggestion=""):
    return {
        "prompt": prompt,
        "keywords": keywords,
        "review_suggestion": suggestion,
    }


def test_the_reviewers_correction_leads_the_prompt():
    """Generators weight the opening of a prompt most heavily, so the
    correction goes first and the staging follows."""
    brief = _corrected_brief(_desc(
        prompt="a dim room with a desk",
        suggestion="an abandoned hospital security desk with monitors",
    ))
    assert brief.startswith("an abandoned hospital security desk with monitors")
    assert "a dim room with a desk" in brief


def test_without_feedback_the_brief_is_unchanged():
    assert _corrected_brief(_desc(prompt="a dark corridor")) == "a dark corridor"


def test_keywords_are_used_when_there_is_no_prompt():
    assert _corrected_brief(_desc(keywords="dark corridor")) == "dark corridor"


def test_feedback_alone_is_enough_to_regenerate():
    assert _corrected_brief(_desc(suggestion="a security desk")) == "a security desk"


def test_an_empty_descriptor_yields_an_empty_brief():
    assert _corrected_brief(_desc()) == ""


def test_whitespace_only_feedback_is_ignored():
    assert _corrected_brief(
        _desc(prompt="a dark corridor", suggestion="   ")) == "a dark corridor"


def test_the_regenerated_prompt_actually_differs_from_the_rejected_one():
    """The property that matters: a second attempt asks for something else."""
    original = _desc(prompt="a dim room with a desk")
    corrected = _desc(
        prompt="a dim room with a desk",
        suggestion="an abandoned hospital security desk with monitors",
    )
    assert _corrected_brief(corrected) != _corrected_brief(original)


def test_the_complaint_is_never_what_gets_generated():
    """The reviewer's complaint names the wrong subject ("a fireplace
    screen"); putting that in a prompt asks for more of it. Only the
    suggestion field is used."""
    import inspect

    source = inspect.getsource(_corrected_brief)
    assert "review_suggestion" in source
    assert "issues" not in source


def test_generation_consumes_the_corrected_brief():
    import inspect

    from core import image_sourcer

    source = inspect.getsource(image_sourcer._generate_missing_visuals)
    assert "_corrected_brief(d)" in source, (
        "generation still builds its prompt from the rejected brief"
    )
