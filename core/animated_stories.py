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
    #: "local" when Remotion animates the still for free -- the normal case;
    #: "clip" once a paid image-to-video file exists; "still" only when even
    #: local animation could not be planned. Never "clip" without a file.
    kind: str = "still"
    source_image: str = ""
    clip_path: str = ""
    animation_provider: str = ""
    animation_model: str = ""
    generation_status: str = "pending"
    attempts: int = 0
    error: str = ""
    #: The free recipe Remotion renders. Present on every scene, including
    #: ones that were later escalated, so a failed paid clip can fall back to
    #: local motion rather than to a frozen frame.
    local_motion: dict = field(default_factory=dict)
    #: Why this beat was or was not escalated to the paid model.
    escalation_reason: str = ""

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
    max_ai_clips: int = 1,
    allow_ai: bool = True,
) -> list[AnimatedScene]:
    """Give every scene local motion, and escalate only what needs it.

    The default outcome for a beat is `local`: Remotion animates the still for
    nothing. A beat is escalated to the paid model only when
    `local_motion.needs_ai_motion` says the movement is articulated -- limbs
    moving relative to each other, which no camera transform reproduces -- and
    then only while both `max_ai_clips` and the budget allow it.

    `allow_ai=False` is the provider-unavailable path. It is not an error:
    every scene still animates, the video still ships, and the manifest says
    why nothing was escalated.
    """
    from core.local_motion import needs_ai_motion, plan_local_motion

    # 1. Everything animates locally first. Nothing below can take this away;
    #    an escalated scene keeps its recipe so a failed clip falls back to
    #    motion rather than to a frozen frame.
    for scene in scenes:
        scene.local_motion = plan_local_motion(
            scene.beat, scene.index, seconds=scene.duration
        ).to_record()
        scene.kind = "local"
        scene.generation_status = "local"

    # 2. Candidates: beats whose motion local compositing cannot fake, and
    #    that are long enough for a clip to be worth generating.
    candidates = []
    for scene in scenes:
        needs, reason = needs_ai_motion(scene.beat)
        scene.escalation_reason = reason
        if not needs:
            continue
        if not (min_clip_seconds <= scene.duration <= max_clip_seconds):
            scene.escalation_reason = (
                f"{reason}, but {scene.duration:.1f}s is outside the clip window"
            )
            continue
        candidates.append(scene)

    if not allow_ai:
        for scene in candidates:
            scene.escalation_reason = (
                f"{scene.escalation_reason}; animated locally "
                "(paid model unavailable)"
            )
        budget.skip(f"{len(candidates)} candidate(s): paid model unavailable")
        candidates = []

    # Hardest motion first, then story order so the choice is deterministic.
    candidates.sort(key=lambda s: (-s.duration, s.index))

    escalated = 0
    for scene in candidates:
        if escalated >= max_ai_clips:
            scene.escalation_reason = (
                f"{scene.escalation_reason}; animated locally "
                f"(cap of {max_ai_clips} paid clip(s) reached)"
            )
            budget.skip(f"scene {scene.index}: paid clip cap reached")
            continue
        if not budget.can_afford_one():
            scene.escalation_reason = (
                f"{scene.escalation_reason}; animated locally "
                f"(${budget.remaining_usd:.3f} left)"
            )
            budget.skip(
                f"scene {scene.index}: AI motion budget exhausted "
                f"(${budget.remaining_usd:.3f} left)"
            )
            continue
        # Charge as the scene is committed, not after it is generated.
        budget.charge()
        scene.kind = "clip"
        scene.generation_status = "pending"
        escalated += 1

    logger.info(
        f"Animated Stories plan: {len(scenes)} scene(s), "
        f"{len(scenes) - escalated} animated locally at $0, "
        f"{escalated} escalated to the paid model "
        f"(cap {max_ai_clips}, ${budget.ceiling_usd:.2f} budget)"
    )
    return scenes


def timeline_record(scenes: list[AnimatedScene], budget: AnimationBudget) -> dict:
    """The run's animation manifest, written beside the other provenance."""
    return {
        "scenes": [s.to_record() for s in scenes],
        "total_scenes": len(scenes),
        #: Paid image-to-video clips only.
        "animated_scenes": sum(1 for s in scenes if s.kind == "clip"),
        #: Free Remotion-animated scenes -- normally all of them.
        "local_scenes": sum(1 for s in scenes if s.kind == "local"),
        #: Neither animated nor escalated. Should be zero.
        "still_scenes": sum(1 for s in scenes if s.kind == "still"),
        "narration_seconds": round(
            max((s.end_time for s in scenes), default=0.0), 3
        ),
        **budget.summary(),
    }
