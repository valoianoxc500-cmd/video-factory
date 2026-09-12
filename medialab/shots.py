"""Shot boundaries, so a clip is cut where the camera cuts.

Both products need this and both were guessing without it. AI Video Maker took
the first N seconds of a stock clip, which regularly straddles an edit and
produces a beat that jumps mid-shot. Police Chase Studio ranked moments from
the transcript alone, so a cut could land in the middle of a PIT manoeuvre.

PySceneDetect's content detector is the right tool and is cheap at this scale
-- one downscaled pass over a clip that is usually under a minute. It is also
optional: if it is unavailable or the source defeats it, callers fall back to
the naive window rather than losing the video.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("medialab")

__all__ = ["Shot", "detect_shots", "longest_clean_window", "best_window_near"]

#: Below this a "shot" is a flash frame or a detector artefact, not something
#: worth cutting to.
_MIN_SHOT_SECONDS = 0.8


@dataclass(frozen=True)
class Shot:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def detect_shots(video: Path, *, threshold: float = 27.0) -> list[Shot]:
    """Shot boundaries in a video. Empty when detection is unavailable.

    An empty list is a normal answer, not an error: a single continuous
    dashcam take genuinely has no cuts, and the caller's fallback is the same
    either way.
    """
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except Exception:
        logger.debug("[medialab] PySceneDetect unavailable; skipping shot detection")
        return []

    try:
        source = open_video(str(video))
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=threshold))
        # Downscale before analysing: the detector only needs gross frame
        # differences, and full resolution makes this several times slower for
        # the same boundaries.
        manager.auto_downscale = True
        manager.detect_scenes(source, show_progress=False)
        scenes = manager.get_scene_list()
    except Exception as exc:
        logger.debug(f"[medialab] shot detection failed for {video.name}: {exc}")
        return []

    shots = [
        Shot(start.get_seconds(), end.get_seconds())
        for start, end in scenes
        if end.get_seconds() - start.get_seconds() >= _MIN_SHOT_SECONDS
    ]
    if shots:
        logger.debug(f"[medialab] {video.name}: {len(shots)} shot(s)")
    return shots


def longest_clean_window(
    video: Path, *, wanted: float, shots: list[Shot] | None = None
) -> tuple[float, float]:
    """A `wanted`-second window that does not straddle a cut.

    Returns (start, duration). Falls back to starting at zero when nothing is
    detected, which is what the caller did before this existed.
    """
    shots = detect_shots(video) if shots is None else shots
    if not shots:
        return 0.0, wanted

    # The longest shot is the safest place to sit; within it, skip the first
    # moment so the cut itself is not in frame.
    best = max(shots, key=lambda s: s.duration)
    if best.duration <= wanted:
        return round(best.start, 3), round(max(0.5, best.duration), 3)

    lead_in = min(0.35, (best.duration - wanted) / 2)
    return round(best.start + lead_in, 3), round(wanted, 3)


def best_window_near(
    seconds: float, *, wanted: float, shots: list[Shot], total: float
) -> tuple[float, float]:
    """A `wanted`-second window around `seconds`, starting on a cut.

    Used where the interesting instant is already known -- a detected crash,
    a transcript hit -- and only the in-point is in question. Starting on a
    shot boundary is what stops a clip opening mid-manoeuvre.

    The window keeps its requested length even when the shot containing the
    instant is shorter than that. Truncating instead was worse in both
    directions: a 20-second request came back as a 7-second clip, and police
    sources are compilations where a pursuit legitimately runs across several
    camera angles. A clean in-point is the part that matters; refusing to
    cross any cut at all just shortchanges the clip.
    """
    wanted = max(1.0, min(wanted, max(1.0, total)))
    containing = next(
        (s for s in shots if s.start <= seconds <= s.end and s.duration >= 1.0),
        None,
    )
    if containing is None:
        start = max(0.0, min(seconds - wanted * 0.42, max(0.0, total - wanted)))
        return round(start, 3), round(wanted, 3)

    if containing.duration <= wanted:
        # Open on this shot's cut and run the full length into what follows.
        start = min(containing.start, max(0.0, total - wanted))
        return round(max(0.0, start), 3), round(min(wanted, total - start), 3)

    # Bias earlier than the instant so the build-up is included: a crash with
    # no approach reads as a jump cut.
    start = max(containing.start, min(seconds - wanted * 0.45, containing.end - wanted))
    return round(start, 3), round(wanted, 3)
