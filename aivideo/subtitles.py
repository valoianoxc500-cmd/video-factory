"""Word timings into a burned-in caption track, Arabic included.

The caption format is ASS rather than SRT. SRT can only carry text; ASS carries
the font, size, outline, shadow, colours and position, which is the entire
point of a caption-style picker. Everything the customer chose in the UI is
expressible here and is burned by one FFmpeg filter.

Arabic is the part that needs care. Three separate things have to be right or
the output is wrong in a way no exception reports:

  * **shaping** -- Arabic letters change form by position. Unshaped text
    renders as disconnected islands. `arabic_reshaper` fixes this.
  * **direction** -- the logical order stored in the file is not the visual
    order. libass does not run the bidi algorithm, so the text is reordered
    here with `python-bidi`. Without it Arabic renders backwards.
  * **the font** -- a family with no Arabic coverage draws boxes or falls back
    silently to something that cannot join. The resolver below only returns
    files that exist on this machine.

Numbers are deliberately left in Western digits: bidi already places them
correctly, and Arabic short-form content overwhelmingly uses them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from aivideo.spec import CaptionStyle
from aivideo.voice import Word

logger = logging.getLogger("aivideo")

#: Font family -> candidate files, most preferred first. Bundled families are
#: tried before system ones so a deployment can pin its own look, and every
#: family ends at a file that is present on essentially any machine.
_FONT_FILES: dict[str, tuple[str, ...]] = {
    "inter": ("Inter-Bold.ttf", "Inter.ttf", "segoeui.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"),
    "montserrat": ("Montserrat-Black.ttf", "Montserrat-Bold.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"),
    "playfair": ("PlayfairDisplay-Bold.ttf", "georgiab.ttf", "georgia.ttf", "DejaVuSerif-Bold.ttf"),
    # Arabic-capable
    "cairo": ("Cairo-Bold.ttf", "Cairo-Regular.ttf", "tahomabd.ttf", "arialbd.ttf",
              "NotoNaskhArabic-Bold.ttf", "DejaVuSans-Bold.ttf"),
    "tajawal": ("Tajawal-Bold.ttf", "tahomabd.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"),
    "notoarabic": ("NotoNaskhArabic-Bold.ttf", "NotoSansArabic-Bold.ttf", "arialbd.ttf"),
    "arial": ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"),
    "tahoma": ("tahomabd.ttf", "tahoma.ttf", "DejaVuSans-Bold.ttf"),
}

_FONT_DIRS = (
    Path(__file__).resolve().parent / "fonts",
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts"),
    Path("/System/Library/Fonts/Supplemental"),
)


def resolve_font(family: str) -> tuple[str, Path | None]:
    """(ASS font name, file on disk). The file is what FFmpeg actually needs.

    Arabic families resolve through `medialab.arabic`, which prefers the OFL
    faces bundled with the repository over whatever the host machine happens
    to have installed -- a caption should look the same on every deployment,
    and system Arabic fallbacks differ wildly.
    """
    from medialab import arabic as ar

    if family in ar.ARABIC_FONTS:
        path = ar.font_file(family)
        if path is not None:
            # The family name from the file's name table, not the filename:
            # "Tajawal-Bold.ttf" is the family "Tajawal", and a style asking
            # for "Tajawal-Bold" matches nothing and gets substituted.
            return ar.font_name(family), path

    for candidate in _FONT_FILES.get(family, _FONT_FILES["arial"]):
        for directory in _FONT_DIRS:
            path = directory / candidate
            if path.exists():
                return path.stem, path
    return family, None


def _has_arabic(text: str) -> bool:
    from medialab import arabic as ar

    return ar.has_arabic(text)


def shape_arabic(text: str) -> str:
    """Join Arabic so libass draws it correctly.

    One implementation, in `medialab.arabic`, shared with Police Chase Studio.
    Two copies of this is how the two products ended up with captions that
    were broken in different ways; the rule about who runs bidi is subtle
    enough that it must exist in exactly one place.

    A no-op for text with no Arabic in it, so this is safe to call on every
    line regardless of the run's language.
    """
    from medialab import arabic as ar

    return ar.shape(text)


@dataclass
class Line:
    text: str
    start: float
    end: float
    words: list[Word]


def group_lines(
    words: list[Word], per_line: int, *, language: str = "en"
) -> list[Line]:
    """Chunk word timings into caption phrases.

    Two strategies, because the languages break differently.

    Arabic goes through `medialab.arabic.segment_phrases`, which breaks on
    clause boundaries and never after a conjunction or preposition. Counting
    words instead produced captions that split mid-phrase -- the single
    ugliest thing about the old Arabic output -- because Arabic binds those
    particles to what follows.

    Latin keeps the word-count rule, with sentence punctuation as an early
    break, which reads correctly and is what the English presets are tuned
    against.

    Either way the timing comes from the word boundaries: phrases are matched
    back onto the word stream in order, so a phrase shows exactly while its
    words are spoken.
    """
    from medialab import arabic as ar

    per_line = max(1, per_line)
    if not words:
        return []

    if language == "ar" or ar.has_arabic(" ".join(w.text for w in words[:40])):
        return _phrase_lines(words, per_line)

    lines: list[Line] = []
    bucket: list[Word] = []
    for word in words:
        bucket.append(word)
        ends_sentence = bool(re.search(r"[.!?،؟…]$", word.text.strip()))
        if len(bucket) >= per_line or ends_sentence:
            lines.append(Line(
                " ".join(w.text for w in bucket).strip(),
                bucket[0].start, bucket[-1].end, list(bucket),
            ))
            bucket = []

    if bucket:
        lines.append(Line(
            " ".join(w.text for w in bucket).strip(),
            bucket[0].start, bucket[-1].end, list(bucket),
        ))
    return [line for line in lines if line.text]


def _phrase_lines(words: list[Word], per_line: int) -> list[Line]:
    """Arabic phrases, timed from the word stream they came from.

    `per_line` becomes a ceiling on phrase length rather than an exact word
    count: a phrase that ends naturally at three words stays three words.
    """
    from medialab import arabic as ar

    # Roughly the characters that fit on two readable lines at preset sizes.
    max_chars = max(18, per_line * 9)
    phrases = ar.segment_phrases(
        " ".join(w.text for w in words), max_chars=max_chars
    )
    if not phrases:
        return []

    lines: list[Line] = []
    cursor = 0
    for phrase in phrases:
        needed = len(phrase.split())
        bucket = words[cursor:cursor + needed]
        if not bucket:
            break
        cursor += needed
        lines.append(Line(phrase, bucket[0].start, bucket[-1].end, list(bucket)))

    # Any words the segmentation dropped (punctuation collapsing can shorten
    # the token count) still have to be shown, or the caption track ends early.
    if cursor < len(words):
        tail = words[cursor:]
        lines.append(Line(
            " ".join(w.text for w in tail).strip(),
            tail[0].start, tail[-1].end, list(tail),
        ))
    return [line for line in lines if line.text]


def _ass_colour(hex_colour: str, default: str = "&H00FFFFFF") -> str:
    """#RRGGBB or #RRGGBBAA -> ASS &HAABBGGRR. ASS is BGR and alpha-inverted."""
    value = str(hex_colour or "").lstrip("#")
    if len(value) not in (6, 8):
        return default
    r, g, b = value[0:2], value[2:4], value[4:6]
    alpha = "00" if len(value) == 6 else f"{255 - int(value[6:8], 16):02X}"
    return f"&H{alpha}{b}{g}{r}".upper()


#: ASS numeric alignment for a bottom-anchored numpad layout.
_ALIGNMENT = {"bottom": 2, "center": 5, "top": 8}


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def build_ass(
    words: list[Word],
    style: CaptionStyle,
    size: tuple[int, int],
    output: Path,
    *,
    language: str = "en",
) -> Path | None:
    """Write the caption track. None when there is nothing to caption."""
    if not style.enabled or not words:
        return None

    lines = group_lines(words, style.words_per_line, language=language)
    if not lines:
        return None

    from medialab import arabic as ar

    width, height = size
    font_name, font_file = resolve_font(style.font)
    # A preset's size is what the viewer should see, not what the font is
    # asked for: the Arabic faces draw about a third smaller than the Latin
    # ones at the same nominal size.
    font_size = ar.size_for(style.font, style.size)
    if font_file is None:
        logger.warning(
            f"[aivideo] no file found for caption font {style.font!r}; "
            f"libass will substitute"
        )

    primary = _ass_colour(style.text_color)
    outline = _ass_colour(style.outline_color, "&H00000000")
    # Karaoke highlighting uses the secondary colour; presets that do not
    # highlight simply set it to the same value as the text.
    secondary = _ass_colour(style.highlight_color, primary)

    boxed = str(style.background or "none").lower() != "none"
    back = _ass_colour(style.background, "&H80000000") if boxed else "&H00000000"
    border_style = 3 if boxed else 1        # 3 = opaque box behind the text

    margin_v = int(height * (0.10 if style.position == "bottom" else 0.04))
    alignment = _ALIGNMENT.get(style.position, 2)
    bold = -1 if style.weight in {"bold", "black", "semibold"} else 0

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_name},{font_size},{primary},{secondary},{outline},{back},{bold},0,0,0,100,100,0,0,{border_style},{style.outline_width},0,{alignment},{int(width * 0.06)},{int(width * 0.06)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # Characters that fit on one line inside the safe margins, measured at
    # the size the glyphs are actually drawn at rather than the nominal one.
    usable_px = width * 0.88
    per_line_chars = max(12, int(usable_px / max(1.0, ar.estimate_width("x", font_size))))

    events: list[str] = []
    for line in lines:
        text = line.text.upper() if style.uppercase else line.text
        # Two lines maximum: a third pushes into the middle of a vertical
        # frame and starts covering the subject.
        wrapped = ar.wrap_two_lines(text, max_chars=per_line_chars)
        # Shape each line separately and only after wrapping. Shaping first
        # would reorder the string, and a break chosen in visual order lands
        # in the wrong place in the sentence.
        shaped = [ar.shape(part) for part in wrapped]
        body = r"\N".join(shaped)
        # Escape the characters ASS treats as markup, leaving the line break.
        body = body.replace("{", "(").replace("}", ")")
        events.append(
            f"Dialogue: 0,{_timestamp(line.start)},{_timestamp(line.end)},"
            f"Caption,,0,0,0,,{body}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    logger.info(
        f"[aivideo] captions: {len(lines)} lines, font {font_name}"
        f"{' (Arabic shaped)' if language == 'ar' else ''}"
    )
    return output
