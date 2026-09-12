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
from dataclasses import dataclass, field

import clients

logger = logging.getLogger("aivideo")

#: Speaking rate used to turn a duration into a word budget.
#:
#: Measured, not estimated. Synthesising a fixed passage through Edge TTS and
#: reading the word-boundary timings gives 154 wpm for en-US-Aria, 158 for
#: en-GB-Sonia, 104 for ar-EG-Salma and 112 for ar-SA-Hamed; a full 139-word
#: Arabic narration through the real pipeline came out at 99.5 wpm once
#: sentence pauses are included.
#:
#: The Arabic figure used to be 130, which is why a 60-second Arabic request
#: produced 84 seconds of narration. Arabic words are longer and Arabic voices
#: speak them slower, and the gap is far bigger than it looks.
_WORDS_PER_MINUTE = {"en": 150, "ar": 100}

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
  "beats": [
    {{
      "says": "the clause of the narration this beat covers, {name}",
      "shows": "one English sentence describing what the viewer should SEE",
      "search_terms": ["english query", "alternate english query"]
    }}
  ]
}}

BEAT RULES
- One beat per distinct idea in the narration, {terms} of them, in order.
- "shows" is the visual intent: a concrete, filmable scene. It is what a
  human editor would write on a shot list, and it is what the footage is
  judged against later, so be specific about subject, setting and action.
- Give each beat 2 alternate search terms, different enough that if the first
  returns nothing the second is a real second chance -- not a synonym.
- Beats must be visually DIFFERENT from each other. Do not open three beats
  on a city skyline or an aerial drone shot; that is the single most common
  way these videos look generic.

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


@dataclass
class Beat:
    """One narration idea and the shot that should illustrate it."""

    says: str = ""
    shows: str = ""
    terms: list[str] = field(default_factory=list)

    @property
    def intent(self) -> str:
        """What the footage will be judged against."""
        return self.shows or (self.terms[0] if self.terms else self.says)


def _clean_beats(raw, fallback_terms: list[str]) -> list[Beat]:
    beats: list[Beat] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        terms = _clean_terms(item.get("search_terms"), item.get("shows") or "")
        beats.append(Beat(
            says=" ".join(str(item.get("says") or "").split())[:400],
            shows=" ".join(str(item.get("shows") or "").split())[:280],
            terms=terms,
        ))
    if beats:
        return beats[:_MAX_TERMS]
    # An older-shaped reply, or none: one beat per search term still gives the
    # pipeline something ordered to work with.
    return [Beat(shows=t, terms=[t]) for t in fallback_terms]


async def write_script(
    topic: str,
    *,
    duration_seconds: int,
    language: str,
    ledger=None,
) -> tuple[str, list[Beat]]:
    """Return (narration, beats).

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
    beats = _clean_beats(payload.get("beats"), terms)
    logger.info(
        f"[aivideo] script: {len(script.split())} words, {len(beats)} beats"
    )
    return script, beats
