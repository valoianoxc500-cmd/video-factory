"""Draw thumbnail headline text onto generated artwork.

Image models cannot render Arabic reliably: the glyphs come back unjoined,
mirrored, or as invented letterforms, so a thumbnail whose headline is baked
into the generated image always fails the "exact text" review gate. For
right-to-left languages the artwork is generated without text and the headline
is composited here, where shaping and bidi ordering are deterministic.
"""

import logging
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("video_factory")

# Faces that carry Arabic glyphs, heaviest first. Segoe UI Black and most
# other display weights ship Latin only, so coverage is verified at load time
# rather than assumed from the file name.
_RTL_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arialbd.ttf",     # Arial Bold
    r"C:\Windows\Fonts\tahomabd.ttf",    # Tahoma Bold
    r"C:\Windows\Fonts\trado.ttf",       # Traditional Arabic Bold
    r"C:\Windows\Fonts\segoeui.ttf",     # Segoe UI
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/GeezaPro.ttc",
)

# An Arabic presentation form (what the reshaper emits) and a codepoint no
# font defines. Identical rasters mean both fell back to .notdef.
_COVERAGE_PROBE = "\ufedf"       # ARABIC LETTER LAM INITIAL FORM
_UNDEFINED_PROBE = "\ue123"      # Private-use area, never mapped

_RTL_SCRIPT_RANGES = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0700, 0x074F),  # Syriac
    (0x0780, 0x07BF),  # Thaana
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB1D, 0xFDFF),  # Hebrew/Arabic presentation forms
    (0xFE70, 0xFEFF),  # Arabic presentation forms-B
)


def is_rtl_text(text: str) -> bool:
    """Whether the string contains right-to-left script."""
    return any(
        any(low <= ord(ch) <= high for low, high in _RTL_SCRIPT_RANGES)
        for ch in text
    )


def shape_rtl(text: str) -> str:
    """Apply Arabic joining and bidi reordering for a non-shaping renderer.

    Pillow only shapes complex scripts when built with Raqm; without it the
    letters render isolated and left-to-right. Reshaping to presentation forms
    and reversing via the bidi algorithm produces the same visual result.
    """
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError:
        logger.warning(
            "arabic-reshaper/python-bidi not installed; RTL thumbnail text will "
            "render unjoined. Install them with: pip install -r requirements.txt"
        )
        return text
    return get_display(arabic_reshaper.reshape(text))


def _glyph_raster(font: ImageFont.FreeTypeFont, char: str) -> bytes:
    mask = font.getmask(char, mode="L")
    return bytes(mask) if mask.size[0] else b""


def _covers_arabic(font: ImageFont.FreeTypeFont) -> bool:
    """Whether the face has real Arabic glyphs rather than .notdef boxes."""
    try:
        covered = _glyph_raster(font, _COVERAGE_PROBE)
        missing = _glyph_raster(font, _UNDEFINED_PROBE)
    except Exception:
        return False
    return bool(covered) and covered != missing


def _load_font(size: int, *, needs_arabic: bool = True) -> ImageFont.FreeTypeFont:
    fallback: ImageFont.FreeTypeFont | None = None
    for candidate in _RTL_FONT_CANDIDATES:
        if not Path(candidate).exists():
            continue
        try:
            font = ImageFont.truetype(candidate, size)
        except OSError:
            continue
        if not needs_arabic or _covers_arabic(font):
            return font
        fallback = fallback or font
        logger.debug(f"Font {Path(candidate).name} has no Arabic glyphs; skipping")

    if fallback is not None:
        raise RuntimeError(
            "No installed font has Arabic glyphs. Install Noto Naskh Arabic "
            "(or Arial/Tahoma on Windows), or add a path to _RTL_FONT_CANDIDATES."
        )
    raise RuntimeError(
        "No usable TrueType font found for thumbnail headlines. "
        "Add a path to _RTL_FONT_CANDIDATES."
    )


def _wrap(
    draw: ImageDraw.ImageDraw,
    words: list[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join(current + [word])
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def draw_headline(
    image_path: Path,
    text: str,
    *,
    fill: str = "#FFFFFF",
    stroke: str = "#000000",
    max_lines: int = 3,
) -> None:
    """Composite `text` onto the image at `image_path`, in place.

    The headline is placed across the lower third with a heavy outline so it
    stays legible over any artwork.
    """
    headline = unicodedata.normalize("NFC", text.strip())
    if not headline:
        return

    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    width, height = img.size

    max_width = int(width * 0.88)
    rtl = is_rtl_text(headline)
    # Wrap on logical words, then shape each finished line so joining and
    # bidi ordering are applied to exactly what gets drawn.
    words = headline.split()

    size = int(height * 0.16)
    while size > 16:
        font = _load_font(size, needs_arabic=rtl)
        lines = _wrap(draw, words, font, max_width)
        if len(lines) <= max_lines:
            widest = max(draw.textlength(ln, font=font) for ln in lines)
            if widest <= max_width:
                break
        size = int(size * 0.9)
    else:
        font = _load_font(16, needs_arabic=rtl)
        lines = _wrap(draw, words, font, max_width)[:max_lines]

    rendered = [shape_rtl(ln) if rtl else ln for ln in lines]

    line_height = int(size * 1.25)
    block_height = line_height * len(rendered)
    y = height - int(height * 0.08) - block_height
    stroke_width = max(3, size // 12)

    for line in rendered:
        line_width = draw.textlength(line, font=font)
        x = (width - line_width) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke,
        )
        y += line_height

    img.save(image_path, "PNG")
    logger.info(
        f"Composited {'RTL ' if rtl else ''}headline onto {image_path.name} "
        f"({len(rendered)} line(s) at {size}px)"
    )
