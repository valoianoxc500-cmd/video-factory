"""Stage 3: Image processing — subject-aware crop/resize.

Prepares images for video assembly. Geometry only — no visual effects here.
Visual effects (grain, vignette) are applied at video level in Stage 4.

The reframing itself lives in core.framing, shared with image_sourcer so a
photo is framed by the same rules wherever it enters the pipeline.
"""

import logging
from pathlib import Path

from PIL import Image

from core.framing import subject_aware_fit
from core.utils import ChannelConfig

logger = logging.getLogger("video_factory")


def process_images(
    workspace: Path,
    config: ChannelConfig,
    target_size: tuple[int, int] = (1920, 1080),
    watermark_path: Path | None = None,
) -> list[Path]:
    """Process all raw images into ready-for-assembly images.

    Steps per image:
    1. Smart crop to 16:9 using face/subject detection
    2. Resize to target resolution
    3. Apply channel watermark (optional)

    Returns list of output paths.
    """
    raw_dir = workspace / "images" / "raw"
    ready_dir = workspace / "images" / "ready"
    ready_dir.mkdir(parents=True, exist_ok=True)

    output_paths = []
    for img_path in sorted(raw_dir.glob("section_*")):
        out_path = ready_dir / img_path.with_suffix(".png").name
        if out_path.exists():
            output_paths.append(out_path)
            continue

        try:
            img = Image.open(img_path).convert("RGB")
            img = _fit_to_canvas(img, target_size)

            if watermark_path and watermark_path.exists():
                img = _apply_watermark(
                    img,
                    watermark_path,
                    position=config.style.watermark.get("position", "bottom_right"),
                    opacity=config.style.watermark.get("opacity", 0.3),
                )

            img.save(out_path, "PNG")
            output_paths.append(out_path)
            logger.info(f"Processed: {img_path.name} → {out_path.name}")

        except Exception as e:
            logger.error(f"Failed to process {img_path.name}: {e}")

    logger.info(f"Processed {len(output_paths)} images")
    return output_paths


def _fit_to_canvas(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """Fill the target canvas, cropping toward the subject.

    A 16:9 photo letterboxed into a 1080x1920 frame occupies about a fifth of
    the height, with blurred bars over the rest -- it reads as a landscape
    video pasted into a vertical one, and the final-review gate rejects it as
    "not native vertical video". So the default is a subject-aware cover crop.

    The blurred-surround path is kept for sources too extreme to crop (wide
    panoramas), where filling the frame would cut the subject out entirely.
    """
    tw, th = target_size
    iw, ih = img.size
    target_ratio = tw / th
    img_ratio = iw / ih

    # If aspect ratio matches (within tolerance), just resize
    if abs(img_ratio - target_ratio) < 0.05:
        return img.resize(target_size, Image.Resampling.LANCZOS)

    # Shared with image_sourcer so the two reframing passes cannot disagree.
    # It keeps the crop on the subject with portrait headroom, and handles the
    # panorama case with a blurred surround internally.
    return subject_aware_fit(img, target_size)


def _apply_watermark(
    img: Image.Image,
    watermark_path: Path,
    position: str = "bottom_right",
    opacity: float = 0.3,
) -> Image.Image:
    """Overlay a watermark image with transparency."""
    try:
        wm = Image.open(watermark_path).convert("RGBA")
        # Scale watermark to ~10% of image width
        wm_w = img.size[0] // 10
        wm_h = int(wm.size[1] * (wm_w / wm.size[0]))
        wm = wm.resize((wm_w, wm_h), Image.Resampling.LANCZOS)

        # Apply opacity
        alpha = wm.getchannel("A")
        alpha = alpha.point(lambda a: int(a * opacity))
        wm.putalpha(alpha)

        # Position
        margin = 20
        pos_map = {
            "bottom_right": (img.size[0] - wm_w - margin, img.size[1] - wm_h - margin),
            "bottom_left": (margin, img.size[1] - wm_h - margin),
            "top_right": (img.size[0] - wm_w - margin, margin),
            "top_left": (margin, margin),
        }
        pos = pos_map.get(position, pos_map["bottom_right"])

        result = img.copy().convert("RGBA")
        result.paste(wm, pos, wm)
        return result.convert("RGB")

    except Exception as e:
        logger.warning(f"Watermark failed: {e}")
        return img
