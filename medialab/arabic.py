"""Arabic caption text that actually reads like Arabic.

Shared by AI Video Maker and Police Chase Studio, because a caption bug fixed
in one product and not the other is a bug the customer still sees.

Four things have to be right at once, and each fails silently in the finished
MP4 rather than raising:

  **Shaping.** Arabic letters change form by position -- initial, medial,
  final, isolated. Unshaped text renders as a row of disconnected islands that
  a reader parses letter by letter, if at all.

  **Direction.** The logical order stored in a file is not the visual order.
  libass does not run the Unicode bidi algorithm, so the text has to arrive
  already reordered. Skip this and Arabic renders backwards.

  **Mixed runs.** A sentence with Arabic, an English brand name and a number
  has three directional runs in it. Naive reversal mangles the Latin and the
  digits; the bidi algorithm handles them, which is why it is used rather than
  a `[::-1]`.

  **Segmentation.** Splitting Arabic every N words gives lines that break
  mid-phrase, and Arabic phrases are longer than English ones. Breaking on
  conjunctions and prepositions instead keeps a line readable.

The other half of the job is layout: at most two lines, balanced, inside a
safe margin, at a size a phone can read.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "has_arabic", "shape", "segment_phrases", "wrap_two_lines",
    "font_file", "font_name", "fonts_dir", "size_for", "ARABIC_FONTS",
    "estimate_width",
]

_ARABIC_RANGES = (
    ("\u0600", "\u06FF"),   # Arabic
    ("\u0750", "\u077F"),   # Arabic Supplement
    ("\u08A0", "\u08FF"),   # Arabic Extended-A
    ("\uFB50", "\uFDFF"),   # Presentation Forms-A
    ("\uFE70", "\uFEFF"),   # Presentation Forms-B
)

#: Words a line should not end on. Arabic conjunctions and prepositions bind
#: tightly to what follows, so a break after one reads as a stutter.
#:
#: Written as escapes rather than literals on purpose: this file has already
#: been corrupted once by a PowerShell Get-Content/Set-Content round trip that
#: double-encoded every Arabic character, and the damage was invisible -- the
#: mojibake simply never matched, so binding detection silently stopped
#: working while the tests still passed. Escapes cannot be mangled that way.
_BINDING = {
    "\u0648",                      # wa   (and)
    "\u0623\u0648",                # aw   (or)
    "\u062b\u0645",                # thumma (then)
    "\u0641\u064a",                # fi   (in)
    "\u0645\u0646",                # min  (from)
    "\u0625\u0644\u0649",          # ila  (to)
    "\u0639\u0644\u0649",          # ala  (on)
    "\u0639\u0646",                # an   (about)
    "\u0645\u0639",                # maa  (with)
    "\u0628\u064a\u0646",          # bayn (between)
    "\u0639\u0646\u062f",          # inda (at)
    "\u062d\u062a\u0649",          # hatta (until)
    "\u0644\u0643\u0646",          # lakin (but)
    "\u0644\u0623\u0646",          # li-anna (because)
    "\u0623\u0646",                # an   (that)
    "\u0625\u0646",                # inna (indeed)
    "\u0643\u0645\u0627",          # kama (as)
    "\u0628\u0639\u062f",          # baad (after)
    "\u0642\u0628\u0644",          # qabl (before)
    "\u0645\u0646\u0630",          # mundhu (since)
    "\u062e\u0644\u0627\u0644",    # khilal (during)
    "\u0647\u0630\u0627",          # hadha (this m.)
    "\u0647\u0630\u0647",          # hadhihi (this f.)
    "\u0630\u0644\u0643",          # dhalik (that)
    "\u0627\u0644\u062a\u064a",    # allati (which f.)
    "\u0627\u0644\u0630\u064a",    # alladhi (which m.)
    "\u0643\u0644",                # kull (every)
    "\u0628\u0639\u0636",          # baad (some)
    "\u063a\u064a\u0631",          # ghayr (other than)
    "\u0628\u0644",                # bal (rather)
}

#: Where a line may break. Sentence enders first, then clause boundaries.
#: Includes the Arabic question mark (U+061F), comma (U+060C) and
#: semicolon (U+061B), which are different codepoints from the Latin ones.
_STRONG_BREAK = re.compile("[.!?\u061f\u2026]+")
_SOFT_BREAK = re.compile("[,\u060c;\u061b:]+")

#: Fonts that can shape Arabic, best first. Bundled OFL faces are preferred
#: over whatever the host happens to have: a deployment should render the same
#: captions everywhere, and the system fallbacks differ per machine.
#:
#: The entry requirement is complete coverage of the Arabic presentation forms
#: (U+FB50-FEFF), and it is not a formality. Tajawal was the default here and
#: had to be dropped: it carries 116 of those codepoints and is missing all 36
#: *isolated* forms, so every word beginning with alef, beh, noon or jeem lost
#: its first letter to a tofu box in the finished MP4. Nothing raised. Noto
#: Sans Arabic, Noto Kufi Arabic, Arial and Tahoma all cover the full set --
#: `test_every_arabic_font_can_draw_every_letter` is what keeps that true.
ARABIC_FONTS: dict[str, tuple[str, ...]] = {
    "notosansarabic": ("NotoSansArabic.ttf",),
    "notokufiarabic": ("NotoKufiArabic.ttf",),
    # System fallbacks, only if the bundled files are somehow missing.
    "arial": ("arialbd.ttf", "arial.ttf"),
    "tahoma": ("tahomabd.ttf", "tahoma.ttf"),
}

#: Nominal size multiplier per face, so a preset's size means the same thing
#: whichever font draws it. Measured by rendering the same word at one nominal
#: size and comparing the ink: the Noto Arabic faces draw an x-height of 24px
#: where Tajawal drew 35px, because their vertical metrics leave room for
#: stacked marks. Without this, switching to Noto silently shrank every
#: caption by a third.
_SIZE_SCALE = {
    "notosansarabic": 1.45,
    "notokufiarabic": 1.45,
    "arial": 1.0,
    "tahoma": 1.0,
}


def size_for(family: str, size: int) -> int:
    """The size to ask the renderer for so `size` is what the viewer sees."""
    return max(8, round(size * _SIZE_SCALE.get(family, 1.0)))

_BUNDLED = Path(__file__).resolve().parent / "fonts"
_SYSTEM_DIRS = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts"),
    Path("/System/Library/Fonts/Supplemental"),
)


def has_arabic(text: str) -> bool:
    return any(
        any(low <= ch <= high for low, high in _ARABIC_RANGES) for ch in str(text or "")
    )


def font_file(family: str) -> Path | None:
    """The .ttf backing an Arabic family, bundled copies first."""
    for candidate in ARABIC_FONTS.get(family, ARABIC_FONTS["notosansarabic"]):
        bundled = _BUNDLED / candidate
        if bundled.exists():
            return bundled
        for directory in _SYSTEM_DIRS:
            path = directory / candidate
            if path.exists():
                return path
    return None


def fonts_dir() -> Path | None:
    """The bundled font directory, for FFmpeg's `fontsdir`.

    libass only sees fonts it has been pointed at. Without this the bundled
    OFL faces are invisible to the renderer no matter what the style names.
    """
    return _BUNDLED if _BUNDLED.exists() else None


def font_name(family: str) -> str:
    """The family name libass will match, e.g. "Tajawal" for Tajawal-Bold.ttf.

    Not the filename stem. libass resolves a style's font by the family name
    recorded inside the file, so asking for "Tajawal-Bold" matches nothing and
    silently substitutes some default face -- which for Arabic usually means
    unjoined or missing glyphs. The name table is the only authority on this,
    so it is read rather than guessed.
    """
    path = font_file(family)
    if path is None:
        return family
    try:
        from PIL import ImageFont

        return ImageFont.truetype(str(path), 20).getname()[0] or path.stem
    except Exception:
        return path.stem


def shape(text: str) -> str:
    """Join Arabic letters into their contextual forms.

    Joining only. The visual reordering is deliberately left to the renderer,
    and getting that division wrong is what produced the broken captions this
    module was written to fix. Measured on the FFmpeg build this ships with
    (libass with harfbuzz and fribidi), rendering the same sentence three
    ways:

      * plain logical text -- libass shapes it itself, but Tajawal loses
        glyphs doing it: alef-hamza, final beh and final teh came out as tofu
        boxes. Correct order, missing letters.
      * reshaped **and** bidi-reordered -- libass reorders it a second time.
        The words land in the right places, so it passes a glance, but every
        letter is drawn in the form it had before the reversal and the text
        renders visibly disconnected. This is what the first Arabic captions
        looked like.
      * reshaped only -- correct joining, correct right-to-left order,
        correct placement of digits and the Arabic question mark.

    So: presentation forms in logical order, and libass runs bidi over them.

    A no-op on text with no Arabic in it, so callers can apply it to every
    line without checking first. Falls back to the original string if the
    reshaper is unavailable -- disconnected letters are bad, but an exception
    that loses the whole caption track is worse.
    """
    body = str(text or "")
    if not has_arabic(body):
        return body
    try:
        import arabic_reshaper

        # Ligatures off: the default set turns common word pairs into single
        # glyphs that many fonts lack, which shows up as tofu boxes.
        reshaper = arabic_reshaper.ArabicReshaper(
            configuration={"delete_harakat": False, "support_ligatures": False}
        )
        return reshaper.reshape(body)
    except Exception:
        return body


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", str(text or "").strip()) if t]


def estimate_width(text: str, size: int) -> float:
    """Rough rendered width in pixels.

    Only needs to be good enough to decide where to break a line. Arabic
    glyphs are narrower than Latin capitals at the same point size, and the
    joined script packs tighter still, hence the lower factor.
    """
    body = str(text or "")
    factor = 0.46 if has_arabic(body) else 0.55
    return len(body) * size * factor


#: A phrase shorter than this on its own reads as a mistake -- one word
#: flashing between two full captions.
_RUNT_CHARS = 14


def _absorb_runts(phrases: list[str], *, max_chars: int) -> list[str]:
    """Fold a stranded tail back into its neighbour.

    Packing words up to a character limit regularly leaves the last two or
    three of a clause on their own -- "...during the decades" then "past".
    Merging backwards where it fits keeps the clause intact.
    """
    merged: list[str] = []
    for phrase in phrases:
        if (
            merged
            and len(phrase) <= _RUNT_CHARS
            and len(merged[-1]) + len(phrase) + 1 <= int(max_chars * 1.35)
        ):
            merged[-1] = f"{merged[-1]} {phrase}"
        else:
            merged.append(phrase)
    return merged


def segment_phrases(text: str, *, max_chars: int = 42) -> list[str]:
    """Split narration into caption-sized phrases at natural boundaries.

    Sentence enders bind hardest, then commas, then word count -- and never
    after a conjunction or preposition, because Arabic binds those to what
    follows and a break there reads as a stutter.
    """
    body = " ".join(str(text or "").split())
    if not body:
        return []

    chunks: list[str] = []
    for sentence in _STRONG_BREAK.split(body):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue
        for clause in _SOFT_BREAK.split(sentence):
            clause = clause.strip()
            if clause:
                chunks.append(clause)

    phrases: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            phrases.append(chunk)
            continue
        # Still too long: pack words up to the limit, never ending on a word
        # that binds to the next one.
        current: list[str] = []
        for token in _tokens(chunk):
            trial = current + [token]
            if current and len(" ".join(trial)) > max_chars:
                while len(current) > 1 and current[-1] in _BINDING:
                    token = current.pop() + " " + token
                phrases.append(" ".join(current))
                current = [token]
            else:
                current = trial
        if current:
            phrases.append(" ".join(current))

    return _absorb_runts([p for p in phrases if p], max_chars=max_chars)


def wrap_two_lines(text: str, *, max_chars: int = 30) -> list[str]:
    """Lay a phrase out on at most two balanced lines.

    Two is the cap because a third line pushes into the middle of a vertical
    frame and starts covering the subject. Balanced because a 9-word line
    above a 1-word line looks like a mistake, and the break avoids binding
    words for the same reason `segment_phrases` does.
    """
    body = " ".join(str(text or "").split())
    if not body:
        return []
    if len(body) <= max_chars:
        return [body]

    tokens = _tokens(body)
    if len(tokens) == 1:
        return [body]

    # The split point closest to the middle that does not strand a binding
    # word at the end of the first line.
    best_index, best_cost = 1, None
    for index in range(1, len(tokens)):
        first = " ".join(tokens[:index])
        second = " ".join(tokens[index:])
        cost = abs(len(first) - len(second))
        if tokens[index - 1] in _BINDING:
            cost += 40
        if len(first) > max_chars * 1.6 or len(second) > max_chars * 1.6:
            cost += 80
        if best_cost is None or cost < best_cost:
            best_index, best_cost = index, cost

    return [" ".join(tokens[:best_index]), " ".join(tokens[best_index:])]
