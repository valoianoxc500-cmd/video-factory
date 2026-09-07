"""Guard against speech-to-text drifting out of Arabic mid-narration.

The failure this exists for
---------------------------
Google's chirp_2 recognizer, pinned to a single ar-EG language code, still
switches language when it meets an English proper noun. On the Flight 19 test
the narration read

    ... الملازم تشارلز تيلور، مربكاً، حيث أبلغ عن ضياعه وأن بوصلاته لا تعمل بشكل صحيح

and immediately after "تشارلز تيلور" (Charles Taylor) the transcript became

    30.12s  بیکن     31.88s  انڈیا   ("India", in Urdu)
    31.04s  ہائتو    32.40s  ہی
    31.40s  ابلگا    33.88s  ہو

Eight of fifty-eight tokens, a contiguous 5.2-second burst, rendered on screen
as unreadable Urdu. The audio was correct Arabic throughout; only the captions
were wrong.

How it is detected
------------------
Persian and Urdu use letters that Modern Standard Arabic never does -- گ چ پ ژ
ڈ ڑ ہ ی ے and friends. Their presence in an Arabic transcript is not a
judgement call, it is a script violation, so detection is exact rather than
heuristic. On the clean section of that same video this flagged nothing.

How it is repaired
------------------
The narration text is known exactly -- the audio is a reading of it -- so a
corrupted span is not guesswork to fix. The drifted words are replaced with
the narration words that belong in that time range, keeping the surrounding
STT timings. Alignment uses an orthography-insensitive comparison, because a
correct transcript still writes خرافه for خرافة.

Nothing here touches the audio. The spoken narration is never modified.
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger("video_factory")

# Letters used in Persian/Urdu (and other Perso-Arabic scripts) that never
# appear in Modern Standard Arabic. Seeing one means the recognizer changed
# language, not that the writer chose an unusual spelling.
FOREIGN_LETTERS = frozenset(
    "گ"  # گ  gaf
    "چ"  # چ  che
    "پ"  # پ  pe
    "ژ"  # ژ  zhe
    "ڤ"  # ڤ  ve
    "ڈ"  # ڈ  ddal
    "ڑ"  # ڑ  rre
    "ں"  # ں  noon ghunna
    "ہ"  # ہ  heh goal
    "ھ"  # ھ  heh doachashmee
    "ۂ"  # ۂ
    "ۃ"  # ۃ
    "ی"  # ی  farsi yeh
    "ے"  # ے  yeh barree
    "ڵ"  # ڵ
    "ڕ"  # ڕ
    "ۆ"  # ۆ
    "ۇ"  # ۇ
    "ې"  # ې
)

_DIACRITICS = re.compile(r"[ً-ْـٰ]")
_NON_LETTER = re.compile(r"[^ء-ي٠-٩a-zA-Z0-9]")


def contains_foreign_script(text: str) -> bool:
    """Whether `text` uses a letter Modern Standard Arabic never uses."""
    return bool(FOREIGN_LETTERS & set(text or ""))


def drifted_indices(words: list[dict]) -> list[int]:
    """Positions of transcript tokens that left the Arabic script."""
    return [
        i for i, w in enumerate(words or [])
        if contains_foreign_script(str(w.get("word", "")))
    ]


def drift_ratio(words: list[dict]) -> float:
    """Share of tokens that left the Arabic script, 0.0 when there are none."""
    if not words:
        return 0.0
    return len(drifted_indices(words)) / len(words)


def describe_drift(words: list[dict]) -> str:
    """A short, loggable account of what drifted and when."""
    bad = drifted_indices(words)
    if not bad:
        return "no script drift"
    first, last = words[bad[0]], words[bad[-1]]
    sample = ", ".join(str(words[i].get("word", "")) for i in bad[:4])
    return (
        f"{len(bad)}/{len(words)} tokens ({len(bad) / len(words):.0%}) left the "
        f"Arabic script between {float(first.get('start', 0)):.1f}s and "
        f"{float(last.get('end', 0)):.1f}s: {sample}"
    )


def normalize_arabic(text: str) -> str:
    """Fold the spelling differences a correct transcript still produces.

    STT writes خرافه for خرافة and ابلغ for أبلغ. Those are the same word for
    alignment purposes, so alef forms, both yehs, ta marbuta and diacritics are
    all folded before comparison.
    """
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _DIACRITICS.sub("", text)
    for src, dst in (
        ("أ", "ا"), ("إ", "ا"), ("آ", "ا"),  # أإآ -> ا
        ("ة", "ه"),                                             # ة  -> ه
        ("ى", "ي"),                                             # ى  -> ي
        ("ی", "ي"),                                             # ی  -> ي
        ("ہ", "ه"), ("ھ", "ه"),                       # ہھ -> ه
    ):
        text = text.replace(src, dst)
    return _NON_LETTER.sub("", text).lower()


def _spread(words: list[str], start: float, end: float) -> list[dict]:
    """Lay `words` across [start, end], weighted by length.

    Matches the deterministic estimator's model: longer words take longer to
    say, so an even split drifts noticeably by the end of a phrase.
    """
    span = max(end - start, 0.01)
    weights = [len(w) + 2 for w in words]      # +2 approximates the inter-word gap
    total = sum(weights) or 1
    out: list[dict] = []
    cursor = start
    for word, weight in zip(words, weights):
        width = span * (weight / total)
        out.append({
            "word": word,
            "start": round(cursor, 3),
            "end": round(min(cursor + width, end), 3),
        })
        cursor += width
    return out


def repair_word_timestamps(
    words: list[dict],
    narration: str,
) -> tuple[list[dict], int]:
    """Replace drifted tokens with the narration words that belong there.

    Returns (words, replaced_count). The list is returned unchanged when there
    is no drift. When the clean tokens around the drift cannot be located in
    the narration, the caller is told nothing was repaired so it can fall back
    to deriving every timing from the text.
    """
    bad = drifted_indices(words)
    if not bad:
        return words, 0

    narration_words = narration.split()
    if not narration_words:
        return words, 0
    norm_narration = [normalize_arabic(w) for w in narration_words]

    def _locate(token: str, search_from: int) -> int:
        """Index of `token` in the narration at or after `search_from`."""
        needle = normalize_arabic(token)
        if not needle:
            return -1
        for i in range(search_from, len(norm_narration)):
            if norm_narration[i] == needle:
                return i
        return -1

    first, last = bad[0], bad[-1]

    # Anchor on the last clean token before the drift and the first after it.
    left_anchor = -1
    narration_cursor = 0
    for i in range(first):
        found = _locate(str(words[i].get("word", "")), narration_cursor)
        if found >= 0:
            left_anchor = found
            narration_cursor = found + 1

    right_anchor = len(narration_words)
    for i in range(last + 1, len(words)):
        found = _locate(str(words[i].get("word", "")), narration_cursor)
        if found >= 0:
            right_anchor = found
            break

    replacement = narration_words[left_anchor + 1:right_anchor]
    if not replacement or right_anchor <= left_anchor:
        logger.warning(
            "caption repair: could not anchor the drifted span in the "
            "narration; falling back to text-derived timings"
        )
        return words, 0

    start = float(words[first].get("start", 0.0))
    end = float(words[last].get("end", start))
    repaired = words[:first] + _spread(replacement, start, end) + words[last + 1:]

    logger.info(
        f"caption repair: replaced {len(bad)} drifted token(s) with "
        f"{len(replacement)} narration word(s) across "
        f"{start:.1f}s-{end:.1f}s"
    )
    return repaired, len(bad)
