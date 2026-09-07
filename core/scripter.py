"""Stage 1: Script generation — AI writes narration + image prompts, reviewed by Gate #1."""

import json
import logging
import math
import re
from pathlib import Path

import clients
import prompts
from core.reviewer import ReviewGateError, review_gate
from core.utils import (
    duration_window,
    estimate_section_seconds,
    script_timing_profile,
    words_for_seconds,
)
from core.utils import (
    ChannelConfig, Script, ScriptSection, VisualSlot,
    compute_sections_range, minimum_visual_slots_for_duration, save_script,
)
from settings import settings

logger = logging.getLogger("video_factory")

_MAX_DURATION_RETRIES = 2

# How far past its estimate a section's narration is assumed to run when
# deciding how many visual slots it needs. `script_timing_profile` is keyed on
# the TTS model only, so it cannot know that a channel has asked for a slower,
# more deliberate read. Measured overshoot on a Horror run was 1.16x and 1.25x
# on the two long sections. Calibrated to the measurement rather than rounded
# up: at 1.30 a correctly paced 14.7s section is told it needs five slots
# instead of four, and every surplus slot is another image to source, review
# and pay for. Overrun past this degrades instead of failing, because
# compute_sub_durations now rebalances to the lowest peak available. It only
# ever raises the slot requirement -- narration, timing and rendered duration
# are untouched.
_TTS_DURATION_MARGIN = 1.25

# Spare visual slots per section, so losing one to sourcing does not break the
# hold cap. On a web-photo-only channel a slot that no real photograph can
# satisfy is dropped rather than filled with a wrong or generated image, which
# means the surviving slots have to cover the section between them. A Horror
# run was planned with the six slots its 25.85s section needed, lost one to
# sourcing, and the five that remained held 5.41s each against a 5.0s cap --
# failing at render after the narration and every image had been paid for.
# One spare absorbs the common case; a section losing two is rare enough that
# the cap should genuinely fail rather than be padded around.
_SOURCING_DROP_ALLOWANCE = 1

# ── Photographable-subject rule ──────────────────────────────────
#
# A web-photo-only channel can only ever be given what a real photograph or an
# authentic archival document contains. A slot asking for a graphic device or a
# scene nobody ever photographed cannot be satisfied by any search, so the
# candidate filter rejects everything, the relevance gate falls back to a
# top-ranked near-miss, and the image review gate then fails the run.
#
# A D.B. Cooper run died exactly this way. The review gate's own words:
#   "Several images failed due to incorrect subjects or styles (e.g. book
#    covers or cartoons instead of cinematic photos) ... several specific
#    requested details (like the tie on the seat or the red question mark)
#    were missing."
# There is no photograph of a red question mark, and no photograph of the tie
# staged on the seat -- the tie exists only as an FBI evidence photograph.
#
# These are matched against the slot's search keywords, which are what actually
# reach the image search.
# Graphic devices and mood shots. Nothing exempts these: naming a real artefact
# in the same breath does not make them sourceable. A slot asking for
# "question mark D.B. Cooper composite sketch" reached image search because the
# archival allowlist matched "composite sketch" and short-circuited before the
# question mark was ever considered -- so search was sent looking for a
# photograph containing a question mark, and the review gate flagged it.
_ALWAYS_FORBIDDEN_KEYWORD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bquestion\s*mark\b", "a question mark is a graphic device, not a subject"),
    (r"\bexclamation\s*mark\b", "punctuation is a graphic device, not a subject"),
    (r"\b(mystery|mysterious|spooky|creepy|eerie|scary)\s+"
     r"(background|backdrop|wallpaper|texture|atmosphere|vibes?)\b",
     "a mood backdrop is not a real-world subject"),
    (r"\b(book|album|magazine)\s*cover\b", "cover art is artwork, not documentation"),
    (r"\bpodcast\s*(thumbnail|cover|art)\b", "podcast art is artwork, not documentation"),
    (r"\b(movie|film)\s*poster\b", "poster art is artwork, not documentation"),
    (r"\b(re-?enactment|recreation|reconstruction|staged|dramatis(ed|ation))\b",
     "a staged recreation is not a real photograph of the event"),
    (r"\b(silhouette|shadowy\s+figure|faceless\s+man|unknown\s+man)\b",
     "an anonymous stand-in figure is not the real subject"),
    (r"\bred\s+(circle|arrow)\b", "an annotation overlay is not a subject"),
    (r"\bcinematic\b", "a style word matches artwork, not a real place or object"),
)

# Artwork wording, which an authentic archival artefact is allowed to trip --
# an FBI composite sketch is a drawing and still a real case record.
_ARTWORK_KEYWORD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(cartoon|clipart|clip\s*art|vector\s*art|concept\s*art|"
     r"digital\s*art|artist'?s?\s+impression|render(ing|ed)?)\b",
     "illustration is not a photograph or archival document"),
)

# Charts, diagrams and visual metaphors.
#
# These are the briefs that stopped a Football News run: three slots asked for
# "رسم بياني" -- a diagram -- typed as google_photo, on a channel where every
# photo slot must be a real photograph. Search does the only thing it can and
# returns renders and infographics, the relevance gate correctly rejects all of
# them as not-photographs, the beats are dropped, and the run dies for want of
# images it was never going to find.
#
# The remedy is not a better search query. A chart is a thing the renderer
# draws, so these belong in an info_card or info_slide, and catching them here
# means the scripter is told that before any sourcing is paid for.
#
# Arabic matters as much as English here: the failing run was Arabic, and
# `.lower()` leaves Arabic unchanged, so the two sets sit side by side. Arabic
# has no word boundaries that \b can use around its script, so those patterns
# are matched as substrings.
_CONCEPTUAL_KEYWORD_PATTERNS: tuple[tuple[str, str], ...] = (
    # English
    (r"\b(diagram|infographic|info-?graphic|flow\s*chart|flowchart|"
     r"bar\s*chart|pie\s*chart|line\s*chart|chart|graph)\b",
     "a chart or diagram is drawn, not photographed"),
    (r"\b(domino\s+effect|ripple\s+effect|chain\s+reaction)\b",
     "a visual metaphor is not a real-world subject"),
    (r"\b(timeline|org\s*chart|mind\s*map|venn\s+diagram)\b",
     "a diagrammatic device is drawn, not photographed"),
    # Arabic
    (r"رسم\s*بياني", "a chart or diagram is drawn, not photographed"),
    (r"رسم\s*توضيحي", "an explanatory illustration is drawn, not photographed"),
    (r"مخطط", "a diagram or schematic is drawn, not photographed"),
    (r"إنفوجرافيك|انفوجرافيك", "an infographic is drawn, not photographed"),
    (r"تأثير\s*الدومينو", "a visual metaphor is not a real-world subject"),
    (r"جدول\s*بياني", "a data table is drawn, not photographed"),
)

# Archival artefacts that ARE authentic documents and must never be caught by
# the patterns above -- an FBI composite sketch is a real, citable case record,
# even though it is a drawing. It stays allowed only because the keywords name
# it as the composite sketch rather than as a photograph of the man.
_ARCHIVAL_KEYWORD_ALLOWLIST: tuple[str, ...] = (
    r"\b(composite|police|forensic|wanted)\s+sketch\b",
    r"\bfbi\s+sketch\b",
    r"\bsketch\s+of\s+the\s+suspect\b",
    r"\b(evidence|case|court|police|fbi)\s+(photo(graph)?|file|document|report|record)s?\b",
    r"\bwanted\s+poster\b",
    r"\barchival\s+(photo(graph)?|footage|document)s?\b",
    r"\bnewspaper\s+(clipping|front\s*page|archive)\b",
)


def non_photographable_reason(keywords: str) -> str:
    """Why these search keywords cannot be satisfied by a real photo, or "".

    Returns an empty string for anything sourceable, including archival
    artefacts that happen to be drawings.
    """
    text = " ".join(str(keywords or "").split()).lower()
    if not text:
        return ""

    # Graphic devices are rejected regardless of what else is in the string.
    for pattern, reason in _ALWAYS_FORBIDDEN_KEYWORD_PATTERNS:
        if re.search(pattern, text):
            return reason

    # Artwork wording is forgiven only when the slot is genuinely asking for an
    # archival artefact that happens to be drawn.
    is_archival = any(
        re.search(allowed, text) for allowed in _ARCHIVAL_KEYWORD_ALLOWLIST
    )
    if is_archival:
        return ""
    for pattern, reason in _ARTWORK_KEYWORD_PATTERNS:
        if re.search(pattern, text):
            return reason
    # Same tier as artwork, and for the same reason: a genuine archival chart
    # named as a case document is still a real record, so the allowlist above
    # gets first refusal.
    for pattern, reason in _CONCEPTUAL_KEYWORD_PATTERNS:
        if re.search(pattern, text):
            return reason
    return ""


def conceptual_brief_reason(keywords: str) -> str:
    """Why this brief wants a drawn graphic rather than a photograph, or "".

    Separate from `non_photographable_reason` because the two have different
    remedies. An unphotographable *subject* needs different keywords; a chart
    needs a different slot type -- info_card or info_slide, which the renderer
    draws. Telling the scripter to "find a more concrete subject" for a bar
    chart sends it looking for a photograph of one.
    """
    text = " ".join(str(keywords or "").split()).lower()
    if not text:
        return ""
    if any(re.search(allowed, text) for allowed in _ARCHIVAL_KEYWORD_ALLOWLIST):
        return ""
    for pattern, reason in _CONCEPTUAL_KEYWORD_PATTERNS:
        if re.search(pattern, text):
            return reason
    return ""


# Arabic and Latin question marks. The Arabic one is what MSA narration uses.
_QUESTION_MARKS = ("؟", "?")


def _ends_with_question(text: str) -> bool:
    """Whether narration closes on a question rather than a statement."""
    stripped = str(text or "").strip().rstrip("\"'”’)]»")
    return stripped.endswith(_QUESTION_MARKS)


def _ensure_closing_question(script_data: dict, *, fallback: str) -> None:
    """Guarantee the narration ends on a question the viewer can answer.

    The channel closes on a short, story-specific question rather than a
    subscribe prompt -- the thing that actually earns a comment is being asked
    what you think happened. The script is asked for one, and the model
    normally writes a far better question than any generic wording because it
    can draw on the case's own unresolved detail.

    This is the floor under that, not a replacement for it: when the last
    sentence is not a question, the configured fallback is appended so the beat
    is never silently missing. The fallback asks only for the viewer's reading
    of the story, so it introduces no fact or claim of its own.

    An empty `fallback` disables the behaviour entirely.
    """
    if not fallback.strip():
        return

    sections = [s for s in (script_data.get("sections") or []) if isinstance(s, dict)]
    if not sections:
        return

    last = sections[-1]
    narration = str(last.get("narration") or "").strip()
    if _ends_with_question(narration):
        return

    last["narration"] = f"{narration} {fallback.strip()}".strip()
    logger.info(
        "Narration did not close on a question; appended the channel's "
        "closing question so the ending still invites a reply"
    )


# Qualifiers that decide *which* photograph of a subject qualifies, without
# changing what the subject is. Stripped only on web-photo-only channels, and
# only from slot briefs -- never from narration, which is where the facts live.
#
# Ordered longest-first within each family so "under a grey sky" is consumed
# before a bare "grey" could be.
_STAGING_QUALIFIER_PATTERNS: tuple[str, ...] = (
    # Viewpoint and framing.
    r"\b(?:the\s+)?(?:view|shot|angle|perspective)\s+from\s+(?:the\s+)?"
    r"(?:inside|outside|within|above|below|behind|afar|a\s+distance)\b",
    r"\b(?:looking|seen|viewed|shot|photographed)\s+"
    r"(?:out\s+)?from\s+(?:the\s+)?(?:inside|outside|within|above|below|behind)\b",
    r"\b(?:from\s+)?(?:inside|within)\s+looking\s+out\b",
    # The trailing "of (the)" goes with the framing word: dropping "aerial
    # shot" alone from "aerial shot of the mountain pass" leaves "of the
    # mountain pass", which is worse than what it replaced.
    r"\b(?:bird'?s[-\s]?eye|worm'?s[-\s]?eye|low|high|wide|close[-\s]?up|"
    r"aerial|overhead|point[-\s]of[-\s]view|pov)\s+(?:shot|angle|view)"
    r"(?:\s+of(?:\s+the)?)?\b",
    # Season.
    r"\bin\s+(?:the\s+)?(?:deep\s+)?(?:winter|summer|spring|autumn|fall)\b",
    r"\b(?:winter|summer|spring|autumn|autumnal|wintry|snowy|snow[-\s]covered)\s+"
    r"(?:landscape|scene|setting|backdrop|surroundings)\b",
    # Weather and sky.
    r"\bunder\s+(?:a|an|the)\s+[a-z\- ]{0,20}\b(?:sky|clouds?|sun|moon)\b",
    # Only *qualified* weather. A bare "in snow" is the setting of the story
    # and narrows the search usefully; "in heavy fog" is a photograph the
    # archive is unlikely to hold of the right subject.
    r"\bin\s+(?:the\s+)?(?:heavy|light|driving|thick|dense|falling|blowing|"
    r"swirling)\s+(?:rain|snow|fog|mist|blizzard|storm|sunshine|overcast)\b",
    r"\b(?:heavy|thick|dense|light)\s+(?:fog|mist|snow|rain|cloud)\b",
    # Time of day.
    r"\bat\s+(?:night|dusk|dawn|sunset|sunrise|midday|noon|twilight|midnight)\b",
    r"\b(?:night[-\s]?time|day[-\s]?time|early\s+morning|late\s+afternoon|"
    r"golden\s+hour|blue\s+hour)\b",
    # Instrument, device and subject state.
    r"\bwith\s+(?:the\s+|its\s+)?(?:needle|dial|gauge|meter|hand|pointer)\s+"
    r"(?:indicating|showing|pointing\s+(?:to|at)|at)\s+[a-z\- ]{0,24}\b",
    r"\b(?:displaying|showing|indicating)\s+(?:a\s+)?"
    r"(?:high|low|zero|maximum|minimum|elevated)\s+(?:reading|value|level|number)s?\b",
    r"\b(?:switched|turned)\s+(?:on|off)\b",
    # A bare atmospheric word left trailing on a brief -- "torn tent snow
    # night". It narrows the photograph without naming anything, and only the
    # trailing position is safe: "night watchman" is a subject, "… at night"
    # is lighting.
    r"\s(?:night|dusk|dawn|midnight|twilight|daytime|nighttime)\s*$",
)

_STAGING_QUALIFIER_RES = tuple(
    re.compile(p, re.I) for p in _STAGING_QUALIFIER_PATTERNS
)

# Removing a qualifier can strand the preposition that introduced it --
# "memorial plaque in" or "torn tent under". Trailing connectives are dropped
# so the brief reads as a subject rather than a sentence fragment.
_DANGLING_CONNECTIVE_RE = re.compile(
    r"\s+\b(?:in|on|at|under|with|from|into|over|beneath|during|and|of|the|a|an)\b"
    r"(?=\s*$|\s+(?:in|on|at|under|with|from|and)\b)",
    re.I,
)


def _destage_slot_briefs(script_data: dict, *, web_photos_only: bool) -> None:
    """Strip staging the archive cannot satisfy from slot keywords and prompts.

    The image review gate judges each photograph against its slot prompt, so an
    unattainable prompt fails a correct image. A Horror run was rejected for
    showing "recovered money instead of original stacks" -- but the Cooper
    money was only ever photographed after recovery, decayed on a riverbank.
    The archive's only real photograph of the subject was marked wrong because
    the brief asked for one that has never existed. Same for "an action
    (digging) instead of a close-up of the ground".

    Removing the staging leaves the subject, which is what the channel can
    actually source and what the gate should be judging. The subject itself is
    untouched, so relevance is still fully checked -- this drops demands for a
    pose, a quantity, an angle or a time of day, not for the right thing.

    A Dyatlov Pass run failed the same way for a wider family of qualifiers:

        "view from inside torn tent snow night"      the viewpoint
        "memorial plaque in a snowy landscape"       the season
        "Geiger counter with the needle at a high reading"   the instrument state
        "modern empty desolate landscape under a grey sky"   the weather

    Real photographs of the plaque and the tent exist; they were rejected for
    being the wrong season, or shot from outside. Those constraints decide
    which photograph qualifies without changing what the photograph is of, so
    dropping them costs nothing factual and turns "no candidate" into "the
    right subject". Anything that changes the subject -- who, what, where,
    when in history -- is left alone.
    """
    if not web_photos_only:
        return

    from core.image_sourcer import (
        _ACTION_WRAPPER_RE,
        _ATMOSPHERE_TAIL_RE,
        _MEDIUM_WRAPPER_RE,
    )

    def _clean(text: str) -> str:
        raw = str(text or "")
        lowered = " ".join(raw.split()).lower()

        # Someone posing with the subject, or a count of it, is always staging.
        stripped = _ACTION_WRAPPER_RE.sub(" ", raw)
        stripped = _ATMOSPHERE_TAIL_RE.sub(" ", stripped)

        # Viewpoint, season, weather, time of day and instrument state. Each
        # narrows which photograph counts without changing what it is of.
        for pattern in _STAGING_QUALIFIER_RES:
            stripped = pattern.sub(" ", stripped)

        # Repeatedly, because one pass is not enough: removing "a snowy
        # landscape" from "memorial plaque in a snowy landscape" leaves
        # "memorial plaque in a", and a single sub() consumes the "a" while
        # stepping over the "in" that preceded it.
        for _ in range(4):
            reduced = _DANGLING_CONNECTIVE_RE.sub(" ", stripped)
            if reduced == stripped:
                break
            stripped = reduced

        # Framing wording is only staging when the brief is not asking for an
        # archival artefact. "FBI evidence photograph of the tie" names one,
        # and stripping its "photograph of" produced "FBI evidence the tie" --
        # worse than the original and no longer a recognisable archival request.
        if not any(re.search(p, lowered) for p in _ARCHIVAL_KEYWORD_ALLOWLIST):
            stripped = _MEDIUM_WRAPPER_RE.sub(" ", stripped)

        return " ".join(stripped.split())

    for section in script_data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for slot in section.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            for field in ("keywords", "prompt"):
                original = slot.get(field)
                if not isinstance(original, str) or not original.strip():
                    continue
                cleaned = _clean(original)
                # Never blank a field out: an empty brief is worse than an
                # over-specified one.
                if cleaned and cleaned.lower() != original.lower():
                    slot[field] = cleaned


# Words that carry no search value once a prompt becomes a query.
_KEYWORD_STOPWORDS = frozenset("""
a an the of in on at to from with and or for by as is are was were be being been
this that these those its it his her their there here into onto over under above
showing shows show seen looking view shot image photo photograph picture close
up wide angle scene depicting depicts featuring feature featured
""".split())

# A `visual` value long enough to be prose rather than a type name.
_PROSE_VISUAL_RE = re.compile(r"\s")

# Slots whose picture is found by searching, so they need search keywords as
# well as a prompt. ai_photo and ai_illustration are deliberately absent: they
# are generated from the prompt, and demanding keywords for them would fail
# scripts on the channels that use an AI lane.
_SEARCHED_VISUAL_TYPES = frozenset(
    VisualSlot.SOURCEABLE_TYPES - {"ai_photo", "ai_illustration"}
)


# Mood, style and atmosphere words. They describe how a picture should feel,
# which image search cannot act on: "eerie" matches horror-poster artwork, and
# "cinematic" matches stills from films. Both then fail the relevance gate for
# not being real photographs of the subject.
#
# Deliberately conservative. A word stays off this list if it can name
# something concrete -- "dark room", "desolate landscape" and "abandoned
# factory" are all searchable places, so dark/desolate/abandoned are absent.
_MOOD_STYLE_WORDS = frozenset("""
strange eerie eerily creepy spooky haunting hauntingly mysterious mystery
ominous sinister chilling unsettling unnerving foreboding uncanny
dramatic dramatically cinematic cinematically atmospheric moody
stylized stylised artistic aesthetic epic breathtaking stunning
beautiful gorgeous majestic serene tranquil peaceful idyllic
striking evocative haunted ghostly spectral surreal ethereal dreamlike
otherworldly nostalgic melancholy melancholic somber sombre
tense suspenseful thrilling terrifying frightening scary horrifying
grim bleak forlorn desolation gloom gloomy foreboding
vivid vibrant dreamy magical mystical whimsical
""".split())

# Weather and season written as a feeling rather than a thing. The setting is
# real and worth searching for; the adjective form is not what a caption says.
_ATMOSPHERE_NOUNS = {
    "snowy": "snow",
    "snow-covered": "snow",
    "foggy": "fog",
    "misty": "mist",
    "rainy": "rain",
    "stormy": "storm",
    "windy": "wind",
    "cloudy": "cloud",
    "wintry": "winter",
}


def strip_mood_words(keywords: str) -> str:
    """Reduce a search brief to the concrete things in it.

    Subjects, places, objects, people, documents and events survive. Mood and
    style words are dropped, and weather written as an adjective becomes the
    thing itself ("snowy" -> "snow") so it still narrows the search.

    Returns "" only when the input held nothing concrete at all; callers keep
    the original in that case rather than shipping an empty brief.
    """
    text = re.sub(r"[^\w\s-]", " ", str(keywords or ""))
    kept: list[str] = []
    for word in text.split():
        lowered = word.lower()
        if lowered in _MOOD_STYLE_WORDS:
            continue
        kept.append(_ATMOSPHERE_NOUNS.get(lowered, word))
    return " ".join(kept)


def _derive_keywords(prompt: str, *, limit: int = 8) -> str:
    """Turn a visual prompt into a search query.

    Not a summary -- just the prompt's content words in order, which is what a
    human writes when they turn a description into a search. Punctuation and
    filler go; the subject survives in the order the prompt named it.
    """
    text = re.sub(r"[^\w\s-]", " ", str(prompt or ""))
    words: list[str] = []
    for word in text.split():
        lowered = word.lower()
        if lowered in _KEYWORD_STOPWORDS or len(lowered) < 2:
            continue
        words.append(word)
        if len(words) >= limit:
            break
    return " ".join(words)


def _repair_slot_fields(script_data: dict) -> None:
    """Put misplaced slot fields where the schema expects them.

    Two shapes the model produces that are perfectly usable but structurally
    wrong, and that used to burn an entire generation before anyone noticed:

    1. The description lands in `visual` instead of `prompt`. A Dyatlov run
       failed both revision attempts this way, every slot reading

           "visual": "Group photo of the Dyatlov hikers, smiling and posing…"

       The content was fine; only the field was wrong. It is moved to `prompt`
       and the slot is typed as a sourced photograph.

    2. `prompt` is written but `keywords` is left empty. Image search is given
       the keywords, not the prompt, so such a slot cannot be sourced at all --
       two info_slides shipped that way and took their beats down with them.
       The keywords are derived from the prompt.

    Repairing here rather than failing keeps validation strict: the rules do
    not move, the model's output is put into the shape the rules already
    expect. Anything that cannot be repaired still fails.
    """
    for section in script_data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for slot in section.get("slots") or []:
            if not isinstance(slot, dict):
                continue

            visual = str(slot.get("visual") or "").strip()

            # 1. A description in the type field.
            if visual and visual not in _ALLOWED_SLOT_VISUALS and _PROSE_VISUAL_RE.search(visual):
                if not str(slot.get("prompt") or "").strip():
                    slot["prompt"] = visual
                # google_photo is the safest landing place: it is a sourced
                # photograph on every channel, and the photo-only channels
                # route it through web search exactly as they would have.
                slot["visual"] = "google_photo"
                visual = "google_photo"
                logger.info(
                    "Slot carried its description in the \"visual\" field; "
                    "moved it to \"prompt\" and typed the slot google_photo"
                )

            # 2. Keywords derived from the prompt when the model omitted them.
            if visual in _SEARCHED_VISUAL_TYPES:
                keywords = str(slot.get("keywords") or "").strip()
                prompt = str(slot.get("prompt") or "").strip()
                if not keywords and prompt:
                    derived = _derive_keywords(prompt)
                    if derived:
                        keywords = derived
                        slot["keywords"] = derived
                        logger.info(
                            f"Slot had no search keywords; derived "
                            f"{derived!r} from its prompt"
                        )

                # 3. Mood and style words removed from whatever the keywords
                #    now are. Image search cannot act on "eerie" or
                #    "cinematic"; they pull in artwork and film stills, which
                #    the relevance gate then rejects for not being real
                #    photographs. Stripping them here means the slot is
                #    searched for its subject instead of failing.
                #
                #    Never blanked: a brief made only of mood words keeps its
                #    original text so validation can report it rather than the
                #    repair silently emptying the field.
                if keywords:
                    concrete = strip_mood_words(keywords)
                    if concrete and concrete.lower() != keywords.lower():
                        slot["keywords"] = concrete
                        logger.info(
                            f"Removed mood/style words from slot keywords: "
                            f"{keywords!r} -> {concrete!r}"
                        )


def _coerce_slot_keywords(script_data: dict) -> None:
    """Flatten list-shaped slot keywords into the search string the model owes us.

    `VisualSlot.keywords` is typed `str`, and the model returns one almost
    every time -- but it occasionally emits a JSON array instead, typically on
    a section's last and most generic slot. Pydantic will not coerce a list
    into a str, so a single stray field killed a run *after* research, script
    generation and the review gate had all been paid for:

        slots.4.keywords
          Input should be a valid string
          [input_value=['unsolved mystery background'], input_type=list]

    The value is perfectly usable, so it is repaired in place rather than
    thrown away. Multi-element lists join on ", " -- the same shape the model
    produces when it writes keywords as one string. Strings, and every other
    type, are left exactly as they are for the validator to judge.
    """
    for section in script_data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for slot in section.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            keywords = slot.get("keywords")
            if isinstance(keywords, list):
                slot["keywords"] = ", ".join(
                    str(part).strip() for part in keywords if str(part).strip()
                )


# Every visual type the pipeline knows how to source or render.
_ALLOWED_SLOT_VISUALS = (
    VisualSlot.IMAGE_TYPES | VisualSlot.COMPONENT_TYPES
)


def _thumbnail_strategy_options(
    config: ChannelConfig,
    allowed_names: list[str],
) -> list[dict[str, str]]:
    strategies_by_name = {
        strategy.name: strategy
        for strategy in config.thumbnail_strategies
    }
    return [
        {
            "name": strategies_by_name[name].name,
            "instruction": strategies_by_name[name].instruction,
        }
        for name in allowed_names
    ]


def _validate_thumbnail_strategy_choice(
    chosen_strategy: str,
    *,
    video_type: str,
    allowed_names: list[str],
) -> str:
    allowed = set(allowed_names)
    if chosen_strategy not in allowed:
        available = ", ".join(sorted(allowed))
        raise ValueError(
            "Script selected thumbnail strategy "
            f"'{chosen_strategy}' not allowed for video type '{video_type}'. "
            f"Allowed: {available}"
        )
    return chosen_strategy


def _title_banner_numbering_errors(
    script_data: dict,
    numbering_order: str | None,
) -> list[str]:
    """Validate numbered title_banner slots before the script is saved."""
    if not numbering_order:
        return []

    banners = []
    for section in script_data.get("sections", []):
        for slot in section.get("slots", []):
            if slot.get("visual") == "title_banner":
                props = slot.get("props") or {}
                banners.append({
                    "section_id": section.get("id"),
                    "section_number": props.get("section_number"),
                })

    if not banners:
        return []

    errors: list[str] = []
    actual_numbers: list[int] = []
    for banner in banners:
        section_id = banner["section_id"]
        section_number = banner["section_number"]
        if not isinstance(section_number, int):
            errors.append(
                f"Section {section_id}: title_banner.section_number must be an integer."
            )
            continue
        actual_numbers.append(section_number)

    if len(actual_numbers) != len(banners):
        return errors

    if numbering_order == "ascending":
        expected_numbers = list(range(1, len(banners) + 1))
    elif numbering_order == "descending":
        expected_numbers = list(range(len(banners), 0, -1))
    else:
        raise ValueError(f"Unsupported numbering_order: {numbering_order}")

    if actual_numbers != expected_numbers:
        errors.append(
            "title_banner section_number values must be "
            f"{expected_numbers} in section order; got {actual_numbers}."
        )

    return errors


def _script_word_totals(
    data: dict,
) -> tuple[int, int]:
    """Return (section_count, total_words)."""
    secs = data.get("sections", [])
    total_words = sum(len(s.get("narration", "").split()) for s in secs)
    return len(secs), total_words


def _apply_word_based_duration_estimates(
    data: dict,
    *,
    timing_profile,
) -> dict:
    total_estimated_seconds = 0.0
    for index, section in enumerate(data.get("sections", []), start=1):
        word_count = len(str(section.get("narration", "")).split())
        estimated_seconds = round(
            estimate_section_seconds(word_count, profile=timing_profile),
            1,
        )
        section.setdefault("id", index)
        section["estimated_duration_seconds"] = estimated_seconds
        total_estimated_seconds += estimated_seconds
    data["total_estimated_duration_seconds"] = round(total_estimated_seconds, 1)
    return data


def _script_validation_errors(
    data: dict,
    *,
    numbering_order: str | None,
    max_visual_hold_seconds: float,
    crossfade: float,
    timing_profile,
    min_sections: int | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    web_photos_only: bool = False,
) -> tuple[list[str], list[dict]]:
    sections = data.get("sections", [])
    errors = _title_banner_numbering_errors(data, numbering_order)
    pacing_issues: list[dict] = []
    subscribe_cta_sections: list[int] = []

    if (
        min_sections is not None
        and min_words is not None
        and max_words is not None
    ):
        total_words = 0
        for section in sections:
            total_words += len(str(section.get("narration", "")).split())
        if len(sections) < min_sections:
            errors.append(
                f"script has {len(sections)} sections; needs at least {min_sections}"
            )
        if total_words < min_words:
            errors.append(
                f"script is too short: {total_words} words; needs at least {min_words}"
            )
        if total_words > max_words:
            errors.append(
                f"script is too long: {total_words} words; must be at most {max_words}"
            )

    for idx, section in enumerate(sections, start=1):
        section_id = section.get("id", idx)
        slots = list(section.get("slots", []))
        if not slots:
            errors.append(f"Section {section_id} has no visual slots.")
            continue

        # Structural slot check. Without it a slot missing "visual" passes
        # review and then dies in Script parsing, where there is no retry.
        for slot_idx, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                errors.append(
                    f"Section {section_id} slot {slot_idx} is not an object."
                )
                continue
            visual = str(slot.get("visual", "")).strip()
            if not visual:
                errors.append(
                    f'Section {section_id} slot {slot_idx} is missing the required '
                    f'"visual" field. Every slot needs a visual type.'
                )
            elif visual not in _ALLOWED_SLOT_VISUALS:
                errors.append(
                    f'Section {section_id} slot {slot_idx} has unknown visual '
                    f'"{visual}". Allowed: {", ".join(sorted(_ALLOWED_SLOT_VISUALS))}.'
                )

        word_count = len(str(section.get("narration", "")).split())
        estimated_seconds = estimate_section_seconds(
            word_count,
            profile=timing_profile,
        )
        # Size the slot requirement against how long the narration will
        # actually run, not the estimate. The timing profile is keyed on the
        # TTS model alone, so a channel whose voice direction asks for slow,
        # deliberate delivery with pauses overshoots it: a Horror run measured
        # 21.3s -> 24.69s and 22.8s -> 28.45s (up to 1.25x). Validating against
        # the bare estimate passed a script whose real sections then needed six
        # slots and had five, and the render failed the max-hold check after
        # the narration had already been paid for.
        planning_seconds = estimated_seconds * _TTS_DURATION_MARGIN
        minimum_slots = minimum_visual_slots_for_duration(
            planning_seconds,
            max_visual_hold_seconds,
            crossfade,
        ) + _SOURCING_DROP_ALLOWANCE
        avg_hold = (
            planning_seconds + (crossfade * max(len(slots) - 1, 0))
        ) / len(slots)
        if avg_hold > max_visual_hold_seconds:
            issue = {
                "section_id": section_id,
                "word_count": word_count,
                "current_slots": len(slots),
                "minimum_slots": minimum_slots,
                "recommended_action": "split_section" if minimum_slots > 6 else "add_slots",
                "message": (
                    f"Section {section_id}: {len(slots)} slots is too sparse for "
                    f"{word_count} narration words; needs at least {minimum_slots} slots "
                    f"to keep beats under {max_visual_hold_seconds:.0f}s, so add more slots "
                    "or split the section."
                ),
            }
            pacing_issues.append(issue)
            errors.append(issue["message"])

        title_banner_slots = [
            slot for slot in slots
            if str(slot.get("visual", "")) == "title_banner"
        ]
        if len(title_banner_slots) > 1:
            errors.append(
                f"Section {section_id} has {len(title_banner_slots)} title_banner slots; only one is allowed per section."
            )
        if title_banner_slots and str(slots[0].get("visual", "")) != "title_banner":
            errors.append(
                f"Section {section_id}: title_banner must be the first slot in its section."
            )

        if any(str(slot.get("visual", "")) == "subscribe_cta" for slot in slots):
            subscribe_cta_sections.append(section_id)

        for slot_index, slot in enumerate(slots, start=1):
            visual = str(slot.get("visual", ""))
            props = slot.get("props") or {}
            if visual == "text_only_slide" and props.get("variant") != "ai_prompt_preview":
                errors.append(
                    f"Section {section_id} slot {slot_index}: text_only_slide is reserved for internal AI prompt preview only."
                )
            if visual == "info_slide" and not str(slot.get("prompt", "")).strip():
                errors.append(
                    f"Section {section_id} slot {slot_index}: info_slide requires an image prompt; do not use image-less text slides."
                )
            # Every slot that gets an image needs both halves of the contract:
            # the prompt is what the review gate judges the picture against,
            # the keywords are what image search is actually given. A slot
            # missing either cannot be sourced -- two info_slides shipped with
            # an empty keywords field and took their beats down with them.
            #
            # `_repair_slot_fields` derives keywords from the prompt before
            # this runs, so reaching here means neither field was usable.
            if visual in _SEARCHED_VISUAL_TYPES:
                what = (
                    "background image" if visual == "subscribe_cta" else "image"
                )
                # info_slide already has its own prompt check above; emitting a
                # second one for the same slot gives the model two
                # instructions for one problem.
                if visual != "info_slide" and not str(
                    slot.get("prompt", "")
                ).strip():
                    errors.append(
                        f"Section {section_id} slot {slot_index}: {visual} has "
                        f"an empty \"prompt\" field. Describe the {what} to "
                        f"source; the review gate judges the picture against "
                        f"this description."
                    )
                if not str(slot.get("keywords", "")).strip():
                    errors.append(
                        f"Section {section_id} slot {slot_index}: {visual} has "
                        f"an empty \"keywords\" field. Image search is given "
                        f"the keywords, not the prompt, so this beat cannot be "
                        f"sourced at all. Add concrete {what} search keywords "
                        f"naming the real subject to photograph."
                    )
            # On a web-photo-only channel the keywords are the whole contract
            # with image search: if they name something no photograph or
            # archival document contains, every candidate is rejected and the
            # image review gate fails the run after sourcing has been paid for.
            # Caught here so the scripter revises it instead.
            if web_photos_only and visual in VisualSlot.IMAGE_TYPES:
                keywords_text = str(slot.get("keywords", ""))
                # A chart has a different remedy from an unphotographable
                # subject: change the slot type, not the search terms. Telling
                # the scripter to "find a concrete subject" for a bar chart
                # only sends it looking for a photograph of one.
                conceptual = conceptual_brief_reason(keywords_text)
                if conceptual:
                    errors.append(
                        f"Section {section_id} slot {slot_index}: keywords "
                        f"{keywords_text!r} describe a graphic the renderer draws "
                        f"-- {conceptual}. Do not use '{visual}' for it. Change "
                        f"this slot to \"info_card\" (short callout; set "
                        f"props.text) or \"info_slide\" (titled slide; set "
                        f"props.text, optional props.title) and write the point "
                        f"as text, or replace the beat with a real photographable "
                        f"subject such as the stadium, the player or the crowd."
                    )

                reason = "" if conceptual else non_photographable_reason(keywords_text)
                if reason:
                    errors.append(
                        f"Section {section_id} slot {slot_index}: keywords "
                        f"{keywords_text!r} cannot be sourced as a real "
                        f"photograph -- {reason}. Replace them with a concrete "
                        f"real-world subject that could plausibly appear in an actual "
                        f"photograph or authentic archival document (the real place, "
                        f"aircraft, object, building, document or person), rather than "
                        f"a concept, mood or graphic device."
                    )

            visual_policy = str(slot.get("visual_policy", "source_as_written"))
            if visual_policy not in VisualSlot.VISUAL_POLICIES:
                errors.append(
                    f"Section {section_id} slot {slot_index}: unknown visual_policy '{visual_policy}'."
                )
            if visual_policy == "photo_backed_info_slide" and visual != "info_slide":
                errors.append(
                    f"Section {section_id} slot {slot_index}: visual_policy photo_backed_info_slide requires visual info_slide."
                )
            if visual in VisualSlot.BACKDROP_FIGURE_TYPES:
                if not str(slot.get("prompt", "")).strip():
                    errors.append(
                        f"Section {section_id} slot {slot_index}: {visual} requires a background image prompt."
                    )
                if not str(slot.get("keywords", "")).strip():
                    errors.append(
                        f"Section {section_id} slot {slot_index}: {visual} requires background image keywords."
                    )

    if len(subscribe_cta_sections) > 1:
        errors.append(
            f"Script has {len(subscribe_cta_sections)} subscribe_cta sections; only one is allowed per video."
        )
    return errors, pacing_issues


def _prompt_word_targets(
    *,
    target_seconds: int,
    sections_range: list[int],
    timing_profile,
) -> tuple[int, int, int]:
    representative_sections = max(1.0, sum(sections_range) / 2)
    target_words = max(
        1,
        int(round(words_for_seconds(
            target_seconds,
            section_count=representative_sections,
            profile=timing_profile,
        ))),
    )
    min_words_per_section = max(
        1,
        int(math.ceil(
            words_for_seconds(
                target_seconds,
                section_count=sections_range[1],
                profile=timing_profile,
            ) / sections_range[1]
        )),
    )
    target_words_per_section = max(
        1,
        int(round(target_words / representative_sections)),
    )
    return target_words, min_words_per_section, target_words_per_section


def _duration_word_bounds(
    *,
    min_duration: float,
    max_duration: float,
    section_count: int,
    timing_profile,
) -> tuple[int, int]:
    min_words = max(
        1,
        int(math.ceil(words_for_seconds(
            min_duration,
            section_count=section_count,
            profile=timing_profile,
        ))),
    )
    max_words = max(
        min_words,
        int(math.floor(words_for_seconds(
            max_duration,
            section_count=section_count,
            profile=timing_profile,
        ))),
    )
    return min_words, max_words


async def generate_script(
    config: ChannelConfig,
    plan: dict,
    workspace: Path,
    *,
    allow_review_failure: bool = False,
    preview_mode: bool = False,
) -> tuple[Script, dict]:
    """Generate and review a script for the given plan.

    Returns a validated, reviewed Script.
    """
    topic = plan["topic"]
    video_type = plan["video_type"]
    angle = plan.get("angle", "")
    narrative_hook = plan.get("narrative_hook", "")
    research_context = plan.get("research_context", "")

    # Get video type config
    vt_config = config.video_types.get(video_type)
    if not vt_config:
        raise ValueError(f"Video type '{video_type}' not found in channel config")
    numbering_order = vt_config.numbering_order

    # Generate initial script
    logger.info(f"Generating script: {topic} ({video_type})")

    # Auto-calculate sections from duration (~1 section per 45s)
    target = config.video.target_duration_minutes
    sections_range = compute_sections_range(target, vt_config)
    target_seconds = target * 60
    min_sections = sections_range[0]
    timing_profile = script_timing_profile(settings.gemini_tts_model)
    min_duration, max_duration = duration_window(
        target_seconds,
        preview_mode=preview_mode,
    )
    max_duration_retries = _MAX_DURATION_RETRIES
    max_visual_hold_seconds = config.rendering_defaults.max_visual_hold_seconds
    intra_crossfade = config.rendering_defaults.intra_slot_crossfade
    target_words, min_words_per_section, target_words_per_section = _prompt_word_targets(
        target_seconds=target_seconds,
        sections_range=sections_range,
        timing_profile=timing_profile,
    )
    generation_duration_requirements = (
        f"12. TOTAL WORD COUNT: Aim for about {target_words} words of narration "
        f"across all sections combined for this {target}-minute video.\n"
        f"13. SECTIONS: You MUST write at least {sections_range[0]} sections "
        f"(up to {sections_range[1]}). DO NOT write fewer than {sections_range[0]}.\n"
        "14. PER-SECTION NARRATION: "
        f"with {sections_range[0]}-{sections_range[1]} sections, usually write "
        f"about {min_words_per_section}-{target_words_per_section} words per section. "
        "Write detailed, concrete narration — not filler."
    )
    revision_duration_guard = (
        f"Target about {target_words} total words. "
        f"Keep between {sections_range[0]} and {sections_range[1]} sections. "
        "Do NOT add or remove sections beyond this range."
    )
    thumbnail_strategies = _thumbnail_strategy_options(
        config,
        vt_config.allowed_thumbnail_strategies,
    )

    prompt = prompts.script_generation_prompt(
        topic=topic,
        video_type=video_type,
        angle=angle,
        channel_name=config.channel_name,
        audience=config.niche.audience,
        language=config.language,
        target_duration_minutes=target,
        sections_range=sections_range,
        section_style=vt_config.section_style,
        pacing=vt_config.pacing,
        style_prompt_suffix=config.image_sourcing.style_prompt_suffix,
        title_format_instruction=plan["title_format_instruction"],
        description_style_instruction=plan["description_style_instruction"],
        thumbnail_strategies=thumbnail_strategies,
        visual_guidance=config.video.visual_guidance,
        narrative_hook=narrative_hook,
        research_context=research_context,
        section_color_palette=config.style.text.get("section_color_palette"),
        music_pool=config.video.music_pool,
        voice_variations=config.voice.voice_prompt_variations,
        transition_pool=config.style.video.get("transition_pool", ["fade"]),
        numbering_order=numbering_order,
        duration_requirements=generation_duration_requirements,
        channel_goal=plan.get("channel_goal", ""),
        content_family=plan.get("content_family", ""),
        lead_magnet=plan.get("lead_magnet", ""),
        low_ticket_offer=plan.get("low_ticket_offer", ""),
        mid_ticket_offer=plan.get("mid_ticket_offer", ""),
        later_offers=plan.get("later_offers", []),
        video_jobs=plan.get("video_jobs", []),
        cta_rules=plan.get("cta_rules", []),
        cta_angle=plan.get("cta_angle", ""),
        min_visible_beat_seconds=config.rendering_defaults.image_slot_min_duration,
        max_visual_hold_seconds=max_visual_hold_seconds,
        web_photos_only=config.image_sourcing.web_photos_only,
    )
    system_inst = prompts.script_system(
        tone=config.script_style.tone,
        instructions=config.script_style.instructions,
    )

    def _validate_generated_script(
        content: dict,
    ) -> tuple[list[str], list[dict], int, int, int, int]:
        # Normalise before judging. The model sometimes puts the description
        # in `visual` or omits `keywords` entirely -- both usable content in
        # the wrong shape, and both previously fatal. Repairing first means the
        # validator judges the script's substance rather than its typing.
        _coerce_slot_keywords(content)
        _repair_slot_fields(content)
        _apply_word_based_duration_estimates(
            content,
            timing_profile=timing_profile,
        )
        n_sections, total_words = _script_word_totals(content)
        current_section_count = max(n_sections, min_sections)
        min_words, max_words = _duration_word_bounds(
            min_duration=min_duration,
            max_duration=max_duration,
            section_count=current_section_count,
            timing_profile=timing_profile,
        )
        errors, pacing_issues = _script_validation_errors(
            content,
            numbering_order=numbering_order,
            min_sections=min_sections,
            min_words=min_words,
            max_words=max_words,
            max_visual_hold_seconds=max_visual_hold_seconds,
            crossfade=intra_crossfade,
            timing_profile=timing_profile,
            web_photos_only=config.image_sourcing.web_photos_only,
        )
        return errors, pacing_issues, n_sections, total_words, min_words, max_words

    def _script_validation_feedback(
        *,
        errors: list[str],
        pacing_issues: list[dict],
        n_sections: int,
        total_words: int,
        min_words: int,
        max_words: int,
    ) -> str:
        lines = [
            "Script failed validation after generation.",
            f"Current sections: {n_sections}; allowed range: {sections_range[0]}-{sections_range[1]}.",
            f"Current narration words: {total_words}; allowed range: {min_words}-{max_words}; target about {target_words}.",
            "Validation failures:",
            *[f"- {error}" for error in errors],
        ]
        if pacing_issues:
            lines.append("Pacing-specific slot budgets:")
            lines.extend(
                f"- Section {issue['section_id']}: {issue['current_slots']} slots now, "
                f"needs at least {issue['minimum_slots']}."
                for issue in pacing_issues
            )
        return "\n".join(lines)

    script_data = await clients.generate_json(
        prompt,
        system_instruction=system_inst,
        temperature=0.8,
        max_output_tokens=16384,
        operation_label="script_generate",
    )

    # ── Word-budget validation loop ───────────────────────────────

    for retry in range(max_duration_retries + 1):
        (
            errors,
            pacing_issues,
            n_sections,
            total_words,
            min_words,
            max_words,
        ) = _validate_generated_script(script_data)

        if not errors:
            logger.info(
                f"Script structure OK: {n_sections} sections, "
                f"{total_words} words "
                f"(target about {target_words}, allowed {min_words}-{max_words})"
            )
            break

        if retry == max_duration_retries:
            raise ValueError(
                f"Script validation failed after {max_duration_retries} retries: "
                + "; ".join(errors)
            )

        logger.warning(
            f"Script validation failed (attempt {retry + 1}/{max_duration_retries}): "
            f"{n_sections} sections, {total_words} words "
            f"(target about {target_words}, allowed {min_words}-{max_words}): "
            + "; ".join(errors)
        )

        revision_prompt = prompts.script_revision_prompt(
            script_json=json.dumps(script_data, ensure_ascii=False, indent=2),
            feedback=_script_validation_feedback(
                errors=errors,
                pacing_issues=pacing_issues,
                n_sections=n_sections,
                total_words=total_words,
                min_words=min_words,
                max_words=max_words,
            ),
            sections_range=tuple(sections_range),
            duration_guard=revision_duration_guard,
        )

        script_data = await clients.generate_json(
            revision_prompt,
            system_instruction=system_inst,
            temperature=0.7,
            max_output_tokens=16384,
            operation_label="script_validation_revision",
        )

    # ── Gate #1: Script Quality Review ────────────────────────────
    min_score = config.review_thresholds.script_min_score
    max_attempts = config.review_thresholds.script_max_attempts

    async def _regenerate(content: dict, feedback) -> dict:
        feedback_str = feedback.get("feedback", "") if isinstance(feedback, dict) else feedback
        revision_prompt = prompts.script_revision_prompt(
            script_json=json.dumps(content, ensure_ascii=False, indent=2),
            feedback=feedback_str,
            sections_range=tuple(sections_range),
            duration_guard=revision_duration_guard,
        )
        revised = await clients.generate_json(
            revision_prompt,
            system_instruction=system_inst,
            temperature=0.7,
            max_output_tokens=16384,
            operation_label="script_revision",
        )
        for retry in range(max_duration_retries + 1):
            (
                errors,
                pacing_issues,
                n_sections,
                total_words,
                min_words,
                max_words,
            ) = _validate_generated_script(revised)
            if not errors:
                return revised
            if retry == max_duration_retries:
                raise ValueError("Script revision failed validation: " + "; ".join(errors))

            logger.warning(
                "Script revision failed validation "
                f"(attempt {retry + 1}/{max_duration_retries}): "
                + "; ".join(errors)
            )
            revision_prompt = prompts.script_revision_prompt(
                script_json=json.dumps(revised, ensure_ascii=False, indent=2),
                feedback=_script_validation_feedback(
                    errors=errors,
                    pacing_issues=pacing_issues,
                    n_sections=n_sections,
                    total_words=total_words,
                    min_words=min_words,
                    max_words=max_words,
                ),
                sections_range=tuple(sections_range),
                duration_guard=revision_duration_guard,
            )
            revised = await clients.generate_json(
                revision_prompt,
                system_instruction=system_inst,
                temperature=0.7,
                max_output_tokens=16384,
                operation_label="script_revision_validation_correction",
            )
        return revised

    def _review_prompt(content: dict) -> str:
        return prompts.script_review_prompt(
            script_json=json.dumps(content, ensure_ascii=False, indent=2),
            min_score=min_score,
            numbering_order=numbering_order,
            max_visual_hold_seconds=max_visual_hold_seconds,
            web_photos_only=config.image_sourcing.web_photos_only,
        )

    try:
        result = await review_gate(
            content=script_data,
            review_prompt_fn=_review_prompt,
            system_instruction=prompts.script_review_system(),
            regenerate_fn=_regenerate,
            max_attempts=max_attempts,
            gate_name="script_review",
        )
    except ReviewGateError as e:
        if not allow_review_failure:
            raise
        logger.warning(
            "[script_review] Continuing with best rejected script because "
            "allow_review_failure=True"
        )
        result = e.result

    # Parse into Script model
    final_data = result["content"]
    _coerce_slot_keywords(final_data)
    _destage_slot_briefs(
        final_data,
        web_photos_only=config.image_sourcing.web_photos_only,
    )
    _ensure_closing_question(
        final_data,
        fallback=config.closing_question_fallback,
    )
    final_errors, _, _, _, _, _ = _validate_generated_script(final_data)
    if final_errors:
        raise ValueError("Reviewed script failed validation: " + "; ".join(final_errors))
    chosen_thumbnail_strategy = _validate_thumbnail_strategy_choice(
        final_data.get("thumbnail_strategy", ""),
        video_type=video_type,
        allowed_names=vt_config.allowed_thumbnail_strategies,
    )
    script = Script(
        title=final_data.get("title", plan.get("topic", "Untitled")),
        video_type=final_data.get("video_type", video_type),
        description=final_data.get("description", ""),
        tags=final_data.get("tags", []),
        hook=final_data.get("hook", ""),
        content_family=plan.get("content_family", final_data.get("content_family", "")),
        lead_magnet=plan.get("lead_magnet", final_data.get("lead_magnet", "")),
        low_ticket_offer=plan.get("low_ticket_offer", final_data.get("low_ticket_offer", "")),
        mid_ticket_offer=plan.get("mid_ticket_offer", final_data.get("mid_ticket_offer", "")),
        thumbnail_text=final_data.get("thumbnail_text", ""),
        thumbnail_brief=final_data.get("thumbnail_brief", ""),
        thumbnail_strategy=chosen_thumbnail_strategy,
        total_estimated_duration_seconds=final_data.get("total_estimated_duration_seconds", 0),
        music_track="",
        voice_variation=0,
        intra_transition="fade",
        sections=[
            ScriptSection(**s) for s in final_data.get("sections", [])
        ],
    )

    # Validate LLM visual slots
    for s in script.sections:
        if not s.slots:
            raise ValueError(f"Section {s.id} has no visual slots")
        if s != script.sections[0] and not s.transition_type:
            raise ValueError(f"Section {s.id} missing transition_type")

    # Per-section colors only when the channel opts in via section_color_palette.
    # Without a palette, clear any AI-emitted accent_color so the channel's
    # title_accent_color is used consistently — both section-level and
    # per-slot props (InfoSlide, backdrop figure scenes).
    channel_accent = config.style.text.get("title_accent_color")
    palette = config.style.text.get("section_color_palette")
    if palette:
        for i, section in enumerate(script.sections):
            if not section.accent_color:
                section.accent_color = palette[i % len(palette)]
    else:
        for section in script.sections:
            section.accent_color = None
            for slot in section.slots:
                if "accent_color" in slot.props and channel_accent:
                    slot.props["accent_color"] = channel_accent

    _inject_forced_slots(script, config.forced)
    save_script(workspace, script)
    logger.info(f"Script saved: {script.title} ({len(script.sections)} sections)")

    return script, result


_FORCED_SLOT_DEFAULTS: dict[str, dict] = {
    "TitleCard": {
        "visual": "title_card",
        "props": {"title": "", "subtitle": "", "accent_color": "#00D4FF"},
    },
    "FactHighlight": {
        "visual": "fact_highlight",
        "props": {"value": "42", "label": "test stat", "unit": "%", "accent_color": "#FFE500"},
    },
}

# Map from force name to visual type for lookup
_FORCE_TO_VISUAL = {"TitleCard": "title_card", "FactHighlight": "fact_highlight"}
FORCED_SLOT_NAMES = set(_FORCED_SLOT_DEFAULTS)


def _forced_slot_media(script: Script, section: ScriptSection) -> tuple[str, str]:
    title = next(
        (
            str(slot.props.get("title", "")).strip()
            for slot in section.slots
            if slot.visual == "title_banner" and slot.props.get("title")
        ),
        "",
    )
    subject = title or section.narration.split(".", 1)[0].strip() or script.title
    return (
        f"Realistic photo of {subject.rstrip('. ')}.",
        f"{subject} real photo".strip(),
    )


def _inject_forced_slots(script: Script, forced: set[str]) -> None:
    """Inject forced backdrop figure slots into sections that don't already have one."""
    forced_slot_names = forced & FORCED_SLOT_NAMES
    if not forced_slot_names:
        return

    present = set()
    for section in script.sections:
        for slot in section.slots:
            if slot.visual in VisualSlot.BACKDROP_FIGURE_TYPES:
                present.add(slot.visual)

    missing_visuals = {_FORCE_TO_VISUAL[name] for name in forced_slot_names}
    missing_visuals -= present
    if not missing_visuals:
        return

    available = list(script.sections)
    visual_to_force = {v: k for k, v in _FORCE_TO_VISUAL.items()}

    for visual_type in sorted(missing_visuals):
        if not available:
            logger.warning(f"test.force: no available section for {visual_type}")
            break
        section = available.pop(0)
        force_name = visual_to_force[visual_type]
        slot_data = dict(_FORCED_SLOT_DEFAULTS[force_name])
        prompt, keywords = _forced_slot_media(script, section)
        slot_data["prompt"] = prompt
        slot_data["keywords"] = keywords
        if force_name == "TitleCard":
            slot_data["props"] = {"title": script.title, "subtitle": "", "accent_color": "#00D4FF"}
        if visual_type == "title_card":
            section.slots.insert(0, VisualSlot(**slot_data))
        else:
            section.slots.append(VisualSlot(**slot_data))
        logger.info(f"test.force: injected {force_name} into section {section.id}")
