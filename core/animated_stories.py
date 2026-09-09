"""Animated Stories: stick-figure scenes, animated, on the narration's clock.

A fourth generation path, additive to Football, Horror and True Stories. None
of those three reach anything in this module and nothing here reads their
config: an animated story is a different product, not a mode of an existing
one.

The shape of a run:

    topic -> script -> beats -> character sheet -> scene stills -> clips

The character sheet is what makes it a series rather than twelve unrelated
drawings. One reference image is generated first and every scene prompt
restates the same character in the same words, because a text-to-image model
given "a stick figure" twice will draw two different people.

Budget is a hard input, not a report. Image-to-video is the expensive part by
an order of magnitude -- stills cost cents, clips cost dimes -- so the planner
decides up front how many beats can be animated inside the ceiling and holds
the still for the rest. That degrades a run instead of overspending it, and it
is why `plan_scenes` takes a budget and returns some scenes marked `still`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

logger = logging.getLogger("video_factory")

# ── the house style ──────────────────────────────────────────────────
#
# Stated once, here, and appended verbatim to every prompt. Scattering these
# words across prompt templates is how a character drifts between scenes.

STICK_FIGURE_STYLE = (
    "Stick figure animation style: large round white head, expressive cartoon "
    "eyes with visible eyebrows and an expressive mouth, thin simple "
    "stick-figure limbs and torso, colourful thematic clothing. Cinematic "
    "cartoon environment with a rich detailed background, dramatic lighting, "
    "saturated colours, clean vector-like line work, professional animated "
    "series look. Full body visible. No text, no watermark, no signature."
)

#: What a scene still must never be. Kept separate from the style so it can be
#: asserted independently in tests.
STYLE_EXCLUSIONS = (
    "Not photorealistic. No live action. No 3D render. No realistic human "
    "faces. No photographic texture."
)


def character_sheet_prompt(character: str) -> str:
    """The one reference image every later scene is matched against.

    A sheet rather than a portrait: several angles and expressions in one
    frame give the scene prompts something specific to restate, and give a
    reviewer something to compare a scene against.
    """
    subject = " ".join(str(character or "").split()) or "the main character"
    return (
        f"Character reference sheet for {subject}. "
        "Three views of the SAME character side by side on a plain neutral "
        "background: front view, three-quarter view, and side view, plus two "
        "small head close-ups showing a neutral expression and a frightened "
        "expression. Identical head shape, identical clothing, identical "
        "colours and identical proportions in every view. "
        f"{STICK_FIGURE_STYLE} {STYLE_EXCLUSIONS}"
    )


def scene_prompt(beat: str, *, character: str, setting: str = "") -> str:
    """One scene still, drawn as the same character as the sheet."""
    action = " ".join(str(beat or "").split())
    subject = " ".join(str(character or "").split()) or "the main character"
    where = f" {setting.strip()}" if setting and setting.strip() else ""
    return (
        f"{action}{where}. "
        f"The character is {subject} -- the SAME character as the reference "
        "sheet: same head shape, same face style, same clothing, same colours, "
        "same proportions, same accessories. Do not redesign the character. "
        f"{STICK_FIGURE_STYLE} {STYLE_EXCLUSIONS}"
    )


def motion_prompt(beat: str) -> str:
    """What actually moves in this scene.

    Derived from the beat rather than invented, because motion that does not
    match the narration is worse than a held still: the viewer sees the
    character do something the story never said.
    """
    action = " ".join(str(beat or "").split())
    return (
        f"Animate this scene: {action}. Real character and environment "
        "movement -- the character walks, turns, reaches, reacts or moves as "
        "the action describes, and background elements move naturally with it."
    )


# ── scenes on the narration's clock ──────────────────────────────────


@dataclass
class AnimatedScene:
    """One beat of the story, and what was made for it."""

    index: int
    section_id: int
    beat: str
    start_time: float
    end_time: float
    #: "clip" once animated, "still" when the budget or a failure left the
    #: scene as its source image. Never "clip" without a file.
    kind: str = "still"
    source_image: str = ""
    clip_path: str = ""
    animation_provider: str = ""
    animation_model: str = ""
    generation_status: str = "pending"
    attempts: int = 0
    error: str = ""

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end_time - self.start_time), 3)

    def to_record(self) -> dict:
        record = asdict(self)
        record["duration"] = self.duration
        return record


@dataclass
class AnimationBudget:
    """How much of this run may be spent turning stills into clips.

    `remaining` is checked before each generation, not after: a run must not
    discover it is over budget by going over it.
    """

    ceiling_usd: float
    cost_per_clip_usd: float
    spent_usd: float = 0.0
    clips: int = 0
    retries: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, round(self.ceiling_usd - self.spent_usd, 6))

    @property
    def affordable_clips(self) -> int:
        if self.cost_per_clip_usd <= 0:
            return 0
        return int(self.remaining_usd // self.cost_per_clip_usd)

    def can_afford_one(self) -> bool:
        return self.affordable_clips >= 1

    def charge(self, *, retry: bool = False) -> None:
        self.spent_usd = round(self.spent_usd + self.cost_per_clip_usd, 6)
        if retry:
            self.retries += 1
        else:
            self.clips += 1

    def skip(self, reason: str) -> None:
        self.skipped.append(reason)

    def summary(self) -> dict:
        return {
            "ceiling_usd": round(self.ceiling_usd, 4),
            "cost_per_clip_usd": round(self.cost_per_clip_usd, 4),
            "animation_spend_usd": round(self.spent_usd, 4),
            "clips": self.clips,
            "retries": self.retries,
            "skipped": len(self.skipped),
            "skipped_reasons": self.skipped[:10],
        }


def scenes_from_script(script) -> list[AnimatedScene]:
    """One scene per visible beat, timed from the narration itself.

    Uses the section's measured word timestamps where audio has run, so a
    scene's span is the span of the words it illustrates. Falls back to an
    even split of the section only when there are no timings yet, which is the
    planning pass before narration exists.
    """
    scenes: list[AnimatedScene] = []
    index = 0
    clock = 0.0

    for section in getattr(script, "sections", []) or []:
        slots = [
            s for s in (getattr(section, "slots", []) or [])
            if getattr(s, "visual", "") != "text_overlay"
        ]
        if not slots:
            continue
        duration = float(
            getattr(section, "actual_duration_seconds", 0)
            or getattr(section, "estimated_duration_seconds", 0)
            or 0.0
        )
        words = getattr(section, "word_timestamps", None) or []
        spans = _spans_for(slots, duration, words, clock)

        for slot, (start, end) in zip(slots, spans):
            index += 1
            scenes.append(AnimatedScene(
                index=index,
                section_id=int(getattr(section, "id", 0) or 0),
                beat=str(getattr(slot, "prompt", "") or getattr(slot, "keywords", "")),
                start_time=round(start, 3),
                end_time=round(end, 3),
            ))
        clock += duration

    return scenes


def _spans_for(slots, duration, words, offset):
    """(start, end) per slot, from measured words when available."""
    count = len(slots)
    if count == 0:
        return []
    if not words or len(words) < count:
        step = duration / count if count else duration
        return [
            (offset + i * step, offset + (i + 1) * step) for i in range(count)
        ]

    # Split the section's words evenly by count, then take each chunk's own
    # measured start and end. The narration is the clock.
    per = max(1, len(words) // count)
    spans = []
    for i in range(count):
        chunk = words[i * per: (i + 1) * per] if i < count - 1 else words[i * per:]
        if not chunk:
            last = spans[-1][1] if spans else offset
            spans.append((last, last))
            continue
        start = offset + float(chunk[0].get("start", 0.0) or 0.0)
        end = offset + float(chunk[-1].get("end", 0.0) or 0.0)
        spans.append((start, max(end, start)))
    return spans


def plan_scenes(
    scenes: list[AnimatedScene],
    budget: AnimationBudget,
    *,
    min_clip_seconds: float = 3.0,
    max_clip_seconds: float = 6.0,
) -> list[AnimatedScene]:
    """Decide which scenes are animated, inside the budget.

    Longest beats first. A five-second beat carries motion; a one-second beat
    animated is a flicker that costs the same. Everything unaffordable stays a
    still, which is a complete scene rather than a missing one.
    """
    eligible = [
        s for s in scenes
        if min_clip_seconds <= s.duration <= max_clip_seconds
    ]
    # Longest first, then in story order so the choice is deterministic.
    eligible.sort(key=lambda s: (-s.duration, s.index))

    for scene in eligible:
        if not budget.can_afford_one():
            budget.skip(
                f"scene {scene.index}: animation budget exhausted "
                f"(${budget.remaining_usd:.3f} left)"
            )
            continue
        # Charge as the scene is committed, not after it is generated.
        # Planning without charging left the budget permanently affordable and
        # every eligible beat was marked for animation -- the ceiling existed
        # and bounded nothing.
        budget.charge()
        scene.kind = "clip"

    animated = sum(1 for s in scenes if s.kind == "clip")
    logger.info(
        f"Animated Stories plan: {len(scenes)} scene(s), {animated} to animate "
        f"within ${budget.ceiling_usd:.2f} "
        f"(${budget.cost_per_clip_usd:.2f}/clip), "
        f"{len(scenes) - animated} held as stills"
    )
    return scenes


def timeline_record(scenes: list[AnimatedScene], budget: AnimationBudget) -> dict:
    """The run's animation manifest, written beside the other provenance."""
    return {
        "scenes": [s.to_record() for s in scenes],
        "total_scenes": len(scenes),
        "animated_scenes": sum(1 for s in scenes if s.kind == "clip"),
        "still_scenes": sum(1 for s in scenes if s.kind != "clip"),
        "narration_seconds": round(
            max((s.end_time for s in scenes), default=0.0), 3
        ),
        **budget.summary(),
    }
