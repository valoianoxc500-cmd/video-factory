"""Clipping: many clips out of one video the user owns, without losing any.

This sits on top of `viral.processing`, which does the single-file format and
quality work, and adds the things a clipping *product* needs that a one-shot
re-encode does not:

    many clips per job     one bad range must not cost the other nine
    captions               optional, styled, and never fatal
    subject focus          with a defined answer when detection is unavailable
    resumability           a retry re-renders what failed, not what succeeded

Two rules shape almost every decision below.

**A caption is a garnish, not the dish.** A clip whose captions could not be
built is still a correct clip of the right footage at the right length. So
every caption path returns something usable or returns nothing -- it never
raises, and it never causes the clip around it to be discarded. The transcript
comes from a real recogniser and real recognisers emit malformed rows, so the
parsing here assumes nothing about its shape.

**A batch is not a transaction.** Clip four failing is not a reason to throw
away clips one to three, which are finished, correct, and what the user asked
for. `run_clip_batch` therefore reports per-clip outcomes and only fails the
job when *nothing* succeeded.

Rights are unchanged and non-negotiable: `plan_clips` calls the same
`require_publishable` gate as `build_plan`, and refuses the same prohibited
techniques. Nothing here downloads, circumvents, or re-identifies anything.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from viral.processing import PLATFORM_PROFILES, SourceProbe
from viral.rights import (
    RightsAttestation,
    RightsError,
    is_prohibited_technique,
    require_publishable,
)

logger = logging.getLogger("viral.clipping")


# ── user-facing options ──────────────────────────────────────────────

@dataclass(frozen=True)
class Aspect:
    id: str
    label: str
    width: int
    height: int

    @property
    def ratio(self) -> float:
        return self.width / self.height


#: Vertical first: this is a short-form clipper, and 9:16 is the default a
#: user who expresses no preference should get.
ASPECTS: dict[str, Aspect] = {
    "9:16": Aspect("9:16", "Vertical", 1080, 1920),
    "1:1": Aspect("1:1", "Square", 1080, 1080),
    "4:5": Aspect("4:5", "Portrait", 1080, 1350),
    "16:9": Aspect("16:9", "Landscape", 1920, 1080),
}
DEFAULT_ASPECT = "9:16"


@dataclass(frozen=True)
class QualityProfile:
    id: str
    label: str
    crf: int
    preset: str
    audio_bitrate: str


#: CRF rather than a fixed bitrate: a talking-head clip and a motion-heavy one
#: need very different bitrates for the same visible quality, and a fixed
#: number over-spends on one and smears the other.
QUALITIES: dict[str, QualityProfile] = {
    "high": QualityProfile("high", "High", crf=18, preset="slow", audio_bitrate="192k"),
    "balanced": QualityProfile("balanced", "Balanced", crf=21, preset="medium", audio_bitrate="160k"),
    "fast": QualityProfile("fast", "Fast", crf=24, preset="veryfast", audio_bitrate="128k"),
}
DEFAULT_QUALITY = "balanced"

#: How the crop window is chosen horizontally.
FOCUS_MODES = ("auto", "center", "left", "right")
DEFAULT_FOCUS = "auto"

CAPTION_STYLES = ("clean", "bold", "minimal")
DEFAULT_CAPTION_STYLE = "clean"

#: Clip length bounds. Below one second there is nothing to watch; the upper
#: bound is the longest any supported platform accepts.
MIN_CLIP_SECONDS = 1.0
MAX_CLIP_SECONDS = 600.0

#: Attempts per clip. Bounded on purpose -- a clip that fails twice for the
#: same reason will fail a hundred times, and the job should end.
MAX_ATTEMPTS_PER_CLIP = 2


# ── customer-facing state ────────────────────────────────────────────

#: The only states a customer is shown. Internal stage names, task rows and
#: provider errors never reach the UI.
CUSTOMER_STATES = ("Preparing", "Analyzing", "Creating clips", "Rendering", "Ready")

_STATE_BY_STAGE = {
    "queued": "Preparing",
    "downloading": "Preparing",
    "probing": "Preparing",
    "analyzing": "Analyzing",
    "transcribing": "Analyzing",
    "focusing": "Analyzing",
    "planning": "Creating clips",
    "clipping": "Creating clips",
    "rendering": "Rendering",
    "encoding": "Rendering",
    "uploading": "Rendering",
    "done": "Ready",
}


def customer_state(stage: str) -> str:
    """The simple state for an internal stage name.

    Anything unrecognised reads as "Preparing" rather than leaking the raw
    name: a new internal stage should never surface as a new customer state.
    """
    return _STATE_BY_STAGE.get(str(stage or "").strip().lower(), "Preparing")


#: Substrings that mean the text came from a tool, not from us.
_INTERNAL = re.compile(
    r"ffmpeg|ffprobe|libx264|traceback|stack|errno|exit code|codec|"
    r"/[\w.-]+/|[a-z]:\\|\bgs://|http[s]?://|subprocess|stderr",
    re.IGNORECASE,
)


def safe_clip_error(raw: object) -> str:
    """A sentence a customer can read, from anything at all.

    Deliberately lossy. The detail belongs in the worker log; what reaches the
    screen must never be an ffmpeg filter graph or a storage path.
    """
    text = str(raw or "").strip()
    if not text:
        return "That clip could not be created. Your other clips are unaffected."
    if _INTERNAL.search(text):
        return "That clip could not be created. Your other clips are unaffected."
    if re.search(r"rights|licen[cs]e|own", text, re.IGNORECASE):
        # Rights messages are written for the customer already and are the one
        # thing they can act on.
        return text if len(text) <= 220 else text[:217] + "..."
    if len(text) > 160:
        return "That clip could not be created. Your other clips are unaffected."
    return text


# ── clip specs ───────────────────────────────────────────────────────

@dataclass
class ClipSpec:
    """One clip the user asked for."""

    id: str
    start: float
    end: float
    aspect: str = DEFAULT_ASPECT
    captions: bool = False
    caption_style: str = DEFAULT_CAPTION_STYLE
    focus: str = DEFAULT_FOCUS
    quality: str = DEFAULT_QUALITY
    #: Set when the user named a speaker to keep in frame; advisory only.
    speaker: str = ""

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end - self.start), 3)

    def to_record(self) -> dict:
        record = asdict(self)
        record["duration"] = self.duration
        return record


@dataclass
class ClipRejection:
    """A requested clip that was not planned, and why -- in customer words."""

    id: str
    reason: str


@dataclass
class ClipPlan:
    specs: list[ClipSpec] = field(default_factory=list)
    rejected: list[ClipRejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_record(self) -> dict:
        return {
            "clips": [s.to_record() for s in self.specs],
            "rejected": [asdict(r) for r in self.rejected],
            "warnings": list(self.warnings),
        }


def _clean_option(value: object, allowed, default: str) -> str:
    """An option the caller sent, or the default. Never raises on junk input."""
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def plan_clips(
    source: SourceProbe,
    requests: list[dict],
    *,
    attestation: RightsAttestation | None = None,
    platform: str = "tiktok",
    requested_steps: list[str] | None = None,
) -> ClipPlan:
    """Turn requested ranges into validated clip specs.

    The rights gate runs first and unchanged: no plan is produced for footage
    the user has not attested to, and a prohibited technique is refused rather
    than quietly dropped.

    A range that cannot be honoured is *rejected with a reason*, not silently
    corrected -- a clip that came back three seconds shorter than asked for,
    with no explanation, is worse than one that came back refused.
    """
    require_publishable(attestation)

    for requested in requested_steps or []:
        if is_prohibited_technique(requested):
            raise RightsError(
                f"Refusing the requested step {requested!r}. Steps whose "
                f"purpose is to defeat copyright, duplicate or moderation "
                f"detection are not implemented. Clipping here is limited to "
                f"formatting and quality work on footage you own."
            )

    profile = PLATFORM_PROFILES.get(str(platform or "").strip().lower())
    plan = ClipPlan()
    duration = float(getattr(source, "duration", 0.0) or 0.0)

    seen: set[str] = set()
    for index, raw in enumerate(requests or []):
        if not isinstance(raw, dict):
            plan.rejected.append(ClipRejection(f"clip_{index + 1}", "That clip was not readable."))
            continue

        clip_id = str(raw.get("id") or f"clip_{index + 1}").strip()[:60] or f"clip_{index + 1}"
        if clip_id in seen:
            plan.rejected.append(ClipRejection(clip_id, "Duplicate clip, skipped."))
            continue
        seen.add(clip_id)

        try:
            start = float(raw.get("start") or 0.0)
            end = float(raw.get("end") or 0.0)
        except (TypeError, ValueError):
            plan.rejected.append(ClipRejection(clip_id, "That clip's start and end were not numbers."))
            continue

        if not math.isfinite(start) or not math.isfinite(end):
            plan.rejected.append(ClipRejection(clip_id, "That clip's start and end were not numbers."))
            continue

        start = max(0.0, start)
        if end <= start:
            plan.rejected.append(ClipRejection(clip_id, "That clip ends before it starts."))
            continue

        # Only clamp against a duration we actually measured. A probe that
        # failed reports 0.0, and clamping to that would reject every clip.
        if duration > 0:
            if start >= duration:
                plan.rejected.append(ClipRejection(clip_id, "That clip starts after the video ends."))
                continue
            if end > duration:
                plan.warnings.append(f"{clip_id}: shortened to the end of the video.")
                end = duration

        length = end - start
        if length < MIN_CLIP_SECONDS:
            plan.rejected.append(ClipRejection(clip_id, "That clip is too short to use."))
            continue
        if length > MAX_CLIP_SECONDS:
            plan.rejected.append(ClipRejection(clip_id, "That clip is longer than clipping supports."))
            continue

        if profile and length > profile.max_seconds:
            plan.warnings.append(
                f"{clip_id}: {length:.0f}s is longer than {profile.name} accepts "
                f"({profile.max_seconds:.0f}s). It will still be created."
            )

        plan.specs.append(ClipSpec(
            id=clip_id,
            start=round(start, 3),
            end=round(end, 3),
            aspect=_clean_option(raw.get("aspect"), ASPECTS, DEFAULT_ASPECT),
            captions=bool(raw.get("captions")),
            caption_style=_clean_option(raw.get("caption_style"), CAPTION_STYLES, DEFAULT_CAPTION_STYLE),
            focus=_clean_option(raw.get("focus"), FOCUS_MODES, DEFAULT_FOCUS),
            quality=_clean_option(raw.get("quality"), QUALITIES, DEFAULT_QUALITY),
            speaker=str(raw.get("speaker") or "").strip()[:80],
        ))

    if not plan.specs and not plan.rejected:
        plan.rejected.append(ClipRejection("clip_1", "No clips were requested."))
    return plan


# ── captions, defensively ────────────────────────────────────────────

@dataclass
class CaptionCue:
    start: float
    end: float
    text: str


def _coerce_word(row: object) -> dict | None:
    """One transcript row, or None when it is unusable.

    Real recognisers return rows with missing keys, string numbers, nulls and
    occasionally a bare string. Every one of those has been treated here as
    "skip this word", never as "fail the clip".
    """
    if isinstance(row, str):
        text = row.strip()
        return {"word": text, "start": None, "end": None} if text else None
    if not isinstance(row, dict):
        return None

    text = str(row.get("word") or row.get("text") or "").strip()
    if not text:
        return None

    def _time(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        return number

    return {"word": text, "start": _time(row.get("start")), "end": _time(row.get("end"))}


def caption_cues(
    words: list,
    *,
    clip_start: float = 0.0,
    clip_end: float | None = None,
    max_chars: int = 42,
    max_seconds: float = 3.0,
) -> list[CaptionCue]:
    """Caption cues for one clip, from whatever the transcript turned out to be.

    Times are rebased to the clip, so cue zero starts at zero regardless of
    where the clip was cut from.

    Returns `[]` for a transcript that cannot be used at all. It never raises:
    a caption failure must cost the captions, not the clip.
    """
    try:
        return _caption_cues(words, clip_start, clip_end, max_chars, max_seconds)
    except Exception as exc:  # noqa: BLE001 - captions must never be fatal
        logger.warning(f"[clipping] captions unavailable, continuing without: {exc}")
        return []


def _caption_cues(
    words: list,
    clip_start: float,
    clip_end: float | None,
    max_chars: int,
    max_seconds: float,
) -> list[CaptionCue]:
    if not isinstance(words, list):
        return []

    usable = [w for w in (_coerce_word(row) for row in words) if w]
    if not usable:
        return []

    timed = [w for w in usable if w["start"] is not None and w["end"] is not None]

    # Fallback segmentation: a transcript with words but no usable timings is
    # still worth captioning if we know how long the clip is. Spread the words
    # across the clip weighted by length -- the same model the narration
    # estimator uses. Without a clip length there is nothing to spread across,
    # so captions are dropped rather than guessed.
    if not timed:
        span_end = clip_end if clip_end is not None else None
        if span_end is None or span_end <= clip_start:
            return []
        timed = _spread_words([w["word"] for w in usable], clip_start, span_end)

    # A recogniser can emit rows out of order or with end before start; both
    # produce cues that never leave the screen.
    cleaned: list[dict] = []
    for word in timed:
        start = float(word["start"])
        end = float(word["end"])
        if end <= start:
            end = start + 0.12
        cleaned.append({"word": word["word"], "start": start, "end": end})
    cleaned.sort(key=lambda w: w["start"])

    window_end = clip_end if clip_end is not None else float("inf")
    inside = [
        w for w in cleaned
        if w["end"] > clip_start and w["start"] < window_end
    ]
    if not inside:
        return []

    cues: list[CaptionCue] = []
    line: list[str] = []
    line_start = None
    line_end = 0.0

    def flush() -> None:
        nonlocal line, line_start, line_end
        if line and line_start is not None:
            cues.append(CaptionCue(
                start=round(max(0.0, line_start - clip_start), 3),
                end=round(max(0.05, line_end - clip_start), 3),
                text=" ".join(line),
            ))
        line, line_start, line_end = [], None, 0.0

    for word in inside:
        candidate = len(" ".join(line + [word["word"]]))
        too_long = line and candidate > max_chars
        too_slow = line and line_start is not None and (word["end"] - line_start) > max_seconds
        if too_long or too_slow:
            flush()
        if line_start is None:
            line_start = max(word["start"], clip_start)
        line.append(word["word"])
        line_end = min(word["end"], window_end) if window_end != float("inf") else word["end"]
    flush()

    return cues


def _spread_words(words: list[str], start: float, end: float) -> list[dict]:
    """Lay words across a span, weighted by length.

    Local rather than imported so clipping does not depend on the narration
    pipeline's module graph; the weighting matches it deliberately.
    """
    span = max(end - start, 0.01)
    weights = [len(w) + 2 for w in words]
    total = sum(weights) or 1
    out: list[dict] = []
    cursor = start
    for word, weight in zip(words, weights):
        width = span * (weight / total)
        out.append({"word": word, "start": cursor, "end": min(cursor + width, end)})
        cursor += width
    return out


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    if millis == 1000:          # rounding can carry into the next second
        whole, millis = whole + 1, 0
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"


def cues_to_srt(cues: list[CaptionCue]) -> str:
    """SRT for a cue list. Empty string for no cues, never a malformed file."""
    blocks = []
    for index, cue in enumerate(cues, start=1):
        end = max(cue.end, cue.start + 0.05)
        blocks.append(
            f"{index}\n{_srt_time(cue.start)} --> {_srt_time(end)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


#: libass style per caption style. `MarginV` keeps the text clear of the lower
#: third where a face usually sits in a vertical crop.
_CAPTION_STYLE_ARGS = {
    "clean": "FontSize=17,Bold=1,Outline=2,Shadow=0,MarginV=120",
    "bold": "FontSize=21,Bold=1,Outline=3,Shadow=1,MarginV=140",
    "minimal": "FontSize=14,Bold=0,Outline=1,Shadow=0,MarginV=100",
}


def caption_filter(subtitle_path: Path, style: str) -> str:
    """The libass filter fragment for burning captions in.

    `subtitles` (libass) rather than `drawtext`: drawtext needs an explicit
    font file on this machine and silently renders nothing without one, and it
    cannot wrap or time a cue list on its own.
    """
    args = _CAPTION_STYLE_ARGS.get(style, _CAPTION_STYLE_ARGS[DEFAULT_CAPTION_STYLE])
    # Windows paths contain backslashes and a drive colon; both are filtergraph
    # syntax and must be escaped or the whole graph fails to parse.
    escaped = str(subtitle_path).replace("\\", "/").replace(":", r"\:")
    return f"subtitles='{escaped}':force_style='{args}'"


# ── focus ────────────────────────────────────────────────────────────

def focus_centre(
    source_path: Path,
    spec: ClipSpec,
    *,
    detector=None,
) -> float:
    """Horizontal crop centre (0..1) for a clip.

    `auto` asks the subject detector and falls back to centre when it is
    unavailable, errors, or returns something out of range. Detection being
    absent is a normal condition on a machine without the optional imaging
    dependencies -- it degrades the framing, it does not fail the clip.
    """
    mode = spec.focus if spec.focus in FOCUS_MODES else DEFAULT_FOCUS
    if mode == "center":
        return 0.5
    if mode == "left":
        return 0.25
    if mode == "right":
        return 0.75

    if detector is None:
        try:
            from viral.processing import detect_subject_centre as detector  # type: ignore
        except Exception:
            return 0.5

    try:
        # Sample inside the clip, not at the file's start: the subject at
        # 0:01 of a ten-minute video says nothing about a clip at 6:00.
        at = spec.start + min(1.0, max(0.0, spec.duration / 4))
        value = float(detector(source_path, at))
    except Exception as exc:  # noqa: BLE001 - framing must never be fatal
        logger.warning(f"[clipping] subject detection unavailable ({exc}); centring")
        return 0.5

    if not math.isfinite(value):
        return 0.5
    return max(0.0, min(1.0, value))


# ── the ffmpeg command for one clip ──────────────────────────────────

def build_clip_command(
    source_path: Path,
    output_path: Path,
    spec: ClipSpec,
    *,
    centre: float = 0.5,
    subtitle_path: Path | None = None,
    has_audio: bool = True,
) -> list[str]:
    """The exact invocation for one clip. Pure: builds, does not run."""
    aspect = ASPECTS.get(spec.aspect, ASPECTS[DEFAULT_ASPECT])
    quality = QUALITIES.get(spec.quality, QUALITIES[DEFAULT_QUALITY])
    centre = max(0.0, min(1.0, float(centre)))

    filters = [
        f"scale={aspect.width}:{aspect.height}:force_original_aspect_ratio=increase",
        f"crop={aspect.width}:{aspect.height}:"
        f"'min(max((in_w-out_w)*{centre:.4f},0),in_w-out_w)':'(in_h-out_h)/2'",
    ]
    if spec.captions and subtitle_path is not None:
        filters.append(caption_filter(subtitle_path, spec.caption_style))

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        # Seek before -i so the decoder starts at the cut rather than
        # decoding everything before it; -t is relative to that.
        "-ss", f"{spec.start:.3f}",
        "-i", str(source_path),
        "-t", f"{spec.duration:.3f}",
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", quality.preset,
        "-crf", str(quality.crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", quality.audio_bitrate, "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd.append(str(output_path))
    return cmd


# ── the batch ────────────────────────────────────────────────────────

@dataclass
class ClipResult:
    id: str
    status: str            # "done" | "failed" | "skipped"
    path: str = ""
    error: str = ""        # customer-safe
    attempts: int = 0

    def to_record(self) -> dict:
        return asdict(self)


def run_clip_batch(
    specs: list[ClipSpec],
    render,
    *,
    completed: dict[str, str] | None = None,
    max_attempts: int = MAX_ATTEMPTS_PER_CLIP,
) -> list[ClipResult]:
    """Render every clip, keeping whatever succeeds.

    `render(spec) -> path` does the work; injected so the batching, retry and
    resume behaviour can be tested without ffmpeg.

    `completed` maps clip id to an already-rendered path from an earlier
    attempt at this job. Those are returned as-is and `render` is never called
    for them, which is what makes a retry cheap instead of a second full bill.

    One clip's failure is contained: it is recorded and the loop continues.
    The caller decides what an all-failed batch means; this function does not
    raise for it, because "three of four worked" is a result, not an error.
    """
    done = dict(completed or {})
    results: list[ClipResult] = []

    for spec in specs:
        if spec.id in done and str(done[spec.id] or "").strip():
            results.append(ClipResult(spec.id, "skipped", path=done[spec.id], attempts=0))
            logger.info(f"[clipping] {spec.id}: already rendered, keeping it")
            continue

        attempts = 0
        last_error: Exception | None = None
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            attempts = attempt
            try:
                path = render(spec)
                if not path:
                    raise RuntimeError("renderer produced no file")
                results.append(ClipResult(spec.id, "done", path=str(path), attempts=attempt))
                last_error = None
                break
            except RightsError:
                # Never retried and never softened: a rights refusal is a
                # decision, not a transient fault.
                raise
            except Exception as exc:  # noqa: BLE001 - one clip, not the job
                last_error = exc
                logger.warning(
                    f"[clipping] {spec.id}: attempt {attempt} failed: "
                    f"{str(exc)[:200]}"
                )
        if last_error is not None:
            results.append(ClipResult(
                spec.id, "failed",
                error=safe_clip_error(last_error),
                attempts=attempts,
            ))

    kept = sum(1 for r in results if r.status in {"done", "skipped"})
    logger.info(
        f"[clipping] {kept}/{len(results)} clip(s) available "
        f"({sum(1 for r in results if r.status == 'skipped')} reused)"
    )
    return results


def batch_summary(results: list[ClipResult]) -> dict:
    """What the job row should record, and what the screen should say."""
    done = [r for r in results if r.status == "done"]
    reused = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]
    available = len(done) + len(reused)

    if not results:
        message = "No clips were created."
    elif not failed:
        message = f"{available} clip{'s' if available != 1 else ''} ready."
    elif available:
        message = (
            f"{available} clip{'s' if available != 1 else ''} ready. "
            f"{len(failed)} could not be created."
        )
    else:
        message = "No clips could be created from this video."

    return {
        "clips": [r.to_record() for r in results],
        "ready": available,
        "reused": len(reused),
        "failed": len(failed),
        # The job only fails when nothing survived; partial success is success.
        "ok": available > 0,
        "message": message,
    }
