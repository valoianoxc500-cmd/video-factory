"""Aligning a match clock to an operator's footage timeline.

Match time is not video time. A file may start with a studio build-up, the
broadcast clock pauses for half-time, and stoppage time is not a real minute.
Assuming `video_seconds = minute * 60` puts every clip in the wrong place --
usually minutes away from the moment it claims to show.

So alignment is never guessed. It comes from an explicit sidecar the operator
writes next to their footage:

    match.mp4
    match.align.json    {"kickoff_offset_seconds": 132.0,
                         "second_half_kickoff_seconds": 3180.0}

`kickoff_offset_seconds` is where 0'00" sits in the file. Without it, nothing
is aligned. `second_half_kickoff_seconds` is where 45'00" sits; without it,
only first-half events can be placed, because the half-time break length is
unknowable from the file alone.

Anything that cannot be placed with confidence returns None and the caller
renders that moment from the existing photo pipeline instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("video_factory")

SIDECAR_SUFFIX = ".align.json"

# Stoppage time is reported as a minute past the half boundary (45', 90'), but
# it is played time, not clock time. Events inside stoppage are placed relative
# to the half they belong to, which is accurate to within the added minutes.
FIRST_HALF_END_MINUTE = 45
SECOND_HALF_END_MINUTE = 90


@dataclass
class FootageAlignment:
    """Where the match clock sits inside one footage file."""

    source: Path
    kickoff_offset_seconds: float
    second_half_kickoff_seconds: float | None = None
    duration_seconds: float = 0.0
    note: str = ""

    @property
    def can_align_second_half(self) -> bool:
        return self.second_half_kickoff_seconds is not None


def sidecar_path(footage: Path) -> Path:
    """The alignment file the operator writes for a given clip."""
    return footage.with_suffix(footage.suffix + SIDECAR_SUFFIX)


def load_alignment(footage: Path, *, duration_seconds: float = 0.0) -> FootageAlignment | None:
    """Read the sidecar for `footage`, or None when it is absent or unusable.

    Returning None is the normal case for footage an operator dropped in
    without alignment, and it means "use photos for the moments", not "fail".
    """
    path = sidecar_path(footage)
    if not path.exists():
        # Also accept "match.align.json" beside "match.mp4", which is what
        # people write by hand more often than "match.mp4.align.json".
        alt = footage.with_suffix(SIDECAR_SUFFIX)
        if not alt.exists():
            logger.info(
                f"no alignment sidecar for {footage.name}; its moments will "
                f"use the photo pipeline"
            )
            return None
        path = alt

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"could not read {path.name}: {exc}")
        return None
    if not isinstance(data, dict):
        logger.warning(f"{path.name} is not a JSON object")
        return None

    kickoff = data.get("kickoff_offset_seconds")
    try:
        kickoff = float(kickoff)
    except (TypeError, ValueError):
        logger.warning(
            f"{path.name} has no usable kickoff_offset_seconds; "
            f"{footage.name} cannot be aligned"
        )
        return None
    if kickoff < 0:
        logger.warning(f"{path.name} has a negative kickoff offset; ignoring file")
        return None

    second_half = data.get("second_half_kickoff_seconds")
    try:
        second_half = float(second_half)
    except (TypeError, ValueError):
        second_half = None
    if second_half is not None and second_half <= kickoff:
        logger.warning(
            f"{path.name}: second_half_kickoff_seconds ({second_half}) is not "
            f"after kickoff ({kickoff}); second-half events will use photos"
        )
        second_half = None

    alignment = FootageAlignment(
        source=footage,
        kickoff_offset_seconds=kickoff,
        second_half_kickoff_seconds=second_half,
        duration_seconds=duration_seconds,
        note=str(data.get("note") or ""),
    )
    logger.info(
        f"aligned {footage.name}: kickoff at {kickoff:.0f}s"
        + (
            f", second half at {second_half:.0f}s"
            if second_half is not None
            else ", second half unaligned (first-half moments only)"
        )
    )
    return alignment


def minute_to_video_seconds(
    minute: int,
    alignment: FootageAlignment,
) -> float | None:
    """Video-timeline position of a match minute, or None if not placeable.

    First-half minutes are measured from kickoff. Second-half minutes are
    measured from the second-half kickoff, which is the only way to absorb a
    half-time break of unknown length -- so without that offset, second-half
    moments are refused rather than placed by assumption.
    """
    if minute < 0:
        return None

    if minute <= FIRST_HALF_END_MINUTE:
        seconds = alignment.kickoff_offset_seconds + minute * 60.0
    else:
        if not alignment.can_align_second_half:
            logger.info(
                f"{minute}' is in the second half and {alignment.source.name} "
                f"has no second-half offset; using the photo pipeline"
            )
            return None
        assert alignment.second_half_kickoff_seconds is not None
        seconds = (
            alignment.second_half_kickoff_seconds
            + (minute - FIRST_HALF_END_MINUTE) * 60.0
        )

    if seconds < 0:
        return None
    # A position past the end of the file means the alignment or the minute is
    # wrong; placing a clip there would produce an empty or frozen cut.
    if alignment.duration_seconds > 0 and seconds >= alignment.duration_seconds:
        logger.warning(
            f"{minute}' maps to {seconds:.0f}s, past the end of "
            f"{alignment.source.name} ({alignment.duration_seconds:.0f}s); "
            f"using the photo pipeline"
        )
        return None
    return seconds
