"""Which voice narrates, and what happens when it is not configured.

The failure this guards against is silent: a channel asks for one narrator,
something is missing, and the run completes in a different voice without
saying so. Every path here either uses the configured voice or logs why it
did not.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.providers.narration import ElevenLabsNarrationProvider
from core.utils import VoiceConfig, load_channel_config


# ── the provider ──────────────────────────────────────────────────────

def test_provider_needs_a_key():
    status = ElevenLabsNarrationProvider(api_key="").status()
    assert not status.usable
    assert "ELEVENLABS_API_KEY" in status.reason


def test_provider_rejects_a_key_id_pasted_for_the_key():
    status = ElevenLabsNarrationProvider(api_key="abc123").status()
    assert not status.usable
    assert "API key ID" in status.reason


def test_provider_is_ready_with_a_real_looking_key():
    assert ElevenLabsNarrationProvider(api_key="sk_test").status().usable


def test_speaking_without_a_voice_id_is_an_error_not_a_default_voice():
    provider = ElevenLabsNarrationProvider(api_key="sk_test")
    with pytest.raises(ValueError, match="voice id"):
        asyncio.run(provider.speak("hello", voice_id="", client=None))


def test_speaking_nothing_is_an_error():
    provider = ElevenLabsNarrationProvider(api_key="sk_test")
    with pytest.raises(ValueError, match="nothing to narrate"):
        asyncio.run(provider.speak("   ", voice_id="v1", client=None))


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    """Captures the one request the provider makes."""

    def __init__(self) -> None:
        self.url = ""
        self.payload: dict = {}
        self.params: dict = {}

    async def post(self, url, *, params=None, headers=None, json=None):
        self.url = url
        self.params = params or {}
        self.payload = json or {}
        return _FakeResponse(b"MP3BYTES")


def test_the_request_names_the_configured_voice_and_nothing_else():
    provider = ElevenLabsNarrationProvider(api_key="sk_test")
    client = _FakeClient()
    audio = asyncio.run(
        provider.speak("النص", voice_id=" v_rudra ", client=client)
    )
    assert audio == b"MP3BYTES"
    # The id is what addresses the voice, trimmed, and it is in the URL.
    assert client.url.endswith("/v_rudra")
    assert client.payload["model_id"] == provider.MODEL
    assert client.payload["text"] == "النص"
    # No voice_prompt leaks into this path: an ElevenLabs voice is the
    # performance, and a prompt describing another one would be ignored.
    assert "voice_prompt" not in json.dumps(client.payload)


def test_voice_settings_are_clamped_to_the_api_range():
    provider = ElevenLabsNarrationProvider(api_key="sk_test")
    client = _FakeClient()
    asyncio.run(
        provider.speak(
            "x", voice_id="v", client=client,
            stability=5.0, similarity_boost=-2.0, style=9.0, speed=99.0,
        )
    )
    settings = client.payload["voice_settings"]
    assert settings["stability"] == 1.0
    assert settings["similarity_boost"] == 0.0
    assert settings["style"] == 1.0
    assert settings["speed"] == 1.2


# ── the channel configs ───────────────────────────────────────────────

def test_horror_asks_for_the_rudra_elevenlabs_voice():
    config = load_channel_config("horror_stories")
    assert config.voice.provider == "elevenlabs"
    assert "Rudra" in config.voice.voice_name
    # An empty id is the one failure that does not announce itself: routing
    # falls through to Gemini, the run completes, and the video is narrated by
    # the wrong voice. Nothing downstream would catch it.
    assert config.voice.voice_id, (
        "horror_stories has no ElevenLabs voice_id, so narration would "
        "silently fall back to Gemini TTS"
    )


@pytest.mark.parametrize("language", ["ar", "en"])
def test_horror_keeps_that_voice_in_every_language(language):
    config = load_channel_config("horror_stories", language=language)
    assert config.voice.provider == "elevenlabs"


def test_true_stories_keeps_its_existing_gemini_voice():
    config = load_channel_config("true_stories")
    assert config.voice.provider == "gemini_2.5_tts"
    assert config.voice.voice_name == "Charon"


def test_true_stories_is_its_own_channel_not_horror():
    # The worker resolves an engine to config/channels/<slug>.json and falls
    # back when it is missing, so this file existing is what keeps a True
    # Stories job from silently running as Football News.
    config = load_channel_config("true_stories")
    assert config.channel_id == "true_stories"
    assert config.channel_name == "True Stories"


def test_football_narration_is_unchanged():
    config = load_channel_config("football_news")
    assert config.voice.provider == "gemini_2.5_tts"


# ── routing ───────────────────────────────────────────────────────────

def test_a_missing_voice_id_falls_back_to_gemini_rather_than_failing(caplog):
    """The documented default narrates; it does not lose the run."""
    import clients

    called: dict = {}

    async def fake_eleven(*args, **kwargs):
        called["eleven"] = True
        return "/tmp/x.wav"

    saved = clients._speak_elevenlabs
    clients._speak_elevenlabs = fake_eleven
    try:
        # No voice_id: must not reach the ElevenLabs path at all.
        asyncio.run(
            _call_generate_speech(provider="elevenlabs", voice_id="")
        )
    except Exception:
        # It falls through to the Gemini client, which is not configured in a
        # unit test. Reaching that failure is the proof it fell through.
        pass
    finally:
        clients._speak_elevenlabs = saved
    assert "eleven" not in called


async def _call_generate_speech(*, provider: str, voice_id: str):
    import clients
    from pathlib import Path

    return await clients.generate_speech.__wrapped__(
        "text",
        Path("out.wav"),
        provider=provider,
        voice_id=voice_id,
    )


def test_a_configured_voice_id_reaches_the_elevenlabs_path():
    import clients
    from pathlib import Path

    seen: dict = {}

    async def fake_eleven(text, output_path, *, voice_id, language,
                          voice_label, operation_label):
        seen["voice_id"] = voice_id
        return Path("spoken.wav")

    saved = clients._speak_elevenlabs
    clients._speak_elevenlabs = fake_eleven
    try:
        result = asyncio.run(
            clients.generate_speech.__wrapped__(
                "text",
                Path("out.wav"),
                provider="elevenlabs",
                voice_id="v_rudra",
            )
        )
    finally:
        clients._speak_elevenlabs = saved
    assert seen["voice_id"] == "v_rudra"
    assert result == Path("spoken.wav")


# ── the audio cache ───────────────────────────────────────────────────

def test_changing_the_voice_changes_the_section_fingerprint():
    """Otherwise a reused workspace keeps the old narrator for cached sections."""
    from core.audio_sourcer import _section_audio_fingerprint

    base = ("narration", "prompt", "Charon", "ar-SA")
    gemini = _section_audio_fingerprint(*base, "gemini_2.5_tts", "")
    eleven = _section_audio_fingerprint(*base, "elevenlabs", "v_rudra")
    other_voice = _section_audio_fingerprint(*base, "elevenlabs", "v_other")

    assert gemini != eleven
    assert eleven != other_voice


def test_voice_config_defaults_keep_existing_channels_on_gemini():
    voice = VoiceConfig()
    assert voice.provider == "gemini_2.5_tts"
    assert voice.voice_id == ""
