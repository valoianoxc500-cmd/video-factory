"""Why a video performed: hook, structure, pacing, captions, audio, drivers.

Two depths, and the difference is a rights decision not a quality one
--------------------------------------------------------------------
A video found through discovery belongs to someone else. Downloading it to
analyse frame by frame means making a copy the operator has no licence for,
and on most of these platforms it also breaks the terms the API access was
granted under. So discovered videos are analysed at `METADATA` depth: title,
description, hashtags, the public thumbnail, duration and the metrics -- all
of it already public, none of it a copy of the work.

A video the user owns, or holds rights to, is analysed at `FULL` depth: frames
are sampled locally, so the model can see pacing, cut rhythm, caption style and
on-screen text.

`analyse` picks the depth from the rights attestation rather than from a flag,
so the safe path is the default and the deep path requires the same
attestation publishing requires.

The output feeds two places: the analysis card in the UI, and
`viral.scoring.NewVersionInputs` -- so the "probability your version goes
viral" number is grounded in an actual reading of the original rather than in
sliders someone guessed at.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from viral.rights import RightsAttestation, Source
from viral.scoring import NewVersionInputs

logger = logging.getLogger("viral.analysis")


class AnalysisDepth(str, Enum):
    METADATA = "metadata"   # public signals only; no copy is made
    FULL = "full"           # frames sampled from footage the user holds rights to


def depth_for(attestation: RightsAttestation | None) -> AnalysisDepth:
    """Frame-level analysis only for footage the user actually holds."""
    if attestation is None or attestation.source is Source.DISCOVERED:
        return AnalysisDepth.METADATA
    return AnalysisDepth.FULL


@dataclass
class Scene:
    start_seconds: float
    label: str
    purpose: str = ""

    def to_record(self) -> dict:
        return {
            "start_seconds": round(float(self.start_seconds), 2),
            "label": self.label,
            "purpose": self.purpose,
        }


@dataclass
class VideoAnalysis:
    """One video, read. Every field is either observed or explicitly absent."""

    depth: AnalysisDepth = AnalysisDepth.METADATA
    hook: str = ""
    hook_seconds: float | None = None
    topic: str = ""
    video_format: str = ""
    pacing: str = ""
    cuts_per_minute: float | None = None
    scenes: list[Scene] = field(default_factory=list)
    caption_style: str = ""
    audio_style: str = ""
    engagement_drivers: list[str] = field(default_factory=list)
    why_it_performs: str = ""
    replicable_elements: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    #: 0..1 readings the forecast consumes. Absent when not established.
    hook_strength: float | None = None
    retention_potential: float | None = None
    topic_freshness: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def analysed(self) -> bool:
        return bool(self.hook or self.why_it_performs)

    def to_record(self) -> dict:
        return {
            "depth": self.depth.value,
            "hook": self.hook,
            "hook_seconds": self.hook_seconds,
            "topic": self.topic,
            "format": self.video_format,
            "pacing": self.pacing,
            "cuts_per_minute": self.cuts_per_minute,
            "scenes": [s.to_record() for s in self.scenes],
            "caption_style": self.caption_style,
            "audio_style": self.audio_style,
            "engagement_drivers": list(self.engagement_drivers),
            "why_it_performs": self.why_it_performs,
            "replicable_elements": list(self.replicable_elements),
            "risks": list(self.risks),
            "hook_strength": self.hook_strength,
            "retention_potential": self.retention_potential,
            "topic_freshness": self.topic_freshness,
            "notes": list(self.notes),
        }


def to_new_version_inputs(
    analysis: VideoAnalysis,
    *,
    length_fit: float | None = None,
    is_reused_format: bool = True,
) -> NewVersionInputs:
    """Turn a reading into forecast inputs.

    Anything the analysis did not establish is left at the neutral default
    rather than invented, which is what keeps the forecast's confidence honest
    when the reading was shallow.
    """
    kwargs: dict = {"is_reused_format": is_reused_format}
    if analysis.hook_strength is not None:
        kwargs["hook_strength"] = _clamp(analysis.hook_strength)
    if analysis.retention_potential is not None:
        kwargs["retention_potential"] = _clamp(analysis.retention_potential)
    if analysis.topic_freshness is not None:
        kwargs["topic_freshness"] = _clamp(analysis.topic_freshness)
    if length_fit is not None:
        kwargs["length_fit"] = _clamp(length_fit)
    return NewVersionInputs(**kwargs)


def _clamp(value: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# Prompts. Pure, so the instructions themselves are reviewable in a test.
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You analyse short-form video performance for creators. Describe what the "
    "video does and why it holds attention. Be specific and concrete. Never "
    "suggest ways to evade copyright detection, duplicate-content detection, "
    "or platform moderation; if asked, say that is out of scope. Report only "
    "what the supplied material shows -- if something is not visible, say so "
    "rather than guessing."
)

_SCHEMA = """Respond with JSON only:
{
  "hook": "the first-seconds hook, quoted or described",
  "hook_seconds": 0.0,
  "topic": "what it is about",
  "format": "e.g. talking head, POV, compilation, tutorial, reaction",
  "pacing": "how fast it moves and how that serves it",
  "cuts_per_minute": 0.0,
  "scenes": [{"start_seconds": 0.0, "label": "", "purpose": ""}],
  "caption_style": "on-screen text style, or 'none visible'",
  "audio_style": "music, voiceover, trending sound, ambient",
  "engagement_drivers": ["specific reasons viewers watch, share or comment"],
  "why_it_performs": "two or three sentences",
  "replicable_elements": ["what a creator could apply to their own video"],
  "risks": ["anything that could limit reach or breach a platform policy"],
  "hook_strength": 0.0,
  "retention_potential": 0.0,
  "topic_freshness": 0.0
}
The three trailing numbers are 0..1 judgements. Use null for anything the
supplied material does not show."""


def build_metadata_prompt(video: dict) -> str:
    """Prompt for a video we may not copy: public signals only."""
    lines = [
        "Analyse this short-form video from its public metadata and thumbnail.",
        "You have NOT been given the video itself, so do not describe shots, "
        "cuts or footage you cannot see. Where a field would require watching "
        "it, return null and say so in the relevant field.",
        "",
        f"Platform: {video.get('platform', 'unknown')}",
        f"Title: {video.get('title', '')}",
        f"Author: {video.get('author', '')}",
        f"Duration: {_fmt(video.get('duration_seconds'))} seconds",
        f"Views: {_fmt(video.get('views'))}",
        f"Likes: {_fmt(video.get('likes'))}",
        f"Comments: {_fmt(video.get('comments'))}",
        f"Account followers: {_fmt(video.get('followers'))}",
    ]
    if video.get("description"):
        lines += ["", f"Description: {str(video['description'])[:1500]}"]
    if video.get("hashtags"):
        lines.append(f"Hashtags: {' '.join(video['hashtags'])}")
    lines += ["", _SCHEMA]
    return "\n".join(lines)


def build_frame_prompt(video: dict, frame_times: list[float]) -> str:
    """Prompt for footage the user holds rights to, with sampled frames."""
    stamps = ", ".join(f"{t:.1f}s" for t in frame_times)
    return "\n".join([
        "Analyse this short-form video. The attached frames are samples taken "
        f"at {stamps}, in order.",
        "Read the hook from the earliest frames, the structure from how the "
        "frames change, and the caption style from any on-screen text.",
        "",
        f"Title: {video.get('title', '')}",
        f"Duration: {_fmt(video.get('duration_seconds'))} seconds",
        f"Views: {_fmt(video.get('views'))}",
        "",
        _SCHEMA,
    ])


def _fmt(value) -> str:
    return "unknown" if value in (None, "") else str(value)


# ---------------------------------------------------------------------------
# Frame sampling
# ---------------------------------------------------------------------------

def sample_frames(
    source_path: Path,
    duration: float,
    *,
    count: int = 8,
    output_dir: Path | None = None,
) -> list[tuple[float, Path]]:
    """Evenly spaced frames, weighted toward the opening.

    The first three seconds decide whether a short is watched at all, so two
    of the samples always land there regardless of the video's length.
    """
    if duration <= 0 or count < 1:
        return []
    target = output_dir or Path(tempfile.mkdtemp(prefix="vrf_frames_"))
    target.mkdir(parents=True, exist_ok=True)

    times = [0.3, min(1.5, duration * 0.5)]
    remaining = max(0, count - len(times))
    for i in range(remaining):
        times.append(duration * (i + 1) / (remaining + 1))
    times = sorted({round(min(t, max(0.0, duration - 0.05)), 2) for t in times})

    frames: list[tuple[float, Path]] = []
    for index, at in enumerate(times):
        out = target / f"frame_{index:02d}.jpg"
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}",
             "-i", str(source_path), "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and out.exists():
            frames.append((at, out))
        else:
            logger.info(f"frame sample at {at:.2f}s failed: {result.stderr[:120]}")
    return frames


def estimate_cuts_per_minute(source_path: Path, duration: float) -> float | None:
    """Cut rate from ffmpeg's scene detector. None when it cannot be measured."""
    if duration <= 0:
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(source_path), "-filter:v",
             "select='gt(scene,0.4)',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as exc:
        logger.info(f"scene detection failed: {exc}")
        return None
    cuts = len(re.findall(r"showinfo.*?pts_time:", result.stderr or ""))
    return round(cuts / (duration / 60.0), 1) if cuts else 0.0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_analysis(payload: dict, depth: AnalysisDepth) -> VideoAnalysis:
    """Turn a model response into an analysis, keeping absences absent."""
    data = payload if isinstance(payload, dict) else {}
    analysis = VideoAnalysis(
        depth=depth,
        hook=_text(data.get("hook")),
        hook_seconds=_number(data.get("hook_seconds")),
        topic=_text(data.get("topic")),
        video_format=_text(data.get("format") or data.get("video_format")),
        pacing=_text(data.get("pacing")),
        cuts_per_minute=_number(data.get("cuts_per_minute")),
        caption_style=_text(data.get("caption_style")),
        audio_style=_text(data.get("audio_style")),
        engagement_drivers=_strings(data.get("engagement_drivers")),
        why_it_performs=_text(data.get("why_it_performs")),
        replicable_elements=_strings(data.get("replicable_elements")),
        risks=_strings(data.get("risks")),
        hook_strength=_number(data.get("hook_strength")),
        retention_potential=_number(data.get("retention_potential")),
        topic_freshness=_number(data.get("topic_freshness")),
    )
    for entry in data.get("scenes") or []:
        if isinstance(entry, dict) and entry.get("label"):
            analysis.scenes.append(Scene(
                start_seconds=_number(entry.get("start_seconds")) or 0.0,
                label=_text(entry.get("label")),
                purpose=_text(entry.get("purpose")),
            ))
    if depth is AnalysisDepth.METADATA:
        analysis.notes.append(
            "Read from public metadata and the thumbnail. The video itself was "
            "not copied or downloaded, so shot-level detail is not included."
        )
    return analysis


def _text(value) -> str:
    if value in (None, "", "null"):
        return ""
    return str(value).strip()


def _number(value) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strings(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

async def analyse(
    video: dict,
    *,
    attestation: RightsAttestation | None = None,
    source_path: Path | None = None,
    model_call=None,
    vision_call=None,
) -> VideoAnalysis:
    """Analyse one video at the depth its rights basis allows.

    `model_call` and `vision_call` are injected so this is testable without
    a model; they default to the project's existing Gemini clients.
    """
    depth = depth_for(attestation)

    if depth is AnalysisDepth.FULL and source_path and Path(source_path).exists():
        duration = float(video.get("duration_seconds") or 0.0)
        frames = sample_frames(Path(source_path), duration)
        if frames:
            if vision_call is None:
                import clients

                vision_call = clients.review_with_vision
            prompt = build_frame_prompt(video, [t for t, _ in frames])
            payload = await vision_call(
                prompt, [p for _, p in frames],
                system_instruction=SYSTEM_INSTRUCTION,
                operation_label="viral_analysis_frames",
            )
            analysis = parse_analysis(payload or {}, AnalysisDepth.FULL)
            if analysis.cuts_per_minute is None:
                analysis.cuts_per_minute = estimate_cuts_per_minute(
                    Path(source_path), duration)
            return analysis
        logger.info("no frames could be sampled; falling back to metadata depth")
        depth = AnalysisDepth.METADATA

    if model_call is None:
        import clients

        model_call = clients.generate_json

    payload = await model_call(
        build_metadata_prompt(video),
        system_instruction=SYSTEM_INSTRUCTION,
        operation_label="viral_analysis_metadata",
    )
    return parse_analysis(payload or {}, AnalysisDepth.METADATA)


def summarise_for_ui(analysis: VideoAnalysis) -> dict:
    """The analysis card, with an explicit note when it was not established."""
    record = analysis.to_record()
    if not analysis.analysed:
        record["notes"] = record["notes"] + [
            "This video has not been analysed yet."
        ]
    return record
