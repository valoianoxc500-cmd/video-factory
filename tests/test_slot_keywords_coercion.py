"""List-shaped slot keywords must not kill a paid run.

Reproduces the D.B. Cooper failure, which died after research, script
generation and the script_review gate had all completed:

    1 validation error for ScriptSection
    slots.4.keywords
      Input should be a valid string
      [type=string_type, input_value=['unsolved mystery background'],
       input_type=list]
"""

import pytest

from core.scripter import _coerce_slot_keywords
from core.utils import ScriptSection


def _payload(keywords):
    """A section shaped like the model's output, with slot 5 under test."""
    return {
        "sections": [
            {
                "id": 1,
                "narration": "في عام 1971، اختطف رجل غامض طائرة بوينج 727.",
                "slots": [
                    {"visual": "google_photo", "keywords": "Boeing 727 1971", "prompt": "p1"},
                    {"visual": "google_photo", "keywords": "Portland airport 1971", "prompt": "p2"},
                    {"visual": "google_photo", "keywords": "parachute rigging", "prompt": "p3"},
                    {"visual": "google_photo", "keywords": "ransom banknotes", "prompt": "p4"},
                    {"visual": "google_photo", "keywords": keywords, "prompt": "p5"},
                ],
            }
        ]
    }


# --- the exact failing payload --------------------------------------------


def test_the_exact_failing_payload_now_builds():
    data = _payload(["unsolved mystery background"])
    _coerce_slot_keywords(data)
    section = ScriptSection(**data["sections"][0])
    assert section.slots[4].keywords == "unsolved mystery background"


def test_without_coercion_the_payload_still_fails():
    """Guards the guard: the raw payload must genuinely be rejected."""
    with pytest.raises(Exception):
        ScriptSection(**_payload(["unsolved mystery background"])["sections"][0])


# --- coercion rules --------------------------------------------------------


def test_single_element_list_is_unwrapped():
    data = _payload(["Tina Bar money find"])
    _coerce_slot_keywords(data)
    assert data["sections"][0]["slots"][4]["keywords"] == "Tina Bar money find"


def test_multi_element_list_is_joined_with_comma_space():
    data = _payload(["clip-on tie", "seat 18E", "evidence photograph"])
    _coerce_slot_keywords(data)
    assert (
        data["sections"][0]["slots"][4]["keywords"]
        == "clip-on tie, seat 18E, evidence photograph"
    )


def test_existing_strings_are_untouched():
    data = _payload("Northwest Orient Airlines counter")
    _coerce_slot_keywords(data)
    assert (
        data["sections"][0]["slots"][4]["keywords"]
        == "Northwest Orient Airlines counter"
    )
    # The other four slots must be byte-identical too.
    assert data["sections"][0]["slots"][0]["keywords"] == "Boeing 727 1971"


def test_blank_and_whitespace_entries_are_dropped():
    data = _payload(["  ", "hijacked aircraft", ""])
    _coerce_slot_keywords(data)
    assert data["sections"][0]["slots"][4]["keywords"] == "hijacked aircraft"


def test_empty_list_becomes_empty_string_not_a_crash():
    data = _payload([])
    _coerce_slot_keywords(data)
    assert data["sections"][0]["slots"][4]["keywords"] == ""
    ScriptSection(**data["sections"][0])


def test_list_entries_are_stringified():
    data = _payload(["seat", 18])
    _coerce_slot_keywords(data)
    assert data["sections"][0]["slots"][4]["keywords"] == "seat, 18"


# --- must not disturb anything else ---------------------------------------


def test_other_slot_fields_are_left_alone():
    data = _payload(["atmosphere"])
    before_prompts = [s["prompt"] for s in data["sections"][0]["slots"]]
    before_visuals = [s["visual"] for s in data["sections"][0]["slots"]]
    _coerce_slot_keywords(data)
    assert [s["prompt"] for s in data["sections"][0]["slots"]] == before_prompts
    assert [s["visual"] for s in data["sections"][0]["slots"]] == before_visuals


def test_narration_and_section_fields_are_left_alone():
    data = _payload(["atmosphere"])
    narration = data["sections"][0]["narration"]
    _coerce_slot_keywords(data)
    assert data["sections"][0]["narration"] == narration
    assert data["sections"][0]["id"] == 1


@pytest.mark.parametrize(
    "malformed",
    [
        {},                                   # no sections at all
        {"sections": None},                   # null sections
        {"sections": ["not a dict"]},         # section is not an object
        {"sections": [{"slots": None}]},      # null slots
        {"sections": [{"slots": ["nope"]}]},  # slot is not an object
        {"sections": [{"slots": [{}]}]},      # slot has no keywords key
    ],
)
def test_malformed_shapes_do_not_raise(malformed):
    """Normalisation runs before validation, so it must never be the thing that throws."""
    _coerce_slot_keywords(malformed)
