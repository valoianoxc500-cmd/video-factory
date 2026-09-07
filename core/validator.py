"""Post-stage invariant validation — fast, free, deterministic checks."""

import json
import logging
import subprocess
from pathlib import Path

from PIL import Image

from core.utils import meets_minimum_source_size, minimum_source_size
from core.utils import ChannelConfig, Script, expected_sourced_image_slots

logger = logging.getLogger("video_factory")

THUMBNAIL_SIZE = (1280, 720)


# ── Media probing ─────────────────────────────────────────────────

def probe_media(path: Path) -> dict:
    """Run ffprobe and return parsed JSON with streams + format info."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _find_existing_image(directory: Path, stem: str) -> Path | None:
    for suffix in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = directory / f"{stem}{suffix}"
        if candidate.exists():
            return candidate
    return None


# ── Validators ────────────────────────────────────────────────────
# Each returns a list of error strings. Empty list = all checks passed.

def validate_script(script: Script) -> list[str]:
    """Validate script structure after generation."""
    errors = []
    if not script.title:
        errors.append("Script has no title")
    if not script.sections:
        errors.append("Script has no sections")
        return errors
    for i, s in enumerate(script.sections):
        if s.id != i + 1:
            errors.append(f"Section {i+1} has id={s.id} (expected sequential)")
        if not s.narration.strip():
            errors.append(f"Section {s.id} has empty narration")
    total_words = sum(len(s.narration.split()) for s in script.sections)
    if total_words < 50:
        errors.append(f"Script has only {total_words} words (expected > 50)")
    return errors


def validate_raw_images(ws: Path, script: Script, config: ChannelConfig) -> list[str]:
    """Validate raw images exist and are large enough after sourcing."""
    raw_dir = ws / "images" / "raw"
    videos_dir = ws / "videos" / "raw"
    missing = []
    too_small = []
    target_size = tuple(config.video.resolution)
    min_w, min_h = minimum_source_size(target_size)
    for section_id, sub_idx in expected_sourced_image_slots(script, config):
        if (videos_dir / f"section_{section_id:03d}_{sub_idx:02d}.mp4").exists():
            continue
        stem = f"section_{section_id:03d}_{sub_idx:02d}"
        image_path = _find_existing_image(raw_dir, stem)
        if not image_path and sub_idx == 1:
            image_path = _find_existing_image(raw_dir, f"section_{section_id:03d}")
        if image_path:
            with Image.open(image_path) as img:
                width, height = img.size
            if meets_minimum_source_size(width, height, target_size):
                continue
            too_small.append(
                f"{image_path.name} is {width}x{height} "
                f"(minimum {min_w}x{min_h})"
            )
            continue
        missing.append(stem)
    errors = []
    if missing:
        errors.append(f"Missing raw image(s): {', '.join(missing[:10])}")
    if too_small:
        errors.append(f"Raw image(s) too small: {', '.join(too_small[:10])}")
    return errors


def validate_ready_images(
    ws: Path,
    script: Script,
    config: ChannelConfig,
) -> list[str]:
    """Validate processed images exist after processing."""
    ready_dir = ws / "images" / "ready"
    videos_dir = ws / "videos" / "raw"
    missing = []
    for section_id, sub_idx in expected_sourced_image_slots(script, config):
        if (videos_dir / f"section_{section_id:03d}_{sub_idx:02d}.mp4").exists():
            continue
        candidate = ready_dir / f"section_{section_id:03d}_{sub_idx:02d}.png"
        if candidate.exists():
            continue
        missing.append(candidate.name)
    return [f"Missing ready image(s): {', '.join(missing[:10])}"] if missing else []


def validate_audio(ws: Path, script: Script) -> list[str]:
    """Validate audio outputs after sourcing."""
    errors = []
    narration = ws / "audio" / "narration_full.wav"
    if not narration.exists():
        errors.append(f"Missing narration track: {narration.name}")
        return errors
    if narration.stat().st_size == 0:
        errors.append("narration_full.wav is empty")

    missing_dur = [str(s.id) for s in script.sections if s.actual_duration_seconds is None]
    if missing_dur:
        errors.append(f"No actual_duration_seconds for section(s): {', '.join(missing_dur)}")

    # Check full duration ≈ sum of section durations
    section_total = sum(s.actual_duration_seconds for s in script.sections if s.actual_duration_seconds)
    if section_total > 0:
        info = probe_media(narration)
        audio_dur = float(info.get("format", {}).get("duration", 0))
        drift = abs(audio_dur - section_total)
        if drift > 2.0:
            errors.append(f"Audio duration drift: {audio_dur:.1f}s vs section sum {section_total:.1f}s (drift {drift:.1f}s)")

    return errors


def validate_video(path: Path, config: ChannelConfig) -> list[str]:
    """Validate assembled video file."""
    errors = []
    if not path.exists():
        errors.append(f"Video file missing: {path.name}")
        return errors
    if path.stat().st_size == 0:
        errors.append("Video file is empty")
        return errors

    info = probe_media(path)
    streams = info.get("streams", [])

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if len(video_streams) != 1:
        errors.append(f"Expected 1 video stream, found {len(video_streams)}")
    if len(audio_streams) != 1:
        errors.append(f"Expected 1 audio stream, found {len(audio_streams)}")

    if video_streams:
        vs = video_streams[0]
        if vs.get("codec_name") != "h264":
            errors.append(f"Video codec: {vs.get('codec_name')} (expected h264)")
        if vs.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
            errors.append(f"Pixel format: {vs.get('pix_fmt')} (expected yuv420p/yuvj420p)")
        w, h = int(vs.get("width", 0)), int(vs.get("height", 0))
        expected_w, expected_h = config.video.resolution
        if (w, h) != (expected_w, expected_h):
            errors.append(f"Resolution: {w}x{h} (expected {expected_w}x{expected_h})")

    if audio_streams:
        acodec = audio_streams[0].get("codec_name", "")
        if acodec != "aac":
            errors.append(f"Audio codec: {acodec} (expected aac)")

    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0:
        errors.append("Video has zero duration")

    # Decode error check
    decode_cmd = [
        "ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-",
    ]
    decode_result = subprocess.run(decode_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if decode_result.stderr.strip():
        errors.append(f"Decode errors: {decode_result.stderr.strip()[:200]}")

    return errors


def validate_thumbnail(path: Path, config: ChannelConfig | None = None) -> list[str]:
    """Validate thumbnail image.

    The expected size comes from the channel when one is given: a channel
    publishing 9:16 shorts renders a vertical thumbnail, and checking it
    against a hardcoded 1280x720 failed a correct one.
    """
    errors = []
    if not path.exists():
        errors.append(f"Thumbnail missing: {path.name}")
        return errors
    if path.stat().st_size < 10_000:
        errors.append(f"Thumbnail too small: {path.stat().st_size} bytes (expected > 10KB)")

    expected = THUMBNAIL_SIZE
    if config is not None:
        from core.thumbnailer import thumbnail_size

        expected = thumbnail_size(config)

    img = Image.open(path)
    w, h = img.size
    if (w, h) != expected:
        errors.append(f"Thumbnail dimensions: {w}x{h} (expected {expected[0]}x{expected[1]})")

    return errors


# ── Runner ────────────────────────────────────────────────────────

def run_validation(name: str, errors: list[str]) -> None:
    """Log validation results. Raises RuntimeError on any errors."""
    if not errors:
        logger.info(f"[validate] {name}: PASS")
        return
    for e in errors:
        logger.error(f"[validate] {name}: {e}")
    raise RuntimeError(f"Validation failed for {name}: {errors[0]}")
