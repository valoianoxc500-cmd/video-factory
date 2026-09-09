"""Generated scene visuals, for the beats real photography cannot fill.

This is the last tier of image sourcing, not the first. Pexels, Pixabay,
Wikimedia, Unsplash, Google Images and the open-library catalogues are all
tried before anything here runs, and a generated frame is only ever asked for
when those returned nothing relevant or the reviewer rejected what they
returned. A story about a real place should show that place.

Separate from `core/providers/thumbnails.py` on purpose. A thumbnail is one
composed cover image edited from a real photograph; this generates a scene
still from a description. They use different models, they answer to different
review gates, and nothing routes between them.

Sizing is not cosmetic. The pipeline renders 1080x1920 and refuses a source
below 1080x1280, so fal's `portrait_16_9` preset -- 576x1024 -- would be
rejected by the validator that already exists. Frames are therefore requested
at explicit dimensions matching the render target, rounded to the multiple of
16 the model requires.
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

#: FLUX works in multiples of this; a request off the grid is silently
#: rounded by the server, which then fails the pipeline's exact-size checks.
_GRID = 16


def _round_to_grid(value: int) -> int:
    return max(_GRID, int(round(value / _GRID)) * _GRID)


def dimensions_for(aspect_ratio: str, target: tuple[int, int]) -> tuple[int, int]:
    """Width and height to request for this channel's render target.

    Driven by the target rather than by a preset: the presets are all smaller
    than the minimum source size the pipeline enforces, so every preset frame
    would arrive and then be rejected for being too small.
    """
    width, height = int(target[0] or 0), int(target[1] or 0)
    if width <= 0 or height <= 0:
        # No usable target. Fall back to the aspect the caller asked for at a
        # size comfortably above any minimum.
        return (1088, 1920) if aspect_ratio == "9:16" else (1920, 1088)
    return _round_to_grid(width), _round_to_grid(height)


def megapixels(width: int, height: int) -> int:
    """Billed megapixels: fal rounds up to the next whole one."""
    pixels = max(0, int(width)) * max(0, int(height))
    if pixels <= 0:
        return 0
    return -(-pixels // 1_000_000)  # ceiling division


class FluxSchnellProvider:
    """`fal-ai/flux/schnell` — generated scene stills.

    Raises rather than returning a placeholder. The caller treats a failure as
    "this beat is still unsourced" and moves on to its next tier, which is the
    behaviour that keeps an irrelevant frame off the screen.
    """

    name = "fal_flux_schnell"
    MODEL = "fal-ai/flux/schnell"
    URL = "https://fal.run/fal-ai/flux/schnell"
    TIMEOUT = 120.0

    #: Shared across instances for the same reason as the narration provider:
    #: the caller builds one provider per beat and runs beats concurrently, so
    #: a per-instance limiter would cap nothing and a burst would hit fal's
    #: account concurrency limit mid-run.
    _limiter = RateLimiter(concurrency=2, min_interval=0.25)

    def __init__(self, api_key: str | None = None) -> None:
        self._key = (
            api_key if api_key is not None else credential("fal_key", "fal_api_key")
        )

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS, "Set FAL_KEY.",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def generate(
        self,
        prompt: str,
        *,
        client: httpx.AsyncClient,
        width: int,
        height: int,
        steps: int = 4,
    ) -> bytes:
        """PNG bytes for one scene still."""
        if not self.status().usable:
            raise RuntimeError(self.status().reason)
        if not prompt.strip():
            raise ValueError("a generated visual needs a prompt")

        payload = {
            "prompt": prompt,
            "image_size": {"width": int(width), "height": int(height)},
            "num_images": 1,
            "num_inference_steps": int(steps),
            # PNG so the frame survives the pipeline's own re-encoding without
            # a second generation of JPEG artefacts.
            "output_format": "png",
            # Left on. A refused frame is reported and the beat stays
            # unsourced, which is the correct outcome for this pipeline.
            "enable_safety_checker": True,
        }
        headers = {
            "Authorization": f"Key {self._key}",
            "Content-Type": "application/json",
        }

        async def call() -> bytes:
            response = await client.post(self.URL, headers=headers, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"fal returned {response.status_code}: {response.text[:300]}"
                )
            body = response.json()
            if any(body.get("has_nsfw_concepts") or []):
                raise RuntimeError("fal safety checker rejected the generated frame")
            images = body.get("images") or []
            if not images:
                raise RuntimeError("fal returned no image")
            url = str(images[0].get("url") or "")
            if not url:
                raise RuntimeError("fal returned an image with no url")
            if url.startswith("data:"):
                import base64

                return base64.b64decode(url.split(",", 1)[1])
            fetched = await client.get(url)
            fetched.raise_for_status()
            return fetched.content

        async with self._limiter:
            return await with_retries(call, provider=self.name)
