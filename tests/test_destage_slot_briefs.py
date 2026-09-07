"""Slot briefs must ask for something the archive can actually contain.

The image review gate judges each photograph against its slot prompt, so an
unattainable prompt fails a correct image:

    "Image 1.3 showed recovered money instead of original stacks; 3.2 showed an
     action (digging) instead of a close-up of the ground; 3.5 showed a modern
     sign instead of the historical marker."

The Cooper money was only ever photographed after recovery, decayed on a
riverbank. The archive's one real photograph of it was marked wrong because the
brief asked for a photograph that has never existed.
"""

import pytest

from core.scripter import _destage_slot_briefs


def _payload(keywords: str, prompt: str) -> dict:
    return {
        "sections": [
            {
                "id": 1,
                "narration": "n",
                "slots": [{"visual": "google_photo", "keywords": keywords, "prompt": prompt}],
            }
        ]
    }


def _slot(keywords="k", prompt="p", *, web_photos_only=True) -> dict:
    data = _payload(keywords, prompt)
    _destage_slot_briefs(data, web_photos_only=web_photos_only)
    return data["sections"][0]["slots"][0]


# --- the exact failures ----------------------------------------------------

def test_original_stacks_becomes_the_money_itself():
    slot = _slot(prompt="original stacks of the ransom money")
    assert "stacks of" not in slot["prompt"].lower()
    assert "ransom money" in slot["prompt"].lower()


def test_a_demanded_action_is_removed():
    slot = _slot(prompt="close-up of the ground where the money was found")
    assert "close-up of" not in slot["prompt"].lower()
    assert "ground" in slot["prompt"].lower()


def test_a_staged_scene_is_removed_from_keywords_too():
    slot = _slot(keywords="hands holding the clip-on tie")
    assert "holding" not in slot["keywords"].lower()
    assert "clip-on tie" in slot["keywords"].lower()


@pytest.mark.parametrize(
    "prompt,gone",
    [
        ("photograph of the Boeing 727 at night", "at night"),
        ("the Columbia River in the fog", "in the fog"),
        ("an investigator examining the case file", "examining"),
        ("several parachutes on the tarmac", "several"),
    ],
)
def test_staging_and_atmosphere_are_stripped(prompt, gone):
    assert gone.lower() not in _slot(prompt=prompt)["prompt"].lower()


# --- the subject is never lost --------------------------------------------

def test_the_subject_always_survives():
    for prompt in (
        "close-up of hands holding stacks of 1971 twenty dollar bills at night",
        "photograph of the FBI composite sketch",
        "aerial view of the Lewis River",
    ):
        out = _slot(prompt=prompt)["prompt"]
        assert out.strip(), f"{prompt!r} was blanked out"


def test_a_field_is_never_emptied():
    """An empty brief is worse than an over-specified one."""
    slot = _slot(keywords="close-up of", prompt="hands holding")
    assert slot["keywords"].strip()
    assert slot["prompt"].strip()


def test_a_clean_brief_is_untouched():
    slot = _slot(
        keywords="FBI evidence photograph of the recovered ransom money",
        prompt="The recovered ransom money as photographed by the FBI.",
    )
    assert slot["keywords"] == "FBI evidence photograph of the recovered ransom money"
    assert slot["prompt"] == "The recovered ransom money as photographed by the FBI."


def test_the_era_survives():
    """The one qualifier worth keeping: a modern equivalent would be wrong."""
    assert "1971" in _slot(prompt="stacks of 1971 twenty dollar bills")["prompt"]


# --- scoped to web-photo-only channels ------------------------------------

def test_other_channels_are_untouched():
    slot = _slot(
        keywords="hands holding the trophy",
        prompt="close-up of the trophy",
        web_photos_only=False,
    )
    assert slot["keywords"] == "hands holding the trophy"
    assert slot["prompt"] == "close-up of the trophy"


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {"sections": None},
        {"sections": ["not a dict"]},
        {"sections": [{"slots": None}]},
        {"sections": [{"slots": ["nope"]}]},
        {"sections": [{"slots": [{}]}]},
        {"sections": [{"slots": [{"keywords": None, "prompt": 12}]}]},
    ],
)
def test_malformed_shapes_do_not_raise(malformed):
    _destage_slot_briefs(malformed, web_photos_only=True)
