"""Topic in, narration and search terms out.

Adapted from MoneyPrinterTurbo's `app/services/llm.py` (MIT), which asks for
the script and the search terms in two separate calls. The shape of the ask is
the part worth keeping: a plain narration paragraph with no headings, no
speaker labels and no markdown, plus short *English* search terms regardless of
the narration language, because stock libraries index in English.

What differs here: one call instead of two (halves latency and cost), a word
budget derived from the requested duration rather than a fixed length, and a
strict JSON contract so a malformed reply is recoverable rather than a
paragraph we have to guess at.
"""

from __future__ import annotations

import json
import logging
import re

import clients

logger = logging.getLogger("aivideo")

#: Speaking rate used to turn a duration into a word budget. Measured across
#: Edge TTS neural voices: English lands near 150 wpm, Arabic nearer 130 for
#: the same clarity, so Arabic gets fewer words for the same seconds.
_WORDS_PER_MINUTE = {"en": 150, "ar": 130}

_LANGUAGE_NAME = {"en": "English", "ar": "Arabic"}

#: Enough beats to keep footage moving without a search per sentence.
_MIN_TERMS, _MAX_TERMS = 4, 12


def word_budget(duration_seconds: int, language: str) -> int:
    wpm = _WORDS_PER_MINUTE.get(language, 150)
    return max(30, int(duration_seconds / 60 * wpm))


def build_prompt(topic: str, duration_seconds: int, language: str) -> str:
    words = word_budget(duration_seconds, language)
    name = _LANGUAGE_NAME.get(language, "English")
    terms = max(_MIN_TERMS, min(_MAX_TERMS, duration_seconds // 6))
    return f"""Write a short-form video narration about this topic.

TOPIC: {topic}

Return JSON only:
{{
  "script": "the narration, {name}, one flowing paragraph",
  "search_terms": ["english search term", "..."]
}}

SCRIPT RULES
- Write in {name}. Every word of "script" must be {name}.
- About {words} words. This is read aloud at a natural pace and must land
  close to {duration_seconds} seconds.
- One paragraph of continuous narration. No headings, no bullet points, no
  markdown, no speaker labels, no stage directions, no emoji, no hashtags.
- Do not write "in this video" or "welcome back" or any channel boilerplate.
- Open on the single most interesting thing about the topic. Short sentences.
- Do not invent specific statistics, dates, prices or quotes. If you are not
  sure of a number, describe it qualitatively instead.
- End on a complete thought, not mid-sentence.

SEARCH TERM RULES
- Exactly {terms} terms, in ENGLISH even when the script is Arabic. Stock
  footage libraries are indexed in English and an Arabic query returns nothing.
- Each term is 1-3 words describing something a camera can film: a place, an
  object, an action, a landscape. "dubai skyline", "desert road", "stock
  market screen".
- Follow the order of the narration, so the footage tracks what is being said.
- No proper names of living people. No brand names. No text-on-screen requests.
- Concrete and filmable: not "success", "history", "the economy"."""


def _strip_fence(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    return body.strip()


def _clean_script(text: str) -> str:
    """Remove the scaffolding models add even when told not to."""
    body = _strip_fence(str(text or ""))
    body = re.sub(r"^#{1,6}\s.*$", "", body, flags=re.M)      # headings
    # Label strip runs per line and after headings: models put "Narration:" on
    # its own line under a heading, so anchoring to the start of the string
    # left the label in the finished narration.
    body = re.sub(
        r"^\s*(narration|script|voiceover|vo)\s*:\s*", "", body, flags=re.I | re.M
    )
    body = re.sub(r"^\s*[-*]\s+", "", body, flags=re.M)        # bullets
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)               # bold
    body = re.sub(r"\[[^\]]*\]", "", body)                     # [pause]
    body = re.sub(r"\(([^)]*?(?:pause|beat|sfx|music)[^)]*?)\)", "", body, flags=re.I)
    body = re.sub(r"\s*\n\s*", " ", body)
    return re.sub(r"\s{2,}", " ", body).strip()


def _clean_terms(raw, fallback_topic: str) -> list[str]:
    terms: list[str] = []
    for item in raw or []:
        term = re.sub(r"[^\w\s-]", " ", str(item)).strip().lower()
        term = re.sub(r"\s{2,}", " ", term)
        if term and term not in terms:
            terms.append(" ".join(term.split()[:3]))
    if not terms:
        # Never leave footage with nothing to search for: the topic itself is
        # a usable query, and a generic bed keeps the video moving.
        base = re.sub(r"[^\w\s]", " ", fallback_topic).strip().lower()
        terms = [" ".join(base.split()[:3])] if base else []
        terms += ["cinematic landscape", "city timelapse", "abstract motion"]
    return terms[:_MAX_TERMS]


#: Characters per token, for costing. The exact usage lives in the shared
#: `core.costs` tracker, which this package deliberately does not import --
#: coupling the engine to the channel pipeline is what isolation is meant to
#: prevent. Four characters per token is the standard approximation for these
#: models and is close enough to measure an average against; the ledger labels
#: it as derived rather than reported.
_CHARS_PER_TOKEN = 4


async def write_script(
    topic: str,
    *,
    duration_seconds: int,
    language: str,
    ledger=None,
) -> tuple[str, list[str]]:
    """Return (narration, search_terms).

    Raises only when the model gives us nothing usable at all -- an empty
    script is the one thing downstream cannot recover from, because there is
    no narration to time captions against or footage to illustrate.
    """
    prompt = build_prompt(topic, duration_seconds, language)
    payload = await clients.generate_json(
        prompt,
        system_instruction=(
            "You write short-form video narration. You return JSON only, with "
            "no commentary before or after it."
        ),
        temperature=0.8,
        operation_label="aivideo_script",
    )
    if ledger is not None:
        from settings import settings

        rendered = json.dumps(payload, ensure_ascii=False) if payload else ""
        ledger.record_model_call(
            model=settings.gemini_primary_model,
            input_tokens=len(prompt) // _CHARS_PER_TOKEN,
            output_tokens=len(rendered) // _CHARS_PER_TOKEN,
            label="script",
        )
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("script model did not return an object")

    script = _clean_script(payload.get("script") or payload.get("narration") or "")
    if len(script) < 40:
        raise ValueError(f"script too short to narrate ({len(script)} chars)")

    terms = _clean_terms(
        payload.get("search_terms") or payload.get("terms"), topic
    )
    logger.info(
        f"[aivideo] script: {len(script.split())} words, {len(terms)} search terms"
    )
    return script, terms
