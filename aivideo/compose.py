"""FFmpeg composition: clips + narration + music + captions -> one MP4.

Adapted in shape from MoneyPrinterTurbo's `app/services/video.py` (MIT), but
built on FFmpeg directly rather than MoviePy. The repository already renders
every other product through FFmpeg, the worker already has it on PATH, and
MoviePy's per-frame Python loop is the slowest part of MPT on a long video.

The composition is two passes on purpose. Normalising each clip separately is
restartable and lets one bad download be replaced without redoing the others;
a single monolithic filtergraph would make every clip's failure everyone's
failure, which is exactly the behaviour this product is supposed to avoid.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from aivideo.spec import VideoSpec

logger = logging.getLogger("aivideo")

#: Anything longer than this from one FFmpeg invocation means something is
#: wrong; a 90-second short should normalise in seconds and mux in under a
#: minute even on a slow disk.
_NORMALISE_TIMEOUT = 180
_RENDER_TIMEOUT = 900


class RenderFailed(RuntimeError):
    pass


def _run(cmd: list[str], *, timeout: int, label: str) -> None:
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-6:]
        raise RenderFailed(f"{label} failed: {' | '.join(tail)}")


def probe(path: Path) -> dict:
    """Stream/format facts about a media file, or {} when unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return json.loads(out.stdout or "{}")
    except Exception:
        return {}


def validate_output(path: Path, *, min_seconds: float = 3.0) -> tuple[bool, str]:
    """Is this a real, playable video with both streams?

    Run before a job is ever called done. A zero-byte file, an audio-only mux
    or a truncated render all produce a file on disk, and none of them is a
    video the customer can post.
    """
    if not path.exists():
        return False, "no output file was produced"
    if path.stat().st_size < 100_000:
        return False, f"output is only {path.stat().st_size} bytes"

    info = probe(path)
    streams = info.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        return False, "output has no video stream"
    if not audio:
        return False, "output has no audio stream"

    try:
        duration = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < min_seconds:
        return False, f"output is only {duration:.1f}s long"

    width = int(video[0].get("width") or 0)
    height = int(video[0].get("height") or 0)
    if width < 240 or height < 240:
        return False, f"output resolution is {width}x{height}"
    return True, f"{duration:.1f}s {width}x{height}"


def _fit_filter(spec: VideoSpec) -> str:
    """Scale one source clip onto the output canvas."""
    width, height = spec.size
    if spec.fit_mode == "contain":
        # Letterbox onto a blurred copy of itself rather than black bars: a
        # hard bar reads as a mistake, a blurred bed reads as a choice.
        return (
            f"split[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=28[bgb];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1"
    )


def normalise_clip(source: Path, target: Path, spec: VideoSpec, seconds: float) -> Path:
    """One clip, cut to length and conformed to the output canvas.

    Re-encoded rather than stream-copied because the sources arrive in mixed
    resolutions, frame rates and pixel formats, and concat demands they match.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "0", "-t", f"{max(0.8, seconds):.2f}", "-i", str(source),
            "-an",
            "-vf", f"{_fit_filter(spec)},fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20" if spec.quality == "high" else "24",
            str(target),
        ],
        timeout=_NORMALISE_TIMEOUT,
        label=f"normalising {source.name}",
    )
    return target


def build_visual_track(
    clips: list[Path], target: Path, total_seconds: float, spec: VideoSpec
) -> Path:
    """Concatenate the normalised clips, looping them to cover the narration.

    Looping is what makes a missing beat survivable: when footage came back
    short, the clips that did arrive are simply shown again rather than the
    video ending early or the stage failing.
    """
    if not clips:
        raise RenderFailed("no usable clips to build a visual track from")

    per_clip = max(1.2, spec.clip_seconds)
    needed = max(1, int(total_seconds / per_clip) + 1)
    ordered = [clips[i % len(clips)] for i in range(needed)]

    listing = target.parent / "concat.txt"
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in ordered), encoding="utf-8"
    )
    _run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(target)],
        timeout=_RENDER_TIMEOUT,
        label="joining clips",
    )
    return target


def render(
    *,
    visual: Path,
    narration: Path,
    output: Path,
    spec: VideoSpec,
    captions: Path | None = None,
    music: Path | None = None,
    duration: float,
) -> Path:
    """Final mux: trim to the narration, mix audio, burn captions.

    Written to a temporary file and moved into place only after it validates,
    so a partial render never appears as a finished video.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix(".partial.mp4")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(visual), "-i", str(narration)]
    if music is not None:
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    filters: list[str] = []
    # Captions are burned last so nothing is drawn over them.
    if captions is not None:
        # libass needs an escaped path; on Windows the drive colon must go too.
        escaped = captions.as_posix().replace(":", "\\:").replace("'", r"\'")
        filters.append(f"[0:v]subtitles='{escaped}'[v]")
        video_out = "[v]"
    else:
        video_out = "0:v"

    if music is not None:
        filters.append(
            f"[2:a]volume={spec.music_volume:.3f},"
            f"atrim=0:{duration:.2f},afade=t=out:st={max(0, duration - 1.5):.2f}:d=1.5[m]"
        )
        filters.append(f"[1:a]volume={spec.voice_volume:.3f}[vo]")
        filters.append("[vo][m]amix=inputs=2:duration=first:dropout_transition=0[a]")
        audio_out = "[a]"
    else:
        filters.append(f"[1:a]volume={spec.voice_volume:.3f}[a]")
        audio_out = "[a]"

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", video_out, "-map", audio_out]
    cmd += [
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-preset", "medium" if spec.quality == "high" else "veryfast",
        "-crf", "20" if spec.quality == "high" else "24",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        str(staging),
    ]

    _run(cmd, timeout=_RENDER_TIMEOUT, label="rendering")

    ok, detail = validate_output(staging)
    if not ok:
        staging.unlink(missing_ok=True)
        raise RenderFailed(f"render produced an unusable file: {detail}")

    # Atomic promotion: the finished path only ever holds a validated video.
    shutil.move(str(staging), str(output))
    logger.info(f"[aivideo] rendered {output.name} ({detail})")
    return output


def poster_frame(video: Path, target: Path, at: float = 1.0) -> Path | None:
    """A thumbnail for the results grid. Never fatal."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
             "-q:v", "3", str(target)],
            timeout=60, label="thumbnail",
        )
        return target if target.exists() else None
    except Exception as exc:
        logger.info(f"[aivideo] no thumbnail ({type(exc).__name__}); continuing")
        return None
