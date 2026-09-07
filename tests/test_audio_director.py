"""Per-scene audio: planning, sourcing without repeats, and layer rendering.

The point of this tier is that two scenes do not sound the same. So the tests
that matter are the ones about distinctness -- a plan that gives every scene
its own ambience, and a sourcer that never hands the same recording to two
cues.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from core.audio_director import (
    AudioPlan,
    Cue,
    plan_audio,
    render_layer,
    required_credits,
    save_audio_provenance,
    source_cues,
)
from core.providers.base import MediaItem, MediaKind


def _run(coro):
    return asyncio.run(coro)


class _Section:
    def __init__(self, section_id: int, narration: str, duration: float):
        self.id = section_id
        self.narration = narration
        self.actual_duration_seconds = duration
        self.estimated_duration_seconds = duration


class _Script:
    def __init__(self, sections):
        self.title = "The Flannan Isles"
        self.sections = sections


def _script():
    return _Script([
        _Section(1, "The lighthouse stood alone in the gale.", 12.0),
        _Section(2, "Inside, the kitchen table was set for three.", 10.0),
        _Section(3, "The logbook ended mid-sentence.", 8.0),
    ])


MODEL_PLAN = {"scenes": [
    {"section_id": 1, "ambience": "storm wind on bare rock",
     "ambience_prompt": "howling wind over a rocky shore",
     "sfx": [{"at": 2.0, "query": "heavy door slam", "prompt": "a heavy door slams once"}]},
    {"section_id": 2, "ambience": "quiet stone kitchen",
     "ambience_prompt": "still room tone in a stone kitchen", "sfx": []},
    {"section_id": 3, "ambience": "empty room tone",
     "ambience_prompt": "faint room tone, nothing moving", "sfx": []},
]}


async def _model(prompt, **kwargs):
    return MODEL_PLAN


# --- planning ---------------------------------------------------------------

def test_every_scene_gets_its_own_ambience():
    plan = _run(plan_audio(_script(), model_call=_model))
    ambience = plan.ambience
    assert len(ambience) == 3
    assert len({cue.query for cue in ambience}) == 3, "two scenes sound the same"


def test_ambience_spans_its_own_scene():
    plan = _run(plan_audio(_script(), model_call=_model))
    starts = [(c.start, c.duration) for c in plan.ambience]
    assert starts == [(0.0, 12.0), (12.0, 10.0), (22.0, 8.0)]


def test_a_cue_is_placed_relative_to_the_whole_video():
    plan = _run(plan_audio(_script(), model_call=_model))
    cue = plan.sfx[0]
    assert cue.section_id == 1
    assert cue.start == 2.0


def test_a_cue_past_the_end_of_its_scene_is_pulled_back():
    """Otherwise it plays under the next scene, against a different ambience."""
    async def late(prompt, **kwargs):
        return {"scenes": [{
            "section_id": 3, "ambience": "room tone",
            "sfx": [{"at": 999.0, "query": "clock tick", "prompt": "one tick"}],
        }]}

    plan = _run(plan_audio(_script(), model_call=late))
    cue = next(c for c in plan.sfx if c.section_id == 3)
    assert cue.start < 22.0 + 8.0


def test_at_most_two_cues_per_scene():
    async def many(prompt, **kwargs):
        return {"scenes": [{
            "section_id": 1, "ambience": "wind",
            "sfx": [{"at": i, "query": f"sound {i}", "prompt": "x"} for i in range(6)],
        }]}

    plan = _run(plan_audio(_script(), model_call=many))
    assert len([c for c in plan.sfx if c.section_id == 1]) == 2


def test_without_a_model_each_scene_still_gets_its_own_query():
    """Weaker than a read of the script, but still per-scene."""
    plan = _run(plan_audio(_script(), model_call=None))
    queries = [c.query for c in plan.ambience]
    assert len(queries) == 3
    assert len(set(queries)) == 3
    assert any("lighthouse" in q for q in queries)


def test_a_model_failure_falls_back_rather_than_losing_the_audio():
    async def broken(prompt, **kwargs):
        raise RuntimeError("model unavailable")

    plan = _run(plan_audio(_script(), model_call=broken))
    assert len(plan.ambience) == 3


def test_a_scene_with_no_duration_is_skipped():
    script = _Script([_Section(1, "text", 0.0)])
    assert _run(plan_audio(script, model_call=_model)).cues == []


def test_the_prompt_forbids_naming_music_or_a_mood():
    from core.audio_director import _PLAN_SCHEMA

    assert "Never name music" in _PLAN_SCHEMA
    assert "Different scenes must get different ambiences" in _PLAN_SCHEMA


# --- sourcing ---------------------------------------------------------------

class _Freesound:
    """Returns the same two sounds for every query, to force the repeat case."""

    name = "freesound"

    def __init__(self, results=None, usable=True):
        self._usable = usable
        self._results = results

    def status(self):
        from core.providers.base import Availability, ProviderStatus

        return ProviderStatus(
            self.name,
            Availability.READY if self._usable else Availability.NEEDS_CREDENTIALS,
        )

    async def search(self, query, *, client, limit=10, **kwargs):
        if self._results is not None:
            return list(self._results)
        return [
            MediaItem(provider="freesound", provider_id="1", kind=MediaKind.AUDIO,
                      url="https://cdn.freesound/1.mp3", licence="CC0",
                      attribution="A", title="one", duration_seconds=30.0),
            MediaItem(provider="freesound", provider_id="2", kind=MediaKind.AUDIO,
                      url="https://cdn.freesound/2.mp3",
                      licence="CC BY", attribution="B", title="two",
                      duration_seconds=30.0),
        ]


class _Eleven:
    name = "elevenlabs_sfx"

    def __init__(self, usable=True):
        self._usable = usable
        self.calls: list[str] = []

    def status(self):
        from core.providers.base import Availability, ProviderStatus

        return ProviderStatus(
            self.name,
            Availability.READY if self._usable else Availability.NEEDS_CREDENTIALS,
        )

    async def generate(self, description, *, client, duration_seconds=None, loop=False):
        self.calls.append(description)
        return b"ID3generated"

    def describe(self, description, *, loop):
        return {
            "platform": self.name, "url": "", "source_page": "",
            "licence": "generated (ElevenLabs sound effects)", "attribution": "",
            "width": 0, "height": 0, "prompt": description, "loop": loop,
        }


def _client():
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"ID3downloaded")))


def _plan(count=3, kind="ambience"):
    return AudioPlan(cues=[
        Cue(kind, i, start=float(i * 10), duration=10.0,
            query=f"query {i}", prompt=f"prompt {i}")
        for i in range(1, count + 1)
    ])


def test_no_recording_is_used_twice(tmp_path):
    """The provider offers the same two sounds every time; three cues must not
    end up with a duplicate."""
    plan = _run(source_cues(
        _plan(3), tmp_path, client=_client(),
        freesound=_Freesound(), elevenlabs=_Eleven()))

    ids = [
        c.provenance.get("url") for c in plan.cues
        if c.provenance.get("platform") == "freesound"
    ]
    assert len(ids) == len(set(ids)), "the same recording landed on two cues"


def test_the_third_cue_falls_through_to_generation(tmp_path):
    eleven = _Eleven()
    plan = _run(source_cues(
        _plan(3), tmp_path, client=_client(),
        freesound=_Freesound(), elevenlabs=eleven))

    assert eleven.calls == ["prompt 3"]
    assert plan.cues[2].provenance["platform"] == "elevenlabs_sfx"


def test_the_same_prompt_is_never_generated_twice(tmp_path):
    plan = AudioPlan(cues=[
        Cue("sfx", 1, start=0.0, duration=2.0, query="", prompt="same prompt"),
        Cue("sfx", 2, start=5.0, duration=2.0, query="", prompt="same prompt"),
    ])
    eleven = _Eleven()
    _run(source_cues(plan, tmp_path, client=_client(),
                     freesound=_Freesound(results=[]), elevenlabs=eleven))
    assert eleven.calls == ["same prompt"]


def test_generation_is_capped_for_a_run(tmp_path):
    eleven = _Eleven()
    _run(source_cues(
        _plan(6), tmp_path, client=_client(),
        freesound=_Freesound(results=[]), elevenlabs=eleven, max_generated=2))
    assert len(eleven.calls) == 2


def test_recordings_are_preferred_over_generation(tmp_path):
    """Generation costs credits; a recording of a real place usually sounds
    more like one anyway."""
    eleven = _Eleven()
    _run(source_cues(_plan(2), tmp_path, client=_client(),
                     freesound=_Freesound(), elevenlabs=eleven))
    assert eleven.calls == []


def test_an_unconfigured_generator_is_simply_skipped(tmp_path):
    plan = _run(source_cues(
        _plan(3), tmp_path, client=_client(),
        freesound=_Freesound(), elevenlabs=_Eleven(usable=False)))
    assert plan.cues[2].sourced is False
    assert plan.cues[0].sourced is True


def test_the_licence_and_credit_travel_with_each_sound(tmp_path):
    plan = _run(source_cues(_plan(2), tmp_path, client=_client(),
                            freesound=_Freesound(), elevenlabs=_Eleven()))
    licences = {c.provenance.get("licence") for c in plan.cues if c.sourced}
    assert "CC0" in licences and "CC BY" in licences


def test_a_cc_by_sound_produces_a_credit_line(tmp_path):
    plan = _run(source_cues(_plan(2), tmp_path, client=_client(),
                            freesound=_Freesound(), elevenlabs=_Eleven()))
    credits = required_credits(plan)
    assert any("B" in credit for credit in credits)


def test_generated_audio_is_never_credited_to_anyone(tmp_path):
    plan = _run(source_cues(
        _plan(1), tmp_path, client=_client(),
        freesound=_Freesound(results=[]), elevenlabs=_Eleven()))
    record = plan.cues[0].provenance
    assert "generated" in record["licence"]
    assert record["attribution"] == ""


def test_the_provenance_file_lists_every_cue(tmp_path):
    plan = _run(source_cues(_plan(2), tmp_path, client=_client(),
                            freesound=_Freesound(), elevenlabs=_Eleven()))
    path = save_audio_provenance(plan, tmp_path)
    import json

    record = json.loads(path.read_text(encoding="utf-8"))
    assert len(record["cues"]) == 2
    for entry in record["cues"]:
        assert entry["licence"]


# --- rendering --------------------------------------------------------------

def _tone(path: Path, seconds: float, freq: int = 220) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}", str(path)],
        check=True,
    )
    return path


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float((out.stdout or "0").strip() or 0.0)


@pytest.mark.slow
def test_a_layer_is_exactly_as_long_as_the_video(tmp_path):
    """A short final cue must not truncate the mix."""
    bed = _tone(tmp_path / "bed.mp3", 3.0)
    cues = [Cue("ambience", 1, start=0.0, duration=10.0, path=bed)]
    out = tmp_path / "ambience.wav"

    assert render_layer(cues, 20.0, out, fade=1.2, loop_to_fill=True) is True
    assert abs(_duration(out) - 20.0) < 0.05


@pytest.mark.slow
def test_cues_land_where_the_plan_put_them(tmp_path):
    """Silence before the first cue proves the delay was applied."""
    click = _tone(tmp_path / "cue.mp3", 0.4, freq=900)
    cues = [Cue("sfx", 1, start=8.0, duration=1.0, path=click)]
    out = tmp_path / "sfx.wav"
    render_layer(cues, 12.0, out, fade=0.08, loop_to_fill=False)

    head = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(out), "-t", "4", "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    assert "mean_volume: -91" in head or "mean_volume: -inf" in head


def test_a_layer_with_nothing_sourced_is_not_written(tmp_path):
    out = tmp_path / "empty.wav"
    cues = [Cue("ambience", 1, start=0.0, duration=5.0)]
    assert render_layer(cues, 10.0, out, fade=1.0, loop_to_fill=True) is False
    assert not out.exists()


# --- wiring -----------------------------------------------------------------

def test_scene_audio_is_off_by_default():
    from core.utils import ImageSourcingConfig

    assert ImageSourcingConfig().scene_audio is False


def test_the_two_real_channels_opt_in():
    from core.utils import load_channel_config

    for slug in ("horror_stories", "football_news"):
        assert load_channel_config(slug).image_sourcing.scene_audio is True, slug


def test_the_mixer_keeps_every_layer_separate():
    from core.assembler import _build_audio_filter

    graph, label = _build_audio_filter(
        1, 2, 0.2, 30.0, 3, sfx_volume=0.15,
        ambience_idx=4, ambience_volume=0.16,
        scene_sfx_idx=5, scene_sfx_volume=0.34,
    )
    assert label == "[aout]"
    # Each layer keeps its own level.
    for volume in ("volume=0.2", "volume=0.15", "volume=0.16", "volume=0.34"):
        assert volume in graph
    # One mix, not a chain of pairwise mixes that would attenuate the earlier
    # layers each time.
    assert graph.count("amix") == 2          # narration+music, then the rest
    assert "amix=inputs=4" in graph
    assert "loudnorm" in graph


def test_the_mix_is_unchanged_when_there_is_no_scene_audio():
    from core.assembler import _build_audio_filter

    before, _ = _build_audio_filter(1, 2, 0.2, 30.0, 3, sfx_volume=0.15)
    assert "amix=inputs=2" in before
    assert "amb" not in before
    assert "scenesfx" not in before
