"""Offline regressions for Football's factual, visual fallback ladder."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core import generated_visuals, image_sourcer
from core.scripter import annotate_football_player_slots, repair_outdated_club_briefs
from core.utils import ScriptSection, VisualSlot, load_channel_config


def _slot() -> VisualSlot:
    return VisualSlot(
        visual="google_photo",
        keywords="Julian Alvarez Atletico Madrid current club photograph",
        prompt="Julian Alvarez in Atletico Madrid context during a football match.",
        props={
            "football_subject": "player",
            "football_player_name": "Julian Alvarez",
            "football_current_club": "Atletico Madrid",
        },
    )


def _descriptor(tmp_path: Path) -> dict:
    return {
        "section": ScriptSection(id=1, narration="Julian Alvarez remains at Atletico Madrid.", slots=[]),
        "sub_idx": 0,
        "slot": _slot(),
        "img_path": tmp_path / "section_001_01.jpg",
        "sourced": False,
    }


def test_football_config_uses_real_sources_before_narrow_generated_fallback():
    cfg = load_channel_config("football_news").image_sourcing
    assert cfg.web_photos_only is True
    assert cfg.open_library_fallback is True
    assert cfg.allow_generated_player_reconstruction is True
    assert cfg.generated_player_reconstruction_model.startswith("gemini-")
    assert cfg.generated_fallback_model.startswith("fal-ai/")


def test_current_player_annotation_follows_verified_club_and_repairs_old_brief():
    script = {
        "sections": [{"id": 1, "narration": "Julian Alvarez at Atletico Madrid.", "slots": [
            {"visual": "google_photo", "keywords": "Julian Alvarez Manchester City", "prompt": "Julian Alvarez at Manchester City"}
        ]}],
    }
    status = [{"name": "Julian Alvarez", "current_club": "Atletico Madrid", "former_clubs": ["Manchester City"]}]

    assert repair_outdated_club_briefs(script, status) == 2
    assert annotate_football_player_slots(script, status) == 1
    props = script["sections"][0]["slots"][0]["props"]
    assert props["football_player_name"] == "Julian Alvarez"
    assert props["football_current_club"] == "Atletico Madrid"
    assert "Manchester City" not in str(script["sections"][0]["slots"][0])


def test_player_reconstruction_runs_only_for_verified_player_slots(tmp_path, monkeypatch):
    cfg = load_channel_config("football_news")
    player = _descriptor(tmp_path)
    generic = _descriptor(tmp_path)
    generic["slot"] = VisualSlot(visual="google_photo", keywords="football stadium crowd", prompt="football stadium crowd")
    generic["img_path"] = tmp_path / "section_001_02.jpg"
    calls: list[dict] = []

    async def generated(prompt, output_path, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        Path(output_path).write_bytes(b"generated")
        return Path(output_path)

    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", generated)
    monkeypatch.setattr(image_sourcer, "_conform_image_to_target", lambda *a, **k: True)
    image_sourcer._reset_provenance()
    log: list[dict] = []

    count = asyncio.run(image_sourcer._generate_football_player_reconstructions(
        descriptors=[player, generic], config=cfg, sourcing_log=log,
    ))

    assert count == 1
    assert player["sourced"] is True and generic["sourced"] is False
    assert calls[0]["model"] == cfg.image_sourcing.generated_player_reconstruction_model
    assert "not documentary photography" in calls[0]["prompt"]
    assert log[0]["generated_reconstruction"] is True
    assert "not real news photography" in log[0]["provenance"]
    assert player["slot"].props["generated_reconstruction"] is True


def test_rate_limited_or_malformed_player_generation_leaves_slot_for_next_fallback(tmp_path, monkeypatch):
    cfg = load_channel_config("football_news")
    desc = _descriptor(tmp_path)

    async def refused(*args, **kwargs):
        return None

    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", refused)
    assert asyncio.run(image_sourcer._generate_football_player_reconstructions(
        descriptors=[desc], config=cfg, sourcing_log=[],
    )) == 0
    assert desc["sourced"] is False


def test_generic_stadium_fallback_stays_generated_and_never_becomes_player_evidence(tmp_path):
    planned, budget = generated_visuals.plan_fallback(
        [{"section_id": 2, "sub_image_index": 1, "output_path": tmp_path / "stadium.jpg", "brief": "anonymous football stadium crowd at night"}],
        limit=1,
    )
    assert len(planned) == 1
    assert budget.refused == []
    assert "anonymous" in planned[0]["prompt"]


def test_named_player_is_refused_by_the_generic_fallback():
    planned, budget = generated_visuals.plan_fallback(
        [{"section_id": 2, "sub_image_index": 1, "brief": "Julian Alvarez celebrates a goal"}],
        limit=1,
    )
    assert planned == []
    assert budget.refused


def test_football_player_fallback_is_after_all_real_source_and_rescue_passes():
    source = Path(image_sourcer.__file__).read_text(encoding="utf-8")
    assert source.index("await _rescue_underpopulated_sections(") < source.index(
        "await _generate_football_player_reconstructions("
    ) < source.index("fallback_budget = await _generate_missing_visuals(")


def test_no_unrelated_image_reuse_is_preserved():
    image_sourcer._reset_pexels_dedup()
    assert image_sourcer._claim_pexels_photo("same-photo") is True
    image_sourcer._commit_pexels_photo("same-photo")
    assert image_sourcer._claim_pexels_photo("same-photo") is False
