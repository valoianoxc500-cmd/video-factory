"""Freesound and ElevenLabs sound effects.

The licence tests matter most. Freesound reports a licence *URL* rather than a
name, and several of the licences it hosts cannot be used in a monetisable
video, so a mapping slip is the difference between a usable sound bed and a
licence breach.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from core.footage_discovery import is_reusable_licence
from core.providers.audio import (
    ElevenLabsSfxProvider,
    FreesoundProvider,
    licence_name_from_url,
)
from core.providers.base import Availability, MediaKind


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


def _hit(sound_id: int, licence: str, **over) -> dict:
    hit = {
        "id": sound_id,
        "name": f"sound {sound_id}",
        "license": licence,
        "username": "A. Recordist",
        "url": f"https://freesound.org/s/{sound_id}/",
        "duration": 12.5,
        "previews": {
            "preview-hq-mp3": f"https://cdn.freesound.org/{sound_id}-hq.mp3",
            "preview-lq-mp3": f"https://cdn.freesound.org/{sound_id}-lq.mp3",
        },
    }
    hit.update(over)
    return hit


# --- licence URLs ----------------------------------------------------------

@pytest.mark.parametrize("url,name", [
    ("http://creativecommons.org/publicdomain/zero/1.0/", "CC0"),
    ("https://creativecommons.org/publicdomain/mark/1.0/", "Public Domain"),
    ("https://creativecommons.org/licenses/by/4.0/", "CC BY"),
    ("https://creativecommons.org/licenses/by-sa/4.0/", "CC BY-SA"),
    ("https://creativecommons.org/licenses/by-nc/4.0/", "CC BY-NC"),
    ("https://creativecommons.org/licenses/by-nc-sa/3.0/", "CC BY-NC-SA"),
    ("https://creativecommons.org/licenses/by-nd/4.0/", "CC BY-ND"),
])
def test_freesound_licence_urls_map_to_names(url, name):
    assert licence_name_from_url(url) == name


def test_the_narrower_licence_is_matched_first():
    """by-nc-sa contains 'by-sa' and 'by'; matching loosely would pass a
    non-commercial sound off as usable."""
    assert licence_name_from_url(
        "https://creativecommons.org/licenses/by-nc-sa/3.0/") == "CC BY-NC-SA"
    assert is_reusable_licence("CC BY-NC-SA") is False


def test_a_name_is_left_alone():
    assert licence_name_from_url("CC BY 4.0") == "CC BY 4.0"
    assert licence_name_from_url("") == ""


@pytest.mark.parametrize("url,usable", [
    ("http://creativecommons.org/publicdomain/zero/1.0/", True),
    ("https://creativecommons.org/licenses/by/4.0/", True),
    ("https://creativecommons.org/licenses/by-sa/4.0/", True),
    ("https://creativecommons.org/licenses/by-nc/4.0/", False),
    ("https://creativecommons.org/licenses/by-nd/4.0/", False),
    ("https://creativecommons.org/licenses/by-nc-nd/4.0/", False),
])
def test_the_same_reuse_gate_guards_sound_as_guards_footage(url, usable):
    assert is_reusable_licence(licence_name_from_url(url)) is usable


# --- Freesound -------------------------------------------------------------

def test_usable_sounds_are_normalised():
    body = {"results": [_hit(1, "http://creativecommons.org/publicdomain/zero/1.0/")]}
    items = _run(FreesoundProvider(api_key="k").search(
        "wind", client=_client(lambda r: httpx.Response(200, json=body))))

    item = items[0]
    assert item.provider == "freesound"
    assert item.kind is MediaKind.AUDIO
    assert item.url.endswith("1-hq.mp3")
    assert item.source_page == "https://freesound.org/s/1/"
    assert item.licence == "CC0"
    assert item.attribution == "A. Recordist"
    assert item.duration_seconds == 12.5


def test_a_non_commercial_sound_is_dropped_even_if_the_filter_let_it_through():
    """The API filter is a request, not a guarantee."""
    body = {"results": [
        _hit(1, "https://creativecommons.org/licenses/by-nc/4.0/"),
        _hit(2, "http://creativecommons.org/publicdomain/zero/1.0/"),
    ]}
    items = _run(FreesoundProvider(api_key="k").search(
        "wind", client=_client(lambda r: httpx.Response(200, json=body))))

    assert [item.provider_id for item in items] == ["2"]


def test_cc_by_sound_carries_a_credit_obligation():
    body = {"results": [_hit(1, "https://creativecommons.org/licenses/by/4.0/")]}
    item = _run(FreesoundProvider(api_key="k").search(
        "wind", client=_client(lambda r: httpx.Response(200, json=body))))[0]

    assert item.needs_attribution is True
    assert "A. Recordist" in item.credit_line()


def test_a_cc0_sound_needs_no_credit_but_records_the_recordist():
    body = {"results": [_hit(1, "http://creativecommons.org/publicdomain/zero/1.0/")]}
    item = _run(FreesoundProvider(api_key="k").search(
        "wind", client=_client(lambda r: httpx.Response(200, json=body))))[0]

    assert item.needs_attribution is False
    assert item.credit_line() == ""
    assert item.attribution == "A. Recordist"


def test_a_sound_with_no_preview_is_skipped():
    """Original downloads need OAuth2, which this deployment does not hold."""
    body = {"results": [_hit(1, "CC0", previews={})]}
    assert _run(FreesoundProvider(api_key="k").search(
        "wind", client=_client(lambda r: httpx.Response(200, json=body)))) == []


def test_the_token_is_sent_as_a_header_not_in_the_url():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"results": []})

    _run(FreesoundProvider(api_key="sekret").search("wind", client=_client(handler)))
    assert seen["auth"] == "Token sekret"
    assert "sekret" not in seen["url"]


def test_the_licence_filter_is_sent():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["filter"] = request.url.params.get("filter", "")
        return httpx.Response(200, json={"results": []})

    _run(FreesoundProvider(api_key="k").search("wind", client=_client(handler)))
    assert "Creative Commons 0" in seen["filter"]
    assert "Attribution" in seen["filter"]


def test_a_duration_window_is_passed_through():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["filter"] = request.url.params.get("filter", "")
        return httpx.Response(200, json={"results": []})

    _run(FreesoundProvider(api_key="k").search(
        "wind", client=_client(handler), min_duration=5, max_duration=30))
    assert "duration:[5.0 TO 30.0]" in seen["filter"]


def test_no_key_means_no_call():
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("called Freesound with no credentials")

    provider = FreesoundProvider(api_key="")
    assert provider.status().availability is Availability.NEEDS_CREDENTIALS
    assert "FREESOUND_API_KEY" in provider.status().reason
    assert _run(provider.search("wind", client=_client(handler))) == []


# --- ElevenLabs sound effects ----------------------------------------------

def test_a_key_id_pasted_instead_of_the_key_is_diagnosed():
    """The dashboard shows an id beside each key and it is easy to copy that;
    the resulting error does not say so."""
    status = ElevenLabsSfxProvider(api_key="ecca9b0d1234abcd").status()
    assert status.availability is Availability.NEEDS_CREDENTIALS
    assert "API key ID" in status.reason
    assert "sk_" in status.reason


def test_a_real_looking_key_is_accepted():
    assert ElevenLabsSfxProvider(
        api_key="sk_abc123").status().availability is Availability.READY


def test_no_key_at_all_says_which_variable_to_set():
    status = ElevenLabsSfxProvider(api_key="").status()
    assert status.availability is Availability.NEEDS_CREDENTIALS
    assert "ELEVENLABS_API_KEY" in status.reason


def test_a_cue_is_generated_with_the_documented_payload():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3fake-mp3-bytes")

    audio = _run(ElevenLabsSfxProvider(api_key="sk_k").generate(
        "a chair scraping back on a stone floor",
        client=_client(handler), duration_seconds=3.0))

    assert audio == b"ID3fake-mp3-bytes"
    assert seen["key"] == "sk_k"
    assert seen["body"]["model_id"] == "eleven_text_to_sound_v2"
    assert seen["body"]["duration_seconds"] == 3.0
    assert "mp3_44100_128" in seen["url"]


def test_a_looping_bed_is_requested_as_a_loop():
    """An ambience bed that does not end where it starts seams audibly."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"mp3")

    _run(ElevenLabsSfxProvider(api_key="sk_k").generate(
        "rain on a tin roof", client=_client(handler), loop=True))
    assert seen["body"]["loop"] is True


def test_the_duration_is_clamped_to_the_apis_range():
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content)["duration_seconds"])
        return httpx.Response(200, content=b"mp3")

    provider = ElevenLabsSfxProvider(api_key="sk_k")
    _run(provider.generate("x", client=_client(handler), duration_seconds=0.1))
    _run(provider.generate("x", client=_client(handler), duration_seconds=120))
    assert seen == [provider.MIN_SECONDS, provider.MAX_SECONDS]


def test_an_unusable_key_refuses_before_spending_a_credit():
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("called ElevenLabs with an unusable key")

    with pytest.raises(RuntimeError, match="API key ID"):
        _run(ElevenLabsSfxProvider(api_key="not-a-real-key").generate(
            "x", client=_client(handler)))


def test_an_empty_description_is_refused():
    with pytest.raises(ValueError):
        _run(ElevenLabsSfxProvider(api_key="sk_k").generate(
            "   ", client=_client(lambda r: httpx.Response(200, content=b""))))


def test_generated_audio_is_recorded_as_generated():
    """Nothing downstream should be able to present it as a recording."""
    record = ElevenLabsSfxProvider(api_key="sk_k").describe(
        "a door creak", loop=False)
    assert record["platform"] == "elevenlabs_sfx"
    assert "generated" in record["licence"]
    assert record["source_page"] == ""
    assert record["attribution"] == ""
    assert record["prompt"] == "a door creak"


def test_narration_is_not_part_of_this_provider():
    """Voice stays on Gemini TTS; this is sound effects only."""
    provider = ElevenLabsSfxProvider(api_key="sk_k")
    assert not hasattr(provider, "text_to_speech")
    assert "sound-generation" in provider.URL
    assert "text-to-speech" not in provider.URL
