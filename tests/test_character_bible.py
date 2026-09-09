"""The Character Bible: identity is locked, or it is not a bible.

These tests exist because of an observed production failure. A real run
generated twenty-one scenes and the reviewer rejected every batch with
"every image features a different character". The cause was that the
character description resolved to the empty string and every prompt asked
for "the main character".

So the properties pinned here are the two that failure violated: a
description always exists and is never empty, and the same character
produces byte-identical text in every prompt that mentions them.
"""

import asyncio

import pytest

from core import character_bible as cb


def _entry(name="Sam", role="main", **over):
    base = {
        "name": name,
        "role": role,
        "face": "large round white head, black dot eyes, thick eyebrows",
        "hair": "short spiky black hair",
        "clothing": "mustard yellow hoodie and dark blue jeans",
        "colours": "mustard yellow, dark blue, white",
        "accessories": "round wire glasses",
        "proportions": "thin limbs, head one third of height",
        "expression": "wary and alert",
    }
    base.update(over)
    return base


# ── a description always exists ──────────────────────────────────────

def test_an_empty_cast_still_produces_a_complete_character():
    """The original defect: no description at all."""
    bible = cb.bible_from_entries([])
    main = bible.main()
    assert main is not None
    assert main.id == "char_01"
    assert main.is_complete(), "no locked field may be empty"


def test_missing_fields_are_filled_rather_than_left_blank():
    bible = cb.bible_from_entries([{"name": "Sam", "role": "main"}])
    main = bible.main()
    assert main.is_complete()
    for field in cb.LOCKED_FIELDS:
        assert str(getattr(main, field)).strip()


def test_no_locked_description_ever_says_only_the_main_character():
    """The exact phrase that carried no identity in the failed run."""
    bible = cb.bible_from_entries([])
    text = cb.locked_description(bible.main())
    assert text.count("the main character") <= 1
    assert "FACE:" in text and "CLOTHING:" in text and "COLOURS:" in text


# ── the description is stable ────────────────────────────────────────

def test_the_same_character_produces_identical_text_every_time():
    """Paraphrasing per scene is how one character becomes twenty-one."""
    bible = cb.bible_from_entries([_entry()])
    main = bible.main()
    first = cb.locked_description(main)
    for _ in range(5):
        assert cb.locked_description(main) == first


def test_two_scenes_sharing_a_character_share_its_exact_text():
    bible = cb.bible_from_entries([_entry()])
    main = bible.main()
    a = cb.scene_prompt_for("he opens the door", [main], "STYLE", "NO3D")
    b = cb.scene_prompt_for("he runs downstairs", [main], "STYLE", "NO3D")
    locked = cb.locked_description(main)
    assert locked in a and locked in b


def test_every_scene_prompt_carries_the_identity_lock():
    bible = cb.bible_from_entries([_entry()])
    prompt = cb.scene_prompt_for("a quiet room", bible.characters, "S", "E")
    assert cb.IDENTITY_LOCK in prompt


def test_the_lock_permits_pose_and_forbids_redesign():
    lock = cb.IDENTITY_LOCK.lower()
    for allowed in ("pose", "expression", "action", "lighting", "environment"):
        assert allowed in lock
    for forbidden in ("redesign", "replace", "recolour"):
        assert forbidden in lock


# ── ids and roles ────────────────────────────────────────────────────

def test_every_character_gets_a_stable_id():
    bible = cb.bible_from_entries([_entry("Sam"), _entry("Ola", "secondary")])
    assert [c.id for c in bible.characters] == ["char_01", "char_02"]
    assert bible.by_id("char_02").name == "Ola"


def test_exactly_one_main_character():
    bible = cb.bible_from_entries(
        [_entry("Sam", "main"), _entry("Ola", "main"), _entry("Ade", "main")]
    )
    assert sum(1 for c in bible.characters if c.role == "main") == 1


def test_a_cast_with_no_main_still_gets_one():
    bible = cb.bible_from_entries(
        [_entry("Sam", "secondary"), _entry("Ola", "secondary")]
    )
    assert bible.main() is not None
    assert sum(1 for c in bible.characters if c.role == "main") == 1


def test_the_cast_is_capped():
    bible = cb.bible_from_entries([_entry(f"C{i}", "secondary") for i in range(20)])
    assert len(bible.characters) <= 4


# ── which characters are in a beat ───────────────────────────────────

def test_a_named_secondary_character_is_included():
    bible = cb.bible_from_entries([_entry("Sam"), _entry("Ola", "secondary")])
    found = cb.characters_in_beat("Ola turns to the window", bible)
    assert {c.id for c in found} == {"char_01", "char_02"}


def test_a_beat_naming_nobody_still_gets_the_main_character():
    """An empty cast is what produced unlabelled prompts."""
    bible = cb.bible_from_entries([_entry("Sam")])
    found = cb.characters_in_beat("the corridor is empty", bible)
    assert [c.id for c in found] == ["char_01"]


def test_the_main_character_is_always_first():
    bible = cb.bible_from_entries([_entry("Sam"), _entry("Ola", "secondary")])
    found = cb.characters_in_beat("Ola waits", bible)
    assert found[0].role == "main"


# ── building from a model response ───────────────────────────────────

class _Section:
    def __init__(self, narration):
        self.narration = narration


class _Script:
    def __init__(self, *narrations):
        self.sections = [_Section(n) for n in narrations]


def test_a_model_failure_still_yields_a_usable_bible():
    """A network error must not reproduce the empty-description defect."""
    async def boom(*_a, **_k):
        raise RuntimeError("no")

    bible = asyncio.run(
        cb.build_bible(_Script("Sam walked in."), {}, generate_text=boom)
    )
    assert bible.main().is_complete()


def test_unparseable_output_still_yields_a_usable_bible():
    async def junk(*_a, **_k):
        return "sorry, I cannot help with that"

    bible = asyncio.run(
        cb.build_bible(_Script("Sam walked in."), {}, generate_text=junk)
    )
    assert bible.main().is_complete()


def test_a_fenced_json_response_is_parsed():
    async def fenced(*_a, **_k):
        import json
        return "```json\n" + json.dumps({"characters": [_entry("Layla")]}) + "\n```"

    bible = asyncio.run(
        cb.build_bible(_Script("Layla walked in."), {}, generate_text=fenced)
    )
    assert bible.main().name == "Layla"
    assert "mustard yellow" in cb.locked_description(bible.main())


def test_a_story_with_no_narration_does_not_call_the_model():
    called = False

    async def spy(*_a, **_k):
        nonlocal called
        called = True
        return "{}"

    bible = asyncio.run(cb.build_bible(_Script(""), {}, generate_text=spy))
    assert called is False
    assert bible.main().is_complete()


def test_the_sheet_prompt_contains_the_locked_description():
    bible = cb.bible_from_entries([_entry()])
    main = bible.main()
    prompt = cb.sheet_prompt(main, "STYLE", "EXCLUSIONS")
    assert cb.locked_description(main) in prompt
    assert cb.IDENTITY_LOCK in prompt
