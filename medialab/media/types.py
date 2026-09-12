"""One shape for a media result, whatever found it.

Twelve providers answer in twelve schemas. Pexels calls it `src.large2x`,
Commons calls it `imageinfo[0].url`, the Met calls it `primaryImage`, and the
Internet Archive makes you assemble the URL yourself from an identifier and a
filename. Normalising once, here, is what lets the router, the licence filter,
the deduplicator and the ranker all be written once instead of twelve times.

The licence fields are not decoration. An asset that reaches the renderer
carries its source, creator, original page and licence with it, because that
is the record that has to exist if anyone ever asks why a frame was used.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass(frozen=True)
class SearchContext:
    """What the caller knows about the beat being illustrated.

    `subject` drives which providers are asked and in what order; `portrait`
    and `commercial` are constraints every provider and the licence filter
    have to respect.
    """

    subject: str = "generic"
    portrait: bool = True
    #: The product sells the output, so an asset that is free only for
    #: non-commercial use cannot be used. Set False only for internal work.
    commercial: bool = True
    #: The narration this beat carries, for providers that can use it.
    says: str = ""
    #: Hard cap per provider, so one rich source cannot crowd out the rest.
    per_provider: int = 8


@dataclass
class MediaAsset:
    """One candidate, normalised."""

    provider: str
    asset_id: str
    media_type: MediaType
    preview_url: str = ""
    original_url: str = ""          # the human-readable page it came from
    download_url: str = ""          # the bytes
    width: int = 0
    height: int = 0
    duration: float = 0.0           # seconds; 0 for stills
    creator: str = ""
    license: str = ""               # normalised id, e.g. "cc-by", "cc0"
    license_url: str = ""
    attribution: str = ""           # the credit line to keep with the video
    commercial_use_allowed: bool = False
    search_query: str = ""
    title: str = ""
    #: Anything provider-specific worth keeping for provenance.
    extra: dict = field(default_factory=dict)

    @property
    def is_video(self) -> bool:
        return self.media_type is MediaType.VIDEO

    @property
    def key(self) -> str:
        """Identity for exact-duplicate removal across providers."""
        return f"{self.provider}:{self.asset_id}"

    def to_record(self) -> dict:
        data = asdict(self)
        data["media_type"] = self.media_type.value
        return data


@dataclass(frozen=True)
class ProviderStatus:
    """Whether a source can actually be used on this deployment."""

    name: str
    available: bool
    detail: str = ""
    #: The env var that would enable it, when one is needed.
    env_var: str = ""

    def __str__(self) -> str:
        state = "available" if self.available else "not configured"
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.name}: {state}{suffix}"
