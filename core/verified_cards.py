"""The safe floor under image sourcing: a card that states only cited facts.

Every tier above this one tries to find a *photograph* of the thing the beat is
about. When they all miss, the run used to die -- and on run
99db2deb-4a16-4467-b318-a01fba131b7e that is exactly what happened: a fully
grounded match (Real Madrid 2-1 Inter Milan, 8 September 2026, cited to ESPN in
research.json) produced a finished script and finished narration, and then threw
all of it away because nine beats could not obtain an acceptable photo.

A card is not a photograph and must never be mistaken for one. So:

  * every visible string is copied or composed from text the research layer
    already cited. `verify_card_text` re-derives that independently of whoever
    built the card, and a card that fails it is not used;
  * numbers are the sharpest way to lie, so every digit run in the card must
    appear in the cited corpus;
  * the card carries an explicit "verified information" label and its source
    domain, and is refused if it contains photo/footage vocabulary in any of
    the run's languages;
  * it renders as an `info_card` -- a flat editorial panel the renderer already
    draws locally, with no illustration wired in. Nothing here generates an
    image, calls a provider, or costs anything.

What this module deliberately cannot do is rescue an *ungrounded* run. With no
citations there is no corpus, `load_grounded_facts` returns None, and the
failure stays terminal -- which is the correct outcome, because the only way to
fill those beats would be to make something up.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("video_factory")

#: Marks a slot's props as built by this module. Read by the tests, the
#: sourcing log and anything downstream that needs to know a beat is an
#: editorial card rather than a sourced picture.
VERIFIED_CARD_KEY = "verified_card"

#: Longest a card body may get before it stops being readable at 1080x1920.
_MAX_CARD_CHARS = 220
_MAX_LINE_CHARS = 60

# Words that would make a card claim to be a picture of the event. Refused
# outright rather than stripped: a card that wants to say "footage" is a card
# built from the wrong source text.
_PHOTO_WORDS = (
    "photo", "photograph", "footage", "pictured", "image of", "screenshot",
    "camera", "video of",
    "صورة", "صور", "لقطة", "لقطات", "مشهد", "فيديو", "تصوير", "كاميرا",
)

# Chrome the card is allowed to say without it appearing in the corpus: these
# are labels about the card itself, not claims about the world.
_CHROME_EN = {
    "verified", "information", "source", "match", "result", "competition",
    "date", "vs", "v", "and", "the", "of", "on", "in", "at", "final", "score",
}
_CHROME_AR = {
    "معلومات", "موثقة", "المصدر", "المباراة", "النتيجة", "البطولة", "التاريخ",
    "ضد", "و", "في", "النهائية", "نتيجة", "بطاقة",
}

# Deterministic card designs, cycled by variant so a run needing several cards
# does not show the same panel four times running. Each is a different
# arrangement of the *same* cited fields -- none of them can show more than the
# citations support.
#
# Three, not four: a four-entry cycle whose first and last entries matched put
# two identical panels next to each other every time it wrapped, which is the
# one thing the cycle exists to prevent.
_LAYOUTS = ("scoreline", "fact", "matchup")

_LABELS = {
    "en": {
        "heading": "VERIFIED INFORMATION",
        "source": "Source",
        "competition": "Competition",
        "result": "Final score",
        "date": "Date",
        "fixture": "Match",
    },
    "ar": {
        "heading": "معلومات موثقة",
        "source": "المصدر",
        "competition": "البطولة",
        "result": "النتيجة النهائية",
        "date": "التاريخ",
        "fixture": "المباراة",
    },
}

# Competitions are matched, never guessed: the name has to be present in the
# cited text before it can appear on a card.
_COMPETITIONS = (
    ("uefa champions league", {"en": "UEFA Champions League", "ar": "دوري أبطال أوروبا"}),
    ("champions league", {"en": "Champions League", "ar": "دوري الأبطال"}),
    ("europa league", {"en": "Europa League", "ar": "الدوري الأوروبي"}),
    ("premier league", {"en": "Premier League", "ar": "الدوري الإنجليزي"}),
    ("la liga", {"en": "LaLiga", "ar": "الدوري الإسباني"}),
    ("serie a", {"en": "Serie A", "ar": "الدوري الإيطالي"}),
    ("bundesliga", {"en": "Bundesliga", "ar": "الدوري الألماني"}),
    ("ligue 1", {"en": "Ligue 1", "ar": "الدوري الفرنسي"}),
    ("world cup", {"en": "World Cup", "ar": "كأس العالم"}),
    ("copa del rey", {"en": "Copa del Rey", "ar": "كأس ملك إسبانيا"}),
)

# "Real Madrid 2-1 Inter Milan (Sep 8, 2026)" and the same line without the
# parenthetical. Team names are captured, not matched against a club list --
# a club list is a place for a name the sources never mentioned to sneak in.
_SCORELINE_RE = re.compile(
    r"([A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3})"
    r"\s+(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+"
    r"([A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3})"
)
_DATE_RE = re.compile(
    r"\(?\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"\d{1,2},?\s+\d{4})\)?"
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_DIGITS_RE = re.compile(r"\d+")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# A scoreline as it appears on a finished card. Checked as a unit, because
# checking its digits one at a time does not work: "3" occurs in the corpus in
# "3 days ago", so a card reading 3-1 would pass a per-digit test while
# reporting a result that never happened. The pair, and the order of the teams
# around it, have to match what was cited.
_CARD_SCORE_RE = re.compile(
    r"(?P<home>[^\n\d]+?)\s+(?P<hs>\d{1,2})\s*[-–—]\s*(?P<as>\d{1,2})\s+(?P<away>[^\n\d]+)"
)


@dataclass(frozen=True)
class GroundedFacts:
    """Everything the run is allowed to put on a card, and nothing else.

    `corpus` is the union of the cited text: the research brief, each verified
    claim, and each source title. It is the authority for `verify_card_text` --
    a string that cannot be found here does not go on a card.
    """

    corpus: str
    sources: tuple[str, ...] = ()
    home: str = ""
    away: str = ""
    home_score: str = ""
    away_score: str = ""
    date_text: str = ""
    competition_key: str = ""
    _lower: str = field(default="", repr=False, compare=False)

    @property
    def lower(self) -> str:
        return self._lower or self.corpus.lower()

    @property
    def has_scoreline(self) -> bool:
        return bool(self.home and self.away and self.home_score and self.away_score)

    def competition(self, language: str) -> str:
        for key, names in _COMPETITIONS:
            if key == self.competition_key:
                return names.get(language, names["en"])
        return ""


def _language_of(config, script=None) -> str:
    """'ar' when the run narrates in Arabic, else 'en'.

    Read from the channel first because that is what the voice and the captions
    follow; the script's own text is the tiebreak for a channel that has not
    declared one.
    """
    for probe in (
        getattr(getattr(config, "voice", None), "language", None),
        getattr(config, "language", None),
        getattr(getattr(config, "script", None), "language", None),
    ):
        text = str(probe or "").lower()
        if text.startswith("ar"):
            return "ar"
        if text.startswith("en"):
            return "en"
    sample = ""
    for section in getattr(script, "sections", None) or []:
        sample += str(getattr(section, "narration", "") or "")
        if len(sample) > 200:
            break
    arabic = sum(1 for ch in sample if "؀" <= ch <= "ۿ")
    return "ar" if arabic > len(sample) * 0.3 else "en"


def load_grounded_facts(workspace: Path) -> GroundedFacts | None:
    """Read the run's own citations. Returns None when the run is not grounded.

    Only `research.json` is trusted, and only when the research layer marked it
    `grounded`. The plan's `research_context` is a rendering of the same brief,
    so it adds nothing; the user's topic and the model's memory are not read at
    all, which is the point -- neither is evidence.
    """
    path = Path(workspace) / "research.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[verified_card] research.json unreadable: {exc}")
        return None
    if not isinstance(data, dict) or not data.get("grounded"):
        return None

    parts: list[str] = [str(data.get("brief") or "")]
    sources: list[str] = []
    for fact in data.get("verified_facts") or []:
        if isinstance(fact, dict):
            parts.append(str(fact.get("claim") or ""))
    for source in data.get("sources") or []:
        if isinstance(source, dict):
            parts.append(str(source.get("title") or ""))
            domain = str(source.get("domain") or "").strip()
            if domain and domain not in sources:
                sources.append(domain)
    for entity in data.get("key_entities") or []:
        parts.append(str(entity if isinstance(entity, str) else ""))

    corpus = "\n".join(p for p in parts if p.strip())
    if not corpus.strip() or not sources:
        return None

    lower = corpus.lower()
    home = away = home_score = away_score = ""
    score_match = _SCORELINE_RE.search(corpus)
    if score_match:
        home, home_score, away_score, away = (
            score_match.group(1).strip(),
            score_match.group(2),
            score_match.group(3),
            score_match.group(4).strip(),
        )

    date_text = ""
    date_match = _DATE_RE.search(corpus)
    if date_match:
        date_text = date_match.group(1).strip().rstrip(",")
    else:
        iso = _ISO_DATE_RE.search(corpus)
        if iso:
            date_text = iso.group(0)

    competition_key = ""
    for key, _names in _COMPETITIONS:
        if key in lower:
            competition_key = key
            break

    return GroundedFacts(
        corpus=corpus,
        sources=tuple(sources),
        home=home,
        away=away,
        home_score=home_score,
        away_score=away_score,
        date_text=date_text,
        competition_key=competition_key,
        _lower=lower,
    )


def is_verified_card(slot) -> bool:
    """Whether this slot is one of ours, whatever visual type it carries.

    The prop is the authority, not the visual name. Cards were first written as
    `info_card` before `verified_card` existed, and workspaces on disk still
    hold those -- a resumed run has to recognise its own earlier output or it
    will neither refresh it nor reclaim it.
    """
    return bool(
        getattr(slot, "visual", "") in {"verified_card", "info_card"}
        and (getattr(slot, "props", None) or {}).get(VERIFIED_CARD_KEY)
    )


def migrate_legacy_cards(script, config, facts: GroundedFacts | None) -> int:
    """Redraw cards an earlier version of this module wrote.

    A resumed workspace can hold cards from before the designed card system
    existed: visual `info_card`, a single text blob, and none of the structured
    fields the VerifiedCard component draws from. Left alone they render as the
    old grey text panel forever, because the refresh path only runs when the
    review gate fails and a run whose photographs pass never reaches it.

    Rebuilt from the same citations, so this changes how a card looks and never
    what it claims. Returns how many were migrated.
    """
    if facts is None:
        return 0
    language = _language_of(config, script)
    migrated = 0
    in_run = 0
    for section in script.sections:
        for slot in section.non_overlay_slots:
            if not is_verified_card(slot):
                continue
            needs = slot.visual != "verified_card" or "layout" not in (slot.props or {})
            props = build_card_props(
                facts, language,
                accent_color=getattr(section, "accent_color", None),
                variant=in_run,
            )
            in_run += 1
            if props is None or not needs:
                continue
            previous = slot.props or {}
            props["original_prompt"] = previous.get("original_prompt", "")
            props["original_keywords"] = previous.get("original_keywords", "")
            slot.visual = "verified_card"
            slot.visual_policy = "source_as_written"
            slot.props = props
            migrated += 1
    if migrated:
        logger.info(
            f"[verified_card] redrew {migrated} card(s) written by an earlier "
            f"version in the current card design"
        )
    return migrated


def known_entities(facts: GroundedFacts, script=None) -> tuple[set[str], set[str]]:
    """(people, clubs) the run has actually established, for the beat router.

    Clubs come from the parsed scoreline; people come from what the scripter
    already recorded per slot (`football_player_name`), which is the only place
    a name has been checked against a current club. Nothing is guessed from a
    hardcoded roster -- a roster is where a player who left in 2021 gets
    treated as current.
    """
    clubs = {c for c in (facts.home, facts.away) if c}
    people: set[str] = set()
    for section in getattr(script, "sections", None) or []:
        for slot in getattr(section, "non_overlay_slots", None) or []:
            name = str((getattr(slot, "props", None) or {}).get(
                "football_player_name"
            ) or "").strip()
            if name:
                people.add(name)
    return people, clubs


def grounded_brief(workspace: Path) -> str:
    """The run's citations as prose, for a reviewer that would otherwise guess.

    Same source as the cards -- research.json, and only when the research layer
    marked it grounded -- so a gate cannot be shown "verified facts" the card
    builder would refuse. Empty string when the run has no citations, which
    leaves the reviewer's prompt exactly as it was.
    """
    facts = load_grounded_facts(workspace)
    return facts.corpus.strip() if facts else ""


def _chrome(language: str) -> set[str]:
    return {w.lower() for w in (_CHROME_AR if language == "ar" else _CHROME_EN)}


def _allowed_translation_words(facts: GroundedFacts, language: str) -> set[str]:
    """Words a localisation may add that are not themselves new claims.

    A competition name is put on the card in the run's language, and the Arabic
    for "UEFA Champions League" is obviously not in an English ESPN headline.
    That is a translation of something the corpus does say -- the key was
    matched against the corpus before it could be selected -- so its words are
    allowed. Nothing else gets this treatment: a competition is the only field
    that is looked up rather than copied.
    """
    allowed: set[str] = set()
    if not facts.competition_key:
        return allowed
    for key, names in _COMPETITIONS:
        if key != facts.competition_key:
            continue
        for name in names.values():
            allowed.update(w.lower() for w in _WORD_RE.findall(name))
    return allowed


def _scoreline_problems(body: str, facts: GroundedFacts) -> list[str]:
    """A result printed on a card must be the result that was cited.

    Both halves matter. `2 - 1` when the sources say `2 - 1` is fine; `3 - 1`
    is a fabricated result, and `Inter Milan 2 - 1 Real Madrid` is the true
    scoreline attached to the wrong winner, which is worse than a missing card.
    """
    match = _CARD_SCORE_RE.search(body)
    if not match:
        return []
    if not facts.has_scoreline:
        return ["card shows a scoreline but none was cited"]

    if (match.group("hs"), match.group("as")) != (facts.home_score, facts.away_score):
        return [
            f"card scoreline {match.group('hs')}-{match.group('as')} does not "
            f"match the cited {facts.home_score}-{facts.away_score}"
        ]

    home = match.group("home").strip().lower()
    away = match.group("away").strip().lower()
    if facts.home.lower() not in home or facts.away.lower() not in away:
        return [
            f"card attributes the cited {facts.home_score}-{facts.away_score} "
            f"to the wrong side"
        ]
    return []


def verify_card_text(
    text: str,
    facts: GroundedFacts,
    language: str,
) -> list[str]:
    """Re-derive, from the citations alone, whether this card may be shown.

    Deliberately independent of `build_card_props`: the builder could have a
    bug, or a future caller could compose a card some other way, and the check
    that matters is the one that reads only the finished string and the cited
    corpus. Returns the reasons to refuse; empty means the card is safe.

    This is the info-card contract, and it is not the photo reviewer. It asks
    whether the words are supported, readable and honest about being a graphic.
    It does not ask whether an image depicts an event, because there is no
    image.
    """
    problems: list[str] = []
    body = (text or "").strip()
    if not body:
        return ["card has no text"]
    if len(body) > _MAX_CARD_CHARS:
        problems.append(f"card text is {len(body)} chars; unreadable above {_MAX_CARD_CHARS}")
    for line in body.splitlines():
        if len(line.strip()) > _MAX_LINE_CHARS:
            problems.append(f"line too long to read: {line.strip()[:40]}...")
            break

    lowered = body.lower()
    for word in _PHOTO_WORDS:
        if word in lowered:
            problems.append(f"card implies photographic evidence ({word!r})")
            break

    heading = _LABELS[language]["heading"]
    if heading not in body:
        problems.append("card is not labelled as verified information")

    corpus = facts.lower
    problems.extend(_scoreline_problems(body, facts))
    for number in _DIGITS_RE.findall(body):
        if number not in corpus:
            problems.append(f"unsupported number on card: {number}")

    allowed = _chrome(language) | _chrome("en" if language == "ar" else "ar")
    allowed |= _allowed_translation_words(facts, language)
    for word in _WORD_RE.findall(body):
        low = word.lower()
        if low in allowed or len(low) <= 1:
            continue
        if low in corpus:
            continue
        problems.append(f"unsupported word on card: {word}")
        break

    # The run's own language, so a card does not appear in English under Arabic
    # narration. Latin source names and scorelines are expected either way.
    if language == "ar" and not any("؀" <= ch <= "ۿ" for ch in body):
        problems.append("Arabic run received a card with no Arabic text")

    return problems


def build_card_props(
    facts: GroundedFacts,
    language: str,
    *,
    accent_color: str | None = None,
    variant: int = 0,
) -> dict | None:
    """Compose the card, strongest grounded tier first.

    1. the full result -- teams, score, competition, date;
    2. the fixture -- teams and competition, when no score was cited;
    3. the competition alone.

    Below that there is nothing truthful left to print, so it returns None and
    the caller keeps the beat's failure rather than inventing a filler claim.

    `variant` changes which of those cited fields the card leads on, so a
    section that needs several cards does not show the same panel four times in
    a row -- which is what section 2 of run 99db2deb did. It only ever
    reorders and omits; no variant can print anything the tier above did not
    already establish from the citations.
    """
    labels = _LABELS[language]
    lines: list[str] = [labels["heading"]]
    competition = facts.competition(language)

    if facts.has_scoreline:
        result = f"{facts.home}  {facts.home_score} – {facts.away_score}  {facts.away}"
    elif facts.home and facts.away:
        result = f"{facts.home}  –  {facts.away}"
    elif competition:
        result = ""
    else:
        return None

    fixture = f"{facts.home}  –  {facts.away}" if facts.home and facts.away else ""
    # Each entry is a subset of the same cited fields, in a different order.
    layouts = [
        [result, competition, facts.date_text],
        [competition, result],
        [fixture or result, facts.date_text],
        [result, facts.date_text],
    ]
    for line in layouts[variant % len(layouts)]:
        if line and line not in lines:
            lines.append(line)
    if len(lines) == 1:
        # The chosen layout added nothing this run has; fall back to the
        # strongest tier rather than shipping a card that is only a heading.
        for line in (result, competition, facts.date_text):
            if line and line not in lines:
                lines.append(line)
    if len(lines) == 1:
        return None

    if facts.sources:
        lines.append(f"{labels['source']}: {facts.sources[0]}")

    text = "\n".join(lines)
    problems = verify_card_text(text, facts, language)
    if problems:
        logger.warning(f"[verified_card] refused: {problems[0]}")
        return None

    props: dict = {
        # `text` is kept as the verified payload the checker reads and as the
        # fallback for any renderer that only understands a text card. The
        # VerifiedCard component ignores it and uses the structured fields.
        "text": text,
        VERIFIED_CARD_KEY: True,
        "card_kind": "verified_information",
        "grounded_sources": list(facts.sources[:2]),
        # ── structured fields for the VerifiedCard component ──
        "layout": _LAYOUTS[variant % len(_LAYOUTS)],
        "eyebrow": labels["heading"],
        "home": facts.home,
        "away": facts.away,
        "home_score": facts.home_score,
        "away_score": facts.away_score,
        "headline": competition or (result if not facts.has_scoreline else ""),
        "competition": competition,
        "date_text": facts.date_text,
        "source_line": f"{labels['source']}: {facts.sources[0]}" if facts.sources else "",
        "rtl": language == "ar",
    }
    # A layout that needs a scoreline cannot be used without one.
    if props["layout"] in {"scoreline"} and not facts.has_scoreline:
        props["layout"] = "matchup" if (facts.home and facts.away) else "fact"
    if props["layout"] == "matchup" and not (facts.home and facts.away):
        props["layout"] = "fact"
    if accent_color:
        props["accent_color"] = accent_color
    return props


def apply_verified_cards(
    *,
    script,
    config,
    facts: GroundedFacts,
    targets: set[tuple[int, int]],
    raw_dir: Path | None = None,
    videos_dir: Path | None = None,
    sourcing_log: list[dict] | None = None,
) -> list[tuple[int, int]]:
    """Turn the named beats into verified cards. Everything else is untouched.

    `targets` is (section_id, sub_index) with sub_index 1-based, matching what
    the image reviewer reports. Beats not named here keep whatever they have --
    a slot that passed review is never rebuilt, which is the whole point of
    doing this per slot instead of per stage.

    The rejected picture is deleted, not left on disk: a beat that becomes a
    card must not still have the file the reviewer refused sitting where the
    renderer would find it.
    """
    language = _language_of(config, script)
    converted: list[tuple[int, int]] = []

    # Counted across the whole video, not per section. Per-section numbering
    # restarted at every boundary, so the last card of one section and the
    # first of the next were the same panel -- which is the adjacency a viewer
    # actually notices.
    in_run = 0
    for section in script.sections:
        for sub_idx, slot in enumerate(section.non_overlay_slots):
            key = (section.id, sub_idx + 1)
            # Cards this pass is creating, plus cards an earlier pass on the
            # same workspace already created. Re-numbering the existing ones is
            # what keeps a resumed run from stacking four identical panels: the
            # first pass numbered them without knowing how many would follow.
            already = is_verified_card(slot)
            if key not in targets and not already:
                continue
            props = build_card_props(
                facts,
                language,
                accent_color=getattr(section, "accent_color", None),
                variant=in_run,
            )
            in_run += 1
            if props is None:
                logger.warning(
                    f"[verified_card] s{section.id}.{sub_idx + 1}: no cited fact "
                    f"can carry this beat; leaving it uncovered"
                )
                continue

            stem = f"section_{section.id:03d}_{sub_idx + 1:02d}"
            for directory, suffixes in (
                (raw_dir, (".jpg", ".png")),
                (videos_dir, (".mp4",)),
            ):
                if not directory:
                    continue
                for suffix in suffixes:
                    stale = Path(directory) / f"{stem}{suffix}"
                    if stale.exists():
                        stale.unlink()

            # Keep what the beat was about. Clearing these outright made the
            # conversion one-way: a later pass -- a resume, or the cheap
            # generator tier running after a config change -- had no way to
            # tell a stadium beat from a scoreline beat any more, so every
            # card stayed a card forever.
            #
            # Carried from the slot's EXISTING props, not from `props`, which
            # is a freshly built card payload and knows nothing. Reading the
            # new dict here silently dropped the originals every time an
            # already-carded beat was refreshed, and the tier below then
            # generated from an empty brief.
            previous = getattr(slot, "props", None) or {}
            props["original_prompt"] = (
                slot.prompt or previous.get("original_prompt", "")
            )
            props["original_keywords"] = (
                slot.keywords or previous.get("original_keywords", "")
            )
            slot.visual = "verified_card"
            slot.props = props
            slot.prompt = ""
            slot.keywords = ""
            # The policy describes how to *source* the beat, and this beat is
            # no longer sourced. Leaving the old one behind broke the run: a
            # converted slot still carrying `photo_backed_info_slide` failed
            # the sourcer's own invariant, which requires that policy to sit on
            # an info_slide, and took the whole stage down again.
            slot.visual_policy = "source_as_written"
            converted.append(key)

            if sourcing_log is not None:
                sourcing_log.append({
                    "section_id": section.id,
                    "sub_image_index": sub_idx + 1,
                    "file": None,
                    "keywords": "",
                    "source": "verified_info_card (grounded facts)",
                })

    if converted:
        logger.info(
            f"[verified_card] {len(converted)} beat(s) now show a grounded "
            f"information card instead of a photograph: "
            + ", ".join(f"s{s}.{i}" for s, i in converted)
        )
    return converted
