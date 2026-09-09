"""Image-to-video, for the Animated Stories path only.

A third kind of visual, kept apart from the other two on purpose. Scene stills
come from `core/providers/scene_images.py` and thumbnails from
`core/providers/thumbnails.py`; neither of those can reach this module and this
module cannot reach them. Football, Horror and True Stories never animate.

The model is configuration, not a constant. Image-to-video pricing moves fast
and the cheap flat-rate models change every few months, so the channel names
the endpoint and this provider only knows how to call one:

    "animation": {"model": "fal-ai/wan/v2.2-5b/image-to-video", ...}

What it will not do is invent motion. The prompt describes what the character
does -- walks, turns, reaches, recoils -- and explicitly forbids the failure
modes that make an animated still look broken: identity drift, wardrobe
changes, morphing, teleporting and camera moves nobody asked for.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import httpx

from core.providers.base import (
    Availability,
    ProviderStatus,
    RateLimiter,
    credential,
    with_retries,
)

#: Appended to every animation prompt. These are the observed failure modes of
#: image-to-video on character art, and naming them is what keeps a stick
#: figure the same stick figure for the length of a clip.
MOTION_GUARDRAILS = (
    "Animate only what is already in the frame. Keep the character's face, "
    "head shape, body proportions, clothing, colours and accessories exactly "
    "as drawn -- no identity change, no wardrobe change, no morphing, no "
    "teleporting, no duplicate limbs. Do not move the camera: no pan, no zoom, "
    "no dolly, no orbit. No lip sync and no talking mouth animation. The "
    "background stays the same place throughout."
)


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


class FalImageToVideoProvider:
    """One still plus a motion description in, one short clip out.

    Raises rather than returning a placeholder. A scene that could not be
    animated must stay visibly incomplete so the caller can retry it or fall
    back to holding the still -- never be marked done with no file.
    """

    name = "fal_image_to_video"
    #: Cheapest flat-rate 5s model on fal at the time of writing. Overridden
    #: per channel; nothing in this class assumes a particular endpoint.
    DEFAULT_MODEL = "fal-ai/wan/v2.2-5b/image-to-video"
    TIMEOUT = 600.0

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = str(model or self.DEFAULT_MODEL)
        self._key = (
            api_key if api_key is not None else credential("fal_key", "fal_api_key")
        )
        # Shared across instances: the caller builds one provider per scene and
        # runs scenes concurrently, so a per-instance limiter would cap
        # nothing. Two at a time -- these are long calls and a burst is how an
        # account's concurrency limit gets hit mid-run.
        self._limiter = _LIMITER

    @property
    def url(self) -> str:
        return f"https://fal.run/{self.model}"

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS, "Set FAL_KEY.",
            )
        if not self.model:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "No image-to-video model configured for this channel.",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def animate(
        self,
        image: Path,
        motion: str,
        *,
        client: httpx.AsyncClient,
        seconds: float = 5.0,
        resolution: str = "720p",
    ) -> bytes:
        """MP4 bytes for one scene."""
        if not self.status().usable:
            raise RuntimeError(self.status().reason)
        if not Path(image).exists():
            raise ValueError(f"scene image not found: {image}")
        if not str(motion or "").strip():
            raise ValueError("an animated scene needs a motion description")

        payload = {
            "prompt": f"{motion.strip()} {MOTION_GUARDRAILS}",
            "image_url": _data_uri(Path(image)),
            "resolution": resolution,
            "duration": max(1, int(round(seconds))),
            "enable_safety_checker": True,
        }
        headers = {
            "Authorization": f"Key {self._key}",
            "Content-Type": "application/json",
        }

        async def call() -> bytes:
            response = await client.post(self.url, headers=headers, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"fal returned {response.status_code}: {response.text[:300]}"
                )
            body = response.json()
            video = body.get("video") or {}
            url = str(video.get("url") or "")
            if not url:
                # Some endpoints return a list; accept either shape rather than
                # failing a paid generation on a envelope difference.
                videos = body.get("videos") or []
                url = str((videos[0] or {}).get("url") or "") if videos else ""
            if not url:
                raise RuntimeError(
                    f"fal returned no video ({str(body)[:200]})"
                )
            if url.startswith("data:"):
                return base64.b64decode(url.split(",", 1)[1])
            fetched = await client.get(url, timeout=self.TIMEOUT)
            fetched.raise_for_status()
            return fetched.content

        async with self._limiter:
            return await with_retries(call, provider=self.name)


_LIMITER = RateLimiter(concurrency=2, min_interval=0.5)
