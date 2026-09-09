"""Generated scene visuals: which channels use FLUX, and when.

The whole risk in this change is blast radius. FLUX is a *fallback* for two
story channels, and it must not reach Football, must not reach thumbnails, and
must not run at all while real photography is still available. Most of these
tests exist to prove something does NOT happen.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import clients
from core.costs import _load_pricing_catalog
from core.providers.scene_images import (
    FluxSchnellProvider,
    dimensions_for,
    megapixels,
)
from core.utils import load_channel_config

CHANNELS = Path("config/channels")


# ── provider selection: who gets FLUX ────────────────────────────────

@pytest.mark.parametrize("slug", ["horror_stories", "true_stories"])
def test_story_channels_generate_with_flux_schnell(slug):
    config = load_channel_config(slug)
    assert config.image_sourcing.generated_fallback_model == "fal-ai/flux/schnell"


@pytest.mark.parametrize("slug", ["horror_stories", "true_stories"])
@pytest.mark.parametrize("language", ["ar", "en"])
def test_story_channels_use_flux_in_every_language(slug, language):
    config = load_channel_config(slug, language=language)
    assert config.image_sourcing.generated_fallback_model == "fal-ai/flux/schnell"


def test_football_is_not_routed_to_flux():
    """Requirement 1: Football image generation is untouched."""
    config = load_channel_config("football_news")
    assert "fal" not in config.image_sourcing.generated_fallback_model
    assert "flux" not in config.image_sourcing.generated_fallback_model.lower()


def test_football_config_does_not_mention_fal_at_all():
    text = (CHANNELS / "football_news.json").read_text(encoding="utf-8")
    assert "fal-ai" not in text
    assert "flux" not in text.lower()


def test_thumbnails_are_not_routed_to_flux():
    """Requirement 2: the thumbnail system is untouched."""
    thumbnailer = Path("core/thumbnailer.py").read_text(encoding="utf-8")
    thumb_provider = Path("core/providers/thumbnails.py").read_text(encoding="utf-8")
    for text in (thumbnailer, thumb_provider):
        assert "flux" not in text.lower()
        assert "schnell" not in text.lower()
    # And the thumbnail model is still the edit model.
    assert "gemini-25-flash-image/edit" in thumb_provider


def test_the_scene_generator_is_not_the_thumbnail_generator():
    scene = Path("core/providers/scene_images.py").read_text(encoding="utf-8")
    assert "edit_thumbnail_image" not in scene
    assert "/edit" not in scene


# ── the dispatcher routes on the configured model ────────────────────

def _route(model, monkeypatch, tmp_path):
    """Which generator `generate_scene_image` actually calls."""
    called = {}

    async def fake_gemini(prompt, output_path, **kwargs):
        called["gemini"] = kwargs.get("model")
        return output_path

    async def fake_fal(prompt, output_path, **kwargs):
        called["fal"] = kwargs.get("model")
        return output_path

    monkeypatch.setattr(clients, "generate_image_gemini", fake_gemini)
    monkeypatch.setattr(clients, "_generate_scene_image_fal", fake_fal)
    asyncio.run(
        clients.generate_scene_image(
            "a dark road", tmp_path / "x.png", model=model,
            aspect_ratio="9:16", target_size=(1080, 1920),
        )
    )
    return called


def test_a_fal_model_reaches_the_fal_generator(monkeypatch, tmp_path):
    called = _route("fal-ai/flux/schnell", monkeypatch, tmp_path)
    assert called == {"fal": "fal-ai/flux/schnell"}


def test_a_gemini_model_still_reaches_gemini(monkeypatch, tmp_path):
    """Football's path must be byte-for-byte what it was."""
    called = _route("gemini-3.1-flash-lite-image", monkeypatch, tmp_path)
    assert called == {"gemini": "gemini-3.1-flash-lite-image"}
    assert "fal" not in called


def test_no_model_configured_still_reaches_gemini(monkeypatch, tmp_path):
    called = _route(None, monkeypatch, tmp_path)
    assert "gemini" in called and "fal" not in called


# ── FLUX runs only after real sourcing has failed ────────────────────

def test_generation_is_the_last_tier_not_the_first():
    """Requirement 4/5: real media is preferred; generation is a fallback."""
    source = Path("core/image_sourcer.py").read_text(encoding="utf-8")
    # The generator is reached through the fallback planner, which is what
    # runs only for beats nothing else could fill.
    assert "generated_visuals.plan_fallback" in source
    assert "generate_scene_image" in source


@pytest.mark.parametrize("slug", ["horror_stories", "true_stories"])
def test_real_media_sources_are_all_still_configured(slug):
    """Requirement 3: no licensed source was removed."""
    settings_text = Path("settings.py").read_text(encoding="utf-8")
    for provider in ("PEXELS", "PIXABAY", "UNSPLASH", "SERPER"):
        assert provider in settings_text, f"{provider} was removed"
    media = Path("core/providers/media.py").read_text(encoding="utf-8").lower()
    for provider in ("pexels", "pixabay", "wikimedia", "unsplash"):
        assert provider in media, f"{provider} provider was removed"
    # And the channel still allows them.
    config = load_channel_config(slug)
    assert config.image_sourcing.open_library_fallback is True


@pytest.mark.parametrize("slug", ["horror_stories", "true_stories"])
def test_generation_is_still_gated_by_the_refusal_rules(slug):
    """A real person or archival event is still never fabricated."""
    from core import generated_visuals

    # A face presented as a real person, and an artefact presented as
    # evidence, are both still refused before any generator is reached.
    for brief in (
        "portrait of the victim, black and white",
        "archival photograph of the burned house",
        "the police file on the disappearance",
        "a photograph of Jude Bellingham celebrating",
    ):
        assert not generated_visuals.is_safe_to_generate(brief), brief

    planned, budget = generated_visuals.plan_fallback(
        [{"section_id": 1, "sub_image_index": 1,
          "brief": "archival photograph of the victim",
          "output_path": "x.png"}],
        limit=10,
    )
    assert planned == []
    assert budget.refused

    # And something genuinely generic still passes, or the channel could
    # never illustrate anything.
    assert generated_visuals.is_safe_to_generate(
        "an empty fog-covered country road at night, no people"
    )


@pytest.mark.parametrize("slug", ["horror_stories", "true_stories"])
def test_there_is_no_fixed_five_image_limit(slug):
    """Requirement 6: as many visuals as the story needs."""
    config = load_channel_config(slug)
    assert config.image_sourcing.max_generated_fallback_images > 5
    assert config.image_sourcing.generate_when_rejected is True
    # Requirement 8: a rejected generated visual is regenerated.
    assert config.image_sourcing.max_generated_when_rejected >= 1


def test_football_keeps_its_own_smaller_budget():
    assert load_channel_config(
        "football_news"
    ).image_sourcing.max_generated_fallback_images == 5


# ── the frames are the right size and style ──────────────────────────

def test_frames_are_sized_to_the_render_target_not_a_preset():
    """fal's portrait preset is 576x1024, below the 1080x1280 minimum."""
    from core.image_sourcer import minimum_source_size

    width, height = dimensions_for("9:16", (1080, 1920))
    min_w, min_h = minimum_source_size((1080, 1920))
    assert width >= min_w and height >= min_h


def test_frame_dimensions_sit_on_the_models_grid():
    for target in [(1080, 1920), (1920, 1080), (720, 1280)]:
        width, height = dimensions_for("9:16", target)
        assert width % 16 == 0 and height % 16 == 0


def test_a_missing_target_still_yields_a_usable_portrait_frame():
    assert dimensions_for("9:16", (0, 0)) == (1088, 1920)


@pytest.mark.parametrize("slug", ["horror_stories", "true_stories"])
def test_the_style_asks_for_cinematic_photorealism(slug):
    """Requirement 10, and FLUX drifts to illustration without it."""
    suffix = load_channel_config(slug).image_sourcing.style_prompt_suffix.lower()
    assert "photorealistic" in suffix
    assert "cinematic" in suffix
    for banned in ("illustration", "painting", "cgi"):
        assert banned in suffix, f"the prompt should exclude {banned}"


# ── the provider itself ──────────────────────────────────────────────

def test_provider_needs_a_key():
    status = FluxSchnellProvider(api_key="").status()
    assert not status.usable
    assert "FAL_KEY" in status.reason


def test_the_model_is_the_one_that_was_asked_for():
    provider = FluxSchnellProvider(api_key="k")
    assert provider.MODEL == "fal-ai/flux/schnell"
    assert provider.URL.endswith("/fal-ai/flux/schnell")


def test_an_empty_prompt_is_refused():
    provider = FluxSchnellProvider(api_key="k")
    with pytest.raises(ValueError, match="needs a prompt"):
        asyncio.run(provider.generate("  ", client=None, width=1088, height=1920))


class _Response:
    def __init__(self, payload=None, content=b"", status_code=200, text=""):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.body = None
        self.headers = None

    async def post(self, url, *, headers=None, json=None):
        self.headers = headers
        self.body = json
        return _Response(payload=self.payload)

    async def get(self, url):
        return _Response(content=b"PNGBYTES")


def test_the_request_asks_for_the_exact_size_and_png():
    provider = FluxSchnellProvider(api_key="k")
    client = _Client({"images": [{"url": "https://cdn/x.png"}]})
    out = asyncio.run(
        provider.generate("a dark road", client=client, width=1088, height=1920)
    )
    assert out == b"PNGBYTES"
    assert client.body["image_size"] == {"width": 1088, "height": 1920}
    assert client.body["output_format"] == "png"
    assert client.body["num_images"] == 1
    assert client.headers["Authorization"] == "Key k"


def test_a_frame_the_safety_checker_flagged_is_not_returned():
    provider = FluxSchnellProvider(api_key="k")
    client = _Client({
        "images": [{"url": "https://cdn/x.png"}],
        "has_nsfw_concepts": [True],
    })
    with pytest.raises(RuntimeError, match="safety checker"):
        asyncio.run(provider.generate("x", client=client, width=1088, height=1920))


def test_an_error_reports_the_body():
    class _Failing(_Client):
        async def post(self, url, *, headers=None, json=None):
            return _Response(status_code=422, text='{"detail":"bad size"}')

    provider = FluxSchnellProvider(api_key="k")
    with pytest.raises(RuntimeError, match="bad size"):
        asyncio.run(
            provider.generate("x", client=_Failing({}), width=1, height=1)
        )


# ── pricing ──────────────────────────────────────────────────────────

def test_flux_is_in_the_pricing_catalog():
    entry = next(
        (e for e in _load_pricing_catalog().entries
         if e.model == "fal-ai/flux/schnell"),
        None,
    )
    assert entry is not None, "FLUX Schnell has no pricing entry"
    assert entry.service == "generate_content_image"
    assert entry.output_image_rate_usd_per_image == 0.009
    assert entry.reconciliation_supported is False
    assert entry.source_url.startswith("https://fal.ai/")


def test_megapixels_round_up_the_way_fal_bills():
    assert megapixels(1088, 1920) == 3      # 2.09MP -> 3
    assert megapixels(1024, 576) == 1       # 0.59MP -> 1
    assert megapixels(0, 0) == 0


def test_one_generated_scene_is_priced_at_the_published_rate(tmp_path):
    from types import SimpleNamespace

    from core import costs

    costs.initialize_cost_tracking(
        workspace=tmp_path, run_id="flux_pricing", channel="horror_stories"
    )
    costs.record_generate_content_cost(
        model="fal-ai/flux/schnell",
        usage_metadata=SimpleNamespace(),
        operation="generated_fallback",
        service="generate_content_image",
        provider="fal_ai",
        generated_images=1,
    )
    event = costs.get_tracker().events[0]
    assert event.estimated_usd == pytest.approx(0.009)
    assert event.generated_images == 1, "one image must report as one image"
    assert event.reconciliation_supported is False


def test_the_real_call_path_prices_and_traces_itself(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from core import costs

    costs.initialize_cost_tracking(
        workspace=tmp_path, run_id="flux_path", channel="horror_stories"
    )
    monkeypatch.setattr(clients, "reserve_trace", lambda **kw: None)

    class _Prov:
        name = "fal_flux_schnell"
        MODEL = "fal-ai/flux/schnell"
        TIMEOUT = 10.0

        def status(self):
            from core.providers.base import Availability, ProviderStatus

            return ProviderStatus(self.name, Availability.READY)

        async def generate(self, prompt, *, client, width, height, steps=4):
            return b"PNG"

    monkeypatch.setattr("core.providers.scene_images.FluxSchnellProvider", _Prov)

    out = asyncio.run(
        clients.generate_scene_image(
            "a fog-covered road at night",
            tmp_path / "section_001_01.png",
            model="fal-ai/flux/schnell",
            aspect_ratio="9:16",
            target_size=(1080, 1920),
        )
    )
    assert out is not None and out.exists()
    priced = [e for e in costs.get_tracker().events
              if e.model == "fal-ai/flux/schnell"]
    assert len(priced) == 1
    assert priced[0].estimated_usd == pytest.approx(0.009)


def test_provenance_and_the_cost_tracker_agree_on_the_price(tmp_path):
    """One image must not be reported at two different prices.

    The fallback's own record used a single hardcoded rate, so a FLUX frame
    was logged at the Gemini estimate ($0.0258) in the run's provenance while
    the cost tracker recorded $0.009.
    """
    from core import costs, generated_visuals

    costs.initialize_cost_tracking(
        workspace=tmp_path, run_id="agree", channel="horror_stories"
    )
    budget = generated_visuals.FallbackBudget(limit=5)
    visual = generated_visuals.record_generated(
        budget,
        section_id=1,
        sub_image_index=1,
        path=tmp_path / "section_001_01.jpg",
        prompt="a fog-covered road",
        model="fal-ai/flux/schnell",
    )
    assert visual.cost_usd == pytest.approx(0.009)
    assert visual.to_provenance()["cost_usd"] == pytest.approx(0.009)
    assert budget.summary()["cost_per_image_usd"] == pytest.approx(0.009)


def test_each_generator_is_costed_at_its_own_rate(tmp_path):
    from core import costs, generated_visuals

    costs.initialize_cost_tracking(
        workspace=tmp_path, run_id="rates", channel="horror_stories"
    )
    assert generated_visuals.cost_per_image(
        "fal-ai/flux/schnell") == pytest.approx(0.009)
    assert generated_visuals.cost_per_image(
        "gemini-3.1-flash-lite-image") == pytest.approx(0.0336)
    # A model the catalog does not price keeps the legacy estimate rather
    # than reporting zero.
    assert generated_visuals.cost_per_image("not-a-real-model") == pytest.approx(
        generated_visuals.COST_PER_IMAGE_USD
    )


def test_provenance_marks_a_generated_frame_as_generated(tmp_path):
    """`generated: True` is what stops a frame being presented as a photo."""
    from core import costs, generated_visuals

    costs.initialize_cost_tracking(
        workspace=tmp_path, run_id="prov", channel="horror_stories"
    )
    record = generated_visuals.record_generated(
        generated_visuals.FallbackBudget(limit=5),
        section_id=1, sub_image_index=1,
        path=tmp_path / "section_001_01.jpg",
        prompt="a fog-covered road",
        model="fal-ai/flux/schnell",
    ).to_provenance()
    assert record["generated"] is True
    assert record["model"] == "fal-ai/flux/schnell"
    assert record["source"] == "ai_generated_fallback"
    assert "not a photograph" in record["provenance"]
    assert record["prompt"]


def test_a_failed_generation_returns_none_so_the_beat_stays_unsourced(
    tmp_path, monkeypatch
):
    """Requirement 11: never fill a slot with something irrelevant."""
    from core import costs

    costs.initialize_cost_tracking(
        workspace=tmp_path, run_id="flux_fail", channel="horror_stories"
    )
    monkeypatch.setattr(clients, "reserve_trace", lambda **kw: None)

    class _Prov:
        name = "fal_flux_schnell"
        MODEL = "fal-ai/flux/schnell"
        TIMEOUT = 10.0

        def status(self):
            from core.providers.base import Availability, ProviderStatus

            return ProviderStatus(self.name, Availability.READY)

        async def generate(self, prompt, *, client, width, height, steps=4):
            raise RuntimeError("refused")

    monkeypatch.setattr("core.providers.scene_images.FluxSchnellProvider", _Prov)

    out = asyncio.run(
        clients.generate_scene_image(
            "x", tmp_path / "a.png", model="fal-ai/flux/schnell",
            target_size=(1080, 1920),
        )
    )
    assert out is None
    assert not (tmp_path / "a.png").exists()
