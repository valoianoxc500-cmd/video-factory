"""Stage 2a: Image sourcing â€” direct dispatch per script-specified source.

Each section gets script-specified image/video slots that rotate during playback.
After all images are sourced, Gate #2 reviews them with Gemini Vision.
"""

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import httpx
from PIL import Image, ImageFilter, ImageOps

import clients
import prompts
from core import generated_visuals
from core.bilingual_queries import build_bilingual_queries, is_single_script
from core import semantic_match
from core.framing import subject_aware_fit, verticality_score
from core.scripter import strip_mood_words
from core.utils import meets_minimum_source_size, minimum_source_size
from core.reviewer import ReviewGateError, review_gate
from core.utils import (
    Script,
    ChannelConfig,
    VisualSlot,
    expected_sourced_image_slots,
    minimum_visual_slots_for_duration,
    save_script,
    slot_requires_sourced_still,
)
from settings import settings

logger = logging.getLogger("video_factory")

GenerationLane = Literal["photo", "illustration"]

# Map unified visual types to image_source dispatch strings
_VISUAL_TO_SOURCE = {
    "google_photo": "serper",
    "stock_photo": "pexels",
    "ai_photo": "ai_gen",
    "ai_illustration": "ai_gen",
    "info_card": "ai_gen",
    "info_slide": "ai_gen",
    "b_roll": "pexels",
}

# Domains that serve watermarked previews â€” skip these in search results
_STOCK_DOMAINS = {
    "shutterstock.com", "gettyimages.com", "istockphoto.com",
    "alamy.com", "dreamstime.com", "123rf.com", "depositphotos.com",
    "stock.adobe.com",
}

_MAX_CONCURRENT_SOURCES = 6

# How long one slot's recovery ladder may run, and how long the whole retry
# pass may take. A slot walks several queries and pays for a vision review on
# each, so without a clock a handful of stubborn beats ran back to back for
# 15 minutes -- twice, because the review gate runs the pass again for
# whatever it rejects. Anything still unsourced when the clock runs out falls
# through to generation, coverage and finally the drop, all of which are fast.
_SLOT_RETRY_TIMEOUT_SECONDS = 180.0
_RETRY_PASS_TIMEOUT_SECONDS = 600.0

#: Candidates shown to the vision selector for a Serper/Pexels beat.
#:
#: Six rather than eight. These lists *are* relevance-ranked, so depth earns
#: its cost in a way the open-library shortlist does not -- but the winner is
#: almost always in the first few, and each extra image is input tokens on
#: every selection call. Measured at eight, this was the single largest
#: remaining line item in a run.
_PEXELS_CANDIDATE_COUNT = 6
_SERPER_CANDIDATE_COUNT = 8
_SERPER_MAX_ATTEMPTS = 3
_SERPER_RETRY_BACKOFF = 1.5  # seconds, multiplied by the attempt number

# Pexels search shape. Pexels is a curated stock catalogue rather than a web
# index: it matches short noun phrases and returns nothing at all for a slot's
# full descriptive brief. Each beat therefore searches a short ladder of
# progressively simpler queries instead of the one phrasing the script chose,
# and every query in the ladder runs at once.
_PEXELS_QUERY_LADDER_LIMIT = 4
_PEXELS_PER_QUERY_PAGE = 15
# A photographer uploads a whole shoot at once, so the top of a result page is
# regularly six near-identical frames. Capping them keeps the candidate set an
# actual choice.
_PEXELS_MAX_PER_PHOTOGRAPHER = 2
# How much of a candidate's rank comes from matching the beat's own words. The
# rest is how well the photo survives the vertical crop.
_PEXELS_RELEVANCE_WEIGHT = 0.65

# Pexels photo ids that have already shipped on a beat this run, and the ids a
# beat is holding while it downloads and reviews. Beats source concurrently,
# so content-hash dedup alone came too late: two beats could download the same
# photo before either had written its file, and the same picture would appear
# on two unrelated beats.
_PEXELS_COMMITTED_IDS: set[str] = set()
_PEXELS_CLAIMED_IDS: set[str] = set()


def _is_blank_media_prompt(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {"", "empty", "none", "null", "n/a", "placeholder"}


def _is_usable_asset(path: Path | None) -> bool:
    """Whether a path is a real file the renderer could actually show.

    Every "this beat is done" decision is checked against this rather than
    against whatever the sourcing call returned. A generation that was refused,
    safety-blocked or rate-limited comes back without raising, and a download
    can leave a zero-byte file behind; both used to count as a sourced beat.
    """
    if not path:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _generation_style(config: ChannelConfig, is_illustration: bool) -> str:
    img_cfg = config.image_sourcing
    if is_illustration:
        return (
            img_cfg.illustration_style_prompt_suffix
            or img_cfg.style_prompt_suffix
        )
    return img_cfg.style_prompt_suffix


def _generation_lane_for_slot(
    slot: VisualSlot,
    config: ChannelConfig,
    *,
    respect_component_flags: bool = True,
) -> GenerationLane | None:
    """Classify a slot into the photo or illustration generation lane."""
    if slot.visual not in VisualSlot.SOURCEABLE_TYPES:
        return None
    if respect_component_flags and not slot_requires_sourced_still(slot, config):
        return None

    if slot.visual == "info_slide" and bool((slot.props or {}).get("source_as_photo")):
        return "photo"
    if config.image_sourcing.web_photos_only:
        # No illustration lane at all: illustrations are AI-generated.
        return "photo"
    if slot.visual in VisualSlot.ILLUSTRATION_TYPES:
        return "illustration"
    return "photo"


def _generation_model(
    config: ChannelConfig,
    lane: GenerationLane,
) -> str:
    return config.image_sourcing.generation_model


def _generation_operation(lane: GenerationLane, *, retry: bool = False) -> str:
    if lane == "illustration":
        return "image_regeneration_illustration" if retry else "image_generate_illustration"
    return "image_regeneration" if retry else "image_generate"


# Largest enlargement the reframe composite may apply to a source photo.
# Asset provenance. Candidates are downloaded to temp paths and only some are
# chosen, so the URL and licence are recorded at download time and copied onto
# the shipped filename when a candidate wins. Both are cleared per run.
_CANDIDATE_PROVENANCE: dict[str, dict] = {}
_ASSET_PROVENANCE: dict[str, dict] = {}


def _record_candidate_provenance(
    path: Path,
    *,
    platform: str,
    url: str,
    source_page: str = "",
    licence: str = "",
    attribution: str = "",
    width: int = 0,
    height: int = 0,
) -> None:
    _CANDIDATE_PROVENANCE[str(path)] = {
        "platform": platform,
        "url": url,
        "source_page": source_page,
        "licence": licence,
        "attribution": attribution,
        "width": width,
        "height": height,
    }


def _record_generated_asset_provenance(path: Path | str, record: dict) -> None:
    """File a generated frame in the run's asset provenance.

    Generated visuals were only ever appended to `sourcing_log`, which is a
    different store from the one `asset_provenance.json` is written from. A
    real Horror run therefore shipped three FLUX frames and reported
    `generated: 0` -- the frames were in the video and absent from the record
    that says which assets are photographs and which are not. That record is
    the one downstream consumers read, so it has to carry them.
    """
    name = Path(path).name
    _ASSET_PROVENANCE[name] = {**record, "file": name}


#: How many beats in one run may fall through to the open-library tier.
#: Six covers a normal shortfall; beyond that the run has a sourcing problem
#: the next tier should handle rather than one more catalogue.
_OPEN_LIBRARY_BUDGET = 6
_OPEN_LIBRARY_CALLS = 0

#: Candidates shown to the vision selector for an open-library beat.
#:
#: Measured: this selection was 37% of a run's entire model spend. The cost is
#: almost all *input* tokens, and input scales with the number of images, so
#: halving the shortlist halves the bill for the tier. Four is still a real
#: choice -- the selector rejects the whole shortlist when none fits, which is
#: the outcome that matters -- and these candidates come from catalogues whose
#: ordering carries no relevance signal, so the 5th-8th were rarely the winner.
#:
#: The Pexels path keeps its own larger shortlist: its results *are*
#: relevance-ranked, so a deeper list there earns its cost.
_OPEN_LIBRARY_CANDIDATE_COUNT = 4

#: Hard ceiling on vision candidate-selection calls per run, across every
#: tier. A run that wants more than this has a sourcing problem that more
#: model calls will not fix, and the ceiling is what stops a bad run from
#: costing many times a good one.
_CANDIDATE_SELECTION_BUDGET = 40
_CANDIDATE_SELECTION_CALLS = 0


def _reset_selection_budget() -> None:
    global _CANDIDATE_SELECTION_CALLS
    _CANDIDATE_SELECTION_CALLS = 0


def _reset_open_library_budget() -> None:
    """Per run, not per beat."""
    global _OPEN_LIBRARY_CALLS
    _OPEN_LIBRARY_CALLS = 0
    _reset_selection_budget()


def _reset_provenance() -> None:
    _CANDIDATE_PROVENANCE.clear()
    _ASSET_PROVENANCE.clear()
    _reset_open_library_budget()


def _reset_pexels_dedup() -> None:
    """Forget which Pexels photos this run has used. Per run, not per beat."""
    _PEXELS_COMMITTED_IDS.clear()
    _PEXELS_CLAIMED_IDS.clear()


def _claim_pexels_photo(photo_id: str) -> bool:
    """Reserve a photo for the beat about to download it.

    False means another beat has it -- either shipped, or in flight. Photos
    without an id (test doubles, malformed rows) are never reserved; the
    content-hash check downstream still catches those.
    """
    if not photo_id:
        return True
    if photo_id in _PEXELS_COMMITTED_IDS or photo_id in _PEXELS_CLAIMED_IDS:
        return False
    _PEXELS_CLAIMED_IDS.add(photo_id)
    return True


def _release_pexels_photos(photo_ids) -> None:
    """Return photos a beat downloaded but did not ship to the shared pool."""
    _PEXELS_CLAIMED_IDS.difference_update({pid for pid in photo_ids if pid})


def _commit_pexels_photo(photo_id: str) -> None:
    """Retire a photo for the rest of the run: it is shipping on this beat."""
    if photo_id:
        _PEXELS_COMMITTED_IDS.add(photo_id)
        _PEXELS_CLAIMED_IDS.discard(photo_id)


def save_asset_provenance(workspace: Path) -> Path:
    """Write the per-asset source/licence record for this run."""
    out = workspace / "asset_provenance.json"
    out.write_text(
        json.dumps(
            {"assets": list(_ASSET_PROVENANCE.values())},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(f"recorded provenance for {len(_ASSET_PROVENANCE)} asset(s)")
    return out


_MAX_REFRAME_UPSCALE = 2.0

# Below this share of the source retained, a cover crop is discarding so much
# of the frame that the subject is likely lost and a blurred surround is the
# better trade. A 16:9 photo into 9:16 retains ~0.32, so it crops. Mirrors
# core.processor._MIN_COVER_RETENTION.
_MIN_COVER_RETENTION = 0.25


def _minimum_reframe_source_size(target_size: tuple[int, int]) -> tuple[int, int]:
    min_w, min_h = minimum_source_size(target_size)
    return (max(640, min_w // 2), max(360, min_h // 2))


def _reframe_upscale_factor(
    width: int,
    height: int,
    target_size: tuple[int, int],
) -> float:
    """How much `_normalize_photo_bytes_for_target` must enlarge this source.

    The composite scales the photo to *fit inside* the canvas, so only the
    binding axis matters. Comparing raw width and height against the target
    instead rejects almost every real landscape news photo when the target is
    portrait (1080x1920), even when it barely needs enlarging at all.
    """
    target_w, target_h = target_size
    if width <= 0 or height <= 0:
        return float("inf")
    return min(target_w / width, target_h / height)


def _normalize_photo_bytes_for_target(
    image_bytes: bytes,
    *,
    target_size: tuple[int, int],
) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as source:
        img = source.convert("RGB")
    target_w, target_h = target_size
    if img.size == (target_w, target_h):
        return image_bytes

    # Fill the vertical frame by cropping, rather than shrinking the photo to
    # fit and padding the rest with a blur. A 16:9 photo letterboxed into
    # 1080x1920 occupies about a fifth of the height and reads as a landscape
    # video pasted into a vertical one -- the final-review gate rejects it as
    # "not native vertical video". Cropping a landscape source to 9:16 keeps
    # ~32% of its width, which is simply what the format costs.
    # Framing is decided here, once. This used to be a blind ImageOps.fit
    # center crop, which cut the subject out of any photo where they were not
    # dead center -- and because it already produced a target-ratio image,
    # processor's face-aware crop saw a matching aspect ratio and never ran.
    # subject_aware_fit keeps the crop on the subject with portrait headroom
    # and still falls back to a blurred surround for panoramas.
    result = subject_aware_fit(img, (target_w, target_h))

    buffer = io.BytesIO()
    result.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# Aspect ratios the Gemini image models accept, with their numeric value.
# Asking for the one that matches the render target is the difference between
# a native 9:16 frame and a 16:9 frame cropped to a third of its width.
_GENERATION_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (
    ("21:9", 21 / 9),
    ("16:9", 16 / 9),
    ("3:2", 3 / 2),
    ("4:3", 4 / 3),
    ("5:4", 5 / 4),
    ("1:1", 1.0),
    ("4:5", 4 / 5),
    ("3:4", 3 / 4),
    ("2:3", 2 / 3),
    ("9:16", 9 / 16),
)


def generation_aspect_ratio(target_size: tuple[int, int]) -> str:
    """The supported aspect ratio closest to the render target.

    Generated frames used to be requested at the client's 16:9 default whatever
    the channel rendered at, so a 1080x1920 video got 1376x768 images. Those
    are landscape, they fail the minimum portrait size, and cropping one to
    9:16 throws away two thirds of its width -- the picture the model composed
    is not the picture that survives. Asking for 9:16 up front costs nothing
    and keeps the whole frame.
    """
    width, height = int(target_size[0]), int(target_size[1])
    if width <= 0 or height <= 0:
        return "9:16"
    target = width / height
    return min(
        _GENERATION_ASPECT_RATIOS, key=lambda pair: abs(pair[1] - target)
    )[0]


def _conform_image_to_target(
    path: Path,
    *,
    target_size: tuple[int, int],
    label: str,
) -> bool:
    """Make the file at *path* a valid source for *target_size*, or remove it.

    Generated images used to be written straight to the slot's filename by the
    image client, skipping the validation and reframing every downloaded photo
    goes through. Five 1376x768 frames reached final validation that way and
    failed the run on "Raw image(s) too small".

    Returns False when the image cannot be made usable, having deleted it --
    an invalid file left on disk would be renamed into a surviving slot's
    position when unsourced beats are dropped.
    """
    try:
        with Image.open(path) as img:
            width, height = img.size
    except Exception as exc:
        logger.warning(f"{label}: generated file is not a readable image ({exc})")
        path.unlink(missing_ok=True)
        return False

    if (width, height) == tuple(target_size):
        return True

    upscale = _reframe_upscale_factor(width, height, target_size)
    if (
        not meets_minimum_source_size(width, height, target_size)
        and upscale > _MAX_REFRAME_UPSCALE
    ):
        logger.warning(
            f"{label}: generated {width}x{height} needs {upscale:.2f}x "
            f"enlargement to reach {target_size[0]}x{target_size[1]} "
            f"(max {_MAX_REFRAME_UPSCALE:.1f}x); discarding it"
        )
        path.unlink(missing_ok=True)
        return False

    try:
        conformed = _normalize_photo_bytes_for_target(
            path.read_bytes(), target_size=target_size
        )
    except Exception as exc:
        logger.warning(f"{label}: could not reframe the generated image ({exc})")
        path.unlink(missing_ok=True)
        return False

    path.write_bytes(conformed)
    logger.info(
        f"{label}: reframed generated {width}x{height} into "
        f"{target_size[0]}x{target_size[1]}"
    )
    return True


def _generation_prompt(
    *,
    keywords: str,
    prompt: str,
    use_illustration: bool,
) -> str:
    base_prompt = (prompt or keywords).strip()
    if not base_prompt:
        raise ValueError("Image generation requires a prompt or keywords")
    if not use_illustration:
        return base_prompt
    return f"Simple non-photoreal illustration of {base_prompt}."


def _generation_request_preview(
    *,
    keywords: str,
    prompt: str,
    lane: GenerationLane,
    config: ChannelConfig,
    retry: bool = False,
) -> dict[str, str]:
    gen_prompt = _generation_prompt(
        keywords=keywords,
        prompt=prompt,
        use_illustration=lane == "illustration",
    )
    suffix = _generation_style(config, lane == "illustration")
    if suffix:
        gen_prompt += f", {suffix}"
    return {
        "model": _generation_model(config, lane),
        "operation": _generation_operation(lane, retry=retry),
        "prompt": gen_prompt,
    }


def _build_sections_context(
    script: Script,
    raw_dir: Path,
    videos_dir: Path,
    *,
    visual_overrides: dict[tuple[int, int], str] | None = None,
    prompt_overrides: dict[tuple[int, int], str] | None = None,
) -> list[dict]:
    sections_context = []
    for section in script.sections:
        non_overlay_slots = section.non_overlay_slots
        for sub_idx, slot in enumerate(non_overlay_slots, start=1):
            if slot.visual in VisualSlot.CHART_TYPES:
                continue

            file_label = f"section_{section.id:03d}_{sub_idx:02d}"
            img_path = raw_dir / f"{file_label}.jpg"
            if not img_path.exists():
                img_path = raw_dir / f"{file_label}.png"
            if not img_path.exists():
                continue

            visual_type = (
                visual_overrides.get((section.id, sub_idx), slot.visual)
                if visual_overrides else slot.visual
            )
            is_b_roll = visual_type == "b_roll" and (videos_dir / f"{file_label}.mp4").exists()
            key = (section.id, sub_idx)
            sections_context.append({
                "section_id": section.id,
                "sub_image_index": sub_idx,
                "narration": section.narration[:400],
                "visual_type": visual_type,
                "is_section_opener": sub_idx == 1,
                "text_only": slot.visual == "text_only_slide",
                "prompt": (
                    prompt_overrides.get(key, slot.prompt)
                    if prompt_overrides else slot.prompt
                ),
                "image_search_keywords": slot.keywords,
                "image_filename": img_path.name,
                # The identity both sides agree on. Derived from the section
                # and the slot's position among its section's non-overlay
                # slots, exactly as the asset filenames are -- so a rejection
                # resolves to the same beat whether it is a still or a b-roll
                # poster frame, and regardless of how the reviewer numbered
                # the images it was shown.
                "slot_uid": file_label,
                "is_b_roll": is_b_roll,
            })
    return sections_context


def _usable_pexels_key() -> str | None:
    """Return the Pexels key only if it can actually be sent as a header.

    HTTP headers are latin-1; a key with stray non-ASCII characters (a
    half-replaced placeholder, a smart-quoted paste) otherwise blows up deep
    inside httpx and takes the slot's whole sourcing attempt with it.
    """
    key = settings.pexels_api_key
    if not key:
        return None
    if not key.isascii():
        logger.warning(
            "PEXELS_API_KEY contains non-ASCII characters and cannot be used "
            "as an HTTP header; Pexels sourcing is disabled for this run"
        )
        return None
    return key


def _is_stock_domain(url: str) -> bool:
    """Check if a URL belongs to a known stock-photo domain."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in _STOCK_DOMAINS)


def _slot_marker(slot: VisualSlot) -> tuple[str, str, str, str, str]:
    return (
        slot.visual,
        slot.prompt,
        slot.keywords,
        slot.visual_policy,
        repr(slot.props or {}),
    )


def _apply_slot_rewrite(
    slot: VisualSlot,
    *,
    visual: str | None = None,
    prompt: str | None = None,
    keywords: str | None = None,
    visual_policy: str | None = None,
    props: dict | None = None,
) -> bool:
    before = _slot_marker(slot)
    if visual is not None:
        slot.visual = visual
    if prompt is not None:
        slot.prompt = prompt
    if keywords is not None:
        slot.keywords = keywords
    if visual_policy is not None:
        slot.visual_policy = visual_policy
    if props is not None:
        slot.props = props
    return before != _slot_marker(slot)


def _image_source_for_slot(
    slot: VisualSlot,
    config: ChannelConfig | None = None,
) -> str:
    preferred_photo_source = str((slot.props or {}).get("photo_source", "")).strip()
    if preferred_photo_source:
        source = _VISUAL_TO_SOURCE.get(preferred_photo_source, "ai_gen")
    else:
        source = _VISUAL_TO_SOURCE.get(slot.visual, "ai_gen")

    if config is not None and config.image_sourcing.web_photos_only:
        # News visuals must be photographs of the actual subject: everything
        # photographic goes through web image search, never image generation.
        return "serper"
    if (
        config is not None
        and config.image_sourcing.prefer_generated_visuals
        and not preferred_photo_source
    ):
        # Story beats are generated from the beat's own meaning rather than
        # searched for by keyword. A slot that explicitly named a photo source
        # is left alone: that is the case where a real visual was actually
        # wanted, and generation must not displace it.
        return "ai_gen"
    return source


def _apply_ai_prompt_preview_slide(
    *,
    section,
    slot: VisualSlot,
    sub_idx: int,
    lane: GenerationLane,
    config: ChannelConfig,
    retry: bool = False,
) -> tuple[dict[str, str], bool]:
    request = _generation_request_preview(
        keywords=slot.keywords,
        prompt=slot.prompt,
        lane=lane,
        config=config,
        retry=retry,
    )
    title = f"AI Image Prompt s{section.id}.{sub_idx + 1}"
    text = "\n".join(
        [
            f"Model: {request['model']}",
            f"Operation: {request['operation']}",
            f"Visual: {slot.visual}",
            f"Policy: {slot.visual_policy}",
            "",
            "Prompt:",
            request["prompt"],
        ]
    )
    changed = _apply_slot_rewrite(
        slot,
        visual="text_only_slide",
        prompt="",
        keywords="",
        props={"title": title, "text": text},
    )
    slot.props.update({
        "variant": "ai_prompt_preview",
        "model": request["model"],
        "operation": request["operation"],
        "prompt_text": request["prompt"],
    })
    return request, changed


def _apply_source_miss_preview_slide(
    *,
    section,
    slot: VisualSlot,
    sub_idx: int,
    image_source: str,
) -> tuple[dict[str, str], bool]:
    title = f"Image Source Request s{section.id}.{sub_idx + 1}"
    prompt_text = slot.prompt.strip() or "(none)"
    keywords_text = slot.keywords.strip() or "(none)"
    text = "\n".join(
        [
            f"Source: {image_source}",
            f"Visual: {slot.visual}",
            f"Policy: {slot.visual_policy}",
            "",
            "Prompt:",
            prompt_text,
            "",
            "Search keywords:",
            keywords_text,
        ]
    )
    changed = _apply_slot_rewrite(
        slot,
        visual="text_only_slide",
        prompt="",
        keywords="",
        props={"title": title, "text": text},
    )
    slot.props.update({
        "variant": "ai_prompt_preview",
        "model": f"(none; {image_source} source)",
        "operation": "image_source_preview",
        "prompt_text": f"{prompt_text}\n\nSearch keywords:\n{keywords_text}",
    })
    return {
        "model": slot.props["model"],
        "operation": slot.props["operation"],
    }, changed


def _plan_slot_visual(*, section, slot: VisualSlot) -> tuple[str, bool]:
    policy = slot.visual_policy or "source_as_written"
    if policy not in VisualSlot.VISUAL_POLICIES:
        raise ValueError(f"Unknown visual_policy '{policy}' for section {section.id}")

    if slot.visual == "text_only_slide" and (slot.props or {}).get("variant") != "ai_prompt_preview":
        raise ValueError(
            f"Section {section.id}: text_only_slide is reserved for internal AI prompt preview only"
        )

    if slot.visual == "info_slide" and _is_blank_media_prompt(slot.prompt):
        raise ValueError(
            f"Section {section.id}: info_slide requires an image prompt; "
            "use an image-backed slot instead of an image-less text slide"
        )

    if policy == "source_as_written":
        return policy, False

    if policy == "photo_backed_info_slide":
        if slot.visual != "info_slide":
            raise ValueError(
                f"visual_policy photo_backed_info_slide requires info_slide in section {section.id}"
            )
        props = dict(slot.props or {})
        props["source_as_photo"] = True
        props["photo_source"] = "google_photo"
        return policy, _apply_slot_rewrite(
            slot,
            prompt=slot.prompt,
            keywords=slot.keywords,
            props=props,
        )

    if policy in {"google_photo_exact_action", "literal_google_photo"}:
        return policy, _apply_slot_rewrite(
            slot,
            visual="google_photo",
            prompt=slot.prompt,
            keywords=slot.keywords,
        )

    if policy == "single_pose_ai_photo":
        return policy, _apply_slot_rewrite(
            slot,
            visual="ai_photo",
            prompt=slot.prompt,
            keywords=slot.keywords,
        )

    raise ValueError(f"Unhandled visual_policy '{policy}' for section {section.id}")


async def source_images(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
) -> dict:
    """Source multiple images per section using the script-specified source.

    Returns review gate result dict.
    """
    raw_dir = workspace / "images" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _reset_provenance()
    _reset_pexels_dedup()

    # Per run, from the channel's own config, so a deployment whose primary
    # search is unavailable can lean on the open libraries without a code
    # change.
    global _OPEN_LIBRARY_BUDGET
    _OPEN_LIBRARY_BUDGET = max(0, int(config.image_sourcing.open_library_budget))

    sourcing_log = []  # tracks per-image sourcing actions
    seen_hashes: set[str] = set()  # content-hash dedup across sections

    videos_dir = workspace / "videos" / "raw"
    videos_dir.mkdir(parents=True, exist_ok=True)
    target_size = tuple(config.video.resolution)
    fps = config.video.fps
    script_mutated = False

    # Build task descriptors, filtering out cached images/videos
    descriptors = []
    # Slots whose image is already on disk. Not sourced again, but merged in
    # below so the review gate can still re-source them if it rejects one.
    cached_descriptors: list[dict] = []

    # The b-roll ratio is judged against the whole video, so the total has to
    # be known before the first slot is decided.
    total_slots = sum(len(s.non_overlay_slots) for s in script.sections)
    broll_used = 0

    for section in script.sections:
        non_overlay_slots = section.non_overlay_slots
        num_subs = len(non_overlay_slots) or 1

        for sub_idx, slot in enumerate(non_overlay_slots):
            file_label = f"section_{section.id:03d}_{sub_idx + 1:02d}"
            policy, changed = _plan_slot_visual(section=section, slot=slot)
            script_mutated = script_mutated or changed
            keywords = slot.keywords
            prompt = slot.prompt

            # Components rendered by Remotion â€” no sourced still needed
            if slot.visual in VisualSlot.COMPONENT_TYPES and not slot_requires_sourced_still(slot, config):
                logger.info(
                    f"Inline component slot s{section.id}.{sub_idx + 1} "
                    f"({slot.visual}) â€” skipping image source"
                )
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": None,
                    "keywords": keywords,
                    "source": "remotion_inline",
                })
                continue

            lane = _generation_lane_for_slot(slot, config)
            if lane is None:
                logger.info(
                    f"Inline component slot s{section.id}.{sub_idx + 1} "
                    f"({slot.visual}) â€” illustration disabled, skipping image source"
                )
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": None,
                    "keywords": keywords,
                    "source": "remotion_component",
                })
                continue

            image_source = _image_source_for_slot(slot, config)
            if config.test.preview_ai_image_prompts and image_source == "ai_gen":
                request, changed = _apply_ai_prompt_preview_slide(
                    section=section,
                    slot=slot,
                    sub_idx=sub_idx,
                    lane=lane,
                    config=config,
                )
                script_mutated = script_mutated or changed
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": None,
                    "keywords": "",
                    "source": "ai_prompt_preview",
                    "model": request["model"],
                    "operation": request["operation"],
                })
                continue

            # B-roll video slots. A channel that has not opted in, or one that
            # has already used its share of motion footage, sources the slot as
            # a photograph instead -- the beat still gets a visual, it just
            # gets a still one. Falling back beats dropping the slot, which
            # would cost the section a beat it needs for pacing.
            if slot.visual == "b_roll" and not _broll_allowed(config, broll_used, total_slots):
                logger.info(
                    f"Section {section.id} sub-image {sub_idx + 1}: sourcing "
                    f"b-roll slot as a photograph "
                    f"({'video b-roll disabled' if not config.image_sourcing.allow_video_broll else 'b-roll ratio reached'})"
                )
                slot.visual = "stock_photo"

            if slot.visual == "b_roll":
                broll_used += 1
                video_path = videos_dir / f"{file_label}.mp4"
                if video_path.exists():
                    logger.info(f"B-roll already exists: {video_path.name}")
                    sourcing_log.append({
                        "section_id": section.id,
                        "sub_image_index": sub_idx + 1,
                        "file": video_path.name,
                        "keywords": keywords,
                        "source": "pexels_video_cached",
                    })
                    # Describe the slot anyway, marked already-sourced.
                    #
                    # The review gate judges the poster frame this clip wrote
                    # to images/raw, so a rejection names that .jpg. Skipping
                    # the descriptor left the gate with nothing to map the
                    # rejection onto -- "re-sourcing 0 of 3" -- so a rejected
                    # b-roll beat could never be re-sourced or regenerated
                    # however many attempts it was given. Same omission that
                    # was already fixed for cached stills below.
                    cached_descriptors.append({
                        "section": section,
                        "sub_idx": sub_idx,
                        "slot": slot,
                        "slot_uid": file_label,
                        "keywords": keywords,
                        "prompt": prompt,
                        "img_path": raw_dir / f"{file_label}.jpg",
                        "b_roll": True,
                        "lane": "photo",
                        "allow_generation_fallback": (
                            not config.image_sourcing.web_photos_only
                        ),
                        "fallback_to_illustration": False,
                        "video_path": video_path,
                        "target_duration": (
                            section.estimated_duration_seconds / num_subs
                        ),
                        "sourced": True,
                    })
                    continue
                target_dur = section.estimated_duration_seconds / num_subs
                descriptors.append({
                    "section": section,
                    "sub_idx": sub_idx,
                    "slot": slot,
                    "slot_uid": file_label,
                    "keywords": keywords,
                    "prompt": "",
                    "img_path": None,
                    "b_roll": True,
                    "lane": "photo",
                    "allow_generation_fallback": (
                        not config.image_sourcing.web_photos_only
                    ),
                    "fallback_to_illustration": False,
                    "video_path": video_path,
                    "target_duration": target_dur,
                })
                continue

            # Image-based slots (google_photo, stock_photo, ai_photo, ai_illustration, info_card, info_slide)
            # Check for cached video (e.g. from a previous run)
            video_path = videos_dir / f"{file_label}.mp4"
            if video_path.exists():
                logger.info(f"Video exists for slot: {video_path.name} â€” skipping image source")
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": video_path.name,
                    "keywords": keywords,
                    "source": "video_cached",
                })
                # Same reasoning as the cached b-roll branch: the gate reviews
                # this slot's poster frame, so the slot needs a descriptor for
                # a rejection to be actionable.
                cached_descriptors.append({
                    "section": section,
                    "sub_idx": sub_idx,
                    "slot": slot,
                    "slot_uid": file_label,
                    "keywords": keywords,
                    "prompt": prompt,
                    "img_path": raw_dir / f"{file_label}.jpg",
                    "b_roll": True,
                    "lane": lane,
                    "allow_generation_fallback": (
                        not config.image_sourcing.web_photos_only
                    ),
                    "fallback_to_illustration": False,
                    "video_path": video_path,
                    "target_duration": (
                        section.estimated_duration_seconds / num_subs
                    ),
                    "sourced": True,
                })
                continue

            img_path = raw_dir / f"{file_label}.jpg"
            if img_path.exists():
                logger.info(f"Image already exists: {img_path.name}")
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": img_path.name,
                    "keywords": keywords,
                    "source": "cached",
                })
                # Still describe the slot, marked already-sourced. A cached
                # image is skipped by the sourcing pass but must remain
                # reachable by the review gate: on a resumed run every image is
                # cached, and without a descriptor the gate had nothing to
                # re-source, so it re-reviewed identical files until its retry
                # budget ran out and failed the run.
                cached_descriptors.append({
                    "section": section,
                    "sub_idx": sub_idx,
                    "slot": slot,
                    "slot_uid": file_label,
                    "keywords": keywords,
                    "prompt": prompt,
                    "img_path": img_path,
                    "b_roll": False,
                    "lane": lane,
                    "allow_generation_fallback": (
                        not config.image_sourcing.web_photos_only
                        and slot.visual not in {"google_photo"}
                        and policy not in {
                            "literal_google_photo",
                            "google_photo_exact_action",
                            "photo_backed_info_slide",
                        }
                    ),
                    "fallback_to_illustration": False,
                    "sourced": True,
                })
                continue

            descriptors.append({
                "section": section,
                "sub_idx": sub_idx,
                "slot": slot,
                "slot_uid": file_label,
                "keywords": keywords,
                "prompt": prompt,
                "img_path": img_path,
                "b_roll": False,
                "lane": lane,
                "allow_generation_fallback": (
                    not config.image_sourcing.web_photos_only
                    and slot.visual not in {"google_photo"}
                    and policy not in {
                        "literal_google_photo",
                        "google_photo_exact_action",
                        "photo_backed_info_slide",
                    }
                ),
                "fallback_to_illustration": False,
            })

    # Source all non-cached images/videos in parallel (capped by semaphore)
    sem = asyncio.Semaphore(_MAX_CONCURRENT_SOURCES)

    async def _source_one(desc: dict, client: httpx.AsyncClient) -> None:
        nonlocal script_mutated
        section = desc["section"]
        sub_idx = desc["sub_idx"]
        keywords = desc["keywords"]

        if desc["b_roll"]:
            # B-roll video sourcing
            video_path = desc["video_path"]
            async with sem:
                success = await _search_pexels_video(
                    keywords=keywords,
                    output_path=video_path,
                    client=client,
                    seen_hashes=seen_hashes,
                    target_duration=desc["target_duration"],
                    target_size=target_size,
                    fps=fps,
                )

            if success:
                # Extract a frame to images/raw/ so the review gate can check relevance
                frame_path = raw_dir / f"section_{section.id:03d}_{sub_idx + 1:02d}.jpg"
                _extract_video_frame(video_path, frame_path)
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": video_path.name,
                    "keywords": keywords,
                    "source": "pexels_video",
                })
                desc["sourced"] = True
                return

            # Fallback: source as normal image instead
            logger.warning(
                f"Section {section.id} B-roll failed, falling back to image"
            )
            desc["img_path"] = raw_dir / f"section_{section.id:03d}_{sub_idx + 1:02d}.jpg"

        # Normal image sourcing â€” dispatch on slot.visual
        img_path = desc["img_path"]
        slot = desc["slot"]
        image_source = _image_source_for_slot(slot, config)
        lane = desc["lane"]

        async with sem:
            source_used = await _source_single_image(
                keywords=desc.get("keywords", keywords),
                prompt=desc.get("prompt", ""),
                image_source=image_source,
                config=config,
                output_path=img_path,
                client=client,
                seen_hashes=seen_hashes,
                lane=lane,
                allow_generation_fallback=desc.get("allow_generation_fallback", True),
                fallback_to_illustration=desc.get("fallback_to_illustration", False),
                narration=section.narration,
            )

        if source_used:
            sourcing_log.append({
                "section_id": section.id,
                "sub_image_index": sub_idx + 1,
                "file": img_path.name,
                "keywords": keywords,
                "source": source_used,
            })
            desc["sourced"] = True
        else:
            if (
                config.test.preview_ai_image_prompts
            ):
                if desc.get("allow_generation_fallback", True):
                    request, changed = _apply_ai_prompt_preview_slide(
                        section=section,
                        slot=slot,
                        sub_idx=sub_idx,
                        lane=lane,
                        config=config,
                    )
                else:
                    request, changed = _apply_source_miss_preview_slide(
                        section=section,
                        slot=slot,
                        sub_idx=sub_idx,
                        image_source=image_source,
                    )
                script_mutated = script_mutated or changed
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": None,
                    "keywords": "",
                    "source": "ai_prompt_preview",
                    "model": request["model"],
                    "operation": request["operation"],
                })
                desc["sourced"] = True
                return
            # A miss is recorded, not raised. Raising here used to abort the
            # whole gather, which tore down the shared HTTP client while its
            # siblings were still using it ("client has been closed").
            desc["sourced"] = False

    if descriptors:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        ) as client:
            outcomes = await asyncio.gather(
                *[_source_one(desc, client) for desc in descriptors],
                return_exceptions=True,
            )
            for desc, outcome in zip(descriptors, outcomes):
                if isinstance(outcome, BaseException):
                    logger.warning(
                        f"Section {desc['section'].id} sub-image "
                        f"{desc['sub_idx'] + 1}: sourcing raised "
                        f"{type(outcome).__name__}: {outcome}"
                    )
                    desc["sourced"] = False

            # Second pass for the misses, still inside the client's lifetime.
            await _retry_missed_slots(
                descriptors=descriptors,
                script=script,
                config=config,
                client=client,
                seen_hashes=seen_hashes,
                sourcing_log=sourcing_log,
                raw_dir=raw_dir,
            )

            # Third pass, only for beats whose loss would leave a section
            # unable to fill its runtime inside the hold cap. The widening
            # pass judges each slot alone and can afford to give one up; this
            # one knows what the section still owes.
            await _rescue_underpopulated_sections(
                descriptors=descriptors,
                script=script,
                config=config,
                client=client,
                seen_hashes=seen_hashes,
                sourcing_log=sourcing_log,
                raw_dir=raw_dir,
            )

    # Fourth pass, and the last thing tried before beats start being thrown
    # away: generate an atmospheric frame for slots that web search and subject
    # rescue both failed to fill. Off unless the channel opts in, capped per
    # video, and refused outright for anything that would fabricate evidence or
    # a real person -- see core/generated_visuals.py.
    fallback_budget = await _generate_missing_visuals(
        descriptors=descriptors,
        config=config,
        sourcing_log=sourcing_log,
    )

    # Fifth pass: cover whatever is still empty with a licensed clip or a text
    # card, so a beat nothing could be found for costs the beat rather than
    # the whole video. Opt-in per channel.
    await _cover_unsourced_slots(
        descriptors=descriptors,
        config=config,
        sourcing_log=sourcing_log,
        videos_dir=videos_dir,
        raw_dir=raw_dir,
        seen_hashes=seen_hashes,
        target_size=target_size,
        fps=fps,
    )

    # Cached slots join the pool only now: they must not be re-sourced by the
    # pass above, but the review gate downstream has to be able to reach them.
    descriptors.extend(cached_descriptors)

    survivor_map = _drop_unsourced_slots(descriptors, script)
    if survivor_map:
        script_mutated = True
        _renumber_section_media(survivor_map, raw_dir, videos_dir)

    # The script passed validation with enough slots to keep every beat under
    # the hold cap. Dropping unsourced beats can break that contract after the
    # fact, and nothing downstream re-checks it -- the renderer just cycles
    # what it was given. Re-check the sections that actually lost beats.
    if survivor_map:
        enforce_minimum_slots(
            script, config, only_sections=set(survivor_map)
        )

    # â”€â”€ Gate #2: Image Relevance Review â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    image_paths = sorted(raw_dir.glob("section_*_*.jpg"))
    if not image_paths:
        image_paths = sorted(raw_dir.glob("section_*_*.png"))

    sections_context = _build_sections_context(script, raw_dir, videos_dir)

    def _review_prompt(content):
        # Documentary channels judge b-roll against the scene, not the topic:
        # under a factual voiceover a loosely related clip reads as footage of
        # the real event. web_photos_only is what marks those channels.
        return prompts.image_review_prompt(
            sections_context,
            documentary=config.image_sourcing.web_photos_only,
        )

    async def _regenerate(content, feedback):
        """Re-source only the images the reviewer rejected.

        Without this the gate had nothing to retry with, so one bad photo in a
        ten-image run failed the whole pipeline after a single look. The
        reviewer already reports which slot failed and often suggests a better
        query, so the miss is actionable -- re-source those slots and let the
        gate look again.
        """
        nonlocal image_paths, sections_context
        rejected = _rejected_slot_keys(feedback)
        if not rejected:
            # Rejected overall but with no per-image detail: nothing targeted
            # to redo, so re-review as-is rather than re-sourcing blindly.
            logger.warning(
                "[image_review] rejected without per-image results; "
                "cannot target a re-source"
            )
            return content

        rejected_files = _rejected_filenames(rejected, sections_context)

        # Matched on the stable slot id, so a b-roll beat whose reviewed asset
        # is a poster frame resolves as reliably as a plain still.
        targets = [
            d for d in descriptors if d.get("slot_uid") in rejected_files
        ]
        logger.info(
            f"[image_review] re-sourcing {len(targets)} of "
            f"{len(rejected)} rejected image(s): "
            f"{', '.join(sorted(rejected_files)) or '(none matched)'}"
        )

        for desc in targets:
            # Blacklist the rejected file so the retry cannot re-pick it, and
            # take the reviewer's suggested query when it gave one.
            path = desc.get("img_path")
            if path and Path(path).exists():
                try:
                    # Same content hash the download path records, so the
                    # rejected file is now a known duplicate and gets skipped.
                    seen_hashes.add(
                        hashlib.md5(Path(path).read_bytes()).hexdigest()
                    )
                except OSError:
                    pass
            suggestion = _query_from_suggestion(
                rejected_files.get(desc.get("slot_uid"), "")
            )
            if suggestion:
                desc["review_suggestion"] = suggestion
            desc["sourced"] = False

        generate_when_rejected = bool(
            getattr(config.image_sourcing, "generate_when_rejected", False)
        )

        if targets and not generate_when_rejected:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=10),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                follow_redirects=True,
            ) as client:
                await _retry_missed_slots(
                    descriptors=targets,
                    script=script,
                    config=config,
                    client=client,
                    seen_hashes=seen_hashes,
                    sourcing_log=sourcing_log,
                    raw_dir=raw_dir,
                )
        elif targets:
            # The channel has asked for a purpose-built image when a real one
            # was found and judged wrong. Hunting for another stock photo is
            # what already failed -- these are briefs no catalogue holds -- so
            # the beat is drawn to its own description instead.
            #
            # The rejected file is discarded first (blacklisted above), the
            # generated file lands on the same path, and the same reviewer
            # looks at it on the next pass. Nothing here approves anything.
            logger.info(
                f"[image_review] generating {len(targets)} rejected beat(s) "
                f"from their own briefs (channel policy: generate_when_rejected)"
            )
            for desc in targets:
                path = desc.get("img_path")
                if path and Path(path).exists():
                    Path(path).unlink()
                # A rejected b-roll beat was rejected on the frame its clip
                # produced, so the clip is the thing that was wrong. Leaving
                # the .mp4 in place would render that footage anyway and the
                # generated still would never be seen -- the run would ship
                # exactly the visual the gate refused. The beat becomes a
                # still, which is what the generated asset is.
                video_path = desc.get("video_path")
                if video_path and Path(video_path).exists():
                    logger.info(
                        f"[image_review] discarding rejected b-roll "
                        f"{Path(video_path).name}; the beat becomes a still"
                    )
                    Path(video_path).unlink()
                desc["b_roll"] = False
                desc["video_path"] = None
            await _generate_missing_visuals(
                descriptors=targets,
                config=config,
                sourcing_log=sourcing_log,
                allow_override=True,
                limit_override=int(getattr(
                    config.image_sourcing, "max_generated_when_rejected", 8)),
                operation_label="generated_after_rejection",
            )

        # The set of files on disk may have changed, so rebuild what the next
        # review pass looks at.
        refreshed = sorted(raw_dir.glob("section_*_*.jpg")) or sorted(
            raw_dir.glob("section_*_*.png")
        )
        image_paths[:] = refreshed
        sections_context = _build_sections_context(script, raw_dir, videos_dir)
        return content

    # The stage does not get to report success on its own say-so. Every "this
    # beat is done" decision above is a belief about a file; this checks the
    # files. A run shipped `image_source` complete with section_002_01 never
    # written because a rate-limited generation returned None instead of
    # raising, and the checkpoint then recorded the stage as done -- so
    # resuming skipped sourcing and failed validation forever.
    async def _reconcile() -> None:
        nonlocal script_mutated
        repaired = await _reconcile_expected_assets(
            script=script,
            config=config,
            descriptors=descriptors,
            sourcing_log=sourcing_log,
            seen_hashes=seen_hashes,
            raw_dir=raw_dir,
            videos_dir=videos_dir,
            target_size=target_size,
            fps=fps,
        )
        script_mutated = script_mutated or repaired

    try:
        try:
            result = await review_gate(
                content=None,
                review_prompt_fn=_review_prompt,
                system_instruction=prompts.image_review_system(),
                regenerate_fn=_regenerate,
                max_attempts=config.review_thresholds.image_review_max_attempts,
                gate_name="image_review",
                image_paths=image_paths,
            )
        except ReviewGateError:
            # The gate rejecting an image is a verdict on the picture, not on
            # whether a file exists -- and the re-source it triggers leaves
            # beats it could not replace with nothing on disk. When the caller
            # waives this gate (--allow-review-failures), the stage carries on,
            # so those beats have to be reconciled here or validation fails on
            # files nothing will ever write. Reconciling first also means a
            # waived run drops the beat rather than shipping the rejected image.
            await _reconcile()
            raise

        await _reconcile()

        result["sourcing_log"] = sourcing_log
        result["asset_provenance"] = list(_ASSET_PROVENANCE.values())
        return result
    finally:
        # Written in `finally` so a run that fails review still leaves an
        # auditable record of what was downloaded and from where.
        try:
            save_asset_provenance(workspace)
        except Exception as exc:
            logger.warning(f"could not write asset provenance: {exc}")
        if script_mutated:
            save_script(workspace, script)


def _rejected_slot_keys(feedback) -> dict[tuple[int, int], str]:
    """Map (section_id, sub_image_index) -> suggested query for rejected images.

    The reviewer returns `image_results` entries carrying `approved` and often
    a `suggestion`. Warnings (watermark, soft focus) are approved and are left
    alone -- only hard rejections are worth spending another search on.
    """
    if not isinstance(feedback, dict):
        return {}

    rejected: dict[tuple[int, int], str] = {}
    unidentifiable = 0
    for item in feedback.get("image_results") or []:
        if not isinstance(item, dict) or item.get("approved", True):
            continue
        try:
            key = (int(item["section_id"]), int(item["sub_image_index"]))
        except (KeyError, TypeError, ValueError):
            # A rejection the reviewer did not say which image it was about.
            # Skipping it silently made the gate report "no per-image results"
            # and abandon the whole regeneration, so a run could burn both
            # attempts without re-sourcing anything. It is still skipped --
            # guessing which slot it meant is how the wrong beat gets
            # regenerated -- but it is now visible.
            unidentifiable += 1
            continue
        rejected[key] = str(item.get("suggestion", "") or "").strip()

    if unidentifiable:
        logger.warning(
            f"[image_review] {unidentifiable} rejection(s) carried no "
            f"section_id/sub_image_index and could not be targeted; "
            f"{len(rejected)} of {unidentifiable + len(rejected)} actionable"
        )
    return rejected


# Instruction scaffolding the reviewer wraps its suggested query in. The
# schema asks for e.g. "Search for 'stadium crowd at Anfield' instead", so the
# quoted span is the query and the rest is prose.
_SUGGESTION_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:try\s+|use\s+|search\s+(?:for\s+)?|find\s+|look\s+for\s+|"
    r"source\s+|replace\s+with\s+|swap\s+(?:in|for)\s+)+",
    re.IGNORECASE,
)
_SUGGESTION_FILLER_RE = re.compile(
    r"^(?:a|an|the)\s+(?:high[- ]quality\s+|better\s+|clearer\s+|real\s+|actual\s+)*"
    r"(?:image|photo|photograph|picture|shot)\s+(?:of|showing|with)\s+",
    re.IGNORECASE,
)


def _query_from_suggestion(suggestion: str) -> str:
    """Turn the reviewer's prose suggestion into something searchable.

    The suggestion is written for a human ("Search for a high-quality image of
    a ticking clock face"). Passing that verbatim to image search matches the
    sentence, not the subject, and returns worse results than the query that
    already failed. Prefer the quoted span the schema asks for, and otherwise
    strip the instruction wrapper.
    """
    text = (suggestion or "").strip()
    if not text:
        return ""

    quoted = re.search(r"['\"â€˜â€œ]([^'\"â€™â€]{3,})['\"â€™â€]", text)
    if quoted:
        return " ".join(quoted.group(1).split())[:120].strip()

    text = _SUGGESTION_PREFIX_RE.sub("", text).strip()
    text = _SUGGESTION_FILLER_RE.sub("", text).strip()
    text = text.rstrip(".").strip()
    # Prose that survived stripping is still a sentence, not a query; a long
    # one matches nothing, so let the normal tiers handle it instead.
    if len(text.split()) > 12:
        return ""
    return " ".join(text.split())[:120].strip()


def _rejected_filenames(
    rejected: dict[tuple[int, int], str],
    sections_context: list[dict],
) -> dict[str, str]:
    """Resolve reviewer (section_id, sub_image_index) keys to filenames.

    The reviewer numbers images the way `sections_context` lists them, and that
    listing is built *after* unsourced slots are dropped and the survivors
    renumbered. So a rejection of "3.3" does not necessarily mean the
    descriptor whose sub_idx is 2 -- matching on the index silently resolved
    every rejection to nothing. The filename is the identifier both the
    reviewer's context and the descriptors agree on.
    """
    uid_for_key = {
        (s.get("section_id"), s.get("sub_image_index", 1)): s.get("slot_uid")
        for s in sections_context
    }
    return {
        uid_for_key[key]: suggestion
        for key, suggestion in rejected.items()
        if uid_for_key.get(key)
    }


def _latin_ratio(text: str) -> float:
    """Fraction of the letters in *text* that are Latin script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for c in letters if "a" <= c.lower() <= "z")
    return latin / len(letters)


# Below this, a query is mostly non-Latin script and will not match the
# English-language captions and alt text that photo search indexes.
_MIN_LATIN_RATIO_FOR_SEARCH = 0.5


# Wording that describes a staged scene around a subject rather than the
# subject itself. Stripping it turns "hands holding stacks of 1971 twenty
# dollar bills" -- which no archival photograph happens to be -- into "1971
# twenty dollar bills", which the archive does hold.
# How the image is framed. This wording can be part of a legitimate archival
# request ("FBI evidence photograph of the tie"), so it is only stripped when
# the brief is not asking for an archival artefact.
_MEDIUM_WRAPPER_RE = re.compile(
    r"\b("
    r"close[- ]?up\s+(of|shot\s+of)?|photo(graph)?\s+of|picture\s+of|image\s+of|"
    r"view\s+of|shot\s+of|scene\s+(of|showing)|depicting|showing"
    r")\b",
    re.IGNORECASE,
)

# Someone doing something with the subject, or a count of it. Never part of an
# archival request -- the archive holds the object, not a person posing with a
# chosen number of them.
_ACTION_WRAPPER_RE = re.compile(
    r"\b("
    r"hands?\s+(holding|gripping|clutching|examining|opening)|"
    r"(a\s+)?(man|woman|person|someone|investigator|agent)\s+"
    r"(holding|examining|inspecting|carrying|reviewing|looking\s+at)|"
    r"examining|inspecting|digging|"
    r"stacks?\s+of|piles?\s+of|bundles?\s+of|rows?\s+of|original\s+stacks?|"
    # Small counts only. A four-digit number is a year, and the era is the one
    # qualifier worth keeping -- the review gate rejected a 2013-series bill in
    # a 1971 story, so stripping "1971" would trade one failure for another.
    r"\d{1,3}\s+(of\s+)?|several\s+|many\s+|multiple\s+|a\s+single\s+|one\s+"
    r")\b",
    re.IGNORECASE,
)

# Both together, for the search-query ladder where the whole string is being
# reduced to its subject.
_SCENE_WRAPPER_RE = re.compile(
    f"({_MEDIUM_WRAPPER_RE.pattern}|{_ACTION_WRAPPER_RE.pattern})",
    re.IGNORECASE,
)

# Trailing atmosphere that narrows a search without naming anything real.
_ATMOSPHERE_TAIL_RE = re.compile(
    r"\b(at\s+(night|dusk|dawn|sunset|sunrise)|in\s+the\s+(rain|fog|dark|snow)|"
    r"dramatic\s+lighting|moody|ominous|dark\s+and\s+stormy|low\s+angle|"
    r"aerial\s+view|black\s+and\s+white)\b",
    re.IGNORECASE,
)


def _documented_subject_queries(keywords: str) -> list[str]:
    """Progressively reduce a slot query to the real subject inside it.

    A slot can ask for something the archive simply does not contain -- an
    action performed with an object, a quantity of it, a time of day. The
    subject itself usually IS documented, so rather than dropping the beat or
    inventing a picture, the wrapper is stripped and the search is retried for
    the thing itself.

    Returns simplified variants, longest first, excluding the original.
    """
    text = " ".join(str(keywords or "").split())
    if not text:
        return []

    variants: list[str] = []

    stripped = " ".join(_SCENE_WRAPPER_RE.sub(" ", text).split())
    if stripped and stripped.lower() != text.lower():
        variants.append(stripped)

    base = stripped or text
    no_atmosphere = " ".join(_ATMOSPHERE_TAIL_RE.sub(" ", base).split())
    if no_atmosphere and no_atmosphere.lower() not in {
        text.lower(), *(v.lower() for v in variants)
    }:
        variants.append(no_atmosphere)

    # Last resort within this ladder: the trailing noun phrase, which is where
    # the concrete subject almost always sits ("...1971 twenty dollar bills").
    words = (no_atmosphere or base).split()
    if len(words) > 3:
        tail = " ".join(words[-3:])
        if tail.lower() not in {text.lower(), *(v.lower() for v in variants)}:
            variants.append(tail)

    return variants


def _relaxed_query_tiers(
    desc: dict,
    script: Script,
    *,
    web_photos_only: bool = False,
) -> list[str]:
    """Progressively broader real-photo queries for a slot that came up empty.

    Every tier still describes the actual topic, so a rescue never degrades
    into generic stock imagery of the sport in general.
    """
    slot = desc["slot"]
    section = desc["section"]
    title = script.title.strip()

    tiers = [
        # When the review gate rejected this slot it usually names a better
        # query than the one that failed; try that before widening.
        (desc.get("review_suggestion") or "").strip(),
        (slot.keywords or "").strip(),
        (slot.prompt or "").strip(),
        f"{title} {(slot.keywords or '').strip()}".strip(),
        title,
    ]

    seen: set[str] = set()
    ordered: list[str] = []
    for tier in tiers:
        # Long prose queries match nothing; keep them search-sized.
        normalized = " ".join(tier.split())[:120].strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        ordered.append(normalized)

    # On a non-Latin-language channel the script title is narration copy -- an
    # Arabic headline, often phrased as a question. Feeding that to image
    # search returns infographics and text cards rather than photographs of the
    # subject, so those tiers are dropped whenever a Latin-script tier (the
    # slot's own English keywords) survives to search with.
    usable = [t for t in ordered if _latin_ratio(t) >= _MIN_LATIN_RATIO_FOR_SEARCH]
    if usable and len(usable) < len(ordered):
        demoted = [t for t in ordered if t not in usable]
        logger.debug(
            f"Section {section.id} sub-image {desc['sub_idx'] + 1}: demoted "
            f"{len(demoted)} non-Latin query tier(s) below the Latin ones"
        )
        # Demoted, not discarded. English leads because historical material is
        # indexed under its original names, but Arabic-language sources hold
        # photographs -- regional press, local place names, cases never written
        # up in English -- that dropping these tiers made unreachable.
        ordered = usable + [t for t in demoted if is_single_script(t)]

    # The bare script title describes the story, not this beat, so it can only
    # ever return a story-level image. A slot needing the recovered ransom
    # money was widened to the title and came back with an airplane, which the
    # review gate then correctly called "a complete subject mismatch". Where
    # every image must be a real photograph OF THE NAMED SUBJECT, a story-level
    # rescue is worse than no rescue -- the simplification ladder below finds
    # the real subject instead.
    if web_photos_only and len(ordered) > 1:
        title_only = " ".join(title.split()).lower()
        kept = [q for q in ordered if q.lower() != title_only]
        if kept:
            ordered = kept

    if not ordered:
        logger.warning(
            f"Section {section.id} sub-image {desc['sub_idx'] + 1}: "
            f"no usable search text for a retry"
        )
    return ordered


async def _retry_missed_slots(
    *,
    descriptors: list[dict],
    script: Script,
    config: ChannelConfig,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    sourcing_log: list[dict],
    raw_dir: Path,
) -> None:
    """Re-source slots that missed, widening the query on each attempt.

    Runs the same dispatch as the first pass so behaviour (and test seams)
    stay identical. The final attempt drops the relevance gate: by then every
    candidate for every query has been rejected, and a top-ranked photo for
    the slot's own topic beats losing the visual beat altogether.

    Slots recover concurrently and under a clock. Walking them one at a time
    was what made a 35-minute image_source stage: each slot works through a
    ladder of queries, every attempt pays for a vision review, and seven
    stubborn slots ran back to back -- twice, because the review gate runs
    this pass again for whatever it rejects. A slot that cannot finish inside
    its own budget is left unsourced for the passes below rather than allowed
    to hold the stage open.
    """
    missed = [d for d in descriptors if not d.get("sourced", True)]
    if not missed:
        return

    logger.info(
        f"Retrying {len(missed)} unsourced image slot(s) with wider queries "
        f"({_MAX_CONCURRENT_SOURCES} at a time)"
    )

    sem = asyncio.Semaphore(_MAX_CONCURRENT_SOURCES)

    async def _recover_one(desc: dict) -> None:
        label = (
            f"Section {desc['section'].id} sub-image "
            f"{desc.get('sub_idx', 0) + 1}"
        )
        try:
            async with sem:
                await asyncio.wait_for(
                    _recover_missed_slot(
                        desc=desc,
                        script=script,
                        config=config,
                        client=client,
                        seen_hashes=seen_hashes,
                        sourcing_log=sourcing_log,
                        raw_dir=raw_dir,
                    ),
                    timeout=_SLOT_RETRY_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            logger.warning(
                f"{label}: recovery gave up after "
                f"{_SLOT_RETRY_TIMEOUT_SECONDS}s; leaving the beat for the "
                f"fallback passes"
            )
            desc["sourced"] = False
        except Exception as exc:
            logger.warning(f"{label}: recovery raised {type(exc).__name__}: {exc}")
            desc["sourced"] = False

    try:
        await asyncio.wait_for(
            asyncio.gather(*[_recover_one(desc) for desc in missed]),
            timeout=_RETRY_PASS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        # The pass as a whole is capped too, so a batch of slow slots cannot
        # add up to a stage that never ends. Whatever is still unsourced falls
        # through to generation, coverage and finally the drop.
        logger.warning(
            f"retry pass hit its {_RETRY_PASS_TIMEOUT_SECONDS}s budget; "
            f"continuing with the beats it recovered"
        )


async def _recover_missed_slot(
    *,
    desc: dict,
    script: Script,
    config: ChannelConfig,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    sourcing_log: list[dict],
    raw_dir: Path,
) -> None:
    """One slot's recovery ladder. See `_retry_missed_slots`."""
    section = desc["section"]
    sub_idx = desc["sub_idx"]
    slot = desc["slot"]
    img_path = desc.get("img_path") or (
        raw_dir / f"section_{section.id:03d}_{sub_idx + 1:02d}.jpg"
    )
    desc["img_path"] = img_path

    tiers = _relaxed_query_tiers(
        desc, script,
        web_photos_only=config.image_sourcing.web_photos_only,
    )
    # (query, require_relevance_review) -- widen the query first.
    attempts: list[tuple[str, bool]] = [(q, True) for q in tiers[1:]]

    # Then fall back to the real documented subject inside the request. A
    # slot can ask for something the archive does not hold -- an action
    # performed with an object, a quantity of it, a time of day -- while the
    # subject itself is well documented. Strip the staging and search for
    # the thing. The relevance gate stays ON for every one of these: this
    # finds a genuine photograph of the subject, it does not lower the bar
    # for what counts as one.
    seen_queries = {q for q, _ in attempts}
    for tier in tiers:
        for simplified in _documented_subject_queries(tier):
            if simplified not in seen_queries:
                seen_queries.add(simplified)
                attempts.append((simplified, True))

    # Then, only where a wrong-but-present image is better than a missing
    # beat, try again with the relevance gate off.
    #
    # It is not better on a web-photo-only channel. Dropping the gate keeps
    # whatever search ranked first, and search ranks confidently wrong
    # things: a Horror run was handed Bigfoot for an investigator, toy
    # soldiers for parachutes and an icon set for a photograph. Those then
    # reach the image review gate, which correctly rejects them and -- with
    # only two attempts -- fails the whole run. The slot is left unsourced
    # instead, and _drop_unsourced_slots removes the beat; the pacing margin
    # means a section can afford to lose one.
    if not config.image_sourcing.web_photos_only:
        attempts += [(q, False) for q in tiers]

    for query, require_review in attempts:
        try:
            source_used = await _source_single_image(
                keywords=query,
                prompt=desc.get("prompt", ""),
                image_source=_image_source_for_slot(slot, config),
                config=config,
                output_path=img_path,
                client=client,
                seen_hashes=seen_hashes,
                lane=desc["lane"],
                allow_generation_fallback=False,
                fallback_to_illustration=False,
                require_relevance_review=require_review,
                narration=section.narration,
            )
        except Exception as e:
            logger.warning(
                f"Section {section.id} sub-image {sub_idx + 1}: "
                f"retry {query!r} failed: {e}"
            )
            continue

        if not source_used:
            continue

        if require_review:
            logger.info(
                f"Section {section.id} sub-image {sub_idx + 1}: "
                f"recovered with query {query!r}"
            )
        else:
            logger.warning(
                f"Section {section.id} sub-image {sub_idx + 1}: relevance "
                f"gate rejected every candidate; keeping the top-ranked "
                f"result for query {query!r}"
            )
        sourcing_log.append({
            "section_id": section.id,
            "sub_image_index": sub_idx + 1,
            "file": img_path.name,
            "keywords": query,
            "source": f"{source_used}_retry" if require_review
                      else f"{source_used}_top_ranked",
        })
        desc["sourced"] = True
        break

    # Slots whose policy demands a literal photograph of a specific real
    # person or event are never generated: an invented image of a real
    # transfer, match or player IS a fabricated news photograph. Those
    # slots are dropped instead, and the section renders without them.
    literal_photo_policy = (slot.visual_policy or "") in {
        "literal_google_photo",
        "google_photo_exact_action",
        "photo_backed_info_slide",
    }

    # A web-photo-only channel means exactly that: no generated stand-in,
    # ever. This became reachable once unsourceable slots stopped being
    # filled with a rejected candidate -- they fell through to here instead
    # and were quietly illustrated, which is how a Horror run ended up with
    # three 1344x768 generated images in a run that was supposed to contain
    # only real photographs. The beat is dropped instead.
    if config.image_sourcing.web_photos_only:
        if not desc.get("sourced", True):
            logger.warning(
                f"Section {section.id} sub-image {sub_idx + 1}: no real "
                f"photograph found; dropping the beat rather than "
                f"generating a stand-in (web_photos_only)"
            )
        return

    if not desc.get("sourced", True) and not literal_photo_policy:
        # Everything real has been tried. Generate a clearly illustrative
        # stand-in rather than drop the visual beat entirely.
        try:
            source_used = await _source_single_image(
                keywords=tiers[0] if tiers else slot.keywords,
                prompt=desc.get("prompt", "") or slot.prompt,
                image_source="ai_gen",
                config=config,
                output_path=img_path,
                client=client,
                seen_hashes=seen_hashes,
                lane=desc["lane"],
                allow_generation_fallback=True,
                allow_last_resort_generation=True,
                narration=section.narration,
            )
        except Exception as e:
            logger.warning(
                f"Section {section.id} sub-image {sub_idx + 1}: "
                f"last-resort generation failed: {e}"
            )
            source_used = None
        if source_used:
            sourcing_log.append({
                "section_id": section.id,
                "sub_image_index": sub_idx + 1,
                "file": img_path.name,
                "keywords": slot.keywords,
                "source": "ai_gen_last_resort",
            })
            desc["sourced"] = True


def minimum_slots_for_section(section, config: ChannelConfig) -> int:
    """How many visible beats this section needs to respect the hold cap.

    Uses the measured narration duration when audio has already been made,
    and the script's estimate before that. The estimate runs about 15% short
    of real Gemini TTS, so trusting it alone under-counts the beats a section
    will actually need.
    """
    rd = config.rendering_defaults
    duration = (
        getattr(section, "actual_duration_seconds", None)
        or getattr(section, "estimated_duration_seconds", None)
        or 0.0
    )
    if duration <= 0:
        return 1
    return minimum_visual_slots_for_duration(
        duration, rd.max_visual_hold_seconds, rd.intra_slot_crossfade
    )


def underpopulated_sections(
    script: Script,
    config: ChannelConfig,
) -> dict[int, tuple[int, int]]:
    """Sections with too few slots to fill their runtime inside the cap.

    Returns {section_id: (have, need)}. A section in here cannot be rendered
    honestly: the renderer's only options are to hold one image past the cap
    or to cycle the few it has, and cycling is what puts an unrelated image
    on a sentence it was never chosen for.
    """
    short: dict[int, tuple[int, int]] = {}
    for section in script.sections:
        have = len(section.slots)
        need = minimum_slots_for_section(section, config)
        if have < need:
            short[section.id] = (have, need)
    return short


def _subject_rescue_queries(script: Script, section, slot) -> list[str]:
    """Subject-anchored queries for a beat the widening pass could not fill.

    The relaxed tiers broaden a failing query; these narrow it back onto the
    named subject instead. A slot asking for 'ornate old wooden door locked
    dark hallway' fails because it describes a staged scene, while the story's
    actual subject -- the house, the town, the case -- is documented.

    Queries are built per script and never mixed. The first version of this
    used the video's Arabic title against an archive catalogued entirely in
    English ('Ø§Ù„Ø±Ø­Ù„Ø© 19 archival photograph' for Flight 19), which is the
    wrong key and a mixed-script query on top of it.
    """
    return build_bilingual_queries(
        keywords=slot.keywords or "",
        title=script.title or "",
        narration=getattr(section, "narration", "") or "",
        entities=[
            e for e in (getattr(script, "tags", None) or [])
            if isinstance(e, str) and e.strip()
        ][:5],
    )


async def _cover_unsourced_slots(
    *,
    descriptors: list[dict],
    config: ChannelConfig,
    sourcing_log: list[dict],
    videos_dir: Path,
    raw_dir: Path,
    seen_hashes: set[str],
    target_size: tuple[int, int],
    fps: int,
) -> int:
    """Give a beat something honest to show rather than losing the video.

    Runs last, on slots that web search, subject rescue and generation have all
    failed to fill. Two covers, in order:

      1. A licensed Pexels clip for the beat's own keywords. Stock footage of a
         mountain in a storm is not a record of this story's events, and the
         documentary review rules already hold it to the scene rather than the
         topic -- so it sets the moment without claiming to document it.
      2. An info_card: the renderer draws it from the slot's own text. No
         picture, no sourcing, nothing to get wrong.

    What this deliberately does not do is reuse an already-sourced image. A
    photograph that belongs to one beat becomes wrong the moment it is shown
    under another, and a video that cycles four images across twelve beats is
    the failure `enforce_minimum_slots` was written to prevent.

    Returns how many slots it covered.
    """
    if not getattr(config.image_sourcing, "complete_over_coverage", False):
        return 0

    unsourced = [d for d in descriptors if not d.get("sourced", True)]
    if not unsourced:
        return 0

    covered = 0
    broll_ok = getattr(config.image_sourcing, "allow_video_broll", False)

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for desc in unsourced:
            section = desc["section"]
            sub_idx = desc.get("sub_idx", 0)
            slot = desc["slot"]
            keywords = str(desc.get("keywords") or "").strip()

            # 1. Licensed motion footage for this beat's own subject.
            if broll_ok and keywords:
                video_path = (
                    videos_dir / f"section_{section.id:03d}_{sub_idx + 1:02d}.mp4"
                )
                try:
                    got = await _search_pexels_video(
                        keywords=keywords,
                        output_path=video_path,
                        client=client,
                        seen_hashes=seen_hashes,
                        target_duration=desc.get("target_duration", 4.0),
                        target_size=target_size,
                        fps=fps,
                    )
                except Exception as exc:
                    logger.debug(f"cover b-roll failed for section {section.id}: {exc}")
                    got = False

                if got:
                    frame_path = (
                        raw_dir / f"section_{section.id:03d}_{sub_idx + 1:02d}.jpg"
                    )
                    _extract_video_frame(video_path, frame_path)
                    slot.visual = "b_roll"
                    desc["sourced"] = True
                    covered += 1
                    sourcing_log.append({
                        "section_id": section.id,
                        "sub_image_index": sub_idx + 1,
                        "file": video_path.name,
                        "keywords": keywords,
                        "source": "pexels_video (coverage)",
                    })
                    logger.info(
                        f"Section {section.id} sub-image {sub_idx + 1}: covered "
                        f"with a licensed Pexels clip"
                    )
                    continue

            # 2. A card the renderer draws from the slot's own words.
            text = (
                str(getattr(slot, "prompt", "") or "").strip()
                or keywords
                or str(getattr(section, "narration", "") or "").strip()
            )
            if not text:
                continue
            slot.visual = "info_card"
            slot.props = dict(getattr(slot, "props", None) or {})
            slot.props.setdefault("text", text[:280])
            desc["sourced"] = True
            covered += 1
            sourcing_log.append({
                "section_id": section.id,
                "sub_image_index": sub_idx + 1,
                "file": None,
                "keywords": keywords,
                "source": "info_card (coverage)",
            })
            logger.info(
                f"Section {section.id} sub-image {sub_idx + 1}: no photograph "
                f"found; showing an info card instead of dropping the beat"
            )

    if covered:
        logger.info(
            f"covered {covered} unsourced beat(s) so the video can finish"
        )
    return covered


def _broll_allowed(config: ChannelConfig, used: int, total_slots: int) -> bool:
    """Whether one more slot may be sourced as a video clip.

    Two gates. The channel must have opted in, and the video must not become
    mostly motion footage: stock clips are atmosphere between photographs, and
    a story told mainly in stock footage stops looking like the story it is
    telling.
    """
    sourcing = config.image_sourcing
    if not getattr(sourcing, "allow_video_broll", False):
        return False
    ratio = float(getattr(sourcing, "max_broll_ratio", 0.34))
    if total_slots <= 0:
        return False
    # At least one clip is allowed whenever the channel opted in, so a short
    # video is not rounded down to none.
    cap = max(1, int(total_slots * ratio))
    return used < cap


def _corrected_brief(desc: dict) -> str:
    """The beat's brief, steered by whatever the reviewer asked for instead.

    A regeneration that reuses the rejected prompt reproduces the rejected
    image, which is how a beat can fail the same way on every attempt. The
    reviewer's suggestion is a positive description of the wanted subject, so
    it leads -- the original brief follows to keep the scene's staging.
    """
    brief = str(desc.get("prompt") or desc.get("keywords") or "").strip()
    suggestion = str(desc.get("review_suggestion") or "").strip()
    if not suggestion:
        return brief
    if not brief:
        return suggestion
    # Suggestion first: it is the correction, and the generator weights the
    # opening of a prompt most heavily.
    return f"{suggestion}. {brief}"


async def _generate_missing_visuals(
    *,
    descriptors: list[dict],
    config: ChannelConfig,
    sourcing_log: list[dict],
    allow_override: bool = False,
    limit_override: int | None = None,
    operation_label: str = "generated_fallback",
) -> generated_visuals.FallbackBudget | None:
    """Generate atmospheric frames for beats nothing could be found for.

    Runs only after web search and subject rescue have both failed, which is
    why it does not weaken `web_photos_only`: every photo-lane slot was still
    searched for on the web first, and a real photograph still wins every time.
    What changes is the outcome when there is no photograph -- previously the
    whole run was abandoned, now a handful of clearly non-documentary frames
    can cover the gap.

    Anything that would fabricate evidence, a real person or a named event is
    refused and the slot stays empty, so those runs still fail exactly as they
    did before. The generated files are marked `generated: True` in the
    sourcing log, and they face `image_review` like everything else.
    """
    sourcing = config.image_sourcing
    # `allow_override` is the rejected-beat policy, which is a separate
    # decision from the end-of-run cover for beats nothing could be found for.
    # A channel may want one and not the other.
    if not allow_override and not getattr(sourcing, "allow_generated_fallback", False):
        return None

    unsourced = [d for d in descriptors if not d.get("sourced", True)]
    if not unsourced:
        return None

    missing = [
        {
            "section_id": d["section"].id,
            # These two keys are what the descriptors actually carry. Reading
            # "sub_index" and "path" instead silently disabled the whole
            # fallback: output_path came back None for every slot, the
            # generation loop skipped each one on `if not target`, and nothing
            # was logged because nothing was attempted.
            "sub_image_index": d.get("sub_idx", 0) + 1,
            "output_path": d.get("img_path"),
            # Regenerating from the brief that just failed produces the image
            # that just failed -- observed as the same beat coming back as "a
            # fireplace screen instead of a security desk" on every attempt.
            #
            # The reviewer's *suggestion* is used rather than its complaint:
            # the complaint names the wrong thing ("a fireplace screen"), and
            # putting that in a generation prompt asks for more of it.
            "brief": _corrected_brief(d),
            "subject": d.get("subject", ""),
        }
        for d in unsourced
    ]

    planned, budget = generated_visuals.plan_fallback(
        missing,
        limit=(
            limit_override
            if limit_override is not None
            else int(getattr(sourcing, "max_generated_fallback_images", 5))
        ),
        style_suffix=sourcing.style_prompt_suffix,
    )
    if not planned:
        if budget.refused:
            logger.info(
                f"generated fallback produced nothing: {budget.refused[0]}"
            )
        return budget

    model = getattr(
        sourcing, "generated_fallback_model", "gemini-3.1-flash-lite-image"
    )
    target_size = tuple(config.video.resolution)

    # Addressed by the beat it belongs to, so a generated frame can only ever
    # mark its own slot sourced. The linear scan this replaces compared
    # `sub_index` -- a key no descriptor carries -- so it read 0 for every
    # beat and matched the first unsourced descriptor in the section whenever
    # the generated slot happened to be slot 1. Correctly generated frames for
    # any other slot marked nothing at all.
    by_slot = {
        (d["section"].id, d.get("sub_idx", 0) + 1): d
        for d in unsourced
    }

    for item in planned:
        target = item.get("output_path")
        if not target:
            continue
        try:
            written = await clients.generate_scene_image(
                item["prompt"],
                Path(target),
                model=model,
                # This call used to take the client's 16:9 default, so a
                # 1080x1920 channel got 1376x768 frames: landscape, under the
                # minimum portrait size, and straight into final validation.
                aspect_ratio=generation_aspect_ratio(target_size),
                image_size="1K",
                # Only the fal generator reads this; it sizes frames to the
                # render target because its presets are all below the minimum
                # source size this pipeline enforces.
                target_size=target_size,
                operation_label=operation_label,
            )
        except Exception as exc:
            logger.warning(
                f"fallback generation failed for section {item['section_id']}: {exc}"
            )
            continue

        # A refusal, a safety block and a 429 all come back as None rather
        # than an exception, and the quota case arrives in a burst -- three
        # beats in one second. Taking the call as proof of a file is how a run
        # reported image_source complete with section_002_01 never written.
        if written is None or not _is_usable_asset(Path(target)):
            logger.warning(
                f"fallback generation for section {item['section_id']} "
                f"sub-image {item['sub_image_index']} produced no usable file; "
                f"leaving the beat unsourced for the passes below"
            )
            continue

        # Whatever came back has to fit the frame the renderer will show. An
        # image that cannot be reframed is deleted here rather than left to
        # fail validation, and the beat falls through to the passes below.
        if not _conform_image_to_target(
            Path(target),
            target_size=target_size,
            label=(
                f"section {item['section_id']} sub-image "
                f"{item['sub_image_index']}"
            ),
        ):
            continue

        visual = generated_visuals.record_generated(
            budget,
            section_id=item["section_id"],
            sub_image_index=item["sub_image_index"],
            path=target,
            prompt=item["prompt"],
            model=model,
        )
        provenance = visual.to_provenance()
        sourcing_log.append(provenance)
        # Both stores, not just the sourcing log: asset_provenance.json is
        # written from _ASSET_PROVENANCE, and a generated frame missing from
        # it reads as a sourced photograph.
        _record_generated_asset_provenance(target, provenance)

        descriptor = by_slot.get((item["section_id"], item["sub_image_index"]))
        if descriptor is not None:
            descriptor["sourced"] = True
            # Read downstream so a generated frame is never treated as a
            # sourced photograph.
            descriptor["generated"] = True

    if budget.used:
        summary = budget.summary()
        logger.info(
            f"generated {summary['generated_images']} fallback visual(s) "
            f"(limit {summary['limit']}, refused {summary['refused']}, "
            f"cost ${summary['total_cost_usd']:.4f})"
        )
    return budget


async def _rescue_underpopulated_sections(
    *,
    descriptors: list[dict],
    script: Script,
    config: ChannelConfig,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    sourcing_log: list[dict],
    raw_dir: Path,
) -> None:
    """Third pass, for beats whose loss would leave a section too thin.

    The widening pass gives up on a slot in isolation. This one asks a
    different question -- would losing this beat break the section's pacing
    contract? -- and if so tries again with subject-anchored queries.

    The relevance gate stays ON throughout and web_photos_only is untouched,
    so this can only find a genuine photograph of the subject; it never lowers
    the bar for what counts as one, and it never generates an image.
    """
    short = underpopulated_sections(script, config)
    if not short:
        return

    at_risk = [
        d for d in descriptors
        if not d.get("sourced", True) and d["section"].id in short
    ]
    if not at_risk:
        return

    logger.warning(
        f"{len(short)} section(s) would fall below their visual minimum: "
        + "; ".join(
            f"section {sid} has {have} of {need}" for sid, (have, need) in short.items()
        )
    )
    logger.info(
        f"Re-sourcing {len(at_risk)} dropped beat(s) with subject-anchored queries"
    )

    for desc in at_risk:
        section, slot = desc["section"], desc["slot"]
        sub_idx = desc["sub_idx"]
        img_path = desc.get("img_path") or (
            raw_dir / f"section_{section.id:03d}_{sub_idx + 1:02d}.jpg"
        )
        desc["img_path"] = img_path
        # Resolved outside the try: this is configuration, not a search, and
        # a failure here is a bug rather than a query that found nothing. Left
        # inside, the broad except below would have reported it as "query
        # failed" and silently skipped every rescue attempt.
        image_source = _image_source_for_slot(slot, config)

        for query in _subject_rescue_queries(script, section, slot):
            try:
                source_used = await _source_single_image(
                    keywords=query,
                    prompt=desc.get("prompt", ""),
                    image_source=image_source,
                    config=config,
                    output_path=img_path,
                    client=client,
                    seen_hashes=seen_hashes,
                    lane=desc["lane"],
                    narration=section.narration,
                    # Never relaxed. A repeated image is bad; a confidently
                    # wrong one presented as documentary evidence is worse.
                    require_relevance_review=True,
                    # A missing historical photograph is never answered by
                    # generating one, on any channel.
                    allow_generation_fallback=False,
                    allow_last_resort_generation=False,
                )
            except Exception as exc:
                logger.debug(f"subject rescue query {query!r} failed: {exc}")
                continue
            if source_used:
                desc["sourced"] = True
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": img_path.name,
                    "keywords": query,
                    "source": f"{source_used} (subject rescue)",
                })
                logger.info(
                    f"Section {section.id} sub-image {sub_idx + 1}: recovered "
                    f"with {query!r}"
                )
                break


def enforce_minimum_slots(
    script: Script,
    config: ChannelConfig,
    *,
    only_sections: set[int] | None = None,
) -> None:
    """Fail the run when a section cannot be rendered without cycling images.

    Scoped to `only_sections` -- the sections that actually lost beats to
    failed sourcing. The contract this restores is the one the script
    validator already enforced and that dropping slots silently broke; it is
    not a new, stricter rule applied to scripts that never lost anything.

    Deliberately loud. The alternative is what shipped before: a section with
    two photographs stretched over six beats, each reappearing three times in
    twenty-five seconds, with images landing on sentences they were never
    chosen for. A clear failure is recoverable; a quietly repetitive video is
    published.
    """
    short = underpopulated_sections(script, config)
    if only_sections is not None:
        short = {sid: v for sid, v in short.items() if sid in only_sections}
    if not short:
        return
    detail = "; ".join(
        f"section {sid} has {have} real photograph(s) but needs {need} to keep "
        f"every beat under {config.rendering_defaults.max_visual_hold_seconds:.0f}s"
        for sid, (have, need) in sorted(short.items())
    )

    # A channel that would rather finish takes the thin section. Everything
    # that could cover the gap has already been tried by this point: search,
    # subject rescue, generation, a licensed clip, a text card.
    #
    # What the renderer then does is re-show an image from *within the same
    # section*, cycling from that section's start (_reuse_slots_to_hold_the_cap
    # in core/render_sections.py). That is a documentary editor returning to a
    # subject inside one beat group -- the image is real, already through the
    # review gate, and already has its provenance recorded. It is never another
    # section's photograph, so nothing is cycled across unrelated beats.
    if getattr(config.image_sourcing, "complete_over_coverage", False):
        logger.warning(
            f"Finishing with thin sections rather than abandoning the video: "
            f"{detail}. Those sections will re-show their own images to stay "
            f"under the hold cap; no image crosses into another section."
        )
        return

    raise RuntimeError(
        f"Not enough real photographs to render this video without repeating "
        f"images: {detail}. The run is stopped rather than cycling the same "
        f"few images across unrelated beats."
    )


def _drop_unsourced_slots(
    descriptors: list[dict],
    script: Script,
) -> dict[int, list[int]]:
    """Remove slots that could not be sourced so the run can still finish.

    Returns {section_id: [original slot index of each survivor]} for the
    sections that changed, which `_renumber_section_media` needs to re-index
    the media files. A section always keeps at least one slot; if every slot
    in a section failed there is nothing to render and the stage fails loudly.
    """
    doomed_by_section: dict[int, set[int]] = {}
    for desc in descriptors:
        if not desc.get("sourced", True):
            doomed_by_section.setdefault(desc["section"].id, set()).add(id(desc["slot"]))

    if not doomed_by_section:
        return {}

    survivor_map: dict[int, list[int]] = {}
    for section in script.sections:
        doomed = doomed_by_section.get(section.id)
        if not doomed:
            continue

        survivors: list[VisualSlot] = []
        survivor_indices: list[int] = []
        for old_idx, slot in enumerate(section.slots):
            if id(slot) in doomed:
                logger.warning(
                    f"Section {section.id} sub-image {old_idx + 1}: dropping "
                    f"unsourced {slot.visual} slot"
                )
                continue
            survivors.append(slot)
            survivor_indices.append(old_idx)

        if not survivors:
            raise RuntimeError(
                f"Section {section.id}: source failed for every visual slot "
                f"({len(section.slots)}); nothing left to render for this section"
            )

        logger.warning(
            f"Section {section.id} will render with its "
            f"{len(survivors)} remaining slot(s)"
        )
        section.slots = survivors
        survivor_map[section.id] = survivor_indices

    return survivor_map


def _expected_asset_path(
    *,
    section_id: int,
    sub_idx: int,
    raw_dir: Path,
    videos_dir: Path,
    target_size: tuple[int, int] | None = None,
) -> Path | None:
    """The file a beat actually has, or None.

    Deliberately the same rules core.validator.validate_raw_images applies --
    a clip counts, an image counts under any suffix the downloaders write, and
    when *target_size* is given an image must also be large enough to render
    from. The sourcer and the validator disagreeing about what "sourced" means
    is what let a stage report success with a beat the validator then rejected,
    first on a file that did not exist and later on five 1376x768 frames.

    An image that exists but is too small is deleted, not merely ignored:
    leaving it on disk would let `_renumber_section_media` rename it into a
    surviving beat's position once the unsourced one is dropped.
    """
    stem = f"section_{section_id:03d}_{sub_idx:02d}"
    clip = videos_dir / f"{stem}.mp4"
    if _is_usable_asset(clip):
        return clip
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = raw_dir / f"{stem}{suffix}"
        if not _is_usable_asset(candidate):
            continue
        if target_size is None:
            return candidate
        try:
            with Image.open(candidate) as img:
                width, height = img.size
        except Exception:
            candidate.unlink(missing_ok=True)
            continue
        if meets_minimum_source_size(width, height, target_size):
            return candidate
        min_w, min_h = minimum_source_size(target_size)
        logger.warning(
            f"{candidate.name} is {width}x{height}, below the minimum "
            f"{min_w}x{min_h}; discarding it and re-sourcing the beat"
        )
        candidate.unlink(missing_ok=True)
    return None


def _missing_expected_slots(
    script: Script,
    config: ChannelConfig,
    raw_dir: Path,
    videos_dir: Path,
) -> list[tuple[int, int]]:
    """Beats the script still promises with nothing usable on disk.

    "Usable" is the validator's bar, dimensions included: a beat holding an
    image too small to render from is treated as unsourced so the recovery
    chain replaces it, rather than left to fail the stage.
    """
    target_size = tuple(config.video.resolution)
    return [
        (section_id, sub_idx)
        for section_id, sub_idx in expected_sourced_image_slots(script, config)
        if _expected_asset_path(
            section_id=section_id,
            sub_idx=sub_idx,
            raw_dir=raw_dir,
            videos_dir=videos_dir,
            target_size=target_size,
        )
        is None
    ]


async def _reconcile_expected_assets(
    *,
    script: Script,
    config: ChannelConfig,
    descriptors: list[dict],
    sourcing_log: list[dict],
    seen_hashes: set[str],
    raw_dir: Path,
    videos_dir: Path,
    target_size: tuple[int, int],
    fps: int,
) -> bool:
    """Make the stage's success claim match what is on disk, or fail.

    Runs last, after every sourcing pass and the review gate. Each pass above
    tracks its own belief about whether a beat is done, and a belief can be
    wrong: a rate-limited generation returns None rather than raising, a
    download can leave nothing behind, a re-source after the review gate can
    quietly miss. This pass asks the filesystem instead, and gives every beat
    that comes up empty the full recovery chain one more time -- widened
    search, then a generated frame, then a licensed clip or a text card.

    A beat that survives all of that with no file is removed from the script,
    because the alternative is a stage that reports success and fails
    validation a moment later. If removal is not possible either, this raises:
    the stage failing is recoverable, a stage wrongly recorded as complete is
    not.

    Returns whether the script was changed.
    """
    missing = _missing_expected_slots(script, config, raw_dir, videos_dir)
    if not missing:
        return False

    logger.warning(
        f"{len(missing)} beat(s) reported sourced with no file on disk: "
        + ", ".join(f"section_{s:03d}_{i:02d}" for s, i in missing)
        + " â€” running per-asset recovery before the stage can complete"
    )

    by_slot = {
        (desc["section"].id, desc.get("sub_idx", 0) + 1): desc
        for desc in descriptors
    }

    recoverable: list[dict] = []
    for section_id, sub_idx in missing:
        desc = by_slot.get((section_id, sub_idx))
        if desc is None:
            # No descriptor to re-source with -- the drop below handles it.
            continue
        desc["sourced"] = False
        if not desc.get("img_path"):
            desc["img_path"] = raw_dir / f"section_{section_id:03d}_{sub_idx:02d}.jpg"
        recoverable.append(desc)

    if recoverable:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        ) as client:
            await _retry_missed_slots(
                descriptors=recoverable,
                script=script,
                config=config,
                client=client,
                seen_hashes=seen_hashes,
                sourcing_log=sourcing_log,
                raw_dir=raw_dir,
            )

        # Last resorts, in the order that keeps the most truth: a clearly
        # non-documentary generated frame, then a licensed clip, then a card
        # the renderer draws from the beat's own words. Each is gated by the
        # channel, and generated_visuals still refuses anything that would
        # fabricate evidence or a real person.
        await _generate_missing_visuals(
            descriptors=recoverable,
            config=config,
            sourcing_log=sourcing_log,
        )
        await _cover_unsourced_slots(
            descriptors=recoverable,
            config=config,
            sourcing_log=sourcing_log,
            videos_dir=videos_dir,
            raw_dir=raw_dir,
            seen_hashes=seen_hashes,
            target_size=target_size,
            fps=fps,
        )

    # Believe the filesystem, not the passes above.
    still_missing = _missing_expected_slots(script, config, raw_dir, videos_dir)
    if not still_missing:
        logger.info("per-asset recovery filled every missing beat")
        return True

    for section_id, sub_idx in still_missing:
        desc = by_slot.get((section_id, sub_idx))
        if desc is not None:
            desc["sourced"] = False

    survivor_map = _drop_unsourced_slots(descriptors, script)
    if survivor_map:
        _renumber_section_media(survivor_map, raw_dir, videos_dir)
        enforce_minimum_slots(script, config, only_sections=set(survivor_map))

    unresolved = _missing_expected_slots(script, config, raw_dir, videos_dir)
    if unresolved:
        raise RuntimeError(
            "image sourcing cannot complete: no asset for "
            + ", ".join(f"section_{s:03d}_{i:02d}" for s, i in unresolved)
            + " and the beat could not be recovered or removed"
        )

    logger.warning(
        f"removed {len(still_missing)} unrecoverable beat(s) so the stage "
        f"completes with every remaining beat backed by a real file"
    )
    return True


def _renumber_section_media(
    survivor_map: dict[int, list[int]],
    raw_dir: Path,
    videos_dir: Path,
) -> None:
    """Re-index media files after slots were dropped.

    Slot media is addressed by position (section_XXX_NN), so removing a slot
    would otherwise leave later files pointing at the wrong slot.
    """
    for section_id, old_indices in survivor_map.items():
        for new_idx, old_idx in enumerate(old_indices):
            if new_idx == old_idx:
                continue
            old_stem = f"section_{section_id:03d}_{old_idx + 1:02d}"
            new_stem = f"section_{section_id:03d}_{new_idx + 1:02d}"
            for directory, suffixes in (
                (raw_dir, (".jpg", ".jpeg", ".png", ".webp")),
                (videos_dir, (".mp4",)),
            ):
                for suffix in suffixes:
                    source = directory / f"{old_stem}{suffix}"
                    if not source.exists():
                        continue
                    target = directory / f"{new_stem}{suffix}"
                    target.unlink(missing_ok=True)
                    source.rename(target)
                    logger.info(f"Re-indexed {source.name} -> {target.name}")


async def _source_single_image(
    keywords: str,
    prompt: str,
    image_source: str,
    config: ChannelConfig,
    output_path: Path,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    lane: GenerationLane,
    allow_generation_fallback: bool = True,
    fallback_to_illustration: bool = False,
    require_relevance_review: bool = True,
    narration: str = "",
    allow_last_resort_generation: bool = False,
) -> str | None:
    """Use the script-specified source, falling back to generation when allowed.

    When *lane* is illustration, skip web/stock sources and go straight to AI
    generation with the illustration style prompt. Photo lanes fall back to AI
    generation unless allow_generation_fallback is false.
    """
    if lane == "photo":
        try:
            if image_source == "serper":
                success = await _search_serper(
                    keywords,
                    prompt,
                    output_path,
                    client,
                    seen_hashes,
                    tuple(config.video.resolution),
                    require_relevance_review=require_relevance_review,
                    narration=narration,
                )
            elif image_source == "pexels":
                success = await _search_pexels(
                    keywords,
                    prompt,
                    output_path,
                    client,
                    seen_hashes,
                    tuple(config.video.resolution),
                    narration=narration,
                )
            elif image_source == "ai_gen":
                success = False  # handled below
            else:
                return None

            if success:
                logger.info(f"Sourced {output_path.name} from {image_source}")
                return image_source
        except Exception as e:
            logger.warning(f"{output_path.name}: {image_source} failed: {e}")

        # The configured source found nothing. Before settling for a generated
        # image, ask the other freely-licensed catalogues -- a real photograph
        # from Pixabay, Unsplash or Commons is a better answer for a real
        # subject than an invented one, and this is the last point at which
        # that choice is still open.
        if (
            image_source in {"pexels", "serper"}
            and config.image_sourcing.open_library_fallback
        ):
            try:
                if await _search_open_libraries(
                    keywords,
                    prompt,
                    output_path,
                    client,
                    seen_hashes,
                    tuple(config.video.resolution),
                    narration=narration,
                ):
                    logger.info(f"Sourced {output_path.name} from an open library")
                    return "open_libraries"
            except Exception as e:
                logger.warning(f"{output_path.name}: open libraries failed: {e}")

        if not allow_generation_fallback:
            return None
        if config.test.preview_ai_image_prompts:
            return None

    if config.test.preview_ai_image_prompts:
        return None

    # News channels source real photographs. Generation is only reachable here
    # after every web-search tier has already missed, and only when the caller
    # explicitly opts in via allow_last_resort_generation -- so a descriptor
    # that forgets to pass allow_generation_fallback still cannot quietly
    # substitute AI art for a photo.
    if config.image_sourcing.web_photos_only and not allow_last_resort_generation:
        logger.info(
            f"{output_path.name}: no usable web photo yet; deferring to the "
            f"retry tiers rather than generating one"
        )
        return None

    # Generate with AI generation. Use illustration style when flagged.
    try:
        effective_lane: GenerationLane = (
            "illustration"
            if lane == "illustration"
            or fallback_to_illustration
            # A generated stand-in on a news channel must look like an
            # illustration. Rendering a photoreal image of a real player or a
            # real match would be fabricating a news photograph.
            or (allow_last_resort_generation and config.image_sourcing.web_photos_only)
            else "photo"
        )
        if allow_last_resort_generation and config.image_sourcing.web_photos_only:
            logger.warning(
                f"{output_path.name}: no real photograph found after every "
                f"search tier; falling back to a clearly illustrative image"
            )
        request = _generation_request_preview(
            keywords=keywords,
            prompt=prompt,
            lane=effective_lane,
            config=config,
        )
        if effective_lane == "illustration":
            logger.info(f"Sourcing illustration for {output_path.name}")
        target_size = tuple(config.video.resolution)
        result = await clients.generate_scene_image(
            request["prompt"], output_path,
            model=request["model"],
            # Match the render target rather than the client's 16:9 default:
            # a portrait channel was being handed landscape frames that then
            # failed the minimum source size.
            aspect_ratio=generation_aspect_ratio(target_size),
            image_size="1K",
            target_size=target_size,
            operation_label=request["operation"],
        )
        # A refusal, a safety block or a 429 comes back as None, and an
        # interrupted write can leave an empty file; neither is a sourced beat.
        if (
            result is not None
            and _is_usable_asset(output_path)
            # The requested aspect ratio is a request, not a guarantee.
            and _conform_image_to_target(
                output_path,
                target_size=target_size,
                label=output_path.name,
            )
        ):
            logger.info(
                f"Sourced {output_path.name} from ai_gen"
                f"{'(illustration)' if effective_lane == 'illustration' else ''}"
            )
            # Same store as the fallback path: a generated frame absent from
            # asset_provenance.json reads as a sourced photograph.
            _record_generated_asset_provenance(output_path, {
                "generated": True,
                "source": "ai_generated_lane",
                "provenance": "AI-generated illustration, not a photograph",
                "model": request["model"],
                "prompt": request["prompt"],
                "lane": effective_lane,
            })
            return "ai_gen"
        if result is not None:
            logger.warning(
                f"{output_path.name}: ai_gen reported success but wrote no "
                f"usable file"
            )
    except Exception as e:
        logger.warning(f"{output_path.name}: ai_gen fallback failed: {e}")

    return None


async def _search_serper(
    keywords: str,
    prompt: str,
    output_path: Path,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    target_size: tuple[int, int],
    *,
    require_relevance_review: bool = True,
    narration: str = "",
) -> bool:
    """Search for images using Serper.dev (Google Image Search wrapper).

    With require_relevance_review=False the vision gate is skipped and the
    top-ranked search result is kept. Only the last-resort retry does this.
    """
    with tempfile.TemporaryDirectory(prefix="vf_serper_candidates_") as tmp_dir:
        candidate_records = await _collect_serper_candidates(
            keywords=keywords,
            output_name=output_path.name,
            client=client,
            seen_hashes=seen_hashes,
            target_size=target_size,
            tmp_dir=Path(tmp_dir),
        )
        candidate_paths = [record["path"] for record in candidate_records]

        if not candidate_paths:
            return False

        winner = (
            await _select_photo_candidate(
                source_name="Serper",
                keywords=keywords,
                prompt=prompt,
                candidate_paths=candidate_paths,
                operation_label="serper_candidate_selection",
                narration=narration,
            )
            if require_relevance_review
            else candidate_paths[0]
        )
        if winner is None:
            logger.info(f"No acceptable Serper candidate for {output_path.name}")
            return False

        return _finalize_selected_photo_candidate(
            winner=winner,
            output_path=output_path,
            seen_hashes=seen_hashes,
            source_name="Serper",
        )



async def _search_open_libraries(
    keywords: str,
    prompt: str,
    output_path: Path,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    target_size: tuple[int, int],
    narration: str = "",
) -> bool:
    """Try the other freely-licensed libraries when Pexels comes up empty.

    Pixabay, Unsplash and Wikimedia Commons, asked in parallel with the same
    query ladder Pexels uses, and judged by the same candidate selection. Only
    Pexels is left out: it has already run by the time this is reached.

    This sits between stock search and AI generation on purpose. A beat with no
    photograph otherwise falls through to a generated image, and three more
    catalogues of real photographs is a better answer than a made-up one --
    which is the standing rule for this pipeline, not a preference.

    Licences differ across these sources, so each candidate's own licence and
    credit are recorded as it is downloaded, and Unsplash's use is reported
    back to Unsplash as their API guidelines require.
    """
    from core.providers import MediaKind, search_media
    from core.providers.media import (
        CommonsProvider,
        PixabayProvider,
        UnsplashProvider,
    )

    # A run has a fixed allowance for this tier. Without one it fires the whole
    # query ladder at three providers for every beat Pexels missed, which in a
    # real run meant ~60 Commons searches (tripping their robot policy with a
    # 403), all 50 of Unsplash's hourly demo requests, and a Gemini vision
    # review per beat that exhausted the model quota and failed image_review.
    #
    # The tier is a rescue, not a second sourcing pass. Capping it keeps it
    # useful for the handful of beats that need it and harmless to the rest.
    global _OPEN_LIBRARY_CALLS
    if _OPEN_LIBRARY_CALLS >= _OPEN_LIBRARY_BUDGET:
        logger.info(
            f"{output_path.name}: open-library budget spent "
            f"({_OPEN_LIBRARY_BUDGET} beats); leaving this beat to the next tier"
        )
        return False

    providers = [PixabayProvider(), UnsplashProvider(), CommonsProvider()]
    usable = [p for p in providers if p.status().usable]
    if not usable:
        return False

    queries = pexels_query_ladder(keywords, prompt=prompt)
    if not queries:
        return False

    _OPEN_LIBRARY_CALLS += 1

    # Only the first two rungs. The lower rungs are the vaguest phrasings, and
    # a vague query against a stock library returns generic imagery that the
    # relevance review rejects anyway -- so they cost requests without ever
    # producing a usable candidate.
    found: list = []
    for query in queries[:2]:
        result = await search_media(
            query,
            providers=usable,
            client=client,
            limit=_OPEN_LIBRARY_CANDIDATE_COUNT,
            kind=MediaKind.PHOTO,
            orientation="portrait",
        )
        found.extend(result.items)
        # Stop the moment there are enough candidates: another rung is another
        # round of provider requests for a shortlist that is already full.
        if len(found) >= _OPEN_LIBRARY_CANDIDATE_COUNT:
            break

    if not found:
        return False

    with tempfile.TemporaryDirectory(prefix="vf_open_candidates_") as tmp_dir:
        tmp = Path(tmp_dir)
        candidate_hashes: set[str] = set()
        candidate_paths: list[Path] = []
        by_path: dict[str, object] = {}

        for index, item in enumerate(found, start=1):
            # Every extra candidate is another image in the vision prompt, so
            # this bound is what actually caps the tier's model spend.
            if len(candidate_paths) >= _OPEN_LIBRARY_CANDIDATE_COUNT:
                break
            if not item.url:
                continue
            try:
                image_bytes = await _download_valid_image_bytes(
                    client, item.url, target_size, output_path.name)
            except Exception as exc:
                logger.debug(f"Skipping {item.provider} candidate: {exc}")
                continue
            if image_bytes is None:
                continue

            content_hash = hashlib.md5(image_bytes).hexdigest()
            if content_hash in seen_hashes or content_hash in candidate_hashes:
                continue
            candidate_hashes.add(content_hash)

            candidate_path = tmp / f"{item.provider}_{index:02d}.jpg"
            candidate_path.write_bytes(image_bytes)
            # The item already carries exactly the provenance fields this
            # records, so nothing is restated or lost in translation.
            _record_candidate_provenance(candidate_path, **item.to_provenance())
            candidate_paths.append(candidate_path)
            by_path[str(candidate_path)] = item

        if not candidate_paths:
            return False

        winner = await _select_photo_candidate(
            source_name="open libraries",
            keywords=keywords,
            prompt=prompt,
            candidate_paths=candidate_paths,
            operation_label="open_library_candidate_selection",
            narration=narration,
            # This tier's candidates are not relevance-ranked, so an unvetted
            # top result is a coin toss. Better to leave the beat to the next
            # tier than to ship one.
            fallback_to_top=False,
            # Each provider's own words for its result -- caption, tags, title.
            # This is what makes the free ranking meaningful.
            candidate_descriptions=[
                semantic_match.describe(by_path[str(path)])
                for path in candidate_paths
            ],
        )
        if winner is None:
            logger.info(f"No acceptable open-library candidate for {output_path.name}")
            return False

        chosen = by_path.get(str(winner))
        shipped = _finalize_selected_photo_candidate(
            winner=winner,
            output_path=output_path,
            seen_hashes=seen_hashes,
            source_name=getattr(chosen, "provider", "open libraries"),
        )

        # Unsplash requires a use to be reported back. Only once the photo has
        # actually shipped, and never at the cost of the beat.
        if shipped and getattr(chosen, "provider", "") == "unsplash":
            for provider in usable:
                if provider.name == "unsplash":
                    await provider.report_use(chosen, client=client)
                    break

        return shipped


async def _search_pexels(
    keywords: str,
    prompt: str,
    output_path: Path,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    target_size: tuple[int, int],
    narration: str = "",
) -> bool:
    """Search Pexels API and select the best image candidate."""
    with tempfile.TemporaryDirectory(prefix="vf_pexels_candidates_") as tmp_dir:
        candidate_records = await _collect_pexels_candidates(
            keywords=keywords,
            prompt=prompt,
            output_name=output_path.name,
            client=client,
            seen_hashes=seen_hashes,
            target_size=target_size,
            tmp_dir=Path(tmp_dir),
        )
        candidate_paths = [record["path"] for record in candidate_records]
        photo_ids = {
            str(record["path"]): str(record.get("pexels_id") or "")
            for record in candidate_records
        }

        try:
            if not candidate_paths:
                return False

            winner = await _select_photo_candidate(
                source_name="Pexels",
                keywords=keywords,
                prompt=prompt,
                candidate_paths=candidate_paths,
                operation_label="pexels_candidate_selection",
                narration=narration,
            )
            if winner is None:
                logger.info(f"No acceptable Pexels candidate for {output_path.name}")
                return False

            # Retired for the rest of the run before the file is written, so
            # no other beat can be handed the same photograph.
            _commit_pexels_photo(photo_ids.get(str(winner), ""))

            return _finalize_selected_photo_candidate(
                winner=winner,
                output_path=output_path,
                seen_hashes=seen_hashes,
                source_name="Pexels",
            )
        finally:
            # Everything this beat looked at and did not ship goes back into
            # the pool -- holding it would starve later beats of candidates.
            _release_pexels_photos(
                pid for pid in photo_ids.values()
                if pid not in _PEXELS_COMMITTED_IDS
            )


def _finalize_selected_photo_candidate(
    *,
    winner: Path,
    output_path: Path,
    seen_hashes: set[str],
    source_name: str,
) -> bool:
    selected_bytes = winner.read_bytes()
    seen_hashes.add(hashlib.md5(selected_bytes).hexdigest())
    output_path.write_bytes(selected_bytes)
    # Carry the candidate's provenance onto the file that actually ships, so a
    # finished video can be traced back to where each asset came from.
    record = _CANDIDATE_PROVENANCE.get(str(winner))
    if record:
        _ASSET_PROVENANCE[output_path.name] = {**record, "file": output_path.name}
    logger.info(
        f"Selected {source_name} candidate {winner.name} for {output_path.name}"
    )
    return True


async def _select_photo_candidate(
    *,
    source_name: str,
    keywords: str,
    prompt: str,
    candidate_paths: list[Path],
    operation_label: str,
    narration: str = "",
    fallback_to_top: bool = True,
    candidate_descriptions: list[str] | None = None,
) -> Path | None:
    global _CANDIDATE_SELECTION_CALLS

    # Every tier's selection passes through here, so this is the one place the
    # run's ceiling can be enforced. Past it, a beat keeps its top-ranked
    # result where that list was relevance-ranked, and is dropped where it was
    # not -- the same rule the tiers already follow, just without paying a
    # model to restate it.
    # Free ranking first. When one candidate clearly matches the beat's own
    # words and the others clearly do not, the vision model would be paid to
    # agree -- so it is skipped. Anything ambiguous, or a shortlist that looks
    # uniformly wrong, still goes to the model, because "none of these fit" is
    # a verdict only it can give.
    #
    # This is a ranking shortcut, not a gate: image_review still sees the
    # chosen file and can reject it.
    # Only when the caller supplied real captions. The candidate files are
    # temp names like `pixabay_01.jpg`, which say nothing about the image --
    # ranking those would be a confident guess built on noise.
    if candidate_descriptions and len(candidate_descriptions) == len(candidate_paths):
        local = semantic_match.rank(
            candidate_descriptions, f"{keywords} {prompt}")
        if local.confident:
            winner = candidate_paths[local.winner_index]
            logger.info(
                f"{source_name} candidate {local.winner_index + 1}/"
                f"{len(candidate_paths)} selected locally "
                f"({local.reason()}; no vision call)"
            )
            return winner

    if _CANDIDATE_SELECTION_CALLS >= _CANDIDATE_SELECTION_BUDGET:
        logger.info(
            f"{source_name}: candidate-selection budget spent "
            f"({_CANDIDATE_SELECTION_BUDGET} calls); "
            f"{'keeping the top-ranked result' if fallback_to_top else 'dropping the beat'}"
        )
        return candidate_paths[0] if (fallback_to_top and candidate_paths) else None
    _CANDIDATE_SELECTION_CALLS += 1
    try:
        review = await clients.review_with_vision(
            prompt=prompts.pexels_candidate_selection_prompt(
                keywords=keywords,
                prompt=prompt,
                num_images=len(candidate_paths),
                narration=narration,
            ),
            image_paths=candidate_paths,
            operation_label=operation_label,
        )
    except Exception as e:
        # The vision selector is a relevance *refinement* over results that
        # are already ranked by the search engine. If it is unavailable, keep
        # the top-ranked real photo rather than dropping the slot.
        #
        # That reasoning only holds where the list really was ranked for
        # relevance. The open-library tier merges three catalogues and orders
        # them by provider preference and shape, so its top result is not a
        # best match -- and shipping it unvetted is how a train corridor
        # reached a hospital beat and failed the image review gate.
        if not fallback_to_top:
            logger.warning(
                f"{source_name} candidate review unavailable ({e}); "
                f"dropping the beat rather than shipping an unvetted image"
            )
            return None
        logger.warning(
            f"{source_name} candidate review unavailable ({e}); "
            f"keeping the top-ranked search result"
        )
        return candidate_paths[0]
    if not review.get("approved", False):
        logger.info(
            f"{source_name} candidates rejected "
            f"(reason: {review.get('reason', 'n/a')})"
        )
        return None

    winner_index = review.get("winner_index")
    if type(winner_index) is not int or not (1 <= winner_index <= len(candidate_paths)):
        logger.warning(
            f"{source_name} candidate review returned an unusable winner_index "
            f"({winner_index!r} of {len(candidate_paths)}); keeping the "
            f"top-ranked search result"
        )
        return candidate_paths[0]

    logger.info(
        f"{source_name} candidate {winner_index}/{len(candidate_paths)} selected "
        f"(reason: {review.get('reason', 'n/a')})"
    )
    return candidate_paths[winner_index - 1]


async def _serper_images_request(
    query: str,
    client: httpx.AsyncClient,
    output_name: str,
) -> dict | None:
    """Call the Serper image endpoint, retrying only transient failures.

    Returns the parsed payload, or None when the search cannot be completed.
    4xx responses are permanent for this query (bad request, auth, quota
    exhausted) and are reported without retrying; timeouts, connection errors
    and 5xx/429 responses are retried with backoff.
    """
    payload = {"q": query, "num": 10, "imageType": "photo"}
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }

    for attempt in range(1, _SERPER_MAX_ATTEMPTS + 1):
        try:
            resp = await client.post(
                "https://google.serper.dev/images",
                headers=headers,
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == _SERPER_MAX_ATTEMPTS:
                logger.warning(
                    f"Serper search failed for {output_name} after "
                    f"{attempt} attempts: {type(e).__name__}: {e}"
                )
                return None
            await asyncio.sleep(_SERPER_RETRY_BACKOFF * attempt)
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                logger.warning(f"Serper returned non-JSON for {output_name}")
                return None

        retryable = resp.status_code == 429 or resp.status_code >= 500
        if not retryable:
            logger.warning(
                f"Serper rejected the search for {output_name} "
                f"(HTTP {resp.status_code}): {resp.text[:160]}"
            )
            return None
        if attempt == _SERPER_MAX_ATTEMPTS:
            logger.warning(
                f"Serper still returning HTTP {resp.status_code} for "
                f"{output_name} after {attempt} attempts"
            )
            return None
        await asyncio.sleep(_SERPER_RETRY_BACKOFF * attempt)

    return None


async def _collect_serper_candidates(
    *,
    keywords: str,
    output_name: str,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    target_size: tuple[int, int],
    tmp_dir: Path,
    limit: int = _SERPER_CANDIDATE_COUNT,
) -> list[dict[str, str | Path]]:
    if not settings.serper_api_key:
        return []

    query = (keywords or "").strip()
    if not query:
        # Serper answers a blank q with HTTP 400 "Missing query parameter".
        logger.warning(f"Serper search skipped for {output_name}: empty keywords")
        return []

    data = await _serper_images_request(query, client, output_name)
    if data is None:
        return []
    images = data.get("images", [])
    if not images:
        logger.info(f"Serper returned no images for {output_name}: {query!r}")
        return []

    # Nudge vertical-friendly results up without overriding Google's relevance
    # ordering: the bonus is worth at most ~3 positions, so a portrait photo
    # outranks a 16:9 one of similar relevance but never a clearly better
    # match. Relevance still decides; shape breaks ties.
    images = sorted(
        enumerate(images),
        key=lambda pair: pair[0] - 3.0 * verticality_score(
            int(pair[1].get("imageWidth") or 0),
            int(pair[1].get("imageHeight") or 0),
            target_size,
        ),
    )
    images = [img for _, img in images]

    candidate_hashes: set[str] = set()
    candidates: list[dict[str, str | Path]] = []
    for idx, img_result in enumerate(images, start=1):
        if len(candidates) >= limit:
            break

        img_url = img_result.get("imageUrl", "")
        source_url = img_result.get("source", "")
        if not img_url:
            continue
        if _is_stock_domain(img_url) or _is_stock_domain(source_url):
            logger.debug(f"Skipping stock domain: {source_url}")
            continue

        try:
            image_bytes = await _download_valid_image_bytes(
                client,
                img_url,
                target_size,
                output_name,
            )
        except Exception as e:
            logger.debug(f"Skipping Serper candidate download failure: {e}")
            continue
        if image_bytes is None:
            continue

        content_hash = hashlib.md5(image_bytes).hexdigest()
        if content_hash in seen_hashes or content_hash in candidate_hashes:
            continue
        candidate_hashes.add(content_hash)

        candidate_path = tmp_dir / f"serper_{idx:02d}.jpg"
        candidate_path.write_bytes(image_bytes)
        _record_candidate_provenance(
            candidate_path,
            platform="google_images_via_serper",
            url=img_url,
            source_page=source_url,
            # Serper returns search results, not licences. Recorded as unknown
            # rather than guessed, so a reviewer can see what needs checking.
            licence="unknown (web search result)",
            width=int(img_result.get("imageWidth") or 0),
            height=int(img_result.get("imageHeight") or 0),
        )
        candidates.append({"path": candidate_path, "source": "serper"})

    return candidates


# Words that carry no search signal. Shorter than the scripter's list on
# purpose: this one only has to stop a query from being padded out, and a word
# that names something ("photo album", "view finder") must survive.
_PEXELS_QUERY_STOPWORDS = frozenset("""
a an the of in on at to from with and or for by as is are was were be been
this that these those its it his her their there here into onto over under
above showing shows show seen looking view shot image photo photograph
picture close up wide angle scene depicting depicts featuring featured
""".split())


def _search_sized(text: str, *, max_words: int = 6) -> str:
    """Trim a brief to something a stock catalogue will actually match."""
    return " ".join(str(text or "").split()[:max_words]).strip()


def _content_words(text: str) -> list[str]:
    cleaned = re.sub(r"[^\w\s-]", " ", str(text or ""))
    return [
        word
        for word in cleaned.split()
        if len(word) > 1 and word.lower() not in _PEXELS_QUERY_STOPWORDS
    ]


def pexels_query_ladder(
    keywords: str,
    *,
    prompt: str = "",
    limit: int = _PEXELS_QUERY_LADDER_LIMIT,
) -> list[str]:
    """Beat-specific Pexels queries, most specific first.

    One phrasing per beat meant an unlucky brief cost the beat its picture --
    "hands holding a torn 1971 flight manifest at dusk" is not a photo anyone
    filed under that name, so the search came back empty and the beat was
    dropped. The ladder keeps asking the same question in shorter words:

        1. the brief with mood and style words removed
        2. the same, trimmed to a length a stock catalogue matches
        3. the subject with the staging around it stripped away
        4. its first few content words
        5. the trailing noun phrase, which is where the subject usually sits

    Every rung still describes this beat. None of them widen to the story, so
    a rescue cannot quietly turn into generic stock imagery.
    """
    base = " ".join(str(keywords or "").split())
    concrete = strip_mood_words(base) or base

    tiers = [
        # The brief as written leads: it is the most specific thing the beat
        # gave us, and it is what the sourcer has always searched for.
        concrete,
        _search_sized(concrete),
        *(_search_sized(v) for v in _documented_subject_queries(concrete)),
    ]

    words = _content_words(concrete)
    if len(words) > 3:
        tiers.append(" ".join(words[:3]))
    if len(words) >= 2:
        tiers.append(" ".join(words[-2:]))

    # Only when the slot's keywords held almost nothing to search with.
    if len(words) < 2:
        prompt_words = _content_words(strip_mood_words(prompt) or prompt)
        if prompt_words:
            tiers.append(" ".join(prompt_words[:4]))

    ordered: list[str] = []
    seen: set[str] = set()
    for tier in tiers:
        query = " ".join(str(tier or "").split())
        if not query or query.lower() in seen:
            continue
        seen.add(query.lower())
        ordered.append(query)

    # Pexels indexes English captions, so a query that is mostly another script
    # matches nothing -- unless it is the only thing the beat gave us.
    latin = [q for q in ordered if _latin_ratio(q) >= _MIN_LATIN_RATIO_FOR_SEARCH]
    ordered = latin or ordered

    # The cap takes the middle out, not the bottom. The broadest rung is the
    # one that finds a picture when the brief describes something nobody has
    # photographed, so trimming it away would cost exactly the beats this
    # ladder exists to rescue.
    if len(ordered) > limit:
        ordered = ordered[: max(limit - 1, 1)] + ordered[-1:]
    return ordered[:limit]


def _pexels_relevance(photo: dict, tokens: list[str]) -> float:
    """0..1 rating of how well a result matches the beat's own words.

    Pexels ranks for the query it was given, so results from the widest rung
    of the ladder come back confident and generic. Scoring every result back
    against the beat's original words puts the specific ones first again.
    """
    if not tokens:
        return 0.0
    haystack = re.sub(
        r"[^a-z0-9]+",
        " ",
        f"{photo.get('alt') or ''} {photo.get('url') or ''}".lower(),
    )
    matched = sum(1 for token in tokens if token in haystack)
    # Coverage is most of the score; the rest is reserved for phrasing, so a
    # caption that matches every word still ranks below one that also puts
    # them together. Two query words side by side describe the same thing --
    # "fishing boat" -- where the same two scattered across a caption often
    # describe two.
    score = 0.85 * (matched / len(tokens))
    for first, second in zip(tokens, tokens[1:]):
        if f"{first} {second}" in haystack:
            score += 0.15
            break
    return score


def _pexels_rank(photo: dict, tokens: list[str], target_size: tuple[int, int]) -> float:
    """Relevance leads; how well the photo survives the crop breaks ties."""
    fit = verticality_score(
        int(photo.get("width") or 0), int(photo.get("height") or 0), target_size
    )
    return (
        _PEXELS_RELEVANCE_WEIGHT * _pexels_relevance(photo, tokens)
        + (1 - _PEXELS_RELEVANCE_WEIGHT) * fit
    )


def _diversified_pexels_photos(photos: list[dict], limit: int) -> list[dict]:
    """Best-first, but not six frames from the same shoot.

    Photos over the per-photographer cap are deferred rather than dropped: a
    thin catalogue for a query should still fill the candidate set.
    """
    picked: list[dict] = []
    deferred: list[dict] = []
    per_photographer: dict[str, int] = {}

    for photo in photos:
        who = str(photo.get("photographer_id") or photo.get("photographer") or "")
        if who and per_photographer.get(who, 0) >= _PEXELS_MAX_PER_PHOTOGRAPHER:
            deferred.append(photo)
            continue
        per_photographer[who] = per_photographer.get(who, 0) + 1
        picked.append(photo)
        if len(picked) >= limit:
            return picked

    return (picked + deferred)[:limit]


async def _fetch_pexels_photos(
    client: httpx.AsyncClient,
    *,
    query: str,
    orientation: str,
    per_page: int,
    api_key: str,
) -> list[dict]:
    resp = await client.get(
        "https://api.pexels.com/v1/search",
        params={"query": query, "per_page": per_page, "orientation": orientation},
        headers={"Authorization": api_key},
    )
    resp.raise_for_status()
    return list(resp.json().get("photos") or [])


async def _fetch_pexels_videos(
    client: httpx.AsyncClient,
    *,
    query: str,
    orientation: str,
    api_key: str,
    per_page: int = 5,
) -> list[dict]:
    resp = await client.get(
        "https://api.pexels.com/v1/videos/search",
        params={"query": query, "per_page": per_page, "orientation": orientation},
        headers={"Authorization": api_key},
    )
    resp.raise_for_status()
    return list(resp.json().get("videos") or [])


async def _collect_pexels_candidates(
    *,
    keywords: str,
    output_name: str,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    target_size: tuple[int, int],
    tmp_dir: Path,
    limit: int = _PEXELS_CANDIDATE_COUNT,
    prompt: str = "",
) -> list[dict[str, str | Path]]:
    pexels_key = _usable_pexels_key()
    if not pexels_key:
        return []

    queries = pexels_query_ladder(keywords, prompt=prompt)
    if not queries:
        return []

    # Portrait first. The output is 1080x1920, and a landscape source loses
    # ~68% of its width to the crop -- asking Pexels for landscape was
    # discarding the vertical photos that survive reframing intact. Landscape
    # is still queried so a thin portrait catalogue cannot leave the slot
    # unsourced. The whole grid goes out at once: it is the same number of
    # requests the ladder would make one at a time, and a beat is no longer
    # waiting on the rung before it.
    searches = [
        (query, orientation)
        for query in queries
        for orientation in ("portrait", "landscape")
    ]
    results = await asyncio.gather(
        *[
            _fetch_pexels_photos(
                client,
                query=query,
                orientation=orientation,
                per_page=_PEXELS_PER_QUERY_PAGE,
                api_key=pexels_key,
            )
            for query, orientation in searches
        ],
        return_exceptions=True,
    )

    photos: list[dict] = []
    seen_results: set[str] = set()
    failures = 0
    for (query, orientation), result in zip(searches, results):
        if isinstance(result, BaseException):
            # One rung of the ladder failing is not the beat failing, and a
            # beat with weak results is not the run failing.
            failures += 1
            logger.debug(
                f"Pexels query {query!r} ({orientation}) failed: "
                f"{type(result).__name__}: {result}"
            )
            continue
        for photo in result:
            key = str(photo.get("id") or "") or str(
                (photo.get("src") or {}).get("large2x") or ""
            )
            if key:
                if key in seen_results:
                    continue
                seen_results.add(key)
            photos.append({**photo, "_query": query})

    if failures:
        logger.info(
            f"{output_name}: {failures}/{len(searches)} Pexels searches failed; "
            f"ranking the {len(photos)} result(s) that came back"
        )
    if not photos:
        logger.info(f"{output_name}: no Pexels results for {queries}")
        return []

    tokens = [word.lower() for word in _content_words(keywords)]
    photos.sort(key=lambda p: _pexels_rank(p, tokens, target_size), reverse=True)

    # Over-fetch the shortlist: photos already claimed by another beat, ones
    # that fail to download and ones that duplicate an earlier candidate all
    # come off it, and the beat should still end up with a full set.
    candidate_hashes: set[str] = set()
    candidates: list[dict[str, str | Path]] = []
    for photo in _diversified_pexels_photos(photos, limit * 3):
        if len(candidates) >= limit:
            break

        photo_id = str(photo.get("id") or "")
        if not _claim_pexels_photo(photo_id):
            continue

        img_url = (photo.get("src") or {}).get("large2x", "")
        if not img_url:
            _release_pexels_photos([photo_id])
            continue

        try:
            image_bytes = await _download_valid_image_bytes(
                client,
                img_url,
                target_size,
                output_name,
            )
        except Exception as e:
            logger.debug(f"Skipping Pexels candidate download failure: {e}")
            _release_pexels_photos([photo_id])
            continue
        if image_bytes is None:
            _release_pexels_photos([photo_id])
            continue

        content_hash = hashlib.md5(image_bytes).hexdigest()
        if content_hash in seen_hashes or content_hash in candidate_hashes:
            _release_pexels_photos([photo_id])
            continue
        candidate_hashes.add(content_hash)

        candidate_path = tmp_dir / f"pexels_{len(candidates) + 1:02d}.jpg"
        candidate_path.write_bytes(image_bytes)
        _record_candidate_provenance(
            candidate_path,
            platform="pexels",
            url=img_url,
            source_page=str(photo.get("url") or ""),
            licence="Pexels License (free to use, no attribution required)",
            attribution=str(photo.get("photographer") or ""),
            width=int(photo.get("width") or 0),
            height=int(photo.get("height") or 0),
        )
        candidates.append({
            "path": candidate_path,
            "source": "pexels",
            "pexels_id": photo_id,
            "query": str(photo.get("_query") or ""),
        })

    return candidates


def _validated_candidate_record(
    review: dict,
    *,
    key: str,
    candidate_records: list[dict[str, str | Path]],
    source_name: str,
) -> dict[str, str | Path] | None:
    index = review.get(key)
    if index is None:
        return None
    if type(index) is not int:
        raise ValueError(f"{source_name} review missing valid {key}: {review}")
    if index < 1 or index > len(candidate_records):
        raise ValueError(f"{source_name} review {key} out of range: {review}")
    return candidate_records[index - 1]


def _extract_video_frame(video_path: Path, frame_path: Path) -> bool:
    """Extract a frame from a video at 1s for review purposes."""
    cmd = [
        settings.ffmpeg_path, "-y",
        "-i", str(video_path),
        "-ss", "1", "-frames:v", "1",
        "-q:v", "2",
        str(frame_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.warning(f"Frame extraction failed for {video_path.name}")
        return False
    return True


async def _search_pexels_video(
    keywords: str,
    output_path: Path,
    client: httpx.AsyncClient,
    seen_hashes: set[str],
    target_duration: float,
    target_size: tuple[int, int],
    fps: int,
) -> bool:
    """Search Pexels Video API and download+prepare a B-roll clip."""
    pexels_key = _usable_pexels_key()
    if not pexels_key:
        return False

    # Portrait stock footage is far scarcer than portrait stills, so a beat
    # asking for motion needs more than one phrasing to find any. Same ladder
    # the photo path uses, and the same fan-out: portrait first for the 9:16
    # frame, landscape as the fallback that keeps the beat moving.
    # Only the specific rungs. A still that is loosely on-topic reads as
    # atmosphere; a *clip* that is loosely on-topic reads as stock footage and
    # is the thing viewers notice. The lowest rungs are two-word fragments --
    # "phone receiver" became a shot of money, "under the door" a train -- and
    # the final review rejected the video for exactly that. A missing clip
    # falls back to a still, which is a better outcome than a wrong clip.
    queries = pexels_query_ladder(keywords)[:2]
    if not queries:
        return False

    searches = [
        (query, orientation)
        for query in queries
        for orientation in ("portrait", "landscape")
    ]
    results = await asyncio.gather(
        *[
            _fetch_pexels_videos(
                client,
                query=query,
                orientation=orientation,
                api_key=pexels_key,
            )
            for query, orientation in searches
        ],
        return_exceptions=True,
    )

    videos: list[dict] = []
    pooled_ids: set[str] = set()
    failures: list[BaseException] = []
    for (query, orientation), result in zip(searches, results):
        if isinstance(result, BaseException):
            failures.append(result)
            logger.debug(
                f"Pexels video query {query!r} ({orientation}) failed: "
                f"{type(result).__name__}: {result}"
            )
            continue
        for video in result:
            video_id = str(video.get("id") or "")
            if video_id:
                if video_id in pooled_ids:
                    continue
                pooled_ids.add(video_id)
            videos.append(video)

    if not videos:
        if len(failures) == len(searches):
            # Every request erroring is a key or transport problem, not a thin
            # catalogue -- worth saying out loud. The beat still falls back to
            # a still rather than failing the run.
            logger.warning(
                f"{output_path.name}: every Pexels video search failed "
                f"({type(failures[0]).__name__}: {failures[0]}); "
                "falling back to still image sourcing"
            )
        else:
            logger.info(
                f"{output_path.name}: no Pexels footage for {queries}; "
                "falling back to still image sourcing"
            )
        return False

    # Clips that match the beat's own words first. Pexels ranks for whichever
    # rung found them, so without this the widest query's generic footage
    # outranks the specific clip a narrower one turned up.
    tokens = [word.lower() for word in _content_words(keywords)]
    videos.sort(key=lambda v: _pexels_rank(v, tokens, target_size), reverse=True)

    tw, th = target_size

    for video in videos:
        # Skip videos shorter than what we need
        video_duration = video.get("duration", 0)
        if video_duration < target_duration:
            continue

        # Find best HD video file (closest to target width, >= 1280px)
        candidates = [
            vf for vf in video.get("video_files", [])
            if vf.get("width", 0) >= 1280
            and vf.get("file_type", "") == "video/mp4"
        ]
        if not candidates:
            continue

        # Pick the file closest to target width (prefer smaller to save bandwidth)
        candidates.sort(key=lambda vf: abs(vf["width"] - tw))
        best_file = candidates[0]
        download_url = best_file.get("link", "")
        if not download_url:
            continue

        # Dedup by video ID, reserved before the download so two beats running
        # concurrently cannot both ship the same clip.
        video_id = str(video.get("id", ""))
        if video_id and video_id in seen_hashes:
            continue
        if video_id:
            seen_hashes.add(video_id)

        success = await _download_and_prepare_video(
            client, download_url, output_path,
            target_duration, target_size, fps,
        )
        if success:
            return True
        # The reservation only stands for a clip that shipped. A clip that
        # failed to download is still unused footage, and holding its id would
        # keep every later beat away from it too.
        if video_id:
            seen_hashes.discard(video_id)

    return False


async def _download_and_prepare_video(
    client: httpx.AsyncClient,
    url: str,
    output_path: Path,
    target_duration: float,
    target_size: tuple[int, int],
    fps: int,
) -> bool:
    """Download a video and prepare it (trim, scale, crop) for the assembler."""
    try:
        resp = await client.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"B-roll download failed: {e}")
        return False

    # Write to temp file, then FFmpeg process into final output
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    try:
        tmp.write(resp.content)
        tmp.close()

        cmd = _build_browser_safe_broll_encode_cmd(
            input_path=Path(tmp.name),
            output_path=output_path,
            target_duration=target_duration,
            target_size=target_size,
            fps=fps,
        )
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            logger.warning(f"B-roll encode failed: {result.stderr[-300:]}")
            return False

        logger.info(f"B-roll prepared: {output_path.name} ({target_duration:.1f}s)")
        return True
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _build_browser_safe_broll_encode_cmd(
    *,
    input_path: Path,
    output_path: Path,
    target_duration: float,
    target_size: tuple[int, int],
    fps: int,
) -> list[str]:
    w, h = target_size
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    return [
        settings.ffmpeg_path,
        "-y",
        "-i",
        str(input_path),
        "-vf",
        vf,
        "-t",
        f"{target_duration:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-profile:v",
        "high",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(output_path),
    ]


async def _try_download_image(
    client: httpx.AsyncClient,
    img_url: str,
    output_path: Path,
    seen_hashes: set[str],
    target_size: tuple[int, int],
) -> bool:
    """Download an image URL and validate it's a real image with PIL before saving."""
    try:
        image_bytes = await _download_valid_image_bytes(
            client,
            img_url,
            target_size,
            output_path.name,
        )
        if image_bytes is None:
            return False

        # Content-hash dedup: skip if we already saved this exact image
        content_hash = hashlib.md5(image_bytes).hexdigest()
        if content_hash in seen_hashes:
            return False
        seen_hashes.add(content_hash)

        output_path.write_bytes(image_bytes)
        return True
    except Exception:
        return False


async def _download_valid_image_bytes(
    client: httpx.AsyncClient,
    img_url: str,
    target_size: tuple[int, int],
    output_name: str,
) -> bytes | None:
    img_resp = await client.get(img_url, timeout=20)
    img_resp.raise_for_status()

    try:
        img = Image.open(io.BytesIO(img_resp.content))
        width, height = img.size
        img.verify()
    except Exception:
        return None

    if width == target_size[0] and height == target_size[1]:
        return img_resp.content

    if not meets_minimum_source_size(width, height, target_size):
        upscale = _reframe_upscale_factor(width, height, target_size)
        if upscale <= _MAX_REFRAME_UPSCALE:
            logger.info(
                f"Reframing lower-resolution image for {output_name}: "
                f"{width}x{height} into {target_size[0]}x{target_size[1]} "
                f"({upscale:.2f}x)"
            )
            return _normalize_photo_bytes_for_target(
                img_resp.content,
                target_size=target_size,
            )
        logger.info(
            f"Skipping low-resolution image for {output_name}: {width}x{height} "
            f"would need {upscale:.2f}x enlargement "
            f"(max {_MAX_REFRAME_UPSCALE:.1f}x)"
        )
        return None

    return _normalize_photo_bytes_for_target(
        img_resp.content,
        target_size=target_size,
    )
