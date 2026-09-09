"""Offline regression coverage for Horror's real-first visual ladder."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from core import generated_visuals, image_sourcer
from core.utils import Script, ScriptSection, VisualSlot, load_channel_config


HORROR = load_channel_config("horror_stories")


def _descriptor(tmp_path: Path, *, prompt: str = "abandoned coastal radio station") -> dict:
    section = ScriptSection(id=1, narration=f"The story returns to the {prompt}.", slots=[])
    return {
        "section": section,
        "sub_idx": 0,
        "slot": VisualSlot(visual="google_photo", keywords=prompt, prompt=prompt),
        "keywords": prompt,
        "prompt": prompt,
        "img_path": tmp_path / "section_001_01.jpg",
        "sourced": False,
    }


def test_horror_declares_real_first_full_provider_ladder():
    source = Path(image_sourcer.__file__).read_text(encoding="utf-8")
    for provider in ("_search_serper", "_search_pexels", "PixabayProvider", "UnsplashProvider", "CommonsProvider"):
        assert provider in source
    assert HORROR.image_sourcing.prefer_generated_visuals is False
    assert HORROR.image_sourcing.open_library_fallback is True


def test_horror_routes_an_ai_labeled_beat_through_real_sourcing_first():
    slot = VisualSlot(
        visual="ai_photo", keywords="fog-bound coastal road", prompt="fog-bound coastal road",
    )
    assert image_sourcer._image_source_for_slot(slot, HORROR) == "serper"


def test_horror_tries_pexels_before_open_libraries_after_web_search(monkeypatch, tmp_path):
    calls: list[str] = []

    async def no_serper(*args, **kwargs):
        calls.append("serper")
        return False

    async def pexels(*args, **kwargs):
        calls.append("pexels")
        return True

    async def open_libraries(*args, **kwargs):
        calls.append("open")
        return False

    monkeypatch.setattr(image_sourcer, "_search_serper", no_serper)
    monkeypatch.setattr(image_sourcer, "_search_pexels", pexels)
    monkeypatch.setattr(image_sourcer, "_search_open_libraries", open_libraries)

    async def run():
        async with httpx.AsyncClient() as client:
            return await image_sourcer._source_single_image(
                "abandoned lighthouse", "abandoned lighthouse at dusk", "serper",
                HORROR, tmp_path / "beat.jpg", client, set(), "photo",
            )

    assert asyncio.run(run()) == "pexels"
    assert calls == ["serper", "pexels"]


def test_horror_defers_generation_until_the_guarded_flux_pass(monkeypatch, tmp_path):
    async def no_source(*args, **kwargs):
        return False

    async def generation_must_not_run(*args, **kwargs):
        raise AssertionError("direct generation bypassed Horror's guarded FLUX fallback")

    monkeypatch.setattr(image_sourcer, "_search_serper", no_source)
    monkeypatch.setattr(image_sourcer, "_search_pexels", no_source)
    monkeypatch.setattr(image_sourcer, "_search_open_libraries", no_source)
    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", generation_must_not_run)

    async def run():
        async with httpx.AsyncClient() as client:
            return await image_sourcer._source_single_image(
                "documented coastal station", "documented coastal station", "serper",
                HORROR, tmp_path / "beat.jpg", client, set(), "photo",
            )

    assert asyncio.run(run()) is None


def test_horror_flux_fallback_is_story_specific_and_marked_generated(monkeypatch, tmp_path):
    desc = _descriptor(tmp_path)
    calls: list[dict] = []

    async def generated(prompt, output_path, **kwargs):
        calls.append(kwargs)
        Path(output_path).write_bytes(b"generated")
        return Path(output_path)

    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", generated)
    monkeypatch.setattr(image_sourcer, "_is_usable_asset", lambda *a, **k: True)
    monkeypatch.setattr(image_sourcer, "_conform_image_to_target", lambda *a, **k: True)
    image_sourcer._reset_provenance()
    log: list[dict] = []
    asyncio.run(image_sourcer._generate_missing_visuals(
        descriptors=[desc], config=HORROR, sourcing_log=log, limit_override=1,
    ))

    assert calls[0]["model"] == "fal-ai/flux/schnell"
    assert desc["sourced"] is True
    assert desc["generated"] is True
    assert log[0]["generated"] is True
    assert "abandoned coastal radio station" in log[0]["prompt"]


def test_horror_never_generates_fake_archival_evidence():
    planned, budget = generated_visuals.plan_fallback(
        [{"section_id": 1, "sub_image_index": 1, "brief": "archival police photograph of the victim"}],
        limit=1,
        style_suffix=HORROR.image_sourcing.style_prompt_suffix,
    )
    assert planned == []
    assert budget.refused


def test_rate_limited_flux_falls_through_without_false_success(monkeypatch, tmp_path):
    desc = _descriptor(tmp_path)

    async def rate_limited(*args, **kwargs):
        return None

    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", rate_limited)
    log: list[dict] = []
    budget = asyncio.run(image_sourcer._generate_missing_visuals(
        descriptors=[desc], config=HORROR, sourcing_log=log, limit_override=1,
    ))
    assert budget is not None
    assert desc["sourced"] is False
    assert log == []


def test_safe_coverage_uses_each_horror_beats_own_text(tmp_path, monkeypatch):
    first = _descriptor(tmp_path, prompt="the abandoned lighthouse")
    first["sourced"] = True
    missing = _descriptor(tmp_path, prompt="the isolated radio tower")
    missing["sub_idx"] = 1
    missing["img_path"] = tmp_path / "section_001_02.jpg"
    missing["slot"].props = {"title": "Radio tower"}

    async def no_broll(*args, **kwargs):
        return False

    monkeypatch.setattr(image_sourcer, "_search_pexels_video", no_broll)

    covered = asyncio.run(image_sourcer._cover_unsourced_slots(
        descriptors=[first, missing], config=HORROR, sourcing_log=[],
        videos_dir=tmp_path / "videos", raw_dir=tmp_path / "raw", seen_hashes=set(),
        target_size=tuple(HORROR.video.resolution), fps=HORROR.video.fps,
    ))
    assert covered == 1
    assert missing["slot"].visual == "info_card"
    assert missing["slot"].props["text"] == "the isolated radio tower"
    assert first["slot"].visual == "google_photo"


def test_subject_rescue_stays_aligned_to_its_own_narration_beat():
    section = ScriptSection(
        id=1,
        narration="Witnesses described the abandoned lighthouse on the northern coast.",
        slots=[],
    )
    script = Script(title="The lighthouse signal", video_type="story", sections=[section])
    slot = VisualSlot(
        visual="google_photo", keywords="abandoned lighthouse northern coast",
        prompt="The abandoned lighthouse on the northern coast.",
    )
    queries = image_sourcer._subject_rescue_queries(script, section, slot)
    assert queries
    assert "lighthouse" in queries[0].lower()
    assert all("unrelated" not in query.lower() for query in queries)
