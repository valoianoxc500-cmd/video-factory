"""Shared helpers -- logging, file ops, channel config loading."""

import json
import logging
import re
import math
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from rich.console import Console

# Force UTF-8 output on Windows to avoid cp1252 encoding errors
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from rich.logging import RichHandler

from settings import (
    CHANNELS_DIR,
    LOGS_DIR,
    WORKSPACE_DIR,
    DATA_DIR,
)

console = Console()




# Shared deterministic media and timing helpers.
MIN_SOURCE_BASELINE = (1280, 720)
MIN_SOURCE_SCALE = 2 / 3


def minimum_source_size(target_size: tuple[int, int] | list[int]) -> tuple[int, int]:
    """Return the minimum source dimensions for a target render size."""
    target_w, target_h = int(target_size[0]), int(target_size[1])
    min_w = min(target_w, max(MIN_SOURCE_BASELINE[0], int(target_w * MIN_SOURCE_SCALE)))
    min_h = min(target_h, max(MIN_SOURCE_BASELINE[1], int(target_h * MIN_SOURCE_SCALE)))
    return min_w, min_h


def meets_minimum_source_size(
    width: int,
    height: int,
    target_size: tuple[int, int] | list[int],
) -> bool:
    """Return whether an image is large enough to upscale into the target video."""
    min_w, min_h = minimum_source_size(target_size)
    return width >= min_w and height >= min_h


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


def normalized_tokens(text: str) -> list[str]:
    return [token for token in normalized_text(text).split() if token]


_DURATION_MIN_RATIO = 0.90
_PRODUCTION_DURATION_MAX_RATIO = 1.25
_PRODUCTION_DURATION_MAX_EXTRA_SECONDS = 45
_PREVIEW_DURATION_MAX_RATIO = 1.50
_PREVIEW_DURATION_MAX_EXTRA_SECONDS = 60


@dataclass(frozen=True)
class ScriptTimingProfile:
    model_name: str
    words_per_minute: int
    section_overhead_seconds: float

    @property
    def estimator_label(self) -> str:
        return (
            f"{self.words_per_minute} WPM + "
            f"{self.section_overhead_seconds:.1f}s per section ({self.model_name})"
        )


def script_timing_profile(tts_model: str) -> ScriptTimingProfile:
    """Return the script-timing profile for the active TTS model."""
    normalized = tts_model.lower()
    if "flash" in normalized:
        return ScriptTimingProfile(
            model_name=tts_model,
            words_per_minute=128,
            section_overhead_seconds=3.0,
        )
    if "pro" in normalized:
        return ScriptTimingProfile(
            model_name=tts_model,
            words_per_minute=118,
            section_overhead_seconds=3.5,
        )
    return ScriptTimingProfile(
        model_name=tts_model,
        words_per_minute=118,
        section_overhead_seconds=3.5,
    )


def estimate_script_seconds(
    total_words: int,
    *,
    section_count: int,
    profile: ScriptTimingProfile,
) -> int:
    """Estimate full spoken duration from words plus section pacing overhead."""
    word_seconds = (total_words / profile.words_per_minute) * 60 if total_words else 0.0
    estimate = word_seconds + max(section_count, 0) * profile.section_overhead_seconds
    return int(round(estimate))


def estimate_section_seconds(
    word_count: int,
    *,
    profile: ScriptTimingProfile,
) -> float:
    """Estimate section duration from words plus one section of pacing overhead."""
    word_seconds = (word_count / profile.words_per_minute) * 60 if word_count else 0.0
    return word_seconds + profile.section_overhead_seconds


def words_for_seconds(
    seconds: float,
    *,
    section_count: float,
    profile: ScriptTimingProfile,
) -> float:
    """Invert the estimator to derive a word budget for a target duration."""
    available_seconds = max(
        0.0,
        seconds - max(section_count, 0.0) * profile.section_overhead_seconds,
    )
    return available_seconds / 60 * profile.words_per_minute


def duration_window(
    target_seconds: int,
    *,
    preview_mode: bool,
) -> tuple[float, float]:
    """Return the allowed script-duration window for the target."""
    min_duration = target_seconds * _DURATION_MIN_RATIO
    if preview_mode:
        max_duration = max(
            target_seconds * _PREVIEW_DURATION_MAX_RATIO,
            target_seconds + _PREVIEW_DURATION_MAX_EXTRA_SECONDS,
        )
    else:
        max_duration = max(
            target_seconds * _PRODUCTION_DURATION_MAX_RATIO,
            target_seconds + _PRODUCTION_DURATION_MAX_EXTRA_SECONDS,
        )
    return min_duration, max_duration

# ── Pydantic models for validated config ──────────────────────────

class NicheConfig(BaseModel):
    category: str
    focus: str
    audience: str
    content_style: str
    example_topics: list[str] = []
    avoid_topics: list[str] = []


class VideoConfig(BaseModel):
    target_duration_minutes: int = 20
    resolution: list[int] = [1920, 1080]
    fps: int = 30
    transition_duration_seconds: float = 0.8
    background_music_volume: float = 0.15
    music_pool: list[str] = []
    # Transition SFX stems this channel may use. Empty keeps the historical
    # assets/sfx/transitions/ set, so channel-specific one-shots added for one
    # engine cannot appear in another engine's videos.
    sfx_pool: list[str] = []
    # Filenames (without .mp3) to restrict music selection for this channel.
    # Empty = use all tracks in assets/music/.
    visual_guidance: dict[str, str] = {}
    include_intro: bool = False
    include_outro: bool = False


class VideoTypeConfig(BaseModel):
    enabled: bool = True
    sections_range: list[int] | None = None
    section_style: str = ""
    pacing: str = ""
    example: str = ""
    narrative_hooks: list[str] = []
    numbering_order: Literal["ascending", "descending"] | None = None
    allowed_thumbnail_strategies: list[str] = []


def compute_sections_range(
    target_minutes: float,
    vt_config: VideoTypeConfig,
) -> list[int]:
    """Derive min/max section count from target duration and video-type config."""
    # Keep long-form videos in a sane section range unless the channel config
    # explicitly overrides it.
    auto_min = max(2, min(8, int(target_minutes * 60 / 75)))
    auto_max = max(auto_min + 1, min(12, int(target_minutes * 60 / 50)))
    return vt_config.sections_range or [auto_min, auto_max]


class VoiceConfig(BaseModel):
    provider: str = "gemini_2.5_tts"
    language: str = "en-US"
    voice_name: str = "Charon"
    voice_prompt: str = ""
    voice_prompt_variations: list[str] = []
    # ElevenLabs only. The voice *id*, not its display name: names are neither
    # unique nor stable across a rename, so resolving one at request time
    # would let the narrator change without this config changing. Empty means
    # the channel narrates on Gemini TTS regardless of `provider`.
    voice_id: str = ""


class ImageSourcingConfig(BaseModel):
    generation_model: str = "gemini-3.1-flash-image-preview"
    # News/documentary channels must show real photographs of the actual
    # people and events. When true, every photo-lane slot is sourced from web
    # image search and AI image generation is never used as a fallback.
    web_photos_only: bool = False
    # Ask Pixabay, Unsplash and Wikimedia Commons for a beat the configured
    # source could not fill, before falling back to a generated image.
    #
    # Off by default so no channel gains three new upstreams by upgrading, and
    # so a test run never reaches for the network. The channels that want more
    # real photographs opt in. Each catalogue carries its own licence, and the
    # candidate's licence and credit are recorded with it.
    open_library_fallback: bool = False
    # How many beats in one run may fall through to the open-library tier.
    #
    # Sized as a rescue: a handful of beats the primary search missed. It has
    # to be raised when the primary search is unavailable -- with Serper out
    # of credits these catalogues stop being a rescue and become the only
    # source of real photographs, and a cap of six then fails the run.
    open_library_budget: int = 6
    # When the relevance gate rejects a sourced photograph, generate a
    # purpose-built image from that beat's own brief instead of hunting for
    # another stock photo.
    #
    # This is a fallback policy, not a way past the gate: the rejected file is
    # discarded, the generated one faces the same reviewer, and a generated
    # image that also fails is regenerated rather than accepted. It exists
    # because some briefs -- "a poster tacked to a decaying wall", "a wall
    # clock" -- have no match in any licensed catalogue, and the alternative
    # was a run that could never go green.
    #
    # Off by default. A documentary channel should fail rather than illustrate;
    # a fiction channel has no such duty.
    # Target model spend for one finished video, in USD. The guard declines
    # optional paid work as this is approached; it never skips a validator or
    # a stage the video cannot be finished without.
    cost_budget_usd: float = 0.10
    generate_when_rejected: bool = False
    # Ceiling on that. A run needing more than this has a sourcing problem
    # that generation should not paper over.
    max_generated_when_rejected: int = 8
    # Per-scene ambience and sound cues, sourced from Freesound and generated
    # by ElevenLabs when no recording fits. Adds two mixed layers alongside
    # the music bed and transition track, which are unchanged.
    #
    # Off by default: it costs API calls and a little generation credit, so a
    # channel opts in rather than inheriting it.
    scene_audio: bool = False
    # Last-resort cover for a run that would otherwise be stopped.
    #
    # `web_photos_only` still governs ordinary sourcing: every photo-lane slot
    # is searched for on the web, and a real photograph always wins. This only
    # applies after search AND subject rescue have both failed and the run is
    # about to be abandoned for want of images -- at which point a clearly
    # non-documentary illustration for the few missing beats is better than no
    # video. Generated frames are recorded as generated in the provenance log
    # so nothing downstream can mistake one for evidence.
    # Short licensed stock clips mixed among the photographs, so a story has
    # motion as well as stills. Off by default so no channel gains video
    # b-roll by upgrading; the two channels that want it opt in.
    #
    # This is a ceiling on how much of a video may be motion footage, not a
    # target. Photographs still carry the facts -- b-roll is atmosphere, and
    # the review gate holds it to the scene rather than the topic.
    # Finish the video rather than abandon it.
    #
    # When a section still lacks photographs after every sourcing tier, the
    # default is to stop the run: for a news channel a thin video is worse
    # than no video. A storytelling channel trades the other way -- a beat
    # carried by a licensed clip or a text card is still the story, and losing
    # eight minutes of narration and rendering to two unfindable photographs
    # is not.
    #
    # What this never does: reuse an image across unrelated beats, invent
    # evidence, or relax the relevance gate. It changes what happens to a beat
    # nothing could be found for, not the standard for finding it.
    complete_over_coverage: bool = False
    allow_video_broll: bool = False
    max_broll_ratio: float = 0.34
    allow_generated_fallback: bool = False
    # Hard ceiling per video. A run needing more than this is not short of
    # images, it is short of a subject that can be illustrated honestly.
    max_generated_fallback_images: int = 5
    generated_fallback_model: str = "gemini-3.1-flash-lite-image"
    generate_info_slide_illustrations: bool = True
    generate_info_card_illustrations: bool = True
    style_prompt_suffix: str = ""
    illustration_style_prompt_suffix: str = ""


class MetadataVariant(BaseModel):
    name: str
    instruction: str


class YouTubeConfig(BaseModel):
    category_id: str = "22"
    tags: list[str]
    title_formats: list[MetadataVariant]
    description_styles: list[MetadataVariant]


class ScriptStyleConfig(BaseModel):
    tone: str = ""
    instructions: str = ""


class ContentFamilyConfig(BaseModel):
    name: str
    planning_focus: str
    example_topics: list[str] = []
    lead_magnet: str = ""
    low_ticket_offer: str = ""
    cta_angle: str = ""

    @field_validator("name", "planning_focus")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content family fields must be non-empty")
        return value

    @field_validator("lead_magnet", "low_ticket_offer", "cta_angle")
    @classmethod
    def _optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("example_topics")
    @classmethod
    def _nonempty_examples(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("content family example_topics must contain at least one topic")
        for topic in value:
            if not topic.strip():
                raise ValueError("content family example_topics must be non-empty strings")
        return value


class BusinessStrategyConfig(BaseModel):
    channel_goal: str
    video_jobs: list[str]
    content_families: list[ContentFamilyConfig]
    mid_ticket_offer: str = ""
    later_offers: list[str] = []
    cta_rules: list[str] = []
    launch_strategy: str = ""

    @field_validator("channel_goal", "launch_strategy")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("business strategy text fields must be non-empty")
        return value

    @field_validator("mid_ticket_offer")
    @classmethod
    def _optional_offer_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("video_jobs", "later_offers", "cta_rules")
    @classmethod
    def _nonempty_string_lists(cls, value: list[str], info) -> list[str]:
        if info.field_name == "video_jobs" and not value:
            raise ValueError("business strategy video_jobs must not be empty")
        for item in value:
            if not item.strip():
                raise ValueError(f"business strategy {info.field_name} entries must be non-empty")
        return value

    @field_validator("content_families", mode="before")
    @classmethod
    def _validate_content_families(cls, value):
        if not value:
            raise ValueError("business strategy must define at least one content family")
        return value

    @model_validator(mode="after")
    def _unique_family_names(self):
        names = [family.name for family in self.content_families]
        if len(names) != len(set(names)):
            raise ValueError("business strategy content family names must be unique")
        return self


class ReviewThresholds(BaseModel):
    script_min_score: float = 7.0
    script_max_attempts: int = 3
    image_review_max_attempts: int = 2
    thumbnail_max_attempts: int = 2
    final_review_max_attempts: int = 1


class ThumbnailStrategyConfig(BaseModel):
    name: str
    instruction: str
    reference_image: str | None = None
    reference_instruction: str | None = None

    @field_validator("name", "instruction", "reference_image", "reference_instruction")
    @classmethod
    def _nonempty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("thumbnail strategy fields must be non-empty")
        return v

    @model_validator(mode="after")
    def _reference_instruction_required(self):
        if self.reference_image and not self.reference_instruction:
            raise ValueError("reference_instruction is required when reference_image is set")
        return self


class TemplateStyle(BaseModel):
    name: str = ""
    effects: dict[str, Any] = {}
    text: dict[str, Any] = {}
    subtitle_highlight: dict[str, Any] = {}
    video: dict[str, Any] = {}
    watermark: dict[str, Any] = {}
    # Publish a 9:16 thumbnail instead of 16:9. On a vertical short a landscape
    # thumbnail is the only landscape image in the package and the final review
    # gate blocks on it. Opt-in, so an existing channel keeps its thumbnails.
    vertical_thumbnail: bool = False


KNOWN_FORCES = {"TitleCard", "FactHighlight"}


class TestConfig(BaseModel):
    force: list[str] = []
    preview_ai_image_prompts: bool = False

    @field_validator("force", mode="before")
    @classmethod
    def _parse_csv(cls, v):
        if isinstance(v, str):
            items = [x.strip() for x in v.split(",") if x.strip()]
            unknown = set(items) - KNOWN_FORCES
            if unknown:
                raise ValueError(f"Unknown test.force values: {unknown}")
            return items
        return v


class RenderingDefaults(BaseModel):
    component_min_duration: float = 5.0
    # Visual pacing. These two bound how long one image owns the screen, and
    # together they decide how many images a section needs: the scripter's
    # validator demands at least minimum_visual_slots_for_duration() slots,
    # and compute_sub_durations() then snaps the changes to pauses in the
    # narration. At a 16s hold a single photo covered half a section and the
    # video stopped illustrating what was being said; ~5s is roughly one
    # sentence of narration, which is the beat the images should follow.
    image_slot_min_duration: float = 2.5
    max_visual_hold_seconds: float = 5.0
    intra_slot_crossfade: float = 0.3
    sfx_volume: float = 0.15
    sfx_boundary_coverage: float = 0.70
    render_concurrency: int = 4
    frame_sequence_image_format: Literal["jpeg", "png"] = "jpeg"
    frame_sequence_jpeg_quality: int = Field(default=80, ge=0, le=100)


class MatchFootageConfig(BaseModel):
    """Footage policy for a match-analysis engine.

    Defaults are the safe ones: no footage directory and no stock, which makes
    the engine a pure photo pipeline until an operator opts in.
    """

    enabled: bool = False
    # Directory of operator-supplied match video. Empty means none, and the
    # engine falls back to the existing photo system.
    footage_dir: str = ""
    allow_licensed_stock: bool = False
    lead_in_seconds: float = 5.0
    lead_out_seconds: float = 8.0
    min_clip_seconds: float = 10.0
    max_clip_seconds: float = 15.0
    max_moments: int = 6

    def get(self, key: str, default=None):
        """dict-style access, so callers can stay agnostic about the type."""
        return getattr(self, key, default)


class ChannelConfig(BaseModel):
    channel_name: str
    channel_id: str = ""
    language: str = "en"
    # The caption track's language, when it is not the narrator's. Empty (the
    # default) means captions come straight from the narration's own STT word
    # timings, which is what every channel did before this existed and is the
    # only configuration where every word timing is a measurement rather than
    # an interpolation. See core/caption_language.py.
    caption_language: str = ""
    content_mode: str = "visual"  # "visual" | "data_graphics" | "hybrid"
    niche: NicheConfig
    video: VideoConfig = VideoConfig()
    video_types: dict[str, VideoTypeConfig] = {}
    voice: VoiceConfig = VoiceConfig()
    image_sourcing: ImageSourcingConfig = ImageSourcingConfig()
    youtube: YouTubeConfig
    script_style: ScriptStyleConfig = ScriptStyleConfig()
    business_strategy: BusinessStrategyConfig | None = None
    review_thresholds: ReviewThresholds = ReviewThresholds()
    thumbnail_strategies: list[ThumbnailStrategyConfig]
    rendering_defaults: RenderingDefaults = RenderingDefaults()
    # Per-language overrides for channels the viewer can switch. Each entry is
    # a partial ChannelConfig merged over the base by `apply_language_variant`,
    # so a channel with no variants behaves exactly as before.
    language_variants: dict[str, dict[str, Any]] = {}
    # Match Analysis footage layer. Absent on every other engine, which is how
    # the pipeline knows not to run match identification or clip cutting for
    # them. See core/footage.py for the sourcing rules this config drives.
    match_footage: MatchFootageConfig | None = None
    # Closing question for channels that end on one. The narration's last
    # sentence must be a short, story-specific question that invites an
    # opinion; this value is the wording used when the script does not supply
    # one, so the beat can never simply be missing. Empty disables the whole
    # behaviour, which is how channels that do not end this way stay unchanged.
    closing_question_fallback: str = ""

    @field_validator("thumbnail_strategies", mode="before")
    @classmethod
    def _validate_thumbnail_strategies(cls, v):
        if not v:
            raise ValueError("thumbnail_strategies must have at least 1 strategy")
        return v

    @model_validator(mode="after")
    def _validate_video_type_thumbnail_strategies(self):
        valid_names = {strategy.name for strategy in self.thumbnail_strategies}
        for video_type_name, video_type in self.video_types.items():
            if not video_type.enabled:
                continue
            allowed = video_type.allowed_thumbnail_strategies
            if not allowed:
                raise ValueError(
                    "Enabled video_type "
                    f"'{video_type_name}' must declare allowed_thumbnail_strategies"
                )
            if len(allowed) != len(set(allowed)):
                raise ValueError(
                    "Enabled video_type "
                    f"'{video_type_name}' has duplicate allowed_thumbnail_strategies"
                )
            unknown = [name for name in allowed if name not in valid_names]
            if unknown:
                raise ValueError(
                    "Enabled video_type "
                    f"'{video_type_name}' references unknown thumbnail strategies: "
                    + ", ".join(unknown)
                )
        return self

    style: TemplateStyle = TemplateStyle()
    test: TestConfig = TestConfig()

    @property
    def forced(self) -> set[str]:
        return set(self.test.force)


class VisualSlot(BaseModel):
    visual: str        # see type sets below
    prompt: str = ""   # AI gen prompt or visual description
    keywords: str = "" # search keywords (google_photo, stock_photo, b_roll)
    visual_policy: str = "source_as_written"
    props: dict = {}   # component-specific props

    # Canonical visual type sets — import VisualSlot and use these constants
    IMAGE_TYPES = {"google_photo", "stock_photo", "ai_photo", "ai_illustration", "b_roll"}
    CHART_TYPES = {"bar_chart", "line_chart", "donut_gauge", "comparison_bars"}
    BACKDROP_FIGURE_TYPES = {"fact_highlight", "title_card", "title_banner", "subscribe_cta"}
    COMPONENT_TYPES = BACKDROP_FIGURE_TYPES | {"info_card", "info_slide", "text_only_slide", "bar_chart", "line_chart", "donut_gauge", "comparison_bars"}
    ILLUSTRATION_TYPES = {"info_card", "info_slide", "ai_illustration"}
    SOURCEABLE_TYPES = IMAGE_TYPES | {"info_card", "info_slide"} | BACKDROP_FIGURE_TYPES
    VISUAL_POLICIES = {
        "source_as_written",
        "photo_backed_info_slide",
        "google_photo_exact_action",
        "literal_google_photo",
        "single_pose_ai_photo",
    }

    model_config = {"ignored_types": (set,)}


class ScriptSection(BaseModel):
    id: int
    narration: str
    estimated_duration_seconds: float = 15.0
    actual_duration_seconds: float | None = None
    slots: list[VisualSlot] = []
    accent_color: str | None = None
    highlighted_keywords: list[str] = []
    word_timestamps: list[dict] | None = None
    transition_type: str = ""

    @property
    def non_overlay_slots(self) -> list[VisualSlot]:
        return list(self.slots)

    @property
    def sub_slot_count(self) -> int:
        return max(len(self.slots), 1)


class Script(BaseModel):
    title: str
    video_type: str
    description: str = ""
    tags: list[str] = []
    sections: list[ScriptSection] = []
    total_estimated_duration_seconds: float = 0.0
    hook: str = ""
    content_family: str = ""
    lead_magnet: str = ""
    low_ticket_offer: str = ""
    mid_ticket_offer: str = ""
    thumbnail_text: str = ""
    thumbnail_brief: str = ""
    thumbnail_strategy: str = ""
    music_track: str = ""           # stem name from channel's music pool
    voice_variation: int = 0        # index into config.voice.voice_prompt_variations
    intra_transition: str = "fade"  # transition between sub-slots: "fade"|"dissolve"|"wipeleft"


def slot_requires_sourced_still(
    slot: VisualSlot,
    config: ChannelConfig,
) -> bool:
    """Whether a slot should produce a sourced still image for this channel config."""
    if slot.visual not in VisualSlot.SOURCEABLE_TYPES:
        return False
    if slot.visual == "info_slide":
        return True
    if slot.visual == "info_card":
        return config.image_sourcing.generate_info_card_illustrations
    return True


# Marks a rendered beat as re-showing an earlier beat's sourced file rather
# than owning one. Set by core.render_sections when a section has fewer real
# photographs than it needs beats; read here so the validator does not expect a
# file for it.
REUSED_MEDIA_KEY = "_reused_media_index"


def expected_sourced_image_slots(
    script: Script,
    config: ChannelConfig,
) -> list[tuple[int, int]]:
    """Return (section_id, sub_idx) for each slot that should have a sourced still/video asset.

    A beat that re-shows an earlier beat's image has no file of its own -- the
    renderer points it back at the original -- so it is not expected to have
    been sourced. Without this the validator asked for a section_003_04.png
    that nothing ever downloaded.
    """
    result: list[tuple[int, int]] = []
    for section in script.sections:
        for sub_idx, slot in enumerate(section.non_overlay_slots):
            if REUSED_MEDIA_KEY in (slot.props or {}):
                continue
            if slot_requires_sourced_still(slot, config):
                result.append((section.id, sub_idx + 1))
    return result


def compute_sub_durations(
    section: ScriptSection,
    num_slots: int,
    crossfade: float = 0.3,
    *,
    max_visual_hold_seconds: float | None = None,
    override_duration: float | None = None,
) -> list[float]:
    """Compute per-slot durations using word timestamps when available.

    With word timestamps: finds the N-1 largest gaps between consecutive
    words and uses them as transition points (images change at natural pauses).
    Without: falls back to uniform split.

    override_duration: if set, use this instead of section duration (e.g. for
    remaining time after subtracting pre-rendered chart clips).
    """
    duration = override_duration or section.actual_duration_seconds or section.estimated_duration_seconds

    if num_slots <= 1:
        return [max(duration, 1.0)]

    total_cf = crossfade * (num_slots - 1)

    def _uniform_split() -> list[float]:
        uniform = max((duration + total_cf) / num_slots, 1.0)
        return [uniform] * num_slots

    # Uniform fallback
    if not section.word_timestamps or len(section.word_timestamps) < num_slots:
        return _uniform_split()

    # Find gaps between consecutive words
    words = section.word_timestamps
    gaps = []
    for i in range(1, len(words)):
        gap_duration = words[i]["start"] - words[i - 1]["end"]
        gaps.append((gap_duration, i))  # (gap_size, word_index)

    # Take the N-1 largest gaps as split points
    gaps.sort(key=lambda g: g[0], reverse=True)

    # Need N-1 meaningful gaps (>= 0.1s) to split into N slots.
    # If there aren't enough real pauses, gap-based splitting produces
    # degenerate splits (e.g. 1s, 1s, 47s) — fall back to uniform.
    meaningful_gaps = [g for g in gaps if g[0] >= 0.1]
    if len(meaningful_gaps) < num_slots - 1:
        return _uniform_split()

    split_indices = sorted(g[1] for g in meaningful_gaps[: num_slots - 1])

    # Compute durations from split points
    section_start = words[0]["start"]
    sub_durations = []
    prev_time = section_start

    for split_idx in split_indices:
        # Split at midpoint of the gap
        gap_start = words[split_idx - 1]["end"]
        gap_end = words[split_idx]["start"]
        mid = (gap_start + gap_end) / 2
        sub_durations.append(max(mid - prev_time + crossfade, 1.0))
        prev_time = mid

    # Last segment extends to end
    section_end = words[-1]["end"]
    sub_durations.append(max(section_end - prev_time + crossfade, 1.0))

    if max_visual_hold_seconds is not None and max(sub_durations) > max_visual_hold_seconds:
        # Gap-based splitting follows the delivery, but an uneven run of pauses
        # can park one image far longer than the rest. Previously the uniform
        # rescue only applied when it cleared the cap outright, so a section
        # that could not be fixed kept the worst split available: a 24.7s
        # section over 5 slots held one image for 9.24s when an even split
        # would have held 5.18s. Take whichever split has the lower peak --
        # when neither clears the cap, the smaller overrun is still better,
        # and the slot count is what actually fixes it (see the pacing
        # requirement in core.scripter).
        uniform_durations = _uniform_split()
        if max(uniform_durations) < max(sub_durations):
            return uniform_durations

    return sub_durations


def minimum_visual_slots_for_duration(
    duration_seconds: float,
    max_hold_seconds: float,
    crossfade: float = 0.3,
) -> int:
    """Return the minimum number of visible slots needed to stay under a hold cap."""
    if duration_seconds <= 0:
        return 1
    if max_hold_seconds <= crossfade:
        raise ValueError("max_hold_seconds must be greater than crossfade")
    required = (duration_seconds - crossfade) / (max_hold_seconds - crossfade)
    return max(1, math.ceil(required))


def find_output_video(workspace: Path) -> Path:
    """Find the newest descriptive output video in a workspace."""
    videos = [
        p for p in workspace.glob("*.mp4")
        if p.stem != "output" and not p.stem.startswith("clip")
    ]
    if not videos:
        raise FileNotFoundError(f"No descriptive output MP4 found in {workspace}")
    return max(videos, key=lambda p: (p.stat().st_mtime_ns, p.name))


class Checkpoint(BaseModel):
    channel: str
    run_id: str = ""
    topic: str = ""
    video_type: str = ""
    started_at: str = ""
    stages_completed: list[str] = []
    current_stage: str = ""
    last_error: str | None = None
    workspace_dir: str = ""
    stage_timings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    total_duration_seconds: float | None = None
    review_log: dict[str, Any] = Field(default_factory=lambda: {
        "script_review": None,
        "image_review": None,
        "thumbnail_review": None,
        "final_review": None,
    })


# ── Loading helpers ───────────────────────────────────────────────

def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge `patch` over `base`, returning a new dict."""
    merged = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def available_languages(data: dict) -> list[str]:
    """Script languages a channel offers, base language first."""
    base = str(data.get("language") or "").strip()
    variants = list((data.get("language_variants") or {}).keys())
    ordered = ([base] if base else []) + [v for v in variants if v != base]
    return ordered or ([base] if base else [])


def load_channel_config(
    channel_slug: str,
    overrides: list[str] | None = None,
    language: str | None = None,
) -> ChannelConfig:
    """Load a channel, optionally in one of its declared script languages.

    A language variant is a partial config merged over the base, so switching
    to English swaps the narration language, the voice and its direction, and
    the script instructions together -- they have to move as a set, or the
    scripter writes English while the TTS still reads Arabic.

    An unknown or unset language leaves the base config untouched, which is
    what every channel without variants gets.
    """
    path = CHANNELS_DIR / f"{channel_slug}.json"
    if not path.exists():
        raise FileNotFoundError(f"Channel config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))

    wanted = str(language or "").strip().lower()
    if wanted:
        variant = (data.get("language_variants") or {}).get(wanted)
        if variant:
            data = _deep_merge(data, variant)
            data["language"] = wanted
        elif wanted != str(data.get("language", "")).lower():
            raise ValueError(
                f"{channel_slug} does not offer script language {wanted!r}; "
                f"available: {', '.join(available_languages(data))}"
            )

    # CLI overrides win over the variant, so a run can still pin one field.
    for ov in overrides or []:
        apply_dot_override(data, ov)
    return ChannelConfig(**data)


def parse_override_value(raw_value: str) -> Any:
    """Parse a CLI override value into bool/int/float/string."""
    if raw_value.lower() in ("true", "false"):
        return raw_value.lower() == "true"
    try:
        return int(raw_value)
    except ValueError:
        try:
            return float(raw_value)
        except ValueError:
            return raw_value


def apply_dot_override(data: dict, override: str) -> None:
    """Apply a dot-notation override like 'video.target_duration_minutes=1'."""
    if "=" not in override:
        raise ValueError(f"Invalid override (missing '='): {override}")
    key_path, raw_value = override.split("=", 1)
    keys = key_path.split(".")
    value = parse_override_value(raw_value)

    target = data
    for k in keys[:-1]:
        if isinstance(target, list):
            target = target[int(k)]
        else:
            if k not in target or not isinstance(target[k], (dict, list)):
                target[k] = {}
            target = target[k]
    final_key = keys[-1]
    if isinstance(target, list):
        target[int(final_key)] = value
    else:
        target[final_key] = value


def load_topic_history(channel_slug: str) -> list[dict]:
    path = DATA_DIR / f"topic_history_{channel_slug}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_topic_history(channel_slug: str, history: list[dict]) -> None:
    path = DATA_DIR / f"topic_history_{channel_slug}.json"
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def select_least_recent(
    options: list[str],
    history: list[dict],
    history_key: str,
) -> int:
    """Return the index of the option least recently used in history.

    Scans history backwards. The first option not found in recent history wins.
    If all options appear, picks the one used longest ago.
    """
    # Walk history backwards, record last-seen position for each option
    last_seen: dict[str, int] = {}
    for i, entry in enumerate(reversed(history)):
        val = entry.get(history_key)
        if val is not None and val not in last_seen:
            last_seen[val] = i
        if len(last_seen) >= len(options):
            break

    # Pick the option with the largest last_seen (oldest) or missing entirely
    best_idx = 0
    best_age = -1
    for idx, name in enumerate(options):
        if name not in last_seen:
            return idx  # never used — pick immediately
        if last_seen[name] > best_age:
            best_age = last_seen[name]
            best_idx = idx
    return best_idx


# ── Workspace management ─────────────────────────────────────────

def create_workspace(channel_slug: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ws = WORKSPACE_DIR / f"{channel_slug}_{timestamp}_{uuid.uuid4().hex[:6]}"
    for sub in ("images/raw", "images/ready", "audio/sections", "frames", "videos/raw", "data"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


# ── Checkpoint ────────────────────────────────────────────────────

def save_checkpoint(ws: Path, checkpoint: Checkpoint) -> None:
    path = ws / "checkpoint.json"
    path.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")


def load_checkpoint(ws: Path) -> Checkpoint | None:
    path = ws / "checkpoint.json"
    if not path.exists():
        return None
    return Checkpoint.model_validate_json(path.read_text(encoding="utf-8"))


def find_latest_workspace(channel_slug: str) -> Path | None:
    pattern = f"{channel_slug}_*"
    matches = sorted(WORKSPACE_DIR.glob(pattern), reverse=True)
    return matches[0] if matches else None


# ── Script I/O ────────────────────────────────────────────────────

def save_script(ws: Path, script: Script) -> None:
    path = ws / "script.json"
    path.write_text(script.model_dump_json(indent=2), encoding="utf-8")


def load_script(ws: Path) -> Script:
    path = ws / "script.json"
    return Script.model_validate_json(path.read_text(encoding="utf-8"))


# ── Logging ───────────────────────────────────────────────────────

def setup_logging(channel_slug: str, level: int = logging.INFO) -> logging.Logger:
    date_str = datetime.now().strftime("%Y%m%d")
    log_file = LOGS_DIR / f"{channel_slug}_{date_str}.log"

    logger = logging.getLogger("video_factory")
    logger.setLevel(level)
    logger.handlers.clear()

    # Rich console handler
    rich_handler = RichHandler(console=console, show_path=False)
    rich_handler.setLevel(level)
    logger.addHandler(rich_handler)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    return logger
