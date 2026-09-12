"""Narration, and the word timings the captions are built from.

Adapted from MoneyPrinterTurbo's `app/services/voice.py` (MIT). The valuable
idea there is not the synthesis -- it is that Edge TTS streams `WordBoundary`
events alongside the audio, so the exact on-screen time of every word comes
free with the narration. No Whisper pass, no forced aligner, no extra second of
GPU. That is what makes karaoke-style captions cheap enough to be the default.

Edge TTS is free and keyless, which is why it is the default here. The
`synthesize` signature is provider-shaped so ElevenLabs or Azure can be added
as another branch without the pipeline knowing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("aivideo")

#: Edge occasionally drops a connection mid-stream. Cheap to retry, and a
#: failed narration is one of the few things that does end a run.
_ATTEMPTS = 3
_BACKOFF = 2.0


@dataclass
class Word:
    text: str
    start: float      # seconds
    end: float


@dataclass
class Narration:
    audio: Path
    words: list[Word]
    duration: float
    voice: str
    provider: str = "edge"

    @property
    def has_timings(self) -> bool:
        return len(self.words) > 1


class VoiceUnavailable(RuntimeError):
    """No configured TTS provider could produce narration."""


def _rate_arg(rate: float) -> str:
    """Edge wants a signed percentage, not a multiplier."""
    percent = int(round((rate - 1.0) * 100))
    return f"{percent:+d}%"


def _volume_arg(volume: float) -> str:
    percent = int(round((volume - 1.0) * 100))
    return f"{percent:+d}%"


async def _edge_synthesize(
    text: str, voice: str, output: Path, *, rate: float, volume: float
) -> tuple[Path, list[Word]]:
    import edge_tts        # imported lazily so the web/test paths need no TTS

    # `boundary` must be asked for explicitly: edge-tts 7 defaults it to
    # SentenceBoundary, which streams one event for the whole sentence. The
    # WordBoundary events are still there for the asking, and they are the
    # entire reason this pipeline needs no Whisper pass -- without them every
    # caption falls back to being spread evenly across the narration, which
    # drifts further out of sync the longer the video runs.
    communicate = edge_tts.Communicate(
        text, voice,
        rate=_rate_arg(rate), volume=_volume_arg(volume),
        boundary="WordBoundary",
    )
    words: list[Word] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as sink:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                sink.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # Edge reports in 100-nanosecond ticks.
                start = chunk["offset"] / 10_000_000
                length = chunk["duration"] / 10_000_000
                words.append(Word(chunk["text"], start, start + length))

    # Let the TLS transport underneath aiohttp finish closing before the loop
    # can be torn down. Without this the run still succeeds, but Python's
    # Proactor loop on Windows logs a "Fatal error on SSL transport ...
    # Event loop is closed" traceback afterwards -- alarming noise in a worker
    # log for something that already worked.
    await asyncio.sleep(0.25)
    return output, words


def _even_timings(text: str, duration: float) -> list[Word]:
    """Timings when the provider gave none: spread words across the audio.

    Less accurate than real boundaries -- a long word gets the same slice as a
    short one -- but captions that drift slightly are far better than a video
    with no captions, which is what the alternative would be.
    """
    tokens = [w for w in re.split(r"\s+", text.strip()) if w]
    if not tokens or duration <= 0:
        return []
    # Weight by length so "extraordinary" holds longer than "a".
    weights = [max(1, len(t)) for t in tokens]
    total = sum(weights)
    words: list[Word] = []
    cursor = 0.0
    for token, weight in zip(tokens, weights):
        span = duration * weight / total
        words.append(Word(token, cursor, cursor + span))
        cursor += span
    return words


def probe_duration(path: Path) -> float:
    """Audio length in seconds, via ffprobe. 0.0 when it cannot be read."""
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


async def synthesize(
    text: str,
    voice: str,
    output: Path,
    *,
    rate: float = 1.0,
    volume: float = 1.0,
) -> Narration:
    """Narrate `text`, retrying transient provider failures.

    Falls back to even timings if the provider produced audio but no word
    boundaries, so a caption track always exists when narration does.
    """
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            path, words = await _edge_synthesize(
                text, voice, output, rate=rate, volume=volume
            )
            duration = probe_duration(path)
            if not path.exists() or path.stat().st_size < 2_000 or duration <= 0:
                raise RuntimeError("provider returned empty audio")
            if len(words) < 2:
                logger.info(
                    "[aivideo] no word boundaries from the voice provider; "
                    "timing captions evenly across the narration"
                )
                words = _even_timings(text, duration)
            return Narration(
                audio=path, words=words, duration=duration, voice=voice
            )
        except Exception as exc:
            last = exc
            logger.warning(
                f"[aivideo] narration attempt {attempt}/{_ATTEMPTS} failed "
                f"({type(exc).__name__}: {exc})"
            )
            if attempt < _ATTEMPTS:
                await asyncio.sleep(_BACKOFF * attempt)

    raise VoiceUnavailable(f"could not synthesize narration: {last}")
