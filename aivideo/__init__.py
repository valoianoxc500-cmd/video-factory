"""AI Video Maker — topic to finished short-form video.

A general faceless-video maker, deliberately independent of the channel
pipeline in `core/`. Nothing here imports from `core/`, and nothing in `core/`
imports from here, so a failure in one product cannot take the other down.

Engine shape and several stage designs are adapted from MoneyPrinterTurbo
(https://github.com/harry0703/MoneyPrinterTurbo), MIT licensed. See NOTICE.md
for what was taken and what was rewritten.
"""

from aivideo.spec import (  # noqa: F401
    CAPTION_PRESETS,
    DURATIONS,
    VOICES,
    CaptionStyle,
    VideoSpec,
    normalise_spec,
    voices_for,
)

__all__ = [
    "CAPTION_PRESETS", "DURATIONS", "VOICES",
    "CaptionStyle", "VideoSpec", "normalise_spec", "voices_for",
]
