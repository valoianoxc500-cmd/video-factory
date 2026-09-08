"""The unified provider layer: normalisation, licensing, retries, fan-out.

The licensing tests are the load-bearing ones. Four media sources with four
different licences is exactly the situation where an attribution obligation
gets lost, and a video that credits nobody because the record was normalised
away is a licence breach rather than a cosmetic bug.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from core.providers import (
    Availability,
    CommonsProvider,
    MediaKind,
    PexelsProvider,
    PixabayProvider,
    UnsplashProvider,
    availability_report,
    default_media_providers,
    default_research_providers,
    search_media,
    search_research,
)
from core.providers.base import (
    MediaItem,
    RateLimiter,
    backoff_delay,
    is_retryable,
    requires_attribution,
    retry_after_seconds,
    with_retries,
)
from core.providers.research import NewsApiProvider, YouTubeResearchProvider


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


# --- normalisation ---------------------------------------------------------

PEXELS_BODY = {"photos": [{
    "id": 101, "width": 2000, "height": 3000,
    "url": "https://www.pexels.com/photo/101/",
    "photographer": "A. Photographer",
    "alt": "a lighthouse",
    "src": {"large2x": "https://images.pexels.com/101-large.jpg",
            "medium": "https://images.pexels.com/101-med.jpg"},
}]}

PIXABAY_BODY = {"hits": [{
    "id": 202, "imageWidth": 1600, "imageHeight": 2400,
    "pageURL": "https://pixabay.com/photos/lighthouse-202/",
    "largeImageURL": "https://cdn.pixabay.com/202_1280.jpg",
    "previewURL": "https://cdn.pixabay.com/202_150.jpg",
    "user": "B. Contributor", "tags": "lighthouse, dusk",
}]}

UNSPLASH_BODY = {"results": [{
    "id": "abc123", "width": 3000, "height": 4000,
    "alt_description": "lighthouse at dusk",
    "urls": {"full": "https://images.unsplash.com/abc123",
             "small": "https://images.unsplash.com/abc123-small"},
    "links": {"html": "https://unsplash.com/photos/abc123",
              "download_location": "https://api.unsplash.com/photos/abc123/download"},
    "user": {"name": "C. Shooter",
             "links": {"html": "https://unsplash.com/@cshooter"}},
}]}

COMMONS_BODY = {"query": {"pages": {"1": {
    "pageid": 303, "title": "File:Lighthouse.jpg",
    "imageinfo": [{
        "url": "https://upload.wikimedia.org/lighthouse.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Lighthouse.jpg",
        "width": 1200, "height": 1800,
        "extmetadata": {
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "Artist": {"value": "<a href='#'>D. Uploader</a>"},
        },
    }],
}}}}


def test_pexels_normalises_into_the_shared_shape():
    items = _run(PexelsProvider(api_key="k").search(
        "lighthouse", client=_client(lambda r: httpx.Response(200, json=PEXELS_BODY)),
    ))
    item = items[0]
    assert item.provider == "pexels"
    assert item.provider_id == "101"
    assert item.url.endswith("101-large.jpg")
    assert item.source_page == "https://www.pexels.com/photo/101/"
    assert item.attribution == "A. Photographer"
    assert (item.width, item.height) == (2000, 3000)
    assert item.is_portrait is True


def test_a_long_brief_is_trimmed_for_pixabay():
    """Pixabay 400s on a query over 100 characters, and the briefs this is
    called with are whole sentences."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["q"] = request.url.params.get("q", "")
        return httpx.Response(200, json=PIXABAY_BODY)

    brief = (
        "Flashlight beams cutting through a dark dusty and derelict room "
        "inside an old building illuminating dust particles in the air"
    )
    _run(PixabayProvider(api_key="k").search(brief, client=_client(handler)))

    assert len(seen["q"]) <= 100
    assert not seen["q"].endswith(" ")
    # Trimmed on a word boundary, not mid-word.
    assert brief.startswith(seen["q"])


def test_pixabay_normalises_into_the_same_shape():
    items = _run(PixabayProvider(api_key="k").search(
        "lighthouse", client=_client(lambda r: httpx.Response(200, json=PIXABAY_BODY)),
    ))
    item = items[0]
    assert item.provider == "pixabay"
    assert item.url.endswith("202_1280.jpg")
    assert item.source_page.startswith("https://pixabay.com/photos/")
    assert item.attribution == "B. Contributor"


def test_commons_normalises_into_the_same_shape():
    items = _run(CommonsProvider().search(
        "lighthouse", client=_client(lambda r: httpx.Response(200, json=COMMONS_BODY)),
    ))
    item = items[0]
    assert item.provider == "wikimedia_commons"
    assert item.licence == "CC BY-SA 4.0"
    # HTML stripped out of the Commons artist field.
    assert item.attribution == "D. Uploader"


def test_the_normalised_item_matches_the_provenance_record_the_pipeline_writes():
    """`core.image_sourcer` writes exactly these keys per asset."""
    items = _run(PexelsProvider(api_key="k").search(
        "x", client=_client(lambda r: httpx.Response(200, json=PEXELS_BODY))))
    assert set(items[0].to_provenance()) == {
        "platform", "url", "source_page", "licence", "attribution",
        "width", "height",
    }


# --- licensing -------------------------------------------------------------

@pytest.mark.parametrize("licence,needed", [
    ("Pexels License (free to use, no attribution required)", False),
    ("Pixabay Content License (free to use, no attribution required)", False),
    ("CC0", False),
    ("Public Domain", False),
    ("CC BY 4.0", True),
    ("CC BY-SA 4.0", True),
    ("Unsplash License (credit required by API guidelines)", True),
])
def test_attribution_is_decided_by_the_licence(licence, needed):
    assert requires_attribution(licence) is needed


def test_an_unknown_licence_is_treated_as_the_strictest_case():
    """Guessing 'probably fine' is how an unattributed image ships."""
    assert requires_attribution("") is True
    assert requires_attribution("some bespoke terms") is True


def test_pexels_and_pixabay_need_no_credit_but_still_record_the_author():
    for provider, body in ((PexelsProvider(api_key="k"), PEXELS_BODY),
                           (PixabayProvider(api_key="k"), PIXABAY_BODY)):
        item = _run(provider.search(
            "x", client=_client(lambda r: httpx.Response(200, json=body))))[0]
        assert item.needs_attribution is False
        assert item.credit_line() == ""
        assert item.attribution, "the author should be recorded regardless"


def test_unsplash_credits_the_photographer_and_unsplash():
    item = _run(UnsplashProvider(access_key="k", app_name="myapp").search(
        "x", client=_client(lambda r: httpx.Response(200, json=UNSPLASH_BODY))))[0]
    assert item.needs_attribution is True
    credit = item.credit_line()
    assert "C. Shooter" in credit
    assert "Unsplash" in credit


def test_unsplash_links_carry_this_applications_utm_parameters():
    """Required by their API guidelines, not optional."""
    item = _run(UnsplashProvider(access_key="k", app_name="myapp").search(
        "x", client=_client(lambda r: httpx.Response(200, json=UNSPLASH_BODY))))[0]
    assert "utm_source=myapp" in item.source_page
    assert "utm_medium=referral" in item.source_page
    assert "utm_source=myapp" in item.attribution


def test_commons_credits_the_uploader_under_cc_by():
    item = _run(CommonsProvider().search(
        "x", client=_client(lambda r: httpx.Response(200, json=COMMONS_BODY))))[0]
    assert item.needs_attribution is True
    assert "D. Uploader" in item.credit_line()
    assert "Wikimedia Commons" in item.credit_line()


def test_commons_rejects_a_licence_that_does_not_permit_reuse():
    body = {"query": {"pages": {"1": {
        "pageid": 1, "title": "File:X.jpg",
        "imageinfo": [{
            "url": "https://upload.wikimedia.org/x.jpg",
            "extmetadata": {"LicenseShortName": {"value": "CC BY-NC 4.0"}},
        }],
    }}}}
    items = _run(CommonsProvider().search(
        "x", client=_client(lambda r: httpx.Response(200, json=body))))
    assert items == [], "non-commercial material must not reach the pipeline"


def test_commons_uses_the_same_reuse_gate_as_the_footage_path():
    from core.footage_discovery import is_reusable_licence

    assert is_reusable_licence("CC BY-SA 4.0") is True
    assert is_reusable_licence("CC BY-ND 4.0") is False


# --- Unsplash's download obligation ----------------------------------------

def test_unsplash_pings_the_download_endpoint_when_a_photo_is_used():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"url": "https://images.unsplash.com/abc123"})

    provider = UnsplashProvider(access_key="k")
    item = MediaItem(
        provider="unsplash", provider_id="abc123", kind=MediaKind.PHOTO,
        url="https://images.unsplash.com/abc123",
        use_hook="https://api.unsplash.com/photos/abc123/download",
    )
    assert _run(provider.report_use(item, client=_client(handler))) is True
    assert seen == ["https://api.unsplash.com/photos/abc123/download"]


def test_a_failed_download_ping_does_not_cost_the_run_its_picture():
    provider = UnsplashProvider(access_key="k")
    item = MediaItem(
        provider="unsplash", provider_id="abc", kind=MediaKind.PHOTO, url="u",
        use_hook="https://api.unsplash.com/photos/abc/download")
    result = _run(provider.report_use(
        item, client=_client(lambda r: httpx.Response(500, json={}))))
    assert result is False


def test_other_providers_have_no_download_obligation():
    provider = UnsplashProvider(access_key="k")
    item = MediaItem(provider="pexels", provider_id="1", kind=MediaKind.PHOTO, url="u")

    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("pinged Unsplash for a non-Unsplash photo")

    assert _run(provider.report_use(item, client=_client(handler))) is False


# --- credentials -----------------------------------------------------------

def test_a_provider_without_a_key_says_which_variable_to_set():
    """An empty key is an explicit "not configured"; None means look it up."""
    for provider, variable in (
        (PixabayProvider(api_key=""), "PIXABAY_API_KEY"),
        (UnsplashProvider(access_key=""), "UNSPLASH_ACCESS_KEY"),
        (NewsApiProvider(api_key=""), "NEWSAPI_API_KEY"),
        (PexelsProvider(api_key=""), "PEXELS_API_KEY"),
        (YouTubeResearchProvider(api_key=""), "YOUTUBE_API_KEY"),
    ):
        status = provider.status()
        assert status.availability is Availability.NEEDS_CREDENTIALS
        assert variable in status.reason


def test_an_unconfigured_provider_returns_nothing_rather_than_calling_out():
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("called an API with no credentials")

    assert _run(PixabayProvider(api_key="").search("x", client=_client(handler))) == []


def test_commons_needs_no_credentials():
    assert CommonsProvider().status().availability is Availability.READY


def test_newsapi_accepts_the_name_already_in_use(monkeypatch):
    """NEWSAPI_KEY is the spelling this deployment's .env already uses."""
    from core.providers.base import credential

    for name in ("NEWSAPI_API_KEY", "NEWS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEWSAPI_KEY", "from-existing-env")
    assert credential("a_name_no_setting_has", "NEWSAPI_KEY") == "from-existing-env"


# --- retries and rate limits ------------------------------------------------

@pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
def test_transient_failures_are_retried(code):
    error = httpx.HTTPStatusError(
        "x", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(code))
    assert is_retryable(error) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_permanent_failures_are_not_retried(code):
    """Repeating a rejected request spends the quota to get the same answer."""
    error = httpx.HTTPStatusError(
        "x", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(code))
    assert is_retryable(error) is False


def test_a_timeout_is_retried():
    assert is_retryable(httpx.ConnectTimeout("slow")) is True


def test_the_providers_own_retry_after_wins_over_our_backoff():
    error = httpx.HTTPStatusError(
        "x", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(429, headers={"retry-after": "12"}))
    assert retry_after_seconds(error) == 12.0


def test_backoff_grows_and_is_capped():
    delays = [backoff_delay(a, jitter=False) for a in range(1, 8)]
    assert delays == sorted(delays)
    assert delays[-1] <= 30.0


def test_a_retried_call_eventually_succeeds():
    attempts = {"n": 0}

    async def call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.HTTPStatusError(
                "busy", request=httpx.Request("GET", "https://x"),
                response=httpx.Response(429))
        return "ok"

    async def no_sleep(_):
        return None

    assert _run(with_retries(call, provider="t", sleep=no_sleep)) == "ok"
    assert attempts["n"] == 3


def test_a_permanent_failure_is_raised_on_the_first_attempt():
    attempts = {"n": 0}

    async def call():
        attempts["n"] += 1
        raise httpx.HTTPStatusError(
            "nope", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(401))

    async def no_sleep(_):
        return None

    with pytest.raises(httpx.HTTPStatusError):
        _run(with_retries(call, provider="t", sleep=no_sleep))
    assert attempts["n"] == 1


def test_the_rate_limiter_caps_concurrency():
    peak = {"now": 0, "max": 0}

    async def main():
        # Built inside the loop: a Semaphore binds to the running loop.
        limiter = RateLimiter(concurrency=2)

        async def task():
            async with limiter:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
                await asyncio.sleep(0.01)
                peak["now"] -= 1

        await asyncio.gather(*[task() for _ in range(6)])

    _run(main())
    assert peak["max"] == 2, "the cap should be reached but not exceeded"


def test_the_rate_limiter_spaces_calls_apart():
    """A burst of parallel queries is how a per-second limit gets tripped."""
    import time

    async def main():
        limiter = RateLimiter(concurrency=4, min_interval=0.02)
        started = time.monotonic()
        for _ in range(4):
            async with limiter:
                pass
        return time.monotonic() - started

    # Three gaps between four calls.
    assert _run(main()) >= 0.05


# --- fan-out ---------------------------------------------------------------

def _multi_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if "pexels" in host:
        return httpx.Response(200, json=PEXELS_BODY)
    if "pixabay" in host:
        return httpx.Response(200, json=PIXABAY_BODY)
    if "unsplash" in host:
        return httpx.Response(200, json=UNSPLASH_BODY)
    if "wikimedia" in host:
        return httpx.Response(200, json=COMMONS_BODY)
    return httpx.Response(404, json={})


def _all_media():
    return [
        PexelsProvider(api_key="k"), PixabayProvider(api_key="k"),
        UnsplashProvider(access_key="k"), CommonsProvider(),
    ]


def test_every_provider_is_asked_and_the_results_are_merged():
    found = _run(search_media(
        "lighthouse", providers=_all_media(), client=_client(_multi_handler)))
    assert {item.provider for item in found.items} == {
        "pexels", "pixabay", "unsplash", "wikimedia_commons"}
    assert len(found.items) == 4


def test_the_providers_run_concurrently():
    """Serial, four providers at 50ms each would take at least 200ms."""
    import time

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return _multi_handler(request)

    started = time.monotonic()
    _run(search_media("x", providers=_all_media(),
                      client=httpx.AsyncClient(transport=httpx.MockTransport(slow))))
    assert time.monotonic() - started < 0.18


def test_one_provider_failing_does_not_cost_the_others():
    # A rejected key rather than a 500: this is about isolation, and a
    # permanent failure gets there without paying the retry backoff. The
    # retry behaviour itself is covered above, with sleeping stubbed out.
    def handler(request: httpx.Request) -> httpx.Response:
        if "pixabay" in request.url.host:
            return httpx.Response(403, json={"error": "bad key"})
        return _multi_handler(request)

    found = _run(search_media("x", providers=_all_media(), client=_client(handler)))
    assert "pixabay" in found.failures
    assert {item.provider for item in found.items} == {
        "pexels", "unsplash", "wikimedia_commons"}


def test_an_unconfigured_provider_is_reported_not_hidden():
    found = _run(search_media(
        "x",
        providers=[PexelsProvider(api_key="k"), PixabayProvider(api_key="")],
        client=_client(_multi_handler),
    ))
    statuses = {r.provider: r.status.availability for r in found.results}
    assert statuses["pixabay"] is Availability.NEEDS_CREDENTIALS


def test_results_are_ranked_by_suitability_not_arrival():
    found = _run(search_media(
        "x", providers=_all_media(), client=_client(_multi_handler)))
    providers = [item.provider for item in found.items]
    assert providers.index("pexels") < providers.index("wikimedia_commons")


def test_the_record_carries_every_licence_and_credit():
    found = _run(search_media(
        "x", providers=_all_media(), client=_client(_multi_handler)))
    for entry in found.to_record()["items"]:
        assert entry["licence"], f"{entry['platform']} lost its licence"
        assert "needs_attribution" in entry
        assert "credit" in entry


def test_a_thin_result_retries_the_sources_that_came_back_empty():
    calls = {"pixabay": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "pixabay" in request.url.host:
            calls["pixabay"] += 1
            # Empty first, then a hit once asked for more.
            return httpx.Response(
                200, json=PIXABAY_BODY if calls["pixabay"] > 1 else {"hits": []})
        if "pexels" in request.url.host:
            return httpx.Response(200, json=PEXELS_BODY)
        return httpx.Response(200, json={"results": [], "query": {"pages": {}}})

    found = _run(search_media(
        "x", providers=_all_media(), client=_client(handler), minimum=2))
    assert calls["pixabay"] == 2
    assert {item.provider for item in found.items} == {"pexels", "pixabay"}


def test_unsplash_is_never_asked_for_video():
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("asked Unsplash for video")

    assert _run(UnsplashProvider(access_key="k").search(
        "x", client=_client(handler), kind=MediaKind.VIDEO)) == []


# --- research fan-out ------------------------------------------------------

YOUTUBE_SEARCH = {"items": [{"id": {"videoId": "vid1"}}]}
YOUTUBE_VIDEOS = {"items": [{
    "id": "vid1",
    "snippet": {"title": "Lighthouse mystery", "channelTitle": "History Now",
                "description": "What happened at the lighthouse.",
                "publishedAt": "2026-09-01T00:00:00Z",
                "thumbnails": {"high": {"url": "https://img/1.jpg"}}},
    "statistics": {"viewCount": "120000", "likeCount": "9000"},
}]}

NEWS_BODY = {"status": "ok", "articles": [{
    "title": "Lighthouse keeper vanished",
    "url": "https://example.news/lighthouse",
    "source": {"name": "The Example"},
    "description": "A short summary from the publisher.",
    "author": "E. Reporter",
    "publishedAt": "2026-09-04T09:00:00Z",
    "urlToImage": "https://example.news/img.jpg",
}]}


def _research_handler(request: httpx.Request) -> httpx.Response:
    if "newsapi" in request.url.host:
        return httpx.Response(200, json=NEWS_BODY)
    if request.url.path.endswith("/search"):
        return httpx.Response(200, json=YOUTUBE_SEARCH)
    return httpx.Response(200, json=YOUTUBE_VIDEOS)


def test_research_providers_are_merged_and_normalised():
    found = _run(search_research(
        "lighthouse",
        providers=[YouTubeResearchProvider(api_key="k"), NewsApiProvider(api_key="k")],
        client=_client(_research_handler),
    ))
    assert {item.provider for item in found.items} == {"youtube", "newsapi"}
    for item in found.items:
        assert item.title and item.url


def test_research_is_marked_reference_material():
    """Neither source offers anything this pipeline may republish."""
    found = _run(search_research(
        "x",
        providers=[YouTubeResearchProvider(api_key="k"), NewsApiProvider(api_key="k")],
        client=_client(_research_handler),
    ))
    for entry in found.to_record()["items"]:
        assert entry["usage"] == "research_only"


def test_a_research_item_carries_no_media_url():
    """A ResearchItem has no field a caller could mistake for a file."""
    from core.providers.base import ResearchItem

    fields = set(ResearchItem("p", "i", "t", "u").to_record())
    assert "url" in fields
    assert not {"media_url", "download_url", "file"} & fields


def test_newsapi_keeps_the_publishers_summary_and_link_only():
    item = _run(NewsApiProvider(api_key="k").search(
        "x", client=_client(_research_handler)))[0]
    assert item.summary == "A short summary from the publisher."
    assert item.url == "https://example.news/lighthouse"
    assert item.source == "The Example"


def test_the_newsapi_key_is_sent_as_a_header_not_in_the_url():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json=NEWS_BODY)

    _run(NewsApiProvider(api_key="sekret").search("x", client=_client(handler)))
    assert seen["key"] == "sekret"
    assert "sekret" not in seen["url"]


def test_a_newsapi_error_in_a_200_body_is_not_read_as_no_coverage():
    body = {"status": "error", "code": "rateLimited", "message": "Too many requests"}
    with pytest.raises(RuntimeError, match="rateLimited|Too many"):
        _run(NewsApiProvider(api_key="k").search(
            "x", client=_client(lambda r: httpx.Response(200, json=body))))


def test_youtube_research_reports_engagement_without_offering_the_file():
    item = _run(YouTubeResearchProvider(api_key="k").search(
        "x", client=_client(_research_handler)))[0]
    assert item.metrics["views"] == 120000
    assert item.url.startswith("https://www.youtube.com/watch?v=")


def test_research_is_newest_first():
    found = _run(search_research(
        "x",
        providers=[YouTubeResearchProvider(api_key="k"), NewsApiProvider(api_key="k")],
        client=_client(_research_handler),
    ))
    assert found.items[0].provider == "newsapi"      # 04 Sep beats 01 Sep


# --- the existing integration ----------------------------------------------

def test_the_pexels_path_in_the_sourcer_is_untouched():
    """The measured sourcing path keeps its own Pexels code; this layer is an
    addition, not a replacement."""
    from core import image_sourcer

    assert hasattr(image_sourcer, "_search_pexels_video")
    assert hasattr(image_sourcer, "_fetch_pexels_photos")
    assert hasattr(image_sourcer, "pexels_query_ladder")


def test_the_defaults_include_every_provider():
    names = {p.name for p in default_media_providers()}
    assert names == {"pexels", "pixabay", "unsplash", "wikimedia_commons"}
    assert {p.name for p in default_research_providers()} == {"youtube", "newsapi"}


def test_the_availability_report_covers_all_six():
    assert len(availability_report()) == 6
