"""The media aggregator: routing, licensing, normalisation, failure isolation.

Everything here runs on mocked HTTP. No provider is contacted, no key is
required, and the suite finishes in well under a second — which is the point,
because the behaviour that matters most is what happens when a source is
missing, slow or wrong, and none of that is reproducible against the real
internet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from medialab.media import licensing, router
from medialab.media.aggregator import PROVIDERS, provider_status, search
from medialab.media.base import MediaProvider
from medialab.media.types import MediaAsset, MediaType, ProviderStatus, SearchContext


# ── licensing: the part that must never guess ────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("CC0", "cc0"),
    ("cc0 1.0", "cc0"),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
    ("https://creativecommons.org/licenses/by/4.0/", "cc-by"),
    ("https://creativecommons.org/licenses/by-nc-sa/4.0/", "cc-by-nc-sa"),
    ("Creative Commons Attribution-NonCommercial 4.0", "cc-by-nc"),
    ("Attribution-ShareAlike", "cc-by-sa"),
    ("Public Domain Mark 1.0", "pdm"),
    ("PD-USGov-NASA", "usgov"),
    ("No known copyright restrictions", "pdm"),
    ("All rights reserved", "rights-reserved"),
    ("fair use", "rights-reserved"),
])
def test_licence_strings_resolve_to_one_vocabulary(raw, expected):
    assert licensing.normalise(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "custom studio terms", "ask the owner"])
def test_an_unrecognised_licence_is_not_guessed_at(raw):
    """Empty is the safe answer: it fails `evaluate` rather than passing."""
    assert licensing.normalise(raw) == ""


def _asset(license_id: str, **kw) -> MediaAsset:
    return MediaAsset(
        provider=kw.pop("provider", "commons"),
        asset_id=kw.pop("asset_id", "1"),
        media_type=MediaType.IMAGE,
        creator=kw.pop("creator", "A Photographer"),
        license=license_id,
        **kw,
    )


def test_reusable_licences_pass():
    for good in ("cc0", "public-domain", "pdm", "usgov", "cc-by",
                 "pexels", "pixabay", "unsplash"):
        ok, reason = licensing.evaluate(_asset(good))
        assert ok, f"{good} was refused: {reason}"


def test_noncommercial_is_refused_because_the_product_sells_its_output():
    for bad in ("cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd"):
        ok, reason = licensing.evaluate(_asset(bad))
        assert not ok
        assert "commercial" in reason


def test_noderivatives_is_refused_because_everything_here_is_a_derivative():
    ok, reason = licensing.evaluate(_asset("cc-by-nd"))
    assert not ok
    assert "crop" in reason


def test_sharealike_is_refused_rather_than_infecting_the_finished_video():
    ok, reason = licensing.evaluate(_asset("cc-by-sa"))
    assert not ok
    assert "share-alike" in reason


def test_public_access_is_not_reuse():
    """The whole point: reachable does not mean usable."""
    ok, reason = licensing.evaluate(_asset(""))
    assert not ok
    assert "no established reuse basis" in reason
    ok, _ = licensing.evaluate(_asset("rights-reserved"))
    assert not ok


def test_an_attribution_licence_without_a_creator_is_refused():
    ok, reason = licensing.evaluate(_asset("cc-by", creator=""))
    assert not ok
    assert "attribution" in reason


def test_a_kept_asset_carries_its_credit_line_onward():
    asset = _asset("cc-by", title="Bernabeu at night",
                   original_url="https://commons.wikimedia.org/wiki/File:X")
    kept, refused = licensing.filter_usable([asset])
    assert kept and not refused
    credit = kept[0].attribution
    assert "A Photographer" in credit
    assert "CC BY" in credit
    assert "commons.wikimedia.org" in credit
    assert kept[0].commercial_use_allowed is True
    assert kept[0].license_url.startswith("https://creativecommons.org/")


def test_a_public_domain_asset_needs_no_creator():
    ok, _ = licensing.evaluate(_asset("cc0", creator=""))
    assert ok


# ── routing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("intent,subject", [
    ("A wide aerial drone shot of a coral reef", "ocean"),
    ("The Santiago Bernabeu stadium on a match night", "sports"),
    ("A Real Madrid striker celebrating a goal", "sports"),
    ("A satellite view of the Great Barrier Reef from orbit", "space"),
    ("A hurricane seen from above", "weather"),
    ("A Renaissance oil painting of a harbour", "art"),
    ("Archival footage from the 1920s", "history"),
    ("Someone drinking coffee at a kitchen table", "generic"),
])
def test_the_subject_of_a_beat_is_recognised(intent, subject):
    assert router.classify(intent) == subject


def test_a_named_subject_goes_to_the_archives_before_stock():
    """Stock has a model in a generic kit; Commons has the actual person."""
    order = router.route_for("A portrait of Diego Maradona")
    assert order[0] in {"commons", "openverse"}
    assert order.index("commons") < order.index("pexels")


def test_football_routes_through_commons_first_then_falls_back_to_stock():
    order = router.route_for("Real Madrid football stadium")
    assert order[0] == "commons"
    # The stock floor is still there, so a thin archive result is survivable.
    assert "pexels" in order and "pixabay" in order


def test_space_routes_to_nasa_first():
    assert router.route_for("satellite view of Earth")[0] == "nasa"


def test_ocean_routes_to_noaa_first():
    assert router.route_for("coral reef bleaching underwater")[0] == "noaa"


def test_history_prefers_the_archives():
    order = router.route_for("Photographs from the Great Depression")
    assert order[0] == "loc"
    assert order.index("archive") < order.index("pexels")


def test_every_route_ends_at_the_general_libraries():
    """A specialist lane finding nothing must not leave a beat uncovered."""
    for subject in router.SUBJECTS:
        order = router.route_for("x", available=None)
        assert "pexels" in order and "pixabay" in order


@pytest.mark.parametrize("query,core", [
    ("Great Barrier Reef satellite view", "Great Barrier Reef"),
    ("aerial drone shot of a coral reef", "coral reef"),
    ("Real Madrid football stadium", ""),      # nothing to strip; no retry
    ("close up macro photograph", ""),         # nothing meaningful survives
])
def test_a_query_can_be_reduced_to_its_subject_for_a_retry(query, core):
    assert router.core_query(query) == core


@pytest.mark.asyncio
async def test_a_source_that_finds_nothing_is_asked_once_more_more_plainly(
    stub_providers
):
    """NASA held twelve satellite images of the reef and returned none of them.

    Its index AND-matches every word, and nobody titles a NASA image
    "satellite view" because all of them are.
    """
    class _Picky(_Stub):
        async def search(self, client, query, media_type, context):
            self.calls += 1
            if query != "Great Barrier Reef":
                return []
            return [_stub_asset("nasa", "reef", "usgov")]

    picky = _Picky("nasa")
    stub_providers(nasa=picky)
    result = await search("Great Barrier Reef satellite view", providers=["nasa"])
    assert [a.asset_id for a in result.assets] == ["reef"]
    assert picky.calls == 2, "the retry should happen exactly once"


@pytest.mark.asyncio
async def test_a_source_that_answered_is_not_asked_twice(stub_providers):
    happy = _Stub("commons", [_stub_asset("commons", "a")])
    stub_providers(commons=happy)
    await search("Great Barrier Reef satellite view", providers=["commons"])
    assert happy.calls == 1


def test_routing_only_returns_sources_this_deployment_has():
    order = router.route_for("Real Madrid stadium", available={"pexels", "commons"})
    assert order == ["commons", "pexels"]


# ── the aggregator ───────────────────────────────────────────────────

class _Stub(MediaProvider):
    """A provider that returns, fails or hangs, on command."""

    media_types = (MediaType.IMAGE,)

    def __init__(self, name, assets=None, error=None, available=True):
        self.name = name
        self._assets = assets or []
        self._error = error
        self._available = available
        self.calls = 0

    def status(self):
        return ProviderStatus(self.name, self._available)

    async def search(self, client, query, media_type, context):
        self.calls += 1
        if self._error:
            raise self._error
        return list(self._assets)


@pytest.fixture
def stub_providers(monkeypatch):
    def install(**providers):
        monkeypatch.setattr(
            "medialab.media.aggregator.PROVIDERS", providers, raising=False
        )
        return providers
    return install


def _stub_asset(provider, asset_id, license_id="cc0", url=None):
    return MediaAsset(
        provider=provider, asset_id=asset_id, media_type=MediaType.IMAGE,
        download_url=url or f"https://{provider}.test/{asset_id}.jpg",
        original_url=f"https://{provider}.test/page/{asset_id}",
        creator="Someone", license=license_id, width=1600, height=900,
    )


@pytest.mark.asyncio
async def test_results_from_several_sources_are_merged(stub_providers):
    stub_providers(
        commons=_Stub("commons", [_stub_asset("commons", "a")]),
        pexels=_Stub("pexels", [_stub_asset("pexels", "b", "pexels")]),
    )
    result = await search("anything", providers=["commons", "pexels"])
    assert {a.provider for a in result.assets} == {"commons", "pexels"}


@pytest.mark.asyncio
async def test_one_failing_source_does_not_fail_the_search(stub_providers):
    """The rule the whole product rests on, applied to twelve sources."""
    stub_providers(
        commons=_Stub("commons", error=httpx.ConnectError("down")),
        pexels=_Stub("pexels", [_stub_asset("pexels", "b", "pexels")]),
    )
    result = await search("anything", providers=["commons", "pexels"])
    assert [a.provider for a in result.assets] == ["pexels"]
    assert "commons" in result.failed


@pytest.mark.asyncio
async def test_a_timeout_is_retried_once_then_skipped(stub_providers):
    slow = _Stub("commons", error=httpx.ReadTimeout("slow"))
    stub_providers(commons=slow, pexels=_Stub("pexels", [_stub_asset("pexels", "b", "pexels")]))
    result = await search("anything", providers=["commons", "pexels"])
    assert slow.calls == 2, "a timeout should be retried exactly once"
    assert len(result.assets) == 1


@pytest.mark.asyncio
async def test_a_quota_response_is_skipped_not_raised(stub_providers):
    response = httpx.Response(429, request=httpx.Request("GET", "https://x.test"))
    stub_providers(
        openverse=_Stub("openverse", error=httpx.HTTPStatusError(
            "rate limited", request=response.request, response=response)),
        pexels=_Stub("pexels", [_stub_asset("pexels", "b", "pexels")]),
    )
    result = await search("anything", providers=["openverse", "pexels"])
    assert len(result.assets) == 1


@pytest.mark.asyncio
async def test_an_unconfigured_source_is_skipped_silently(stub_providers):
    stub_providers(
        europeana=_Stub("europeana", [_stub_asset("europeana", "z")], available=False),
        pexels=_Stub("pexels", [_stub_asset("pexels", "b", "pexels")]),
    )
    result = await search("anything", providers=["europeana", "pexels"])
    assert [a.provider for a in result.assets] == ["pexels"]


@pytest.mark.asyncio
async def test_unlicensed_results_never_reach_the_caller(stub_providers):
    stub_providers(commons=_Stub("commons", [
        _stub_asset("commons", "ok", "cc0"),
        _stub_asset("commons", "nc", "cc-by-nc"),
        _stub_asset("commons", "unknown", ""),
        _stub_asset("commons", "reserved", "rights-reserved"),
    ]))
    result = await search("anything", providers=["commons"])
    assert [a.asset_id for a in result.assets] == ["ok"]
    assert len(result.refused) == 3
    assert all(reason for _, reason in result.refused)


@pytest.mark.asyncio
async def test_the_same_asset_found_twice_is_kept_once(stub_providers):
    shared = "https://cdn.test/same.jpg"
    stub_providers(
        commons=_Stub("commons", [_stub_asset("commons", "a", url=shared)]),
        openverse=_Stub("openverse", [_stub_asset("openverse", "b", url=shared)]),
    )
    result = await search("anything", providers=["commons", "openverse"])
    assert len(result.assets) == 1


@pytest.mark.asyncio
async def test_the_first_lane_in_the_route_leads_the_results(stub_providers):
    """Ordering is the router's opinion about which source suits the subject."""
    stub_providers(
        pexels=_Stub("pexels", [_stub_asset("pexels", "b", "pexels")]),
        commons=_Stub("commons", [_stub_asset("commons", "a")]),
    )
    result = await search("anything", providers=["commons", "pexels"])
    assert result.assets[0].provider == "commons"


@pytest.mark.asyncio
async def test_a_search_with_no_configured_source_returns_empty_not_an_error(
    stub_providers
):
    stub_providers(europeana=_Stub("europeana", available=False))
    result = await search("anything", providers=["europeana"])
    assert result.assets == []


@pytest.mark.asyncio
async def test_every_provider_is_capped_so_one_cannot_crowd_out_the_rest(
    stub_providers
):
    many = [_stub_asset("commons", str(i)) for i in range(30)]
    stub_providers(commons=_Stub("commons", many))
    result = await search(
        "anything", providers=["commons"], context=SearchContext(per_provider=5)
    )
    assert len(result.assets) == 5


# ── the real registry ────────────────────────────────────────────────

def test_every_named_source_is_registered():
    expected = {
        "pexels", "pixabay", "unsplash",
        "openverse", "commons", "archive", "loc", "smithsonian",
        "europeana", "met", "nasa", "noaa",
    }
    assert expected <= set(PROVIDERS)


def test_every_provider_reports_whether_it_is_usable():
    for status in provider_status():
        assert status.name
        if not status.available:
            assert status.detail, f"{status.name} gives no reason and no fix"


def test_a_source_needing_a_key_names_the_variable():
    for status in provider_status():
        if not status.available:
            assert status.env_var, f"{status.name} does not say which key to set"
            assert status.env_var in status.detail


def test_keyless_sources_work_without_configuration():
    """These must never be the reason a deployment cannot make a video."""
    keyless = {"commons", "archive", "loc", "met", "nasa", "noaa", "openverse"}
    unavailable = {s.name for s in provider_status() if not s.available}
    assert not (keyless & unavailable)
