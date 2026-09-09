"""Stage 8: full-AI thumbnail generation + Gate #3 review."""

import logging
from pathlib import Path

from PIL import Image, ImageOps

import clients
import prompts
from core.reviewer import review_gate
from core import language_guard, thumbnail_source
from core.thumbnail_text import draw_headline, is_rtl_text
from core.utils import Script, ChannelConfig, ThumbnailStrategyConfig
from settings import ASSETS_DIR


logger = logging.getLogger("video_factory")

THUMBNAIL_SIZE = (1280, 720)
# Vertical thumbnail for channels that publish 9:16 shorts. A 16:9 thumbnail on
# a vertical video is the one landscape image in the package, and the final
# review gate blocks on it: "a landscape thumbnail for a vertical Short-form
# video". Opt-in per channel so an existing channel's thumbnails are unchanged.
VERTICAL_THUMBNAIL_SIZE = (1080, 1920)


def thumbnail_size(config: ChannelConfig) -> tuple[int, int]:
    """The thumbnail dimensions this channel publishes at."""
    if config.style.vertical_thumbnail:
        return VERTICAL_THUMBNAIL_SIZE
    return THUMBNAIL_SIZE


def _thumbnail_strategy_by_name(
    config: ChannelConfig,
    strategy_name: str,
) -> ThumbnailStrategyConfig:
    for strategy in config.thumbnail_strategies:
        if strategy.name == strategy_name:
            return strategy
    available = ", ".join(strategy.name for strategy in config.thumbnail_strategies)
    raise ValueError(f"Unknown thumbnail strategy '{strategy_name}'. Available: {available}")


def _thumbnail_content_context(script: Script) -> str:
    """Summarise what the video is about, for the generator and the reviewer.

    Leads with the story's named subjects so the thumbnail shows the right
    people rather than whichever footballer the model finds most iconic.
    """
    parts: list[str] = []
    subjects = _story_subjects(script)
    if subjects:
        parts.append("This story is about: " + ", ".join(subjects))
    for section in script.sections:
        titles = [
            str(slot.props["title"])
            for slot in section.slots
            if slot.visual == "title_banner" and slot.props.get("title")
        ]
        subject = titles[0] if titles else section.narration.split(".", 1)[0]
        parts.append(f"Section {section.id}: {subject}")
    return "; ".join(parts)


def _story_subjects(script: Script) -> list[str]:
    """Proper nouns the script's own image searches were built around.

    The slot keywords already name the exact players, clubs and events the
    narration covers, so they are the most reliable statement of who the
    thumbnail should depict.
    """
    seen: list[str] = []
    for section in script.sections:
        for slot in section.slots:
            for token in str(slot.keywords or "").split():
                cleaned = token.strip(".,!?\"'()")
                # Proper nouns only, and skip the generic search filler.
                if len(cleaned) < 3 or not cleaned[0].isupper():
                    continue
                if cleaned.lower() in {
                    "photograph", "photo", "football", "soccer", "premier",
                    "league", "stadium", "match", "official",
                }:
                    continue
                if cleaned not in seen:
                    seen.append(cleaned)
    return seen[:10]


def _thumbnail_reference_image(strategy: ThumbnailStrategyConfig) -> Path | None:
    if not strategy.reference_image:
        return None

    ref_path = Path(strategy.reference_image)
    if not ref_path.is_absolute():
        ref_path = ASSETS_DIR / ref_path
    if not ref_path.exists():
        raise FileNotFoundError(
            f"Thumbnail strategy '{strategy.name}' reference_image not found: {ref_path}"
        )
    return ref_path


def _thumbnail_reference_instruction(strategy: ThumbnailStrategyConfig) -> str:
    if not strategy.reference_instruction:
        raise ValueError(
            f"Thumbnail strategy '{strategy.name}' reference_instruction is required "
            "when reference_image is set"
        )
    return strategy.reference_instruction


def _validate_thumbnail_script_fields(script: Script) -> None:
    missing = []
    if not script.thumbnail_text:
        missing.append("thumbnail_text")
    if not script.thumbnail_brief:
        missing.append("thumbnail_brief")
    if not script.thumbnail_strategy:
        missing.append("thumbnail_strategy")
    if missing:
        raise ValueError(f"Script missing thumbnail fields: {', '.join(missing)}")


async def create_thumbnail(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
) -> dict:
    """Create and review a full AI-rendered YouTube thumbnail."""
    _validate_thumbnail_script_fields(script)
    thumbnail_path = workspace / "thumbnail.png"
    strategy = _thumbnail_strategy_by_name(config, script.thumbnail_strategy)
    reference_image = _thumbnail_reference_image(strategy)
    reference_instruction = (
        _thumbnail_reference_instruction(strategy)
        if reference_image is not None
        else None
    )
    content_context = _thumbnail_content_context(script)
    logger.info(f"Thumbnail strategy: {strategy.name}")

    # The headline is composited in code, so it is exactly the string the
    # script wrote -- but the script is what has to be in the right language.
    # Checked before the artwork is paid for rather than after.
    language_problems = language_guard.violations(
        script.thumbnail_text, language=config.language, field="thumbnail_text"
    ) + language_guard.violations(
        script.thumbnail_brief, language=config.language, field="thumbnail_brief"
    )
    for problem in language_problems:
        logger.error(f"[thumbnail] {problem}")
    if language_problems:
        raise ValueError(
            f"Thumbnail text is not in the video's language "
            f"({config.language}): {language_problems[0]}"
        )

    # The photograph the thumbnail is built on. Chosen once and reused across
    # every review attempt: a regeneration should change the composition, not
    # silently change who is on the cover.
    subjects = _story_subjects(script)
    target = thumbnail_size(config)
    picked = thumbnail_source.pick_source_images(
        workspace, script, subjects, limit=1,
        target_aspect=target[0] / target[1],
        # The brief names who belongs on the cover. It outranks slot keywords,
        # which are neither authoritative nor complete.
        brief=script.thumbnail_brief,
    )
    source_images = [path for path, _ in picked]
    # Which person each supplied image shows, in the order the images are
    # sent, so the prompt can map face to photograph rather than leaving the
    # model to guess which is which.
    source_people = [
        list(why.get("brief_people") or why.get("people") or []) for _, why in picked
    ]
    depicts_person = bool(picked and picked[0][1].get("depicts_person"))

    await _generate_ai_thumbnail(
        title=script.title,
        thumbnail_text=script.thumbnail_text,
        thumbnail_brief=script.thumbnail_brief,
        strategy_instruction=strategy.instruction,
        content_context=content_context,
        config=config,
        output_path=thumbnail_path,
        reference_image=reference_image,
        reference_instruction=reference_instruction,
        revision_notes="",
        source_images=source_images,
        subjects=subjects,
        source_people=source_people,
        depicts_person=depicts_person,
    )

    async def _regenerate(content, feedback):
        feedback_str = feedback.get("feedback", "") if isinstance(feedback, dict) else feedback
        await _generate_ai_thumbnail(
            title=script.title,
            thumbnail_text=script.thumbnail_text,
            thumbnail_brief=script.thumbnail_brief,
            strategy_instruction=strategy.instruction,
            content_context=content_context,
            config=config,
            output_path=thumbnail_path,
            reference_image=reference_image,
            reference_instruction=reference_instruction,
            revision_notes=feedback_str,
            source_images=source_images,
            subjects=subjects,
            source_people=source_people,
            depicts_person=depicts_person,
        )
        return content

    def _review_prompt(content):
        return prompts.thumbnail_review_prompt(
            title=script.title,
            video_type=script.video_type,
            thumbnail_text=script.thumbnail_text,
            thumbnail_strategy=strategy.name,
            thumbnail_brief=script.thumbnail_brief,
            strategy_instruction=strategy.instruction,
            content_context=content_context,
        )

    return await review_gate(
        content=None,
        review_prompt_fn=_review_prompt,
        system_instruction=prompts.thumbnail_review_system(),
        regenerate_fn=_regenerate,
        max_attempts=config.review_thresholds.thumbnail_max_attempts,
        gate_name="thumbnail_review",
        image_paths=[thumbnail_path],
    )


async def _generate_ai_thumbnail(
    title: str,
    thumbnail_text: str,
    thumbnail_brief: str,
    strategy_instruction: str,
    content_context: str,
    config: ChannelConfig,
    output_path: Path,
    reference_image: Path | None = None,
    reference_instruction: str | None = None,
    revision_notes: str = "",
    source_images: list[Path] | None = None,
    subjects: list[str] | None = None,
    #: One list of names per source image, positionally aligned with them.
    source_people: list[list[str]] | None = None,
    depicts_person: bool = False,
) -> None:
    # The image model never renders text. Any of it.
    #
    # This used to composite only for right-to-left scripts, on the reasoning
    # that Latin was safe. It is not: asked to draw an English headline the
    # model wrote "THE GHOST SHIP LOG" correctly and then hallucinated a line
    # of Arabic-shaped glyphs underneath it, on an English video. final_review
    # caught it as "nonsensical AI-generated Arabic text".
    #
    # Generating text-free artwork and compositing the exact headline in code
    # removes the failure mode rather than narrowing it: the text on the
    # thumbnail is now always the string the script wrote, in the video's own
    # language, rendered by us.
    overlay_text = True
    prompt = prompts.thumbnail_generation_prompt(
        title=title,
        thumbnail_text=thumbnail_text,
        thumbnail_brief=thumbnail_brief,
        strategy_instruction=strategy_instruction,
        content_context=content_context,
        channel_style=config.style.name,
        image_style_prompt_suffix=config.image_sourcing.style_prompt_suffix,
        revision_notes=revision_notes,
        text_free=overlay_text,
    )
    if reference_image is not None:
        if not reference_instruction:
            raise ValueError("reference_instruction is required when reference_image is provided")
        prompt += (
            "\n\nREFERENCE IMAGE INSTRUCTION:\n"
            f"{reference_instruction}"
        )

    # What the base actually shows decides what may be said about it.
    #
    # Claiming a person is present when the base is a trophy is what produced
    # a thumbnail of two invented footballers: the instruction asserted a face
    # to preserve, the picture had none, so the model supplied one. An object
    # base gets an object instruction, and neither instruction ever asks for a
    # named person to be drawn.
    bases = list(source_images or [])
    # Per-image people, in the order the images are sent.
    per_image = list(source_people or [])
    named = [n for group in per_image for n in group]
    if bases:
        if named:
            # Say which photograph is whom. With two faces supplied and no
            # mapping, the model is free to merge or swap them; naming image 1
            # and image 2 is what makes "Bellingham on the left" actionable.
            roster = "\n".join(
                f"  - Image {index}: {', '.join(group)}"
                for index, group in enumerate(per_image, start=1)
                if group
            )
            prompt += (
                "\n\nSOURCE PHOTOGRAPHS — REAL PEOPLE, PRESERVE THEM:\n"
                f"{len(bases)} photograph(s) are supplied, in this order:\n"
                f"{roster}\n"
                "These are real people and they are the subject of this video. "
                "Build the composition from them and place each where the "
                "thumbnail description asks for that person. Preserve every "
                "face, build, hair and kit exactly as photographed -- do not "
                "replace, beautify, restyle, merge or substitute anyone. "
                "Do NOT add, draw or invent any additional person: everyone "
                "visible in the finished thumbnail must come from a supplied "
                "photograph. You may relight, recolour, crop, cut out, extend "
                "the background, add depth and add graphic elements around "
                "them."
            )
        elif depicts_person:
            prompt += (
                "\n\nSOURCE PHOTOGRAPH — REAL PEOPLE, PRESERVE THEM:\n"
                "The supplied photograph shows real people. Build the "
                "composition around them and preserve their faces and clothing "
                "exactly as photographed. Do not replace or substitute anyone, "
                "and do not add any other identifiable person."
            )
        else:
            prompt += (
                "\n\nSOURCE PHOTOGRAPH — OBJECT OR PLACE, NO PEOPLE:\n"
                "The supplied photograph shows an object or a location, not a "
                "person. Build the composition around what is actually in it. "
                "Do NOT add, draw or invent any recognisable person, player or "
                "face -- a face you invent would not be anyone in this story. "
                "You may relight, recolour, crop, extend the background, add "
                "depth and add graphic elements."
            )
    # The strategy's own reference art is a separate input from the story's
    # photograph: one says how the channel's thumbnails look, the other says
    # who is on this one. Both are handed to the edit.
    if reference_image is not None:
        bases.append(reference_image)

    target_size = thumbnail_size(config)
    aspect_ratio = "9:16" if config.style.vertical_thumbnail else "16:9"

    if not bases:
        # Nothing photographic to edit. Fall back to the existing generator to
        # obtain a base, then edit that -- so the final image still comes from
        # the thumbnail model, as required, rather than shipping raw
        # generator output.
        logger.warning(
            "No source image available for the thumbnail; generating a base "
            "to edit"
        )
        base_path = output_path.with_name(f"{output_path.stem}_base.png")
        base = await clients.generate_image_gemini(
            prompt=prompt,
            output_path=base_path,
            reference_image=reference_image,
            aspect_ratio=aspect_ratio,
            operation_label="thumbnail_base_generate",
        )
        if base is None or not base_path.exists():
            raise RuntimeError("Could not obtain a base image for the thumbnail")
        bases = [base_path]

    result = await clients.edit_thumbnail_image(
        prompt=prompt,
        output_path=output_path,
        source_images=bases,
        aspect_ratio=aspect_ratio,
        operation_label="thumbnail_generate",
    )
    if result is None or not output_path.exists():
        raise RuntimeError("Thumbnail edit did not produce an image")

    img = Image.open(output_path).convert("RGB")
    if img.size != target_size:
        # Cover-crop rather than squash: a generated 16:9 image stretched into
        # a 9:16 frame distorts the subject's face.
        img = ImageOps.fit(img, target_size, method=Image.Resampling.LANCZOS)
    img.save(output_path, "PNG")

    if overlay_text:
        draw_headline(output_path, thumbnail_text)
