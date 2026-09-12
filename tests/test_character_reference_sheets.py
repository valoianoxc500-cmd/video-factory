"""The reference sheet has to reach the generator, not just the reviewer.

A real Animated Stories run drew four locked characters, bound all twenty-one
scene prompts to them, and still had eight of nine scenes rejected for drift.
The trace says why: every generation logged `ref=no`. The sheets were drawn,
written to the workspace and handed to the consistency reviewer -- and never
to the model doing the drawing. Each scene was reconstructed from a paragraph
and then judged against a picture it had never seen.

Two of the rejections name the gap exactly: scenes had "plain white faces and
lack the skin tone and blush details shown in the reference sheets". Neither
skin tone nor blush is in any locked field. The scenes were faithful to the
text; the sheet had invented detail on its own and become the standard.

These pin the fix: sheets are registered before the first beat is drawn, the
right sheet is attached to the right beat, the model is told what the attached
image is, a generator that cannot see an image is not chosen when one exists,
and none of it is reachable from a channel that does not animate.
"""

from __future__ import annotations

import json

import pytest

import core.image_sourcer as image_sourcer
from core import character_bible as cb


@pytest.fixture(autouse=True)
def _clean_registry():
    """Never let one test's cast leak into the next -- the worker reuses the
    process across videos, so this is the real condition too."""
    image_sourcer._reset_character_sheets()
    yield
    image_sourcer._reset_character_sheets()


def _workspace(tmp_path, *, characters, draw=("char_01",)):
    char_dir = tmp_path / "character"
    char_dir.mkdir(parents=True, exist_ok=True)
    for char_id in draw:
        (char_dir / f"{char_id}_sheet.png").write_bytes(b"sheet")
    (char_dir / "character_bible.json").write_text(
        json.dumps({"characters": characters}), encoding="utf-8"
    )
    return tmp_path


def _character(char_id="char_01", name="Sam", sheet="char_01_sheet.png"):
    return {
        "id": char_id,
        "name": name,
        "role": "main" if char_id == "char_01" else "secondary",
        "face": "large round white head",
        "hair": "short black hair",
        "clothing": "mustard hoodie",
        "colours": "mustard, navy",
        "accessories": "none",
        "proportions": "thin limbs",
        "expression": "wary",
        "sheet": sheet,
    }


# --- reading the IDs back out of a locked prompt ---------------------------


def test_ids_are_recoverable_from_a_locked_prompt():
    """The bound prompt is all the generation call has to work from."""
    prompt = cb.scene_prompt_for(
        "she opens the door",
        [cb.Character(id="char_02", name="Ada"), cb.Character(id="char_01", name="Sam")],
        "style",
        "exclusions",
    )
    assert cb.character_ids_in(prompt) == ["char_02", "char_01"]


def test_ids_are_deduplicated_and_ordered():
    assert cb.character_ids_in("[char_02] a [char_01] b [char_02]") == [
        "char_02",
        "char_01",
    ]


def test_a_prompt_with_no_cast_names_nobody():
    assert cb.character_ids_in("a wide shot of the lighthouse") == []
    assert cb.character_ids_in("") == []


# --- registering the sheets -------------------------------------------------


def test_sheets_are_registered_from_the_bible(tmp_path):
    ws = _workspace(tmp_path, characters=[_character()])
    assert image_sourcer.register_character_sheets(ws) == 1
    assert image_sourcer._CHARACTER_SHEETS["char_01"].name == "char_01_sheet.png"


def test_a_character_whose_sheet_was_never_drawn_is_skipped(tmp_path):
    """`build_character_sheet` tolerates a missing secondary sheet."""
    ws = _workspace(
        tmp_path,
        characters=[_character(), _character("char_02", "Ada", "char_02_sheet.png")],
        draw=("char_01",),
    )
    assert image_sourcer.register_character_sheets(ws) == 1
    assert "char_02" not in image_sourcer._CHARACTER_SHEETS


def test_an_empty_sheet_file_is_not_a_reference(tmp_path):
    ws = _workspace(tmp_path, characters=[_character()])
    (ws / "character" / "char_01_sheet.png").write_bytes(b"")
    assert image_sourcer.register_character_sheets(ws) == 0


def test_registration_clears_the_previous_run(tmp_path):
    ws = _workspace(tmp_path, characters=[_character()])
    image_sourcer.register_character_sheets(ws)
    assert image_sourcer.register_character_sheets(tmp_path / "empty") == 0
    assert image_sourcer._CHARACTER_SHEETS == {}


# --- the channels that must never see this ----------------------------------


def test_a_channel_with_no_bible_registers_nothing(tmp_path):
    """Football, Horror Stories and True Stories write no character/ dir."""
    assert image_sourcer.register_character_sheets(tmp_path) == 0
    assert image_sourcer._character_reference_for("[char_01] anything") is None


def test_a_corrupt_bible_does_not_raise(tmp_path):
    (tmp_path / "character").mkdir()
    (tmp_path / "character" / "character_bible.json").write_text("{not json", encoding="utf-8")
    assert image_sourcer.register_character_sheets(tmp_path) == 0


# --- attaching the right sheet to the right beat ----------------------------


def test_the_beats_own_lead_is_attached(tmp_path):
    ws = _workspace(
        tmp_path,
        characters=[_character(), _character("char_02", "Ada", "char_02_sheet.png")],
        draw=("char_01", "char_02"),
    )
    image_sourcer.register_character_sheets(ws)

    reference = image_sourcer._character_reference_for("scene [char_02] and [char_01]")
    assert reference is not None
    assert reference.name == "char_02_sheet.png", "the first ID is the beat's lead"


def test_a_beat_naming_an_undrawn_character_falls_through(tmp_path):
    ws = _workspace(
        tmp_path,
        characters=[_character(), _character("char_02", "Ada", "char_02_sheet.png")],
        draw=("char_01",),
    )
    image_sourcer.register_character_sheets(ws)

    reference = image_sourcer._character_reference_for("[char_02] then [char_01]")
    assert reference is not None
    assert reference.name == "char_01_sheet.png"


def test_a_beat_naming_nobody_gets_no_reference(tmp_path):
    ws = _workspace(tmp_path, characters=[_character()])
    image_sourcer.register_character_sheets(ws)
    assert image_sourcer._character_reference_for("a wide shot of the sea") is None


# --- telling the model what the attachment is -------------------------------


def test_the_reference_lock_is_added_only_with_an_attachment(tmp_path):
    sheet = tmp_path / "char_01_sheet.png"
    sheet.write_bytes(b"sheet")

    with_ref = image_sourcer._with_reference_lock("draw the scene", sheet)
    assert cb.REFERENCE_LOCK in with_ref
    assert with_ref.startswith("draw the scene")

    assert image_sourcer._with_reference_lock("draw the scene", None) == "draw the scene"


def test_the_reference_lock_makes_the_sheet_authoritative():
    """The observed rejection was about detail the sheet had and the text did
    not, so the sheet has to win that tie."""
    lock = cb.REFERENCE_LOCK.lower()
    assert "reference sheet" in lock
    assert "skin tone" in lock
    assert "follow the reference sheet" in lock
    assert "do not redraw the reference sheet itself" in lock


# --- choosing a generator that can see the sheet ----------------------------


class _Sourcing:
    def __init__(self, generation_model):
        self.generation_model = generation_model


class _Config:
    def __init__(self, generation_model="gemini-2.5-flash-image"):
        self.image_sourcing = _Sourcing(generation_model)


def test_a_text_only_generator_is_replaced_when_a_sheet_exists(tmp_path):
    sheet = tmp_path / "char_01_sheet.png"
    sheet.write_bytes(b"sheet")
    chosen = image_sourcer._reference_capable_model(
        "fal-ai/flux/schnell", sheet, _Config()
    )
    assert chosen == "gemini-2.5-flash-image"


def test_the_configured_generator_is_kept_when_there_is_no_sheet():
    """No reference means no animated channel, so nothing may change."""
    assert (
        image_sourcer._reference_capable_model("fal-ai/flux/schnell", None, _Config())
        == "fal-ai/flux/schnell"
    )


def test_a_reference_capable_generator_is_left_alone(tmp_path):
    sheet = tmp_path / "char_01_sheet.png"
    sheet.write_bytes(b"sheet")
    assert (
        image_sourcer._reference_capable_model("gemini-2.5-flash-image", sheet, _Config())
        == "gemini-2.5-flash-image"
    )


def test_no_swap_when_the_channel_has_no_other_generator(tmp_path):
    sheet = tmp_path / "char_01_sheet.png"
    sheet.write_bytes(b"sheet")
    assert (
        image_sourcer._reference_capable_model(
            "fal-ai/flux/schnell", sheet, _Config("fal-ai/flux/schnell")
        )
        == "fal-ai/flux/schnell"
    )


# --- the sheet must not invent the standard it is judged by -----------------


def test_the_sheet_prompt_forbids_invented_detail():
    prompt = cb.sheet_prompt(
        cb.Character(id="char_01", name="Sam"), "style", "exclusions"
    ).lower()
    assert "draw only what the description above states" in prompt
    assert "skin tone" in prompt
    assert "blush" in prompt


def test_the_generator_entry_point_accepts_a_reference():
    """clients.generate_scene_image is where the sheet has to be able to go."""
    import inspect

    import clients

    params = inspect.signature(clients.generate_scene_image).parameters
    assert "reference_image" in params
    assert params["reference_image"].default is None


def test_both_generation_call_sites_pass_the_reference():
    """A helper that exists but is wired into neither call site fixes nothing.

    Counted as "at least", not "exactly". Football's person-reconstruction
    path is a third caller that hands the generator an identity reference --
    a face, rather than an Animated Stories character sheet -- and pinning the
    count to two would make adding any further reference-aware caller fail a
    test about Animated Stories wiring.
    """
    from pathlib import Path as P

    source = (
        P(__file__).resolve().parent.parent / "core" / "image_sourcer.py"
    ).read_text(encoding="utf-8")
    assert source.count("reference_image=reference") >= 2
    assert source.count("_with_reference_lock(") >= 3  # definition + both sites
