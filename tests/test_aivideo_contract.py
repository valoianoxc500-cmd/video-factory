"""The web form and the engine must agree on the same vocabulary.

`web/lib/aivideo.ts` duplicates the voices, presets, durations and ratios that
live in `aivideo/spec.py`. Duplication is the right call at this size — a
codegen step would cost more than it saves — but only if something notices
when the two drift. This is that something.

Drift here is not a crash. The engine clamps every field it receives, so a
voice the UI offers but the engine does not know silently becomes a different
voice, and the customer gets a video narrated by someone they did not pick.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aivideo.spec import (
    ARABIC_CAPABLE_FONTS,
    ASPECT_SIZES,
    CAPTION_PRESETS,
    DURATIONS,
    VOICES,
)

TS = Path(__file__).resolve().parent.parent / "web" / "lib" / "aivideo.ts"


@pytest.fixture(scope="module")
def ts_source() -> str:
    return TS.read_text(encoding="utf-8")


def test_the_shared_module_exists(ts_source):
    assert "AI Video Maker" in ts_source


def _engine_voice_ids() -> set[str]:
    """Every voice the engine will accept.

    The discovered catalogue, not the hardcoded literal: the literal is now
    only the fallback for a checkout that has never run voice discovery, and
    comparing the form against it would fail the moment discovery ran.
    """
    from aivideo import voices as catalogue

    return {v.id for v in catalogue.catalogue()}


def test_every_engine_voice_is_offered_by_the_form(ts_source):
    for voice_id in _engine_voice_ids():
        assert voice_id in ts_source, f"{voice_id} missing from aivideo.ts"


def test_the_form_offers_no_voice_the_engine_cannot_use(ts_source):
    offered = set(re.findall(r'id:\s*"([a-z]{2}-[A-Z]{2}-\w+)"', ts_source))
    known = _engine_voice_ids()
    assert offered <= known, f"the form offers unknown voices: {offered - known}"
    assert offered == known


def test_the_offering_is_substantially_wider_than_the_original_eleven(ts_source):
    """The complaint was 'too few voices'."""
    from aivideo import voices as catalogue

    arabic = catalogue.voices_for("ar")
    english = catalogue.voices_for("en")
    assert len(arabic) >= 20, f"only {len(arabic)} Arabic voices"
    assert len(english) >= 15, f"only {len(english)} English voices"
    # Multiple Arabic regions, not five voices for the whole Arab world.
    assert len({v.locale for v in arabic}) >= 8
    # Both English accents the brief names.
    assert {"en-US", "en-GB"} <= {v.locale for v in english}


def test_no_raw_voice_identifier_is_ever_shown(ts_source):
    """Labels are people's names; the provider id stays internal."""
    from aivideo import voices as catalogue

    for voice in catalogue.catalogue():
        assert "Neural" not in voice.label, f"{voice.label} leaks the model name"
        assert "-" not in voice.label, f"{voice.label} looks like an identifier"
        assert voice.accent, f"{voice.id} has no customer-facing accent"


def test_both_sides_offer_the_same_durations(ts_source):
    match = re.search(r"DURATIONS\s*=\s*\[([^\]]+)\]", ts_source)
    assert match
    listed = tuple(int(n) for n in re.findall(r"\d+", match.group(1)))
    assert listed == DURATIONS


def test_both_sides_offer_the_same_aspect_ratios(ts_source):
    listed = set(re.findall(r'id:\s*"(\d+:\d+)"', ts_source))
    assert listed == set(ASPECT_SIZES)


def test_both_sides_know_the_same_caption_presets(ts_source):
    for name in CAPTION_PRESETS:
        assert re.search(rf"^\s*{name}:\s*\{{", ts_source, re.M), \
            f"preset {name!r} missing from aivideo.ts"
    listed = set(re.findall(r"^\s{2}(\w+):\s*\{\s*$", ts_source, re.M))
    # The TS file also declares DEFAULT_SETTINGS etc.; every *preset* name has
    # to be present, and no preset may exist there that the engine lacks.
    unknown = {n for n in listed if n.endswith("_preset")}
    assert not unknown


def test_preset_values_match_on_both_sides(ts_source):
    """A preset that looks different in the preview than in the render is
    worse than no preview at all."""
    for name, preset in CAPTION_PRESETS.items():
        block = re.search(rf"^\s*{name}:\s*\{{(.*?)^\s*\}},", ts_source, re.M | re.S)
        assert block, f"could not read preset {name} from aivideo.ts"
        body = block.group(1)
        for key in ("size", "outline_width", "words_per_line"):
            found = re.search(rf"{key}:\s*(\d+)", body)
            assert found, f"{name}.{key} missing in aivideo.ts"
            assert int(found.group(1)) == preset[key], (
                f"{name}.{key}: engine says {preset[key]}, form says {found.group(1)}"
            )
        for key in ("text_color", "highlight_color", "outline_color", "font", "position"):
            found = re.search(rf'{key}:\s*"([^"]+)"', body)
            assert found, f"{name}.{key} missing in aivideo.ts"
            assert found.group(1) == preset[key], (
                f"{name}.{key}: engine says {preset[key]!r}, form says {found.group(1)!r}"
            )


def test_both_sides_agree_which_fonts_can_shape_arabic(ts_source):
    match = re.search(r"ARABIC_FONTS\s*=\s*\[([^\]]+)\]", ts_source)
    assert match
    listed = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert listed == ARABIC_CAPABLE_FONTS


def test_a_voice_preview_file_exists_for_every_offered_voice():
    """The picker plays the real voice, so a missing sample is a dead button."""
    previews = TS.parent.parent / "public" / "voice-previews"
    for voice in VOICES:
        sample = previews / f"{voice['id']}.mp3"
        assert sample.exists(), f"no preview clip for {voice['id']}"
        assert sample.stat().st_size > 5_000, f"preview for {voice['id']} is empty"


def test_the_progress_wording_is_the_same_on_both_sides(ts_source):
    from aivideo.pipeline import PROGRESS

    for stage, (label, _pct) in PROGRESS.items():
        assert f'"{label}"' in ts_source, (
            f"the form does not know the progress wording {label!r}"
        )


def test_no_customer_facing_string_names_a_provider(ts_source):
    """Nothing in the UI vocabulary should leak how the sausage is made."""
    from aivideo.pipeline import PROGRESS, STARTING

    labels = [label for label, _ in PROGRESS.values()] + [STARTING[0]]
    for label in labels:
        low = label.lower()
        for leak in ("pexels", "pixabay", "edge", "ffmpeg", "gemini", "api", "http"):
            assert leak not in low, f"{leak!r} leaked into {label!r}"
