"""Offline safeguards for the True Stories documentary pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core import generated_visuals, image_sourcer, researcher
from core.news_sources import NewsItem
from core.utils import ScriptSection, VisualSlot, load_channel_config


TRUE = load_channel_config("true_stories")


def _evidence(title: str = "Official archive confirms the documented case") -> NewsItem:
    return NewsItem(
        title=title,
        url="https://www.bbc.com/news/documented-case",
        domain="bbc.com",
        published_at="2026-09-08T10:00:00Z",
        snippet="The archive record describes the documented case.",
        provider="test",
    )


def test_true_stories_requires_retrieved_evidence_before_model_research(monkeypatch):
    async def no_evidence(*args, **kwargs):
        return []

    async def model_must_not_run(*args, **kwargs):
        raise AssertionError("model research must not replace missing documentary evidence")

    monkeypatch.setattr(researcher.news_sources, "fetch_current_news", no_evidence)
    monkeypatch.setattr(researcher.clients, "research_with_search", model_must_not_run)
    out = asyncio.run(researcher.research_topic(topic="A documented disappearance", angle="", config=TRUE))
    assert out["evidence_required"] is True
    assert out["brief"] == ""


def test_retrieved_evidence_is_injected_and_unsupported_claims_are_downgraded(monkeypatch):
    seen: dict[str, str] = {}

    async def evidence(*args, **kwargs):
        return [_evidence("Archive confirms the last documented sighting")]

    async def grounded(prompt, **kwargs):
        seen["prompt"] = prompt
        return {
            "grounded": True,
            "as_of_date": "2026-09-08",
            "sources": [{"url": "https://www.bbc.com/news/documented-case"}],
            "verified_facts": [{"claim": "An invented culprit was convicted", "status": "CONFIRMED"}],
        }

    monkeypatch.setattr(researcher.news_sources, "fetch_current_news", evidence)
    monkeypatch.setattr(researcher.clients, "research_with_search", grounded)
    out = asyncio.run(researcher.research_topic(topic="A documented disappearance", angle="", config=TRUE))
    assert "Archive confirms the last documented sighting" in seen["prompt"]
    assert out["verified_facts"][0]["status"] == "REPORTED"
    assert out["verified_facts"][0]["unsupported_by_retrieved_reporting"] is True


def test_true_stories_searches_real_sources_before_generation():
    slot = VisualSlot(visual="google_photo", keywords="documented station building", prompt="documented station building")
    assert TRUE.image_sourcing.prefer_generated_visuals is False
    assert image_sourcer._image_source_for_slot(slot, TRUE) == "serper"
    assert TRUE.image_sourcing.open_library_fallback is True


def test_generated_reconstruction_is_never_archival_evidence(tmp_path, monkeypatch):
    section = ScriptSection(id=1, narration="The documented record ends at the station.", slots=[])
    desc = {
        "section": section, "sub_idx": 0,
        "slot": VisualSlot(visual="google_photo", keywords="anonymous station exterior", prompt="anonymous station exterior"),
        "keywords": "anonymous station exterior", "prompt": "anonymous station exterior",
        "img_path": tmp_path / "section_001_01.jpg", "sourced": False,
    }

    async def generated(prompt, output_path, **kwargs):
        Path(output_path).write_bytes(b"generated")
        return Path(output_path)

    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", generated)
    monkeypatch.setattr(image_sourcer, "_is_usable_asset", lambda *a, **k: True)
    monkeypatch.setattr(image_sourcer, "_conform_image_to_target", lambda *a, **k: True)
    image_sourcer._reset_provenance()
    asyncio.run(image_sourcer._generate_missing_visuals(
        descriptors=[desc], config=TRUE, sourcing_log=[], limit_override=1,
    ))
    record = image_sourcer._ASSET_PROVENANCE["section_001_01.jpg"]
    assert record["generated"] is True
    assert record["provenance_kind"] == "generated_reconstruction"
    assert record["provenance"] == (
        "AI-generated reconstruction, not archival evidence or a photograph"
    )


def test_no_fake_archival_document_or_exact_undocumented_event_can_be_generated():
    for brief in (
        "archival police file from the case",
        "historical photograph of the undocumented disappearance",
        "FBI evidence photograph of the missing person's belongings",
    ):
        planned, budget = generated_visuals.plan_fallback(
            [{"section_id": 1, "sub_image_index": 1, "brief": brief}], limit=1,
        )
        assert planned == []
        assert budget.refused


def test_rate_limited_generated_reconstruction_falls_through_without_false_success(tmp_path, monkeypatch):
    section = ScriptSection(id=1, narration="A documented setting.", slots=[])
    desc = {
        "section": section, "sub_idx": 0,
        "slot": VisualSlot(visual="google_photo", keywords="anonymous documented setting", prompt="anonymous documented setting"),
        "keywords": "anonymous documented setting", "prompt": "anonymous documented setting",
        "img_path": tmp_path / "section_001_01.jpg", "sourced": False,
    }

    async def rate_limited(*args, **kwargs):
        return None

    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", rate_limited)
    assert asyncio.run(image_sourcer._generate_missing_visuals(
        descriptors=[desc], config=TRUE, sourcing_log=[], limit_override=1,
    )) is not None
    assert desc["sourced"] is False


def test_true_story_sourced_and_archival_assets_keep_distinct_provenance(tmp_path):
    image_sourcer._reset_provenance()
    image_sourcer._mark_true_story_provenance(
        tmp_path / "place.jpg", source="serper", keywords="documented station building", prompt="station exterior",
    )
    image_sourcer._mark_true_story_provenance(
        tmp_path / "archive.jpg", source="serper", keywords="archival police file", prompt="archival case file",
    )
    assert image_sourcer._ASSET_PROVENANCE["place.jpg"]["provenance_kind"] == "sourced"
    assert image_sourcer._ASSET_PROVENANCE["archive.jpg"]["provenance_kind"] == "archival"


def test_safe_coverage_and_dedup_are_still_the_last_two_guards():
    source = Path(image_sourcer.__file__).read_text(encoding="utf-8")
    assert source.index("fallback_budget = await _generate_missing_visuals(") < source.index("await _cover_unsourced_slots(")
    image_sourcer._reset_pexels_dedup()
    assert image_sourcer._claim_pexels_photo("documented-photo") is True
    image_sourcer._commit_pexels_photo("documented-photo")
    assert image_sourcer._claim_pexels_photo("documented-photo") is False
