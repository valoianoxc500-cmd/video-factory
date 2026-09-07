"""Match footage sourcing for the Match Analysis engine.

Legal posture
-------------
Broadcast football footage is licensed, aggressively enforced, and never
free to reuse just because it is reachable. This module therefore has ONE
hard rule, enforced in code rather than left to a prompt:

    Footage is only ever read from a source the operator has explicitly
    designated. There is no path in this module that discovers a video URL
    from a search result, a scrape, or model output and downloads it.

Two designated sources exist:

  local      A directory the operator drops match video into. They own the
             rights question for what they put there; nothing is fetched.
  licensed   Stock providers whose licence permits reuse (Pexels today).
             These give atmosphere and generic action, never match events.

Anything else -- a highlights page, a social clip, a CDN link a model
suggested -- is refused by `is_allowed_source`, which is deny-by-default.

When no designated footage covers a moment, `plan_moment_clips` returns
nothing for it and the caller falls back to the existing photo pipeline.
Missing footage degrades the video; it never fails the run.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("video_factory")

# Seconds of build-up kept before the event, and of reaction kept after it.
LEAD_IN_SECONDS = 5.0
LEAD_OUT_MIN_SECONDS = 5.0
LEAD_OUT_MAX_SECONDS = 10.0
# A moment shorter than this is not worth cutting to; longer than this and the
# analysis stops being a highlight and starts being a rebroadcast.
MIN_CLIP_SECONDS = 10.0
MAX_CLIP_SECONDS = 15.0

# Hosts that are never a lawful automated source for match footage, listed so
# that a misconfiguration is refused loudly instead of silently downloading.
_FORBIDDEN_HOST_MARKERS = (
    "youtube.", "youtu.be", "dailymotion.", "vimeo.", "twitter.", "x.com",
    "facebook.", "fb.watch", "instagram.", "tiktok.", "reddit.", "streamable.",
    "sportskeeda.", "espn.", "skysports.", "bein", "dazn.", "footballia.",
    "1337x", "torrent", "telegram.", "t.me",
)

# Providers whose licence permits reuse of the assets they serve.
_LICENSED_PROVIDERS = {"pexels"}

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}


class FootageError(RuntimeError):
    """Raised only for operator misconfiguration, never for a missing clip."""


@dataclass
class MatchMoment:
    """A verified thing that happened, in match-clock seconds."""

    label: str                 # "goal", "red_card", "penalty", ...
    minute: int                # match minute as reported by the data source
    description: str           # Arabic or English one-liner for the narration
    # Offset into the operator's footage file, when that footage has been
    # aligned to the match clock. None means we cannot cut to this moment.
    footage_offset_seconds: float | None = None


@dataclass
class ClipWindow:
    """A cut to make from a source file."""

    moment: MatchMoment
    source: Path
    start_seconds: float
    duration_seconds: float
    output_name: str


@dataclass
class FootageAvailability:
    """What the engine may legally use for this run."""

    local_clips: list[Path] = field(default_factory=list)
    licensed_enabled: bool = False
    reason: str = ""

    @property
    def has_local(self) -> bool:
        return bool(self.local_clips)


def is_allowed_source(source: str) -> bool:
    """Deny-by-default check for anything that looks like a fetchable source.

    Only a local filesystem path or a licensed-provider identifier passes. A
    URL never does -- this module has no code path that downloads one, and
    this function exists so that any future caller that tries has to fail a
    test rather than quietly ship a scraper.
    """
    candidate = (source or "").strip()
    if not candidate:
        # Windows resolves a whitespace-only path to the working directory,
        # so this must be rejected before it reaches Path.exists().
        return False
    lowered = candidate.lower()

    if lowered.startswith(("http://", "https://", "ftp://", "//")):
        return False
    if any(marker in lowered for marker in _FORBIDDEN_HOST_MARKERS):
        return False
    if lowered in _LICENSED_PROVIDERS:
        return True
    # Anything else is treated as a local path; it still has to exist.
    return Path(candidate).exists()


def discover_local_footage(directory: Path | None) -> list[Path]:
    """Video files the operator placed in their footage directory."""
    if directory is None:
        return []
    if not directory.exists() or not directory.is_dir():
        logger.info(f"no match footage directory at {directory}")
        return []
    clips = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in _VIDEO_SUFFIXES
    )
    if clips:
        logger.info(f"found {len(clips)} operator-provided clip(s) in {directory}")
    return clips


def resolve_availability(
    *,
    footage_dir: Path | None,
    allow_licensed_stock: bool,
) -> FootageAvailability:
    """Decide what this run is allowed to use, and say why."""
    local = discover_local_footage(footage_dir)
    if local:
        reason = f"using {len(local)} operator-provided clip(s)"
    elif allow_licensed_stock:
        reason = "no operator footage; licensed stock only (no match events)"
    else:
        reason = "no operator footage and licensed stock disabled; photos only"
    return FootageAvailability(
        local_clips=local,
        licensed_enabled=allow_licensed_stock,
        reason=reason,
    )


def clip_window_for(moment: MatchMoment, source_duration: float) -> tuple[float, float] | None:
    """(start, duration) for a moment, clamped to the source file.

    Builds ~5s of run-up, the event, and 5-10s of reaction, then trims to the
    10-15s target. Returns None when the moment cannot be located in the file
    or the file is too short to hold a usable window.
    """
    offset = moment.footage_offset_seconds
    if offset is None or source_duration <= 0:
        return None

    start = max(0.0, offset - LEAD_IN_SECONDS)
    lead_in = offset - start
    # Prefer the full reaction, but never exceed the clip cap once the
    # (possibly shortened) lead-in is accounted for.
    lead_out = min(LEAD_OUT_MAX_SECONDS, MAX_CLIP_SECONDS - lead_in)
    lead_out = max(lead_out, min(LEAD_OUT_MIN_SECONDS, MAX_CLIP_SECONDS - lead_in))

    end = min(source_duration, offset + lead_out)
    duration = end - start
    if duration < MIN_CLIP_SECONDS:
        # Try to recover the shortfall from earlier in the file before giving up.
        start = max(0.0, end - MIN_CLIP_SECONDS)
        duration = end - start
    if duration < MIN_CLIP_SECONDS:
        return None
    return start, min(duration, MAX_CLIP_SECONDS)


def probe_duration(path: Path) -> float:
    """Duration of a media file in seconds, or 0.0 if it cannot be read."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception as exc:
        logger.warning(f"ffprobe failed for {path.name}: {exc}")
        return 0.0


def plan_moment_clips(
    moments: list[MatchMoment],
    availability: FootageAvailability,
) -> list[ClipWindow]:
    """Windows to cut, for the moments that designated footage actually covers.

    Moments with no usable window are simply absent from the result; the
    caller renders those from the photo pipeline instead.
    """
    if not availability.has_local:
        return []

    windows: list[ClipWindow] = []
    durations = {p: probe_duration(p) for p in availability.local_clips}
    for index, moment in enumerate(moments, start=1):
        for source in availability.local_clips:
            window = clip_window_for(moment, durations.get(source, 0.0))
            if window is None:
                continue
            start, duration = window
            windows.append(
                ClipWindow(
                    moment=moment,
                    source=source,
                    start_seconds=start,
                    duration_seconds=duration,
                    output_name=f"moment_{index:02d}_{_slug(moment.label)}.mp4",
                )
            )
            break
        else:
            logger.info(
                f"no designated footage covers moment {moment.label} "
                f"at {moment.minute}'; it will use the photo pipeline"
            )
    return windows


def action_crop_x(
    source: Path,
    sample_at_seconds: float,
    *,
    target_size: tuple[int, int] = (1080, 1920),
) -> float:
    """Horizontal centre for the 9:16 crop, as a 0..1 fraction of the width.

    Sampled from a frame at the event itself, so the crop follows where the
    action is rather than the middle of the pitch -- a goal in the left third
    of a 16:9 frame is otherwise cropped out entirely. Reuses the same subject
    detector the stills use, and falls back to centre when it finds nothing.

    One crop is chosen for the whole clip rather than tracking per frame: a
    moving crop reads as a camera wobble, and the action stays in roughly one
    third of the frame across a 10-15s window.
    """
    import tempfile

    from core.framing import find_subject

    try:
        from PIL import Image
    except Exception:
        return 0.5

    with tempfile.TemporaryDirectory(prefix="vf_crop_") as tmp:
        frame = Path(tmp) / "sample.jpg"
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{max(0.0, sample_at_seconds):.3f}",
                "-i", str(source),
                "-frames:v", "1", "-q:v", "3",
                str(frame),
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not frame.exists():
            logger.info(f"could not sample a frame from {source.name}; centring crop")
            return 0.5
        try:
            with Image.open(frame) as img:
                rgb = img.convert("RGB")
                (subject_x, _), detector = find_subject(rgb)
                fraction = subject_x / max(rgb.width, 1)
        except Exception as exc:
            logger.info(f"crop detection failed for {source.name}: {exc}")
            return 0.5

    logger.info(
        f"action crop for {source.name} at {sample_at_seconds:.0f}s: "
        f"x={fraction:.2f} ({detector})"
    )
    return min(max(fraction, 0.0), 1.0)


def cut_clips(
    windows: list[ClipWindow],
    output_dir: Path,
    *,
    target_size: tuple[int, int] = (1080, 1920),
    fps: int = 30,
) -> list[Path]:
    """Cut each window to a 9:16 clip. Failures are skipped, not fatal."""
    if not windows:
        return []
    if shutil.which("ffmpeg") is None:
        raise FootageError("ffmpeg is required to cut match footage")
    output_dir.mkdir(parents=True, exist_ok=True)

    target_w, target_h = target_size
    produced: list[Path] = []
    for window in windows:
        out = output_dir / window.output_name
        if out.exists():
            produced.append(out)
            continue

        # Scale to cover the vertical frame, then crop toward the action
        # rather than the geometric centre.
        event_at = window.moment.footage_offset_seconds or (
            window.start_seconds + window.duration_seconds / 2
        )
        centre = action_crop_x(window.source, event_at, target_size=target_size)
        # ffmpeg's crop x is the left edge, clamped so the window stays inside
        # the scaled frame: (in_w - out_w) is the total slack available.
        vf = (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}:"
            f"'min(max((in_w-out_w)*{centre:.4f},0),in_w-out_w)':"
            f"'(in_h-out_h)/2',"
            f"fps={fps}"
        )
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{window.start_seconds:.3f}",
            "-i", str(window.source),
            "-t", f"{window.duration_seconds:.3f}",
            "-vf", vf,
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not out.exists():
            logger.warning(
                f"could not cut {window.output_name}: "
                f"{result.stderr.strip()[-200:]}"
            )
            continue
        produced.append(out)
        logger.info(
            f"cut {window.output_name} "
            f"({window.duration_seconds:.1f}s from {window.source.name})"
        )
    return produced


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "moment"
