"""Analyse an uploaded clip to find the moments worth building a video around.

This runs BEFORE generation for the Match Analysis engine when the operator
supplies a standalone clip rather than a full match. It answers one question:
"where, in this file's own timeline, does something important happen?"

It is deliberately separate from `core.footage_align`, which maps a match
clock onto a full 90-minute recording. A standalone clip has no match clock,
so alignment does not apply and is not used.

Providers, cheapest first
-------------------------
declared      The operator states the timestamp in the sidecar. Free, exact,
              and the only provider that is certain.
audio_energy  Crowd-roar detection from loudness. Free, but it only works on
              clips whose audio actually carries the crowd; measured on flat
              or commentary-dominated audio it finds nothing and says so
              rather than guessing.
vision        A model watches the video and reports event timestamps. Costs
              an API call, so it is opt-in via CLIP_ANALYSIS_VISION=1.

Every provider returns AnalysisResult with a confidence. `analyse_clip`
refuses to return a low-confidence timestamp: the caller treats "no confident
event" as "do not start generation", which is the requested gate.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("video_factory")

VISION_ENV = "CLIP_ANALYSIS_VISION"

# A detection below this is not acted on. Building a video around the wrong
# five seconds is worse than falling back.
MIN_CONFIDENCE = 0.6

# How far above the clip's own baseline a sustained passage must sit before it
# reads as a crowd reaction rather than normal match noise.
#
# Set high on purpose. Measured against a real goal clip whose goal is at 5s,
# a 4 dB threshold confidently reported 106.5s -- the loudest passage in the
# file, and the wrong moment. Loudness alone does not identify a goal in
# compressed or commentary-led audio, and a confidently wrong timestamp builds
# the whole video around the wrong five seconds. Only an emphatic roar is
# accepted; anything less defers to a declared timestamp or the vision
# provider.
_ROAR_MARGIN_DB = 12.0
_ROAR_WINDOW_SECONDS = 3.0


@dataclass
class DetectedEvent:
    """Something worth cutting to, in the clip's own timeline."""

    seconds: float
    label: str = "goal"
    description: str = ""
    confidence: float = 0.0
    detector: str = ""


@dataclass
class AnalysisResult:
    events: list[DetectedEvent] = field(default_factory=list)
    detector: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Whether generation may proceed on this analysis."""
        return bool(self.events)


def _sidecar_candidates(clip: Path) -> list[Path]:
    return [
        clip.with_suffix(clip.suffix + ".align.json"),
        clip.with_suffix(".align.json"),
    ]


def read_declared_events(clip: Path) -> AnalysisResult:
    """Timestamps the operator stated in the sidecar.

    Clip mode is signalled by `event_offset_seconds` (one event) or `events`
    (several). A sidecar carrying only `kickoff_offset_seconds` is a
    full-match alignment file and is left alone here.
    """
    for path in _sidecar_candidates(clip):
        if not path.exists():
            continue
        try:
            # utf-8-sig, not utf-8: sidecars get hand-written in Notepad and
            # PowerShell, both of which prepend a BOM that json.loads rejects.
            # Without this the declaration is silently ignored and analysis
            # falls through to a guess.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning(f"could not read {path.name}: {exc}")
            continue
        if not isinstance(data, dict):
            continue

        raw_events: list[dict] = []
        if isinstance(data.get("events"), list):
            raw_events = [e for e in data["events"] if isinstance(e, dict)]
        single = data.get("event_offset_seconds")
        if single is not None:
            raw_events.append(
                {
                    "seconds": single,
                    "label": data.get("event_label", "goal"),
                    "description": data.get("event_description", ""),
                }
            )

        events: list[DetectedEvent] = []
        for item in raw_events:
            try:
                seconds = float(item.get("seconds"))
            except (TypeError, ValueError):
                continue
            if seconds < 0:
                continue
            events.append(
                DetectedEvent(
                    seconds=seconds,
                    label=str(item.get("label") or "goal"),
                    description=str(item.get("description") or ""),
                    confidence=1.0,
                    detector="declared",
                )
            )
        if events:
            events.sort(key=lambda e: e.seconds)
            logger.info(
                f"clip analysis: {len(events)} declared event(s) in {path.name} "
                f"at {[round(e.seconds, 1) for e in events]}"
            )
            return AnalysisResult(
                events=events,
                detector="declared",
                reason=f"{len(events)} event(s) declared in {path.name}",
            )
    return AnalysisResult(detector="declared", reason="no declared events")


def loudness_series(clip: Path) -> list[tuple[float, float]]:
    """(time, RMS dB) samples across the clip, or [] when unreadable."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "info", "-i", str(clip), "-af",
                "astats=metadata=1:reset=12,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as exc:
        logger.warning(f"loudness scan failed for {clip.name}: {exc}")
        return []

    samples: list[tuple[float, float]] = []
    timestamp: float | None = None
    for line in (proc.stdout + proc.stderr).splitlines():
        head = re.match(r"frame:\d+\s+pts:\d+\s+pts_time:([\d.]+)", line)
        if head:
            timestamp = float(head.group(1))
            continue
        value = re.search(r"RMS_level=(-?[\d.]+)", line)
        if value and timestamp is not None:
            try:
                samples.append((timestamp, float(value.group(1))))
            except ValueError:
                continue
    return samples


def detect_by_audio_energy(clip: Path) -> AnalysisResult:
    """Find sustained crowd reactions. Returns nothing when the audio is flat.

    A goal in broadcast audio is a roar: several seconds clearly above the
    clip's own baseline. Clips with compressed, muted or commentary-only audio
    have no such signature, and this reports that rather than returning the
    loudest arbitrary second.
    """
    samples = loudness_series(clip)
    if len(samples) < 20:
        return AnalysisResult(
            detector="audio_energy", reason="not enough audio to analyse"
        )

    times = [t for t, _ in samples]
    values = [v for _, v in samples]
    step = max((times[-1] - times[0]) / max(len(times) - 1, 1), 1e-3)
    window = max(1, int(_ROAR_WINDOW_SECONDS / step))

    # Rolling mean, computed only where the window is fully inside the clip so
    # the ends are not inflated by partial windows -- that artefact otherwise
    # ranks the first and last frames as the loudest moments in the file.
    baseline = sum(values) / len(values)
    best: tuple[float, float] | None = None
    running = sum(values[:window])
    for i in range(window, len(values)):
        running += values[i] - values[i - window]
        mean = running / window
        centre = times[i - window // 2]
        if best is None or mean > best[1]:
            best = (centre, mean)

    if best is None:
        return AnalysisResult(detector="audio_energy", reason="no usable window")

    centre, peak = best
    margin = peak - baseline
    if margin < _ROAR_MARGIN_DB:
        reason = (
            f"loudest {_ROAR_WINDOW_SECONDS:.0f}s window is only {margin:.1f} dB "
            f"above the clip baseline ({peak:.1f} vs {baseline:.1f}); no crowd "
            f"reaction is distinguishable"
        )
        logger.info(f"clip analysis: {reason}")
        return AnalysisResult(detector="audio_energy", reason=reason)

    # Map the margin onto a confidence above the floor set by _ROAR_MARGIN_DB.
    confidence = min(1.0, 0.6 + (margin - _ROAR_MARGIN_DB) / 20.0)
    logger.info(
        f"clip analysis: crowd reaction at {centre:.1f}s "
        f"({margin:.1f} dB above baseline, confidence {confidence:.2f})"
    )
    return AnalysisResult(
        events=[
            DetectedEvent(
                seconds=centre,
                label="goal",
                description="crowd reaction",
                confidence=confidence,
                detector="audio_energy",
            )
        ],
        detector="audio_energy",
        reason=f"crowd reaction {margin:.1f} dB above baseline",
    )


def vision_enabled() -> bool:
    """Whether the paid vision provider may run."""
    return os.environ.get(VISION_ENV, "").strip() in ("1", "true", "yes")


def analyse_clip(clip: Path) -> AnalysisResult:
    """Find events in `clip`, cheapest reliable provider first.

    Returns a result with no events when nothing is confident enough to act
    on. The caller must treat that as "do not start generation".
    """
    if not clip.exists():
        return AnalysisResult(reason=f"{clip} does not exist")

    declared = read_declared_events(clip)
    if declared.ok:
        return declared

    audio = detect_by_audio_energy(clip)
    audio.events = [e for e in audio.events if e.confidence >= MIN_CONFIDENCE]
    if audio.ok:
        return audio

    if vision_enabled():
        logger.info(
            "clip analysis: falling back to the vision provider "
            f"({VISION_ENV} is set)"
        )
        return analyse_with_vision(clip)

    return AnalysisResult(
        reason=(
            f"{audio.reason}; no timestamp declared in a sidecar and the "
            f"vision provider is off (set {VISION_ENV}=1 to enable it)"
        )
    )


def analyse_with_vision(clip: Path) -> AnalysisResult:
    """Ask a model to watch the clip and report event timestamps.

    Costs one multimodal call, which is why `analyse_clip` only reaches it
    when CLIP_ANALYSIS_VISION is set.
    """
    try:
        import clients
    except Exception as exc:
        return AnalysisResult(reason=f"vision provider unavailable: {exc}")

    analyse = getattr(clients, "analyse_video_events", None)
    if analyse is None:
        # Kept explicit rather than silently degrading: the pipeline should
        # say the provider is missing, not pretend it found nothing.
        return AnalysisResult(
            reason=(
                "vision provider is enabled but clients.analyse_video_events "
                "is not implemented on this deployment"
            )
        )
    try:
        payload = analyse(clip)
    except Exception as exc:
        return AnalysisResult(reason=f"vision analysis failed: {exc}")

    events: list[DetectedEvent] = []
    for item in (payload or {}).get("events", []):
        try:
            seconds = float(item.get("seconds"))
        except (TypeError, ValueError):
            continue
        confidence = float(item.get("confidence") or 0.0)
        if confidence < MIN_CONFIDENCE:
            continue
        events.append(
            DetectedEvent(
                seconds=seconds,
                label=str(item.get("label") or "goal"),
                description=str(item.get("description") or ""),
                confidence=confidence,
                detector="vision",
            )
        )
    events.sort(key=lambda e: e.seconds)
    return AnalysisResult(
        events=events,
        detector="vision",
        reason=f"vision reported {len(events)} confident event(s)",
    )
