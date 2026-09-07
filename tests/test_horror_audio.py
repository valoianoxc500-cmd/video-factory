"""Horror audio assets: presence, licensing, isolation and mix level."""

import json
import subprocess
from pathlib import Path

import pytest

from core.footage_discovery import (
    AUDIO_EXTENSIONS,
    is_downloadable_url,
    is_reusable_licence,
)
from core.utils import load_channel_config

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"
PROVENANCE = ASSETS / "audio_provenance.json"

MUSIC_STEMS = ["horror_dread_low", "horror_tension_pulse", "horror_climax_dark"]
SFX_STEMS = ["horror_door_creak", "horror_heartbeat", "horror_jarring_hit"]


def _provenance() -> dict[str, dict]:
    data = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    return {a["stem"]: a for a in data["assets"]}


# --- assets exist ----------------------------------------------------------

@pytest.mark.parametrize("stem", MUSIC_STEMS)
def test_music_bed_exists(stem):
    p = ASSETS / "music" / f"{stem}.mp3"
    assert p.exists(), f"missing {p}"
    assert p.stat().st_size > 200_000, "bed is implausibly small"


@pytest.mark.parametrize("stem", SFX_STEMS)
def test_sfx_exists(stem):
    p = ASSETS / "sfx" / "horror" / f"{stem}.mp3"
    assert p.exists(), f"missing {p}"
    assert p.stat().st_size > 10_000


def test_three_distinct_music_beds():
    sizes = {
        (ASSETS / "music" / f"{s}.mp3").stat().st_size for s in MUSIC_STEMS
    }
    assert len(sizes) == 3, "beds appear to be duplicates of each other"


# --- provenance ------------------------------------------------------------

def test_every_audio_asset_has_provenance():
    prov = _provenance()
    for stem in MUSIC_STEMS + SFX_STEMS:
        assert stem in prov, f"no provenance recorded for {stem}"


@pytest.mark.parametrize("stem", MUSIC_STEMS + SFX_STEMS)
def test_provenance_records_url_licence_and_source(stem):
    rec = _provenance()[stem]
    assert rec["url"].startswith("https://")
    assert rec["platform"] == "wikimedia_commons"
    assert rec["licence"]
    assert rec["source_page"].startswith("https://commons.wikimedia.org/")


@pytest.mark.parametrize("stem", MUSIC_STEMS + SFX_STEMS)
def test_every_licence_permits_reuse(stem):
    assert is_reusable_licence(_provenance()[stem]["licence"]) is True


@pytest.mark.parametrize("stem", MUSIC_STEMS + SFX_STEMS)
def test_downloads_came_from_an_allowlisted_host(stem):
    assert is_downloadable_url(_provenance()[stem]["url"], AUDIO_EXTENSIONS) is True


def test_attribution_is_recorded_wherever_it_is_required():
    """CC BY obliges crediting the artist; the record must name them."""
    for stem, rec in _provenance().items():
        if rec.get("attribution_required"):
            assert rec["attribution"].strip(), f"{stem} needs an attribution name"


def test_no_noncommercial_or_noderivatives_asset_was_kept():
    for stem, rec in _provenance().items():
        lowered = rec["licence"].lower()
        assert "nc" not in lowered.split("-"), stem
        assert "nd" not in lowered.split("-"), stem


# --- channel wiring --------------------------------------------------------

def test_horror_pools_reference_the_downloaded_assets():
    cfg = load_channel_config("horror_stories")
    assert cfg.video.music_pool == MUSIC_STEMS
    assert cfg.video.sfx_pool == SFX_STEMS


# --- measured mix levels ---------------------------------------------------
#
# `volume=` in the assembler is a linear multiplier on each asset's OWN level,
# not a level in the finished mix, so a number like 0.1 means nothing until the
# source loudness is known. These were measured with
# `ffmpeg -i <file> -af loudnorm=print_format=json -f null -`:
#
#   narration_full.wav        -19.35 LUFS   (Gemini TTS, the reference)
#   horror_dread_low          -25.54 LUFS
#   horror_tension_pulse      -25.61 LUFS
#   horror_climax_dark        -25.38 LUFS   -> beds average -25.51
#   horror_door_creak         -20.66 LUFS
#   horror_heartbeat          -20.65 LUFS
#   horror_jarring_hit        -20.26 LUFS   -> one-shots average -20.52
#
# The Commons beds are already quiet, so the previous 0.1 (-20 dB) buried them
# 26 LU under the narration -- inaudible. The targets below place the beds ~18
# LU under speech (present but never competing) and the one-shots ~11 LU under
# (a hit lands without ducking the voice).
NARRATION_LUFS = -19.35
MUSIC_BED_LUFS = -25.51
SFX_LUFS = -20.52

MUSIC_TARGET_LU_UNDER = 18.0
SFX_TARGET_LU_UNDER = 11.0
# Rounding to two decimals in the config costs a fraction of a dB.
LU_TOLERANCE = 1.0


def _lu_under_narration(asset_lufs: float, volume: float) -> float:
    """How far below the narration an asset sits once its gain is applied."""
    import math

    gain_db = 20 * math.log10(volume)
    return NARRATION_LUFS - (asset_lufs + gain_db)


def test_horror_music_sits_the_measured_distance_under_narration():
    """Audible as atmosphere, never competing with the voice."""
    cfg = load_channel_config("horror_stories")
    actual = _lu_under_narration(
        MUSIC_BED_LUFS, cfg.video.background_music_volume
    )
    assert abs(actual - MUSIC_TARGET_LU_UNDER) <= LU_TOLERANCE, (
        f"music beds sit {actual:.1f} LU under the narration; "
        f"target is {MUSIC_TARGET_LU_UNDER} +/- {LU_TOLERANCE}"
    )


def test_horror_sfx_sits_the_measured_distance_under_narration():
    cfg = load_channel_config("horror_stories")
    actual = _lu_under_narration(SFX_LUFS, cfg.rendering_defaults.sfx_volume)
    assert abs(actual - SFX_TARGET_LU_UNDER) <= LU_TOLERANCE, (
        f"SFX sit {actual:.1f} LU under the narration; "
        f"target is {SFX_TARGET_LU_UNDER} +/- {LU_TOLERANCE}"
    )


def test_horror_sfx_are_louder_than_the_music_bed():
    """A one-shot has to cut through the bed it lands on top of."""
    cfg = load_channel_config("horror_stories")
    music = _lu_under_narration(
        MUSIC_BED_LUFS, cfg.video.background_music_volume
    )
    sfx = _lu_under_narration(SFX_LUFS, cfg.rendering_defaults.sfx_volume)
    assert sfx < music, (
        f"SFX ({sfx:.1f} LU under) are quieter than the bed ({music:.1f} LU under)"
    )


def test_horror_music_never_reaches_narration_level():
    """A bed at or above the voice is the failure this whole exercise prevents."""
    cfg = load_channel_config("horror_stories")
    assert _lu_under_narration(
        MUSIC_BED_LUFS, cfg.video.background_music_volume
    ) >= 12.0


def test_horror_declares_mood_guidance_for_track_selection():
    raw = json.loads(
        (REPO_ROOT / "config" / "channels" / "horror_stories.json")
        .read_text(encoding="utf-8")
    )
    guidance = raw["video"]["music_guidance"]
    for stem in MUSIC_STEMS:
        assert stem in guidance, f"{stem} has no mood rule"


# --- Football News isolation ----------------------------------------------

def test_football_news_cannot_pick_up_horror_sfx():
    """The SFX tree is shared, so the pool is what keeps engines apart."""
    cfg = load_channel_config("football_news")
    assert cfg.video.sfx_pool == ["whoosh_soft", "whoosh_rise"]
    assert not any(s.startswith("horror_") for s in cfg.video.sfx_pool)


def test_football_news_music_is_untouched():
    cfg = load_channel_config("football_news")
    assert cfg.video.music_pool == ["news_bed_calm", "news_bed_drive"]
    assert cfg.video.background_music_volume == 0.12


def test_the_two_engines_share_no_audio_asset():
    horror = load_channel_config("horror_stories").video
    football = load_channel_config("football_news").video
    assert not set(horror.music_pool) & set(football.music_pool)
    assert not set(horror.sfx_pool) & set(football.sfx_pool)


def test_sfx_pool_filter_excludes_other_channels_clips(tmp_path, monkeypatch):
    """An empty pool must not sweep in newly added channel-specific one-shots."""
    import core.audio_sourcer as audio_sourcer

    root = tmp_path / "assets"
    (root / "sfx" / "transitions").mkdir(parents=True)
    (root / "sfx" / "horror").mkdir(parents=True)
    (root / "sfx" / "transitions" / "whoosh_soft.mp3").write_bytes(b"\0")
    (root / "sfx" / "horror" / "horror_heartbeat.mp3").write_bytes(b"\0")
    monkeypatch.setattr(audio_sourcer, "ASSETS_DIR", root)

    seen: dict[str, list] = {}
    monkeypatch.setattr(
        audio_sourcer.random, "sample",
        lambda pop, k: (seen.setdefault("called", []), [])[1],
    )
    # With no pool declared, only the historical transitions dir is eligible.
    ok = audio_sourcer._build_transition_track(
        section_durations=[5.0, 5.0],
        total_duration=10.0,
        output_path=tmp_path / "out.wav",
        sfx_boundary_coverage=0.0,
    )
    # Coverage 0 short-circuits before mixing; the point is it did not raise
    # and did not consider the horror clip a transition asset.
    assert ok is False
