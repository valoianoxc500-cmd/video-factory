"""The open-library tier: three more catalogues before a beat is invented.

A photo-lane beat that Pexels cannot fill falls through to a generated image.
That is the right last resort and the wrong second one, so Pixabay, Unsplash
and Commons are asked in between. These check that it engages only where a
channel asked for it, that it prefers a real photograph over a generated one,
and that each catalogue's own licence survives into the provenance record.
"""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest
from PIL import Image

from core import image_sourcer
from core.providers import MediaKind
from core.providers.base import MediaItem


def _jpg(size=(1080, 1920), colour=(90, 90, 120)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def _item(provider: str, licence: str, **over) -> MediaItem:
    params = dict(
        provider=provider,
        provider_id=f"{provider}-1",
        kind=MediaKind.PHOTO,
        url=f"https://cdn.{provider}/photo.jpg",
        source_page=f"https://{provider}/page",
        licence=licence,
        attribution="A. Author",
        width=1080,
        height=1920,
    )
    params.update(over)
    return MediaItem(**params)


@pytest.fixture
def stub_search(monkeypatch):
    """Stand in for the network, and record what was asked for."""
    calls: list[dict] = []

    def install(items):
        async def fake_search_media(query, **kwargs):
            calls.append({"query": query, "providers": kwargs.get("providers")})
            from core.providers import MediaSearch

            return MediaSearch(items=list(items))

        monkeypatch.setattr("core.providers.search_media", fake_search_media)
        return calls

    return install


@pytest.fixture
def stub_download(monkeypatch):
    async def fake_download(client, url, target_size, output_name):
        # A distinct image per URL so the de-duplication is exercised.
        #
        # md5 rather than hash(): str hashing is salted per process, so two
        # URLs could collide into the same colour, produce byte-identical
        # JPEGs, and be de-duplicated -- which made this test fail only on
        # some random orderings.
        digest = hashlib.md5(url.encode()).digest()
        return _jpg(colour=(digest[0], digest[1], digest[2]))

    monkeypatch.setattr(image_sourcer, "_download_valid_image_bytes", fake_download)


@pytest.fixture
def stub_selection(monkeypatch):
    """Pick the first candidate, as a passing review would."""
    async def fake_select(*, candidate_paths, **kwargs):
        return candidate_paths[0] if candidate_paths else None

    monkeypatch.setattr(image_sourcer, "_select_photo_candidate", fake_select)


def _search(tmp_path, keywords="abandoned lighthouse at dusk"):
    return asyncio.run(image_sourcer._search_open_libraries(
        keywords,
        "",
        tmp_path / "section_001_01.jpg",
        client=None,
        seen_hashes=set(),
        target_size=(1080, 1920),
    ))


# --- the tier itself --------------------------------------------------------

def test_a_photograph_from_another_catalogue_is_used(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    stub_search([_item("pixabay", "Pixabay Content License")])
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate",
        lambda **kwargs: True)

    assert _search(tmp_path) is True


def test_nothing_found_is_a_clean_no(
    tmp_path, stub_search, stub_download, stub_selection
):
    stub_search([])
    assert _search(tmp_path) is False


def test_pexels_is_not_asked_again(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    """It has already run by the time this tier is reached."""
    calls = stub_search([_item("pixabay", "Pixabay Content License")])
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate", lambda **kwargs: True)

    _search(tmp_path)

    asked = {p.name for call in calls for p in (call["providers"] or [])}
    assert "pexels" not in asked
    assert asked == {"pixabay", "unsplash", "wikimedia_commons"}


def test_only_the_specific_rungs_of_the_ladder_are_searched(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    """The same ladder the Pexels path climbs, but only its top two rungs.

    The lower rungs are the vaguest phrasings. Against a stock catalogue they
    return generic imagery the relevance review rejects anyway, so they spend
    requests -- and in a real run that spend was what tripped Commons' robot
    policy and exhausted Unsplash's hourly allowance.
    """
    calls = stub_search([])
    _search(tmp_path, keywords="a lone parachute over dense pine forest")

    ladder = image_sourcer.pexels_query_ladder(
        "a lone parachute over dense pine forest")
    assert [call["query"] for call in calls] == ladder[:2]
    assert len(calls) <= 2


def test_the_search_stops_once_there_are_enough_candidates(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    plenty = [
        _item("pixabay", "Pixabay Content License",
              provider_id=str(i), url=f"https://cdn.pixabay/{i}.jpg")
        for i in range(image_sourcer._PEXELS_CANDIDATE_COUNT)
    ]
    calls = stub_search(plenty)
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate", lambda **kwargs: True)

    _search(tmp_path)

    assert len(calls) == 1, "the whole ladder ran despite the first rung filling it"


def test_a_run_has_a_fixed_allowance_for_this_tier(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    """Uncapped, one run fired ~60 Commons searches and all 50 of Unsplash's
    hourly demo requests, which is what got both providers to 403."""
    stub_search([_item("pixabay", "Pixabay Content License")])
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate", lambda **kwargs: True)

    image_sourcer._reset_open_library_budget()
    budget = image_sourcer._OPEN_LIBRARY_BUDGET

    results = [_search(tmp_path, keywords=f"beat {i}") for i in range(budget + 3)]

    assert results[:budget] == [True] * budget
    assert results[budget:] == [False] * 3, "the tier ran past its allowance"


def test_the_allowance_resets_between_runs(tmp_path):
    image_sourcer._reset_open_library_budget()
    image_sourcer._OPEN_LIBRARY_CALLS = image_sourcer._OPEN_LIBRARY_BUDGET
    image_sourcer._reset_provenance()
    assert image_sourcer._OPEN_LIBRARY_CALLS == 0


def test_an_unvetted_candidate_is_never_shipped(
    tmp_path, stub_search, stub_download, monkeypatch
):
    """When the relevance review is unavailable this tier drops the beat.

    Its candidates come from three catalogues merged by provider preference,
    not by relevance, so the top one is not a best match. Shipping it unvetted
    is how a train corridor reached a hospital beat.
    """
    stub_search([_item("pixabay", "Pixabay Content License")])

    async def unavailable(**kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", unavailable)
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate",
        lambda **kwargs: True)

    image_sourcer._reset_open_library_budget()
    assert _search(tmp_path) is False


def test_the_pexels_path_still_keeps_its_top_result(tmp_path, monkeypatch):
    """Its list really is relevance-ranked, so that fallback stays."""
    import asyncio

    async def unavailable(**kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", unavailable)
    first = tmp_path / "a.jpg"
    first.write_bytes(_jpg())

    winner = asyncio.run(image_sourcer._select_photo_candidate(
        source_name="Pexels", keywords="k", prompt="",
        candidate_paths=[first], operation_label="pexels_candidate_selection",
    ))
    assert winner == first


# --- licensing survives the tier -------------------------------------------

def test_each_catalogues_own_licence_reaches_the_provenance_record(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    stub_search([
        _item("pixabay", "Pixabay Content License",
              url="https://cdn.pixabay/1.jpg"),
        _item("unsplash", "Unsplash License (credit required by API guidelines)",
              url="https://cdn.unsplash/2.jpg"),
        _item("wikimedia_commons", "CC BY-SA 4.0",
              url="https://upload.wikimedia.org/3.jpg"),
    ])
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate", lambda **kwargs: True)

    image_sourcer._reset_provenance()
    _search(tmp_path)

    recorded = list(image_sourcer._CANDIDATE_PROVENANCE.values())
    assert {r["licence"] for r in recorded} == {
        "Pixabay Content License",
        "Unsplash License (credit required by API guidelines)",
        "CC BY-SA 4.0",
    }
    for record in recorded:
        assert record["source_page"], "a candidate lost its source page"
        assert record["attribution"], "a candidate lost its author"


def test_a_commons_licence_that_forbids_reuse_never_gets_this_far():
    """The provider filters it; this is the second half of that guarantee."""
    from core.footage_discovery import is_reusable_licence

    assert is_reusable_licence("CC BY-NC 4.0") is False
    assert image_sourcer._CANDIDATE_PROVENANCE.get("cc-by-nc") is None


def test_unsplash_use_is_reported_when_its_photo_ships(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    """Required by Unsplash's API guidelines whenever a photo is used."""
    stub_search([_item(
        "unsplash", "Unsplash License (credit required by API guidelines)",
        use_hook="https://api.unsplash.com/photos/x/download")])
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate", lambda **kwargs: True)

    reported: list[str] = []

    async def fake_report_use(self, item, *, client):
        reported.append(item.provider_id)
        return True

    monkeypatch.setattr(
        "core.providers.media.UnsplashProvider.report_use", fake_report_use)
    monkeypatch.setattr(
        "core.providers.media.UnsplashProvider.status",
        lambda self: __import__("core.providers.base", fromlist=["ProviderStatus"])
        .ProviderStatus("unsplash", __import__(
            "core.providers.base", fromlist=["Availability"]).Availability.READY))

    _search(tmp_path)
    assert reported == ["unsplash-1"]


def test_no_use_is_reported_when_the_photo_does_not_ship(
    tmp_path, stub_search, stub_download, stub_selection, monkeypatch
):
    stub_search([_item(
        "unsplash", "Unsplash License",
        use_hook="https://api.unsplash.com/photos/x/download")])
    # The finaliser rejects it, so nothing was used.
    monkeypatch.setattr(
        image_sourcer, "_finalize_selected_photo_candidate", lambda **kwargs: False)

    async def fake_report_use(self, item, *, client):  # pragma: no cover
        raise AssertionError("reported a use for a photo that never shipped")

    monkeypatch.setattr(
        "core.providers.media.UnsplashProvider.report_use", fake_report_use)

    assert _search(tmp_path) is False


# --- the tier is opt-in -----------------------------------------------------

def test_the_fallback_is_off_by_default():
    """No channel gains three upstreams, or a network call, by upgrading."""
    from core.utils import ImageSourcingConfig

    assert ImageSourcingConfig().open_library_fallback is False


def test_the_real_channels_opt_in():
    from core.utils import load_channel_config

    for slug in ("horror_stories", "football_news"):
        config = load_channel_config(slug)
        assert config.image_sourcing.open_library_fallback is True, slug


def test_the_pexels_path_is_still_the_first_choice():
    """This tier runs after the configured source, never instead of it."""
    import inspect

    source = inspect.getsource(image_sourcer._source_single_image)
    assert source.index("_search_pexels(") < source.index("_search_open_libraries(")
