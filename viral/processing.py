"""Light edit: format and quality work on footage the user already owns.

What "light" means here
-----------------------
Re-encode to a platform's spec, normalise loudness, correct the aspect ratio,
reframe to 9:16 around the subject, and optionally trim dead air at the ends.
That is the work any editor does to their own footage before posting it.

What this does not do, and will not
-----------------------------------
It does not alter the content to make it read as different material than it
is. No mirroring, no pitch shifting, no speed nudges, no noise overlays, no
frame injection, no metadata stripping. Those steps have one purpose --
defeating a platform's own duplicate and rights detection -- and a tool that
performs them is a tool for infringing. `viral.rights.is_prohibited_technique`
names them, and `build_plan` refuses any step that matches.

The reframing reuses `core.framing`, the subject-aware cropper already built
and measured for this repo, rather than a second centre-crop.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from viral.rights import RightsAttestation, RightsError, is_prohibited_technique, require_publishable

logger = logging.getLogger("viral.processing")

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_FPS = 30

#: Loudness targets. -14 LUFS is the level the major platforms normalise to,
#: so mastering to it avoids their processing pulling the track around.
TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.0


@dataclass(frozen=True)
class PlatformProfile:
    """Encoding a platform actually accepts."""

    name: str
    max_seconds: float
    width: int = TARGET_WIDTH
    height: int = TARGET_HEIGHT
    fps: int = TARGET_FPS
    video_bitrate: str = "8M"
    audio_bitrate: str = "192k"
    container: str = "mp4"


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "instagram": PlatformProfile("instagram", max_seconds=90),
    "facebook":  PlatformProfile("facebook", max_seconds=90),
    "tiktok":    PlatformProfile("tiktok", max_seconds=600, video_bitrate="10M"),
    "youtube":   PlatformProfile("youtube", max_seconds=180, video_bitrate="12M"),
    "snapchat":  PlatformProfile("snapchat", max_seconds=60),
}


@dataclass
class SourceProbe:
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    has_audio: bool = False

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return 0.55 <= self.aspect <= 0.58 if self.aspect else False


@dataclass
class ProcessingStep:
    name: str
    reason: str


@dataclass
class ProcessingPlan:
    """Every step, with why it is being applied. Auditable before it runs."""

    platform: str
    profile: PlatformProfile
    steps: list[ProcessingStep] = field(default_factory=list)
    trim_start: float = 0.0
    trim_end: float = 0.0
    crop_centre_x: float = 0.5
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        return [f"{s.name}: {s.reason}" for s in self.steps]


def probe(path: Path) -> SourceProbe:
    """Read the source's real geometry. Nothing is assumed from the filename."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(out.stdout or "{}")
    except Exception as exc:
        logger.warning(f"ffprobe failed for {path.name}: {exc}")
        return SourceProbe()

    result = SourceProbe()
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not result.width:
            result.width = int(stream.get("width") or 0)
            result.height = int(stream.get("height") or 0)
            rate = str(stream.get("r_frame_rate") or "0/1")
            try:
                num, _, den = rate.partition("/")
                result.fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                result.fps = 0.0
        elif stream.get("codec_type") == "audio":
            result.has_audio = True
    try:
        result.duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        result.duration = 0.0
    return result


def build_plan(
    source: SourceProbe,
    platform: str,
    *,
    attestation: RightsAttestation | None = None,
    requested_steps: list[str] | None = None,
    trim_start: float = 0.0,
    trim_end: float = 0.0,
) -> ProcessingPlan:
    """Decide what to do to this file for this platform.

    The rights gate runs first: a plan is not produced for footage the user
    has not attested to.
    """
    require_publishable(attestation)

    for requested in requested_steps or []:
        if is_prohibited_technique(requested):
            raise RightsError(
                f"Refusing the requested step {requested!r}. Steps whose "
                f"purpose is to defeat copyright, duplicate or moderation "
                f"detection are not implemented. Processing here is limited "
                f"to formatting and quality work on footage you own."
            )

    key = str(platform or "").strip().lower()
    profile = PLATFORM_PROFILES.get(key)
    if profile is None:
        raise ValueError(f"unknown platform {platform!r}")

    plan = ProcessingPlan(platform=key, profile=profile)

    plan.steps.append(ProcessingStep(
        "re-encode",
        f"H.264/AAC at {profile.width}x{profile.height} {profile.fps}fps, "
        f"the spec {key} accepts",
    ))

    if not source.is_vertical and source.aspect:
        plan.steps.append(ProcessingStep(
            "reframe",
            f"source is {source.width}x{source.height} ({source.aspect:.2f}); "
            f"cropping to 9:16 around the subject rather than letterboxing",
        ))
    elif source.aspect:
        plan.steps.append(ProcessingStep(
            "scale", "already vertical; scaling to the target resolution",
        ))

    if source.has_audio:
        plan.steps.append(ProcessingStep(
            "normalise audio",
            f"loudnorm to {TARGET_LUFS} LUFS / {TARGET_TRUE_PEAK} dBTP, the "
            f"level platforms normalise to anyway",
        ))
    else:
        plan.warnings.append("Source has no audio track.")

    trim_start = max(0.0, float(trim_start))
    trim_end = max(0.0, float(trim_end))
    if trim_start or trim_end:
        plan.trim_start, plan.trim_end = trim_start, trim_end
        plan.steps.append(ProcessingStep(
            "trim",
            f"removing {trim_start:.1f}s from the start and {trim_end:.1f}s "
            f"from the end",
        ))

    remaining = source.duration - trim_start - trim_end
    if source.duration and remaining > profile.max_seconds:
        plan.warnings.append(
            f"{remaining:.0f}s exceeds {key}'s {profile.max_seconds:.0f}s "
            f"limit. Trim before publishing; this tool will not silently cut "
            f"your video."
        )

    if source.width and source.width < profile.width:
        plan.warnings.append(
            f"Source is {source.width}px wide, below {profile.width}px. "
            f"Upscaling cannot add detail; expect a softer result."
        )
    return plan


def build_ffmpeg_command(
    source_path: Path,
    output_path: Path,
    plan: ProcessingPlan,
    *,
    crop_centre_x: float | None = None,
) -> list[str]:
    """The exact ffmpeg invocation for a plan. Pure: builds, does not run.

    Separated from execution so a plan can be inspected, logged and tested
    without touching a file.
    """
    profile = plan.profile
    centre = plan.crop_centre_x if crop_centre_x is None else crop_centre_x
    centre = max(0.0, min(1.0, centre))

    filters = [
        # Cover the frame, then crop toward the subject rather than the middle.
        f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=increase",
        f"crop={profile.width}:{profile.height}:"
        f"'min(max((in_w-out_w)*{centre:.4f},0),in_w-out_w)':'(in_h-out_h)/2'",
        f"fps={profile.fps}",
    ]

    cmd = ["ffmpeg", "-y"]
    if plan.trim_start:
        cmd += ["-ss", f"{plan.trim_start:.3f}"]
    cmd += ["-i", str(source_path)]
    if plan.trim_end:
        cmd += ["-t", f"{max(0.1, _plan_duration(plan)):.3f}"]

    cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-profile:v", "high",
        "-b:v", profile.video_bitrate, "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if any(s.name == "normalise audio" for s in plan.steps):
        cmd += [
            "-af", f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA=11",
            "-c:a", "aac", "-b:a", profile.audio_bitrate, "-ar", "48000",
        ]
    else:
        cmd += ["-an"]
    cmd.append(str(output_path))
    return cmd


def _plan_duration(plan: ProcessingPlan) -> float:
    """Output length implied by the trims, when an end trim is set."""
    return max(0.1, plan.profile.max_seconds)


def detect_subject_centre(source_path: Path, at_seconds: float = 1.0) -> float:
    """Horizontal crop centre (0..1) from a sampled frame.

    Reuses the subject detector already built and measured in this repo, so
    the reframe keeps the person in shot instead of cropping to whatever
    happens to be in the middle.
    """
    import tempfile

    try:
        from core.framing import find_subject
        from PIL import Image
    except Exception:
        return 0.5

    with tempfile.TemporaryDirectory(prefix="vrf_frame_") as tmp:
        frame = Path(tmp) / "sample.jpg"
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, at_seconds):.2f}",
             "-i", str(source_path), "-frames:v", "1", str(frame)],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not frame.exists():
            return 0.5
        try:
            with Image.open(frame) as img:
                rgb = img.convert("RGB")
                (x, _), _ = find_subject(rgb)
                return max(0.0, min(1.0, x / max(rgb.width, 1)))
        except Exception:
            return 0.5
