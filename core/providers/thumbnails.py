"""Thumbnail artwork, edited from a real photograph.

Thumbnails are the one image in the package a person chooses the video by, and
they are a different job from scene visuals. A scene still has to be *true* --
the actual place, the actual person, sourced or clearly marked as generated. A
thumbnail has to be true *and* arresting, which means taking the strongest real
photograph the run already found and composing it, rather than generating a
picture of a person from a description.

That is why this is an edit model rather than a generator. Given a real
photograph of a footballer, `fal-ai/gemini-25-flash-image/edit` relights it,
crops it and builds a composition around it while the face stays the face it
was. A text-to-image model asked for "Emiliano Martinez" invents someone who
resembles him, which on a thumbnail is the most visible possible failure.

Kept deliberately separate from `core/providers/media.py`: nothing here is
reachable from scene sourcing, and nothing there decides what a thumbnail
looks like.
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


def _data_uri(path: Path) -> str:
    """A local image as a data URI.

    The API takes image *URLs*. Uploading to fal's own storage first would be
    a second network round trip and would leave the run's source photographs
    sitting on a third party's CDN; a data URI keeps the image in the one
    request that needs it.
    """
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


class FalGeminiFlashEditProvider:
    """`fal-ai/gemini-25-flash-image/edit` — the only thumbnail generator.

    Raises rather than returning a placeholder: a thumbnail that silently
    failed is a video shipped with the wrong cover, and the caller's retry and
    review gate can only act on an error it is told about.
    """

    name = "fal_gemini_flash_edit"
    MODEL = "fal-ai/gemini-25-flash-image/edit"
    URL = "https://fal.run/fal-ai/gemini-25-flash-image/edit"
    #: Generous: an edit over a large source photograph is not fast, and a
    #: timeout here costs the whole thumbnail stage rather than one beat.
    TIMEOUT = 180.0

    def __init__(self, api_key: str | None = None) -> None:
        self._key = (
            api_key if api_key is not None else credential("fal_key", "fal_api_key")
        )
        self._limiter = RateLimiter(concurrency=1, min_interval=0.4)

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS, "Set FAL_KEY.",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def edit(
        self,
        prompt: str,
        source_images: list[Path],
        *,
        client: httpx.AsyncClient,
        aspect_ratio: str = "16:9",
    ) -> bytes:
        """Image bytes for one thumbnail, composed from `source_images`."""
        if not self.status().usable:
            raise RuntimeError(self.status().reason)
        if not prompt.strip():
            raise ValueError("a thumbnail edit needs a prompt")
        usable = [p for p in source_images if p and Path(p).exists()]
        if not usable:
            raise ValueError(
                "fal-ai/gemini-25-flash-image/edit is an edit model and needs at "
                "least one source image"
            )

        payload = {
            "prompt": prompt,
            "image_urls": [_data_uri(Path(p)) for p in usable],
            "num_images": 1,
            "output_format": "png",
            "aspect_ratio": aspect_ratio,
        }
        headers = {
            "Authorization": f"Key {self._key}",
            "Content-Type": "application/json",
        }

        async def call() -> bytes:
            response = await client.post(self.URL, headers=headers, json=payload)
            if response.status_code >= 400:
                # `raise_for_status` reports the status and nothing else, which
                # for a 422 is unactionable -- the reason is only ever in the
                # body, and without it a validation error and a safety refusal
                # look identical.
                raise RuntimeError(
                    f"fal returned {response.status_code}: "
                    f"{response.text[:400]}"
                )
            body = response.json()
            images = body.get("images") or []
            if not images:
                raise RuntimeError(
                    f"fal returned no image "
                    f"({str(body.get('description') or body)[:200]})"
                )
            url = str(images[0].get("url") or "")
            if not url:
                raise RuntimeError("fal returned an image with no url")
            # An inline data URI comes back decoded rather than re-fetched.
            if url.startswith("data:"):
                return base64.b64decode(url.split(",", 1)[1])
            fetched = await client.get(url)
            fetched.raise_for_status()
            return fetched.content

        async with self._limiter:
            return await with_retries(call, provider=self.name)
