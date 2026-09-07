"""Experimental discovery mode: the gate, the licence filter, the denylist."""

import httpx
import pytest

import core.footage_discovery as disc
import core.footage_stage as stage
from core.footage import MatchMoment


# --- the off switch --------------------------------------------------------

def test_mode_is_off_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv(disc.MODE_ENV, raising=False)
    assert disc.is_enabled() is False


@pytest.mark.parametrize("value", ["", "off", "0", "true", "on", "Experimental "])
def test_only_the_exact_value_enables_the_mode(monkeypatch, value):
    monkeypatch.setenv(disc.MODE_ENV, value)
    # " Experimental " is stripped and lowercased, so it does enable; every
    # other spelling must not.
    expected = value.strip().lower() == "experimental"
    assert disc.is_enabled() is expected


def test_experimental_enables_the_mode(monkeypatch):
    monkeypatch.setenv(disc.MODE_ENV, "experimental")
    assert disc.is_enabled() is True


def test_discovery_is_inert_while_the_mode_is_off(monkeypatch):
    monkeypatch.delenv(disc.MODE_ENV, raising=False)
    result = disc.discover_for_query("anything", __import__("pathlib").Path("."))
    assert result.clips == []
    assert "off" in result.reason


def test_stage_does_not_discover_while_the_mode_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv(disc.MODE_ENV, raising=False)
    called = {"n": 0}
    monkeypatch.setattr(
        disc, "discover_for_query",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    out = stage._discover_supplementary_clips(
        workspace=tmp_path, moments=[MatchMoment("goal", 5, "x")]
    )
    assert out == []
    assert called["n"] == 0


# --- licence filter --------------------------------------------------------

@pytest.mark.parametrize(
    "licence",
    ["CC0", "CC BY 4.0", "CC BY-SA 3.0", "cc-by-sa", "Public Domain", "PDM"],
)
def test_reusable_licences_are_accepted(licence):
    assert disc.is_reusable_licence(licence) is True


@pytest.mark.parametrize(
    "licence",
    [
        "",
        "All rights reserved",
        "CC BY-NC 4.0",       # non-commercial
        "CC BY-ND 4.0",       # no derivatives
        "CC BY-NC-SA 4.0",
        "Fair use",
        "Copyrighted free use with attribution required",
        "unknown",
    ],
)
def test_non_reusable_licences_are_refused(licence):
    assert disc.is_reusable_licence(licence) is False


# --- download allowlist ----------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://upload.wikimedia.org/wikipedia/commons/a/ab/Goal.webm",
        "https://videos.pexels.com/video-files/1/x.mp4",
    ],
)
def test_allowlisted_hosts_are_downloadable(url):
    assert disc.is_downloadable_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc.mp4",
        "https://x.com/i/status/1/video.mp4",
        "https://cdn.skysports.com/goal.mp4",
        "https://beinsports.com/goal.mp4",
        "https://evil.example.com/upload.wikimedia.org/goal.mp4",
        "http://upload.wikimedia.org/goal.mp4",       # not https
        "https://upload.wikimedia.org/page.html",      # not a video
        "",
    ],
)
def test_everything_else_is_not_downloadable(url):
    assert disc.is_downloadable_url(url) is False


def test_broadcast_and_social_hosts_remain_refused_by_the_source_policy():
    """Enabling the mode must not weaken core.footage's deny-by-default rule."""
    from core.footage import is_allowed_source

    for url in [
        "https://www.youtube.com/watch?v=abc",
        "https://x.com/status/1",
        "https://skysports.com/clip.mp4",
        "https://upload.wikimedia.org/wikipedia/commons/a/ab/Goal.webm",
    ]:
        assert is_allowed_source(url) is False


# --- download refusals -----------------------------------------------------

def test_download_refuses_a_non_reusable_licence(tmp_path):
    clip = disc.DiscoveredClip(
        url="https://upload.wikimedia.org/wikipedia/commons/a/ab/G.webm",
        platform="wikimedia_commons",
        licence="CC BY-NC 4.0",
    )
    with httpx.Client() as client:
        assert disc.download_clip(clip, tmp_path, client=client) is None


def test_download_refuses_a_non_allowlisted_host(tmp_path):
    clip = disc.DiscoveredClip(
        url="https://cdn.example.com/goal.mp4",
        platform="somewhere",
        licence="CC0",
    )
    with httpx.Client() as client:
        assert disc.download_clip(clip, tmp_path, client=client) is None


# --- search filtering ------------------------------------------------------

def _commons_response(pages):
    def handler(request):
        return httpx.Response(200, json={"query": {"pages": pages}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_keeps_only_reusable_allowlisted_results():
    pages = {
        "1": {
            "title": "File:Free goal.webm",
            "imageinfo": [{
                "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Free.webm",
                "duration": 12.0,
                "extmetadata": {
                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                    "Artist": {"value": "<a href='#'>Someone</a>"},
                },
            }],
        },
        "2": {
            "title": "File:Restricted.webm",
            "imageinfo": [{
                "url": "https://upload.wikimedia.org/wikipedia/commons/c/cd/R.webm",
                "extmetadata": {"LicenseShortName": {"value": "CC BY-NC 4.0"}},
            }],
        },
        "3": {
            "title": "File:Offsite.webm",
            "imageinfo": [{
                "url": "https://cdn.example.com/offsite.webm",
                "extmetadata": {"LicenseShortName": {"value": "CC0"}},
            }],
        },
    }
    with _commons_response(pages) as client:
        result = disc.search_wikimedia_commons("goal", client=client)

    assert [c.title for c in result.clips] == ["File:Free goal.webm"]
    assert result.clips[0].platform == "wikimedia_commons"
    assert result.clips[0].licence == "CC BY-SA 4.0"
    assert result.clips[0].attribution == "Someone"      # markup stripped
    assert len(result.rejected) == 2


def test_search_failure_returns_empty_rather_than_raising():
    def boom(request):
        raise httpx.ConnectError("no network")

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        result = disc.search_wikimedia_commons("goal", client=client)
    assert result.clips == []
    assert "failed" in result.reason


def test_every_clip_records_its_source_and_platform():
    clip = disc.DiscoveredClip(
        url="https://upload.wikimedia.org/x.webm",
        platform="wikimedia_commons",
        licence="CC0",
        title="t",
        attribution="a",
    )
    record = clip.as_record()
    assert record["url"].startswith("https://")
    assert record["platform"] == "wikimedia_commons"
    assert record["licence"] == "CC0"
    assert record["attribution"] == "a"
