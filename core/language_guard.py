"""One video, one language.

An English Horror run ended on an Arabic sentence. Not a model slip -- the
English instructions carried an Arabic example of the closing question and told
the model to end it with the Arabic question mark, and the CTA fallback was
Arabic too. The config is fixed, but a prompt is an instruction and a model can
still drift, so the output is checked rather than assumed.

The check is by script, not by language identification. Arabic and Latin do not
share a codepoint range, so "does this English narration contain Arabic
letters" is decidable and cheap -- no model call, no heuristic, no false
positive on a loanword. What it deliberately tolerates is punctuation, digits
and the Latin proper nouns an Arabic script legitimately carries (a ship's
name, a place); those are not a second language, they are the same sentence.
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger("video_factory")

_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

#: The Arabic question mark and comma. In an English video these are as wrong
#: as an Arabic word, and they were being asked for by name.
_ARABIC_PUNCT = "؟،؛"


def _strip_marks(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(text or ""))
        if not unicodedata.combining(c)
    )


def arabic_chars(text: str) -> list[str]:
    """Arabic letters and Arabic punctuation present in `text`."""
    raw = str(text or "")
    return _ARABIC.findall(raw) + [c for c in raw if c in _ARABIC_PUNCT]


def latin_words(text: str) -> list[str]:
    """Runs of Latin letters, which in an Arabic script are usually names."""
    return re.findall(r"[A-Za-z]{2,}", _strip_marks(text))


def violations(text: str, *, language: str, field: str) -> list[str]:
    """Why `text` is not in `language`, or an empty list.

    `language` is the video's language code -- "en" or "ar". Anything else is
    not checked: this module knows two scripts and should say so rather than
    guess at a third.
    """
    code = str(language or "").strip().lower()[:2]
    body = str(text or "").strip()
    if not body:
        return []

    if code == "en":
        found = arabic_chars(body)
        if found:
            return [
                f"{field}: English video contains Arabic "
                f"({''.join(dict.fromkeys(found))[:20]}) -- {body[:80]!r}"
            ]
        return []

    if code == "ar":
        # An Arabic script may carry a Latin proper noun; a whole Latin
        # sentence is a different thing. Flag only when Latin dominates.
        words = latin_words(body)
        if not words:
            return []
        latin_letters = sum(len(w) for w in words)
        arabic_letters = len(_ARABIC.findall(body))
        if arabic_letters == 0 or latin_letters > arabic_letters:
            return [
                f"{field}: Arabic video is largely Latin script -- {body[:80]!r}"
            ]
        return []

    return []


def check_script(script, *, language: str) -> list[str]:
    """Every field of a finished script that the viewer sees or hears."""
    problems: list[str] = []
    problems += violations(getattr(script, "title", ""),
                           language=language, field="title")
    problems += violations(getattr(script, "thumbnail_text", ""),
                           language=language, field="thumbnail_text")
    problems += violations(getattr(script, "thumbnail_brief", ""),
                           language=language, field="thumbnail_brief")

    for section in getattr(script, "sections", []) or []:
        label = f"section {getattr(section, 'id', '?')}"
        problems += violations(getattr(section, "narration", ""),
                               language=language, field=f"{label} narration")
        for index, slot in enumerate(getattr(section, "slots", []) or [], 1):
            for key in ("title", "text", "headline", "caption"):
                value = (getattr(slot, "props", None) or {}).get(key)
                if value:
                    problems += violations(
                        str(value), language=language,
                        field=f"{label} slot {index} {key}",
                    )
    return problems


def check_captions(words: list[dict], *, language: str) -> list[str]:
    """The caption track, as the words that will be burned in."""
    text = " ".join(str(w.get("word", "")) for w in (words or []))
    return violations(text, language=language, field="captions")


def closing_question(script, *, language: str) -> list[str]:
    """The ending question specifically. It is the line most often wrong."""
    sections = getattr(script, "sections", []) or []
    if not sections:
        return []
    narration = str(getattr(sections[-1], "narration", "") or "").strip()
    if not narration:
        return []
    # Last sentence, in either script's punctuation.
    parts = [p for p in re.split(r"(?<=[.!?؟])\s+", narration) if p.strip()]
    if not parts:
        return []
    return violations(parts[-1], language=language, field="closing question")


def assert_language(script, *, language: str, where: str) -> list[str]:
    """Log every violation and return them. Never raises.

    Returning rather than raising so the caller decides: the scripter can
    regenerate, while a later stage may only be able to report.
    """
    problems = check_script(script, language=language)
    problems += closing_question(script, language=language)
    for problem in problems:
        logger.error(f"[language] {where}: {problem}")
    return problems
