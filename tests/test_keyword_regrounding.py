"""Unphotographable keywords are regrounded, not waved through.

Regression cover for a Horror run whose Section 3 slot 5 asked for

    "glowing question mark mystery"  ->  "glowing question mark"

(the arrow is the mood-word strip, which removed "mystery" and left the
graphic device). The validator was right to reject it: no photograph contains
a question mark, so every search candidate would be rejected and the run would
die at the image review gate after sourcing had been paid for.

The fix is not a looser validator. Generation now replaces such briefs with a
concrete subject taken from the script's own words -- the person, place,
object or building the story already named -- and anything that cannot be
regrounded is left alone so validation still reports it.
"""

from __future__ import annotations

import pytest

from core.scripter import (
    _repair_slot_fields,
    conceptual_brief_reason,
    non_photographable_reason,
    reground_keywords,
    strip_unphotographable_fragments,
)


# --- the exact failure ----------------------------------------------------


def test_the_glowing_question_mark_brief_is_regrounded():
    """The reported case, with the story's own narration to draw on."""
    grounded = reground_keywords(
        "glowing question mark mystery",
        prompt="A glowing question mark hanging over the scene",
        narration=(
            "Daniel Reyes answered his landline in the Ashcroft Hotel and "
            "heard his own voice on the line."
        ),
        title="The man who called himself",
    )

    assert grounded, "a brief naming only a graphic device must be replaced"
    assert "question mark" not in grounded.lower()
    assert not non_photographable_reason(grounded), (
        "the replacement must satisfy the same rule the original failed"
    )
    # Regrounded onto the real people and places the narration named.
    assert any(word in grounded for word in ("Daniel", "Reyes", "Ashcroft"))


def test_the_regrounded_brief_would_now_pass_validation():
    """End to end: repair the script, then ask the validator's own predicate."""
    script = {
        "title": "The call from his own number",
        "sections": [
            {
                "id": 3,
                "narration": (
                    "Detective Alan Pryce reviewed the call logs at the "
                    "Fairview Police Station until dawn."
                ),
                "slots": [
                    {
                        "visual": "google_photo",
                        "prompt": "A police station at night",
                        "keywords": "police station night exterior",
                    },
                    {
                        "visual": "google_photo",
                        "prompt": "A glowing question mark over the desk",
                        "keywords": "glowing question mark mystery",
                    },
                ],
            }
        ],
    }

    _repair_slot_fields(script, web_photos_only=True)

    repaired = script["sections"][0]["slots"][1]["keywords"]
    assert non_photographable_reason("glowing question mark mystery")
    assert not non_photographable_reason(repaired), (
        f"{repaired!r} still cannot be sourced as a photograph"
    )


# --- other graphic devices, symbols and abstractions ----------------------


@pytest.mark.parametrize(
    "keywords",
    [
        "glowing question mark mystery",
        "red exclamation mark warning",
        "mysterious background texture",
        "eerie backdrop atmosphere",
        "book cover of the case",
        "movie poster of the incident",
        "podcast thumbnail art",
        "dramatised re-enactment of the call",
        "shadowy figure in the doorway",
        "red circle around the window",
        "cinematic shot of the street",
    ],
)
def test_graphic_devices_are_all_regrounded(keywords):
    grounded = reground_keywords(
        keywords,
        prompt="",
        narration=(
            "Officer Maria Delgado searched the Brookline Apartments and "
            "found the telephone still off the hook."
        ),
        title="The call",
    )

    assert grounded, f"{keywords!r} should have been regrounded"
    assert not non_photographable_reason(grounded), (
        f"{keywords!r} was regrounded to {grounded!r}, which still fails"
    )


def test_a_real_subject_named_beside_a_device_survives():
    """"question mark over the Boeing 727" is really about the aircraft."""
    grounded = reground_keywords(
        "question mark over the Boeing 727 airstair",
        narration="Nothing useful here.",
        title="",
    )

    assert "question mark" not in grounded.lower()
    assert "Boeing" in grounded and "727" in grounded


def test_the_salvage_refuses_a_bare_adjective():
    """Stripping the device from the reported brief leaves only "glowing"."""
    assert strip_unphotographable_fragments("glowing question mark mystery") == (
        "glowing mystery"
    )
    grounded = reground_keywords(
        "glowing question mark mystery",
        narration="Elena Vasquez waited outside the Carlton Theatre.",
    )
    assert grounded.lower() not in {"glowing", "glowing mystery"}
    assert any(word in grounded for word in ("Elena", "Vasquez", "Carlton"))


# --- what must not change -------------------------------------------------


def test_valid_keywords_are_left_alone():
    assert reground_keywords(
        "abandoned lighthouse on the cliff",
        narration="Something else entirely.",
        title="A different thing",
    ) == ""


def test_an_archival_artefact_is_still_allowed():
    """A composite sketch is a drawing and a real case record."""
    assert reground_keywords(
        "FBI composite sketch of the suspect",
        narration="Something else entirely.",
    ) == ""


def test_a_chart_brief_is_left_for_the_slot_type_remedy():
    """Charts need a different slot, not different search terms."""
    assert conceptual_brief_reason("bar chart of the call times")
    assert reground_keywords(
        "bar chart of the call times",
        narration="Detective Alan Pryce reviewed the logs.",
    ) == ""


def test_nothing_is_invented_when_the_story_offers_nothing():
    """No photographable subject anywhere means the brief stays as written.

    Validation then rejects it and the scripter is asked to revise, which is
    the correct outcome -- inventing a subject would put a picture in the
    video that the story never mentioned.
    """
    assert reground_keywords(
        "glowing question mark mystery",
        prompt="an eerie question mark",
        narration="it was strange and eerie",
        title="mystery",
    ) == ""


def test_empty_keywords_are_not_touched():
    assert reground_keywords("", narration="Anything at all") == ""


# --- scoping --------------------------------------------------------------


def _script_with_bad_keywords(visual: str) -> dict:
    return {
        "title": "The call",
        "sections": [
            {
                "id": 1,
                "narration": "Sergeant Ruiz walked the Halton Bridge at dawn.",
                "slots": [
                    {
                        "visual": visual,
                        "prompt": "A question mark",
                        "keywords": "glowing question mark mystery",
                    }
                ],
            }
        ],
    }


def test_regrounding_is_off_for_channels_that_do_not_require_real_photos():
    """Only the channels whose validator enforces the rule are touched."""
    script = _script_with_bad_keywords("google_photo")
    _repair_slot_fields(script, web_photos_only=False)
    assert script["sections"][0]["slots"][0]["keywords"] == (
        "glowing question mark"  # mood-strip only, as before
    )


def test_regrounding_applies_to_the_slot_types_the_validator_polices():
    script = _script_with_bad_keywords("google_photo")
    _repair_slot_fields(script, web_photos_only=True)
    assert "question mark" not in script["sections"][0]["slots"][0]["keywords"]


def test_a_sibling_beats_subject_is_borrowed_before_the_title():
    """A sibling names a real subject the story already established."""
    script = {
        "title": "A story",
        "sections": [
            {
                "id": 2,
                "narration": "it was strange",  # nothing concrete here
                "slots": [
                    {
                        "visual": "google_photo",
                        "prompt": "the tower",
                        "keywords": "Blackwood radio tower",
                    },
                    {
                        "visual": "google_photo",
                        "prompt": "a question mark",
                        "keywords": "glowing question mark mystery",
                    },
                ],
            }
        ],
    }

    _repair_slot_fields(script, web_photos_only=True)

    assert script["sections"][0]["slots"][1]["keywords"] == "Blackwood radio tower"
