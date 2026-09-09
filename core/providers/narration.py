"""Narration voices from ElevenLabs.

Separate from `ElevenLabsSfxProvider` on purpose. That one generates sound
*effects* from a description and never touches a voice; this one speaks a
script in one specific, named voice and never generates anything else. They
share an account and a key and nothing else, and keeping them apart is what
stops a sound-effect change from quietly altering how a narrator sounds.

Gemini TTS remains the default for every channel. A channel opts in by naming
a provider and a voice id in its `voice` block:

    "voice": {"provider": "elevenlabs", "voice_id": "<id>", "voice_name": "..."}

The voice id is the whole contract. ElevenLabs voice *names* are not unique
and are not stable -- two voices in one account can share a display name, and
renaming one in the dashboard does not change its id. Resolving "Rudra" by
name at request time would mean the narrator could change without anything in
this repository changing, so a channel that asks for ElevenLabs must carry the
id. Where it is missing the caller falls back to Gemini rather than guessing:
a run that narrates in the wrong voice is worse than one that narrates in the
documented default.
"""

from __future__ import annotations

import httpx

from core.providers.base import (
    Availability,
    ProviderStatus,
    RateLimiter,
    credential,
    with_retries,
)


class ElevenLabsNarrationProvider:
    """One script, one named voice, MP3 bytes back.

    Raises rather than returning silence: an empty WAV reaches the renderer as
    a video with a picture and no narration, which is far harder to notice
    than a failed stage.
    """

    name = "elevenlabs_narration"
    URL = "https://api.elevenlabs.io/v1/text-to-speech"
    #: Multilingual so one voice covers both Arabic and English narration.
    MODEL = "eleven_multilingual_v2"
    #: 44.1 kHz matches what the mixer already expects from Freesound cues.
    OUTPUT_FORMAT = "mp3_44100_128"

    #: Shared across every instance, deliberately.
    #:
    #: A per-instance limiter limits nothing here: the caller builds a fresh
    #: provider for each section and then runs the sections concurrently, so
    #: five "concurrency=1" limiters allowed five simultaneous requests and
    #: ElevenLabs refused the run with `concurrent_limit_exceeded` (free plans
    #: allow 2). One limiter on the class is what actually serialises them.
    _limiter = RateLimiter(concurrency=1, min_interval=0.35)

    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key if api_key is not None else credential("elevenlabs_api_key")

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "Set ELEVENLABS_API_KEY.",
            )
        if not self._key.startswith("sk_"):
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "ELEVENLABS_API_KEY looks like an API key ID rather than the "
                "key itself. The key starts with 'sk_'.",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def speak(
        self,
        text: str,
        *,
        voice_id: str,
        client: httpx.AsyncClient,
        stability: float = 0.4,
        similarity_boost: float = 0.75,
        style: float = 0.0,
        speed: float = 1.0,
    ) -> bytes:
        """MP3 bytes of `text` spoken in `voice_id`.

        The tuning defaults suit a documentary narrator: stability low enough
        that delivery varies across a long read rather than flattening, and
        `style` at zero because exaggeration is what makes a calm narrator
        sound like a performance.
        """
        if not self.status().usable:
            raise RuntimeError(self.status().reason)
        if not str(voice_id or "").strip():
            raise ValueError("an ElevenLabs narration request needs a voice id")
        if not text.strip():
            raise ValueError("nothing to narrate")

        payload = {
            "text": text,
            "model_id": self.MODEL,
            "voice_settings": {
                "stability": max(0.0, min(1.0, float(stability))),
                "similarity_boost": max(0.0, min(1.0, float(similarity_boost))),
                "style": max(0.0, min(1.0, float(style))),
                "use_speaker_boost": True,
                "speed": max(0.7, min(1.2, float(speed))),
            },
        }

        async def call():
            response = await client.post(
                f"{self.URL}/{voice_id.strip()}",
                params={"output_format": self.OUTPUT_FORMAT},
                headers={"xi-api-key": self._key, "Content-Type": "application/json"},
                json=payload,
            )
            if response.status_code >= 400:
                # `raise_for_status` reports the status and nothing else. A
                # bare 401 reads as "wrong key" when it is usually "right key,
                # no scopes" -- ElevenLabs names the missing permission, and
                # only the body carries it. A run that dies at narration
                # should say which switch to flip.
                raise RuntimeError(
                    f"ElevenLabs returned {response.status_code}: "
                    f"{response.text[:400]}"
                )
            return response.content

        async with self._limiter:
            return await with_retries(call, provider=self.name)

    def describe(self, voice_id: str, voice_label: str = "") -> dict:
        """Provenance for a narration track, for the run's audio manifest."""
        return {
            "platform": self.name,
            "model": self.MODEL,
            "voice_id": voice_id,
            "voice_label": voice_label,
            "licence": "generated (ElevenLabs text to speech)",
        }
