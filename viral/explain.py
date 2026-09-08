"""Clip Analyzer: why a video worked, and how to make one like it.

This replaces Re-Create. That flow read a video and then built another one
from it, which is the part that is neither safe nor usually wanted: the rights
question is unresolved, and a machine-made near-copy is not what someone
studying a video is after. What they want is to understand it.

So this reads and explains, and stops. It produces no script, queues no
generation, and writes nothing into the factory. The last section is a plan a
person carries out themselves.

Depth follows the same rule as `viral/analysis.py`: an explanation of the
*visuals*, *pacing* and *audio* requires having actually looked at the video,
so those are only claimed when frames were sampled from a file the user holds
rights to. Where there is no file the explanation is honest about working from
public signals alone, rather than describing shots nobody saw.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from viral.analysis import (
    AnalysisDepth,
    depth_for,
    estimate_cuts_per_minute,
    sample_frames,
)
from viral.rights import RightsAttestation

logger = logging.getLogger("video_factory")

SYSTEM_INSTRUCTION = (
    "You are a short-form video analyst. You explain why a video held "
    "attention, in concrete terms tied to what is actually observable. You "
    "never invent a detail you were not shown: if you were given only public "
    "signals and no frames, you say what the signals support and no more. You "
    "do not write scripts and you do not reproduce the video's own wording."
)

#: The eight things the analysis has to answer, in the order a person reads
#: them. Kept here rather than inline in the prompt so the parser, the prompt
#: and the UI cannot drift apart.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("why_it_performed", "Why it performed well"),
    ("hook", "Hook"),
    ("structure", "Structure"),
    ("pacing", "Pacing"),
    ("visuals", "Visuals"),
    ("captions", "Captions"),
    ("audio", "Audio"),
    ("improvements", "What to improve"),
)


@dataclass
class Explanation:
    """One finished analysis, ready to render."""

    depth: str = AnalysisDepth.METADATA.value
    headline: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    plan: list[str] = field(default_factory=list)
    #: Measured, not judged. Absent where it could not be measured.
    cuts_per_minute: float | None = None
    frames_examined: int = 0
    #: What the analysis could not see, said plainly in the UI.
    limitations: str = ""

    def to_record(self) -> dict:
        return asdict(self)


def _fmt(value) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def build_prompt(video: dict, frame_times: list[float], *, saw_frames: bool) -> str:
    """The analysis request. Frame times are named so timings can be cited."""
    signals = "\n".join(
        f"- {label}: {_fmt(video.get(key))}"
        for key, label in (
            ("platform", "Platform"),
            ("title", "Title"),
            ("author", "Author"),
            ("duration_seconds", "Duration (seconds)"),
            ("views", "Views"),
            ("likes", "Likes"),
            ("comments", "Comments"),
            ("followers", "Author followers"),
        )
    )

    if saw_frames:
        seen = (
            f"<frames>\nYou are shown {len(frame_times)} frames sampled at "
            f"{', '.join(f'{t:.1f}s' for t in frame_times)}. Two of them are "
            f"inside the first two seconds, because that is where the hook "
            f"either lands or does not. Describe what you can see in them and "
            f"reason from that.\n</frames>\n\n"
        )
        scope = (
            "You have frames, so you may describe framing, subject, colour, "
            "on-screen text and how the shot changes across the samples. You "
            "still cannot hear the video: treat audio as inference from what "
            "is visible (a person mid-speech, on-screen captions, a musical "
            "performance) and say so."
        )
    else:
        seen = ""
        scope = (
            "You have NOT seen the video. You have only the public signals "
            "above. For visuals, pacing, captions and audio, say what the "
            "signals and the title imply and state clearly that it could not "
            "be observed. Do not describe shots, colours or edits as if you "
            "had watched it."
        )

    fields = "\n".join(f'    "{key}": "…",' for key, _ in SECTIONS)

    return (
        f"<task>\nExplain why this short video performed the way it did, and "
        f"how someone could make a video like it.\n</task>\n\n"
        f"<signals>\n{signals}\n</signals>\n\n"
        f"{seen}"
        f"<scope>\n{scope}\n</scope>\n\n"
        f"<rules>\n"
        f"- Be concrete. 'Fast cuts hold attention' is worthless; 'the subject "
        f"is already mid-sentence in the first frame, so there is no intro to "
        f"skip' is useful.\n"
        f"- Every section is 2-4 sentences of prose. No bullet lists inside a "
        f"section.\n"
        f"- 'improvements' is about this video, not about videos in general.\n"
        f"- 'plan' is 5 to 8 numbered steps someone follows to make their own "
        f"video in this style. Each step is one action. Do not write the "
        f"script for them and do not reproduce this video's wording.\n"
        f"- Do not claim a metric you were not given.\n"
        f"</rules>\n\n"
        f"Return JSON:\n"
        f"{{\n"
        f'  "headline": "one sentence on what actually made it work",\n'
        f"  \"sections\": {{\n{fields}\n  }},\n"
        f'  "plan": ["step 1", "step 2", "…"],\n'
        f'  "limitations": "what you could not assess, in one sentence"\n'
        f"}}"
    )


def _parse(payload: dict, *, depth: AnalysisDepth, frames: int) -> Explanation:
    """Shape whatever came back into an Explanation, dropping what is missing."""
    raw_sections = payload.get("sections")
    sections: dict[str, str] = {}
    if isinstance(raw_sections, dict):
        for key, _ in SECTIONS:
            text = str(raw_sections.get(key) or "").strip()
            if text:
                sections[key] = text

    plan_raw = payload.get("plan")
    plan = (
        [str(step).strip() for step in plan_raw if str(step).strip()]
        if isinstance(plan_raw, list)
        else []
    )

    return Explanation(
        depth=depth.value,
        headline=str(payload.get("headline") or "").strip(),
        sections=sections,
        plan=plan,
        frames_examined=frames,
        limitations=str(payload.get("limitations") or "").strip(),
    )


async def explain(
    video: dict,
    *,
    attestation: RightsAttestation | None = None,
    source_path: Path | None = None,
    model_call=None,
    vision_call=None,
) -> Explanation:
    """Analyse one video and explain it, at the depth its rights allow.

    `model_call` and `vision_call` are injected so this is testable without a
    model; they default to the project's existing Gemini clients.
    """
    depth = depth_for(attestation)
    duration = float(video.get("duration_seconds") or 0.0)

    frames: list[tuple[float, Path]] = []
    if depth is AnalysisDepth.FULL and source_path and Path(source_path).exists():
        frames = sample_frames(Path(source_path), duration)
        if not frames:
            logger.info("no frames could be sampled; explaining at metadata depth")
            depth = AnalysisDepth.METADATA
    elif depth is AnalysisDepth.FULL:
        # Rights allow it but there is no file to look at.
        depth = AnalysisDepth.METADATA

    saw_frames = bool(frames)
    prompt = build_prompt(video, [t for t, _ in frames], saw_frames=saw_frames)

    if saw_frames:
        if vision_call is None:
            import clients

            vision_call = clients.review_with_vision
        payload = await vision_call(
            prompt,
            [p for _, p in frames],
            system_instruction=SYSTEM_INSTRUCTION,
            operation_label="clip_analyzer_frames",
        )
    else:
        if model_call is None:
            import clients

            model_call = clients.generate_json
        payload = await model_call(
            prompt,
            system_instruction=SYSTEM_INSTRUCTION,
            operation_label="clip_analyzer_metadata",
        )

    result = _parse(payload or {}, depth=depth, frames=len(frames))

    # Cut rate is measured from the file, never asked of the model: it is the
    # one pacing number that can be wrong in a way nobody would notice.
    if saw_frames and source_path:
        result.cuts_per_minute = estimate_cuts_per_minute(Path(source_path), duration)

    if not result.limitations and not saw_frames:
        result.limitations = (
            "The video file was not available, so visuals, pacing, captions "
            "and audio could not be observed directly."
        )
    return result
