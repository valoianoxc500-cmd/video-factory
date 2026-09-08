"""Captions in a language the narrator is not speaking.

Captions have always come out of the narration audio itself: Gemini speaks the
script, Google STT transcribes that audio, and the word timings STT returns are
what puts each word on screen at the moment it is said. That is why captions
have matched narration exactly -- they *are* the narration, measured.

Choosing a caption language different from the voice language breaks that
identity. There is no transcript of English words in an Arabic recording, so
the timings for them have to come from somewhere else.

They come from the narration's own timings, at sentence granularity:

    1. The narration's STT words are grouped into the sentences of the script.
    2. Each sentence keeps its measured [start, end] -- the span in which the
       narrator says that sentence, which is a real measurement and not a
       guess.
    3. The sentence is translated as a unit, and the translated words are laid
       across that same span, weighted by length.

So a caption line appears and disappears exactly when its sentence is spoken.
Within a line the individual word boundaries are interpolated rather than
measured, because no measurement of them exists -- word order differs between
languages, and pretending otherwise would put a word on screen at a time
nothing was said.

That is the honest guarantee this module makes, and it is the one the UI
states: caption lines stay in sync with narration sentence by sentence.

When voice and caption language match, none of this runs -- the STT timings are
used directly, exactly as before.
"""

from __future__ import annotations

import logging
import re

import clients
from core.caption_integrity import _spread

logger = logging.getLogger("video_factory")

#: Sentence enders in both scripts. Arabic uses ؟ and ، alongside the Latin set.
_SENTENCE_END = re.compile(r"(?<=[.!?؟])\s+|(?<=[.!?؟])$")

#: Languages the caption track can be written in, and how to name them to the
#: translator. Kept tiny on purpose: each one is a promise that the renderer
#: can shape the text (right-to-left, font coverage) correctly.
LANGUAGE_NAMES = {
    "ar": "Modern Standard Arabic",
    "en": "English",
}

RTL_LANGUAGES = {"ar"}


def is_supported(code: str) -> bool:
    return str(code or "").strip().lower() in LANGUAGE_NAMES


def is_rtl(code: str) -> bool:
    return str(code or "").strip().lower() in RTL_LANGUAGES


def split_sentences(text: str) -> list[str]:
    """The script's sentences, in order, with whitespace normalised."""
    parts = [
        " ".join(part.split())
        for part in _SENTENCE_END.split(str(text or "").strip())
    ]
    return [p for p in parts if p]


def group_words_into_sentences(
    words: list[dict],
    sentences: list[str],
) -> list[dict]:
    """Assign measured word timings to each sentence.

    Returns one row per sentence: `{"text", "start", "end", "words"}`.

    Words are consumed in order and split by each sentence's own word count,
    which is what makes this robust to a transcript that differs from the
    script in spelling: it never has to match tokens, only count them. A
    sentence that runs out of words keeps the previous sentence's end so the
    track stays monotonic rather than collapsing to zero-length.
    """
    rows: list[dict] = []
    cursor = 0
    total = len(words or [])
    previous_end = 0.0

    for sentence in sentences:
        want = len(sentence.split())
        chunk = list(words[cursor : cursor + want]) if cursor < total else []
        cursor += want
        if chunk:
            start = float(chunk[0].get("start", previous_end) or previous_end)
            end = float(chunk[-1].get("end", start) or start)
        else:
            # Ran past the transcript. Anchor to the end of what was measured
            # rather than inventing a span.
            start = end = previous_end
        end = max(end, start)
        previous_end = end
        rows.append({"text": sentence, "start": start, "end": end, "words": chunk})

    return rows


async def translate_sentences(
    sentences: list[str],
    *,
    source_language: str,
    target_language: str,
    operation_label: str = "caption_translate",
) -> list[str]:
    """Translate each sentence, preserving the count and the order.

    One call for the whole track: sentence-by-sentence calls lose the thread of
    a story and translate pronouns wrongly, and the count has to be preserved
    anyway, so the batch is the natural unit.

    A response that changes the sentence count cannot be aligned -- the timings
    are per sentence -- so it is rejected and the caller falls back rather than
    silently shifting every caption after the mismatch.
    """
    if not sentences:
        return []

    src = LANGUAGE_NAMES.get(source_language, source_language)
    dst = LANGUAGE_NAMES.get(target_language, target_language)

    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences))
    prompt = (
        f"<task>\nTranslate each numbered sentence from {src} into {dst}.\n"
        f"</task>\n\n"
        f"<rules>\n"
        f"- Return EXACTLY {len(sentences)} translations, one per input "
        f"sentence, in the same order.\n"
        f"- These are video captions read while the sentence is spoken. Keep "
        f"each translation close in length to its source so it fits the time "
        f"the narrator takes to say it.\n"
        f"- Translate meaning, not words. Keep names, places and numbers "
        f"exactly as they are.\n"
        f"- Do not merge, split, add or drop sentences.\n"
        f"- Return no commentary.\n"
        f"</rules>\n\n"
        f"<sentences>\n{numbered}\n</sentences>\n\n"
        f'Return JSON: {{"translations": ["...", "..."]}}'
    )

    result = await clients.generate_json(
        prompt,
        temperature=0.3,
        max_output_tokens=8192,
        operation_label=operation_label,
    )

    got = result.get("translations") if isinstance(result, dict) else result
    if not isinstance(got, list):
        raise ValueError("translator returned no translations list")
    out = [" ".join(str(t).split()) for t in got]
    if len(out) != len(sentences):
        raise ValueError(
            f"translator returned {len(out)} sentences for {len(sentences)} "
            f"inputs; cannot align captions to narration"
        )
    if any(not t for t in out):
        raise ValueError("translator returned an empty sentence")
    return out


async def build_caption_words(
    words: list[dict],
    narration: str,
    *,
    voice_language: str,
    caption_language: str,
) -> tuple[list[dict], bool]:
    """Caption word timings in `caption_language`.

    Returns `(words, translated)`. `translated` is False when the STT timings
    were used as they are -- either because the languages match or because
    translation could not be completed, in which case captions stay in the
    spoken language rather than disappearing. A video with captions in the
    wrong language is recoverable; one with no captions is not.
    """
    voice = str(voice_language or "").strip().lower()[:2]
    caption = str(caption_language or "").strip().lower()[:2]

    if not caption or caption == voice:
        return words, False
    if not is_supported(caption):
        logger.warning(
            f"caption language {caption!r} is not supported; "
            f"keeping narration-language captions"
        )
        return words, False
    if not words:
        return words, False

    sentences = split_sentences(narration)
    if not sentences:
        logger.warning("no sentences in narration; keeping STT captions")
        return words, False

    rows = group_words_into_sentences(words, sentences)

    try:
        translated = await translate_sentences(
            sentences, source_language=voice, target_language=caption
        )
    except Exception as exc:
        logger.error(
            f"Caption translation to {caption!r} failed ({exc}); "
            f"captions stay in {voice!r}"
        )
        return words, False

    out: list[dict] = []
    for row, text in zip(rows, translated):
        tokens = text.split()
        if not tokens:
            continue
        span_end = max(float(row["end"]), float(row["start"]) + 0.05)
        out.extend(_spread(tokens, float(row["start"]), span_end))

    if not out:
        logger.error("translated caption track came out empty; keeping STT captions")
        return words, False

    logger.info(
        f"Captions translated {voice} -> {caption}: "
        f"{len(sentences)} sentences, {len(out)} words, "
        f"aligned to narration sentence timings"
    )
    return out, True
