"""Choosing the photograph a thumbnail is built on.

The thumbnail model edits rather than generates, so the single most important
decision in the stage is which image it is handed. A press photograph of the
actual footballer, relit and recomposed, keeps his face. The same prompt over a
photograph of a trophy keeps nothing -- the model has no person to preserve, so
it invents two, and the result is a thumbnail of players who were never in the
story.

That is not hypothetical. Ranking on "real photograph, largest first" picked a
Champions League trophy out of a Real Madrid v Inter run that also held press
photographs of Bellingham, Lautaro, Vinícius and Courtois. Every image was
1920x1080 after processing and every one named a story subject, so the sort had
no key left and fell through to whatever the directory listed first. The edit
duly invented two players and the review gate rejected it.

So the ranking asks, in order:

  1. Is it a real photograph? A sourced photo beats a generated one, always.
     `generated: True` in the run's own provenance is the only thing trusted to
     tell them apart -- not the filename, not the platform string.
  2. Does it show a person the story names? A named footballer is the strongest
     possible base, because his face is the thing the edit must not change.
  3. Does it name a story subject at all?
  4. Does it show people at all -- a crowd, a bench, a celebration? Still far
     better than an object.
  5. Then, and only then, picture quality: how close the frame already is to
     the thumbnail's shape, and how much detail it carries.

Nothing falls through to filename order. Where every key ties, a content hash
decides, so the same workspace always yields the same base.

Where the run produced nothing usable this returns an empty list, and the
caller falls back to its existing strategy to obtain a base before editing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from pathlib import Path

from PIL import Image

from core import arabic_names
from core.utils import Script

logger = logging.getLogger("video_factory")

#: Shipped stills are named section_{id:03d}_{idx:02d}. Anything else in the
#: directory is not a beat's image and is not a thumbnail base.
_ASSET_RE = re.compile(r"^section_(\d{3})_(\d{2})$")

#: Below this, an edit has nothing to work with.
_MIN_PIXELS = 320 * 240

#: Capitalised words that are never a person, so "UEFA Champions League" and
#: "Santiago Bernabeu" are not mistaken for footballers. Deliberately about
#: *kinds* of thing rather than an exhaustive club list: a name this misses is
#: only demoted to the subject-match tier, never promoted to a face.
_NOT_A_PERSON = {
    # competitions and bodies
    "uefa", "fifa", "champions", "league", "cup", "europa", "premier",
    "liga", "serie", "bundesliga", "ligue", "copa", "euro", "worldcup",
    "conference", "nations", "supercup", "derby", "final", "semifinal",
    # club-name components
    "real", "madrid", "inter", "milan", "barcelona", "atletico", "bayern",
    "munich", "manchester", "united", "city", "liverpool", "arsenal",
    "chelsea", "tottenham", "juventus", "napoli", "roma", "lazio", "porto",
    "benfica", "ajax", "dortmund", "leipzig", "sevilla", "valencia", "villa",
    "aston", "newcastle", "everton", "fulham", "brighton", "palace", "forest",
    "wolves", "fc", "afc", "cf", "sc", "ac", "club",
    # venues and geography
    "stadium", "arena", "stadio", "estadio", "park", "bernabeu", "siro",
    "anfield", "wembley", "camp", "nou", "allianz", "etihad", "emirates",
    "trafford", "bridge", "signal", "iduna", "santiago", "san", "old",
    # objects and abstractions
    "trophy", "ball", "pitch", "board", "tactics", "formation", "flag",
    "flags", "logo", "crest", "badge", "banner", "scarf", "kit", "shirt",
    "boots", "net", "goalpost", "scoreboard", "tunnel", "dressing", "room",
    "exterior", "interior", "aerial", "night", "photography", "photograph",
    "photo", "close", "up", "wide", "shot", "action", "match", "game",
    "season", "official", "football", "soccer", "sports", "highlights",
}

#: Words that say people are in the frame even when nobody is named.
_PEOPLE_WORDS = {
    "fans", "crowd", "supporters", "celebration", "celebrating", "cheering",
    "coach", "coaching", "manager", "sideline", "bench", "squad", "team",
    "players", "player", "goalkeeper", "keeper", "striker", "forward",
    "midfielder", "defender", "captain", "portrait", "face", "reaction",
    "interview", "press", "huddle", "lineup", "dribble", "save", "goal",
    "header", "tackle", "sprint", "duel", "curva", "ultras",
}

_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ']+")


def _provenance(workspace: Path) -> dict[str, dict]:
    """Per-file source record for this run, keyed by filename.

    Read from the workspace rather than from image_sourcer's module state, so
    this still works when the thumbnail stage runs on its own (`--stage
    thumbnail`) against an earlier run's workspace.
    """
    path = workspace / "asset_provenance.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"could not read asset provenance: {exc}")
        return {}
    out: dict[str, dict] = {}
    for record in data.get("assets") or []:
        name = str(record.get("file") or "")
        if name:
            out[name] = record
    return out


def named_people(text: str) -> list[str]:
    """People named in a beat's keywords.

    A person is two consecutive capitalised words, neither of which is a
    competition, a club, a venue or an object -- "Jude Bellingham" and "Carlo
    Ancelotti" qualify, "Champions League" and "Santiago Bernabeu" do not.

    Capitalisation is the signal, so this finds nobody in Arabic keywords.
    That is the honest outcome rather than a wrong one: an Arabic beat simply
    falls through to the subject-match tier below.
    """
    words = _WORD_RE.findall(str(text or ""))
    found: list[str] = []
    for first, second in zip(words, words[1:]):
        if not (first[:1].isupper() and second[:1].isupper()):
            continue
        if first.casefold() in _NOT_A_PERSON or second.casefold() in _NOT_A_PERSON:
            continue
        name = f"{first} {second}"
        if name not in found:
            found.append(name)
    return found


def shows_people(text: str) -> bool:
    """Whether the beat's keywords say people are in the frame at all."""
    words = {w.casefold() for w in _WORD_RE.findall(str(text or ""))}
    return bool(words & _PEOPLE_WORDS)


#: Letters that carry no information for matching a transliterated name.
_VOWELS = set("aeiouy")

#: Readings that differ only by which Latin letter an Arabic consonant was
#: spelled with. ج is written j or g, ق is q or k, and so on -- so
#: "Bellingham" and the Arabic reading "belinjham" are the same name.
_CONSONANT_CLASS = str.maketrans({"j": "g", "q": "k", "c": "k", "z": "s"})


def _skeleton(word: str) -> str:
    """A name reduced to what survives transliteration.

    Vowels are dropped (Arabic does not write short ones), doubled letters are
    collapsed ("Bellingham" -> "belingham"), and consonants that share an
    Arabic letter are merged. What is left is stable across spellings:

        Bellingham -> blngm      belinjham -> blngm
        Martinez   -> mrtns      martiniz  -> mrtns
    """
    cleaned = "".join(
        c for c in unicodedata.normalize("NFKD", str(word or "").casefold())
        if c.isalpha() and not unicodedata.combining(c)
    ).translate(_CONSONANT_CLASS)
    consonants = [c for c in cleaned if c not in _VOWELS]
    out: list[str] = []
    for letter in consonants:
        if not out or out[-1] != letter:
            out.append(letter)
    return "".join(out)


def brief_names(person: str, brief: str) -> bool:
    """Whether `brief` names `person`, in either script.

    The comparison runs the other way round from the obvious one. Extracting
    names from a descriptive brief is guesswork -- "صورة مقسومة" ("split
    image") parses as a plausible Arabic name. But the set of people who
    actually have a photograph is small and known, so each is asked whether the
    brief mentions them, which is a decidable question.

    Arabic briefs are matched by transliterating each Arabic word and comparing
    consonant skeletons, so «جود بيلينجهام» matches the English keyword "Jude
    Bellingham" that the photo was searched with.
    """
    surname = _skeleton(str(person or "").split()[-1] if person else "")
    if len(surname) < 3:
        return False

    text = str(brief or "")
    # Latin brief, or Latin words inside an Arabic one.
    for word in _WORD_RE.findall(text):
        if _skeleton(word) == surname:
            return True

    # Arabic brief: every reading of every Arabic word.
    for word in re.findall(r"[؀-ۿ]{3,}", text):
        try:
            readings = arabic_names.transliterate(word)
        except Exception:
            continue
        if any(_skeleton(r) == surname for r in readings):
            return True
    return False


def _person_subjects(subjects: list[str]) -> set[str]:
    """The story's subjects that could be a person rather than a club."""
    return {
        s.casefold()
        for s in subjects
        if s and s.casefold() not in _NOT_A_PERSON
    }


def _slot_keywords(script: Script) -> dict[str, str]:
    """Beat id -> the keywords its image was searched with."""
    out: dict[str, str] = {}
    for section in script.sections:
        for index, slot in enumerate(section.slots, start=1):
            out[f"section_{section.id:03d}_{index:02d}"] = (
                f"{slot.keywords or ''} {slot.prompt or ''}"
            )
    return out


def _pixels(path: Path) -> tuple[int, float]:
    """(pixel count, width/height) for a file, or (0, 0.0) if unreadable."""
    try:
        with Image.open(path) as img:
            if not img.height:
                return 0, 0.0
            return img.width * img.height, img.width / img.height
    except Exception:
        return 0, 0.0


def _content_key(path: Path) -> str:
    """A stable identity for a file, used only as the final tiebreaker.

    Deliberately the content and not the name: two runs that sourced the same
    photograph onto different beats must still choose the same one.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return path.name


def rank_source_images(
    workspace: Path,
    script: Script,
    subjects: list[str],
    *,
    target_aspect: float = 16 / 9,
    brief: str = "",
) -> list[tuple[Path, dict]]:
    """Every usable base image for a thumbnail, strongest first.

    Returns `(path, why)` pairs. `why` carries the reasoning onto the
    thumbnail's trace, and `depicts_person` / `people` are read by the caller
    to decide whether an identity-preservation instruction is even truthful.
    """
    ready = workspace / "images" / "ready"
    raw = workspace / "images" / "raw"
    # `ready` holds the processed PNGs the renderer uses; `raw` is what was
    # actually downloaded. Prefer raw: it has not been cropped to a beat's
    # frame yet, so it gives the edit the most picture to work with.
    directories = [d for d in (raw, ready) if d.is_dir()]
    if not directories:
        return []

    provenance = _provenance(workspace)
    keywords_by_beat = _slot_keywords(script)
    wanted_people = _person_subjects(subjects)

    seen: set[str] = set()
    scored: list[tuple[tuple, Path, dict]] = []
    for directory in directories:
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            if not _ASSET_RE.match(path.stem) or path.stem in seen:
                continue

            pixels, aspect = _pixels(path)
            if pixels < _MIN_PIXELS:
                continue
            seen.add(path.stem)

            record = (
                provenance.get(path.name)
                or provenance.get(f"{path.stem}.jpg")
                or provenance.get(f"{path.stem}.png")
                or {}
            )
            keywords = keywords_by_beat.get(path.stem, "")
            people = named_people(keywords)

            is_real = not bool(record.get("generated"))
            has_named = bool(people)
            # Who the *brief* asked for. This outranks everything below it:
            # the brief is the authoritative statement of who belongs on the
            # cover, and slot keywords are neither authoritative nor complete
            # (`_story_subjects` truncates at ten, which is how Bellingham and
            # Lautaro were ranked below the Inter coach).
            brief_people = [p for p in people if brief and brief_names(p, brief)]
            # A named person the *story* is about, not merely any two
            # capitalised words.
            on_subject = bool(
                wanted_people
                and any(
                    w in keywords.casefold()
                    for w in wanted_people
                )
            )
            has_people = has_named or shows_people(keywords)

            # Meaningful tiebreakers, in this order: a frame already near the
            # thumbnail's shape survives the cover-crop with the subject
            # intact, and at equal resolution a larger file carries more
            # detail. Only after both of those does the content hash decide,
            # so nothing is ever chosen by directory order.
            aspect_fit = -abs(aspect - target_aspect) if aspect else -99.0
            try:
                detail = path.stat().st_size
            except OSError:
                detail = 0

            why = {
                "file": path.name,
                "real_photograph": is_real,
                "depicts_person": has_people,
                "people": people,
                "brief_people": brief_people,
                "named_in_brief": bool(brief_people),
                "names_a_story_subject": on_subject,
                "pixels": pixels,
                "aspect": round(aspect, 3) if aspect else 0,
                "bytes": detail,
                "platform": str(record.get("platform") or "unknown"),
                "keywords": " ".join(keywords.split())[:120],
            }
            scored.append((
                (
                    is_real,                # real photography, always first
                    bool(brief_people),     # the brief asked for this person
                    has_named,              # any named person
                    on_subject,             # a story subject at all
                    has_people,             # anyone at all, over an object
                    aspect_fit,             # crops to shape, subject intact
                    detail,                 # more detail at equal resolution
                    pixels,
                    _content_key(path),     # determinism, never a filename
                ),
                path,
                why,
            ))

    scored.sort(key=lambda row: row[0], reverse=True)
    return [(path, why) for _, path, why in scored]


#: A thumbnail is a composition, not a contact sheet. Beyond this many faces
#: nobody is recognisable at 320px wide, which is the size it is judged at.
_MAX_BASES = 3


def pick_source_images(
    workspace: Path,
    script: Script,
    subjects: list[str],
    *,
    limit: int = 1,
    target_aspect: float = 16 / 9,
    brief: str = "",
) -> list[tuple[Path, dict]]:
    """The bases for this thumbnail, strongest first, or an empty list.

    When the brief names people, this returns **one photograph per named
    person** rather than one photograph overall. A split "A versus B"
    thumbnail built from a single face is exactly the case where the model
    fills the empty half by inventing someone: given one real photo of the
    Inter coach it drew a Real Madrid player who does not exist. Supplying
    both faces removes the gap it was filling.

    `limit` is the floor, not the ceiling: it is raised to cover everyone the
    brief names, up to `_MAX_BASES`.
    """
    ranked = rank_source_images(
        workspace, script, subjects, target_aspect=target_aspect, brief=brief
    )
    if not ranked:
        logger.info("No usable source image for the thumbnail; will need a base")
        return []

    chosen: list[tuple[Path, dict]] = []
    covered: set[str] = set()

    # One photograph per person the brief asked for, best first, and never the
    # same person twice -- two shots of Bellingham are one subject, not two.
    for path, why in ranked:
        if len(chosen) >= _MAX_BASES:
            break
        new_people = {
            _skeleton(p.split()[-1]) for p in why.get("brief_people") or []
        } - covered
        if new_people:
            chosen.append((path, why))
            covered |= new_people

    if chosen:
        logger.info(
            f"Thumbnail bases ({len(chosen)}, one per person named in the "
            f"brief): "
            + "; ".join(
                f"{w['file']} = {', '.join(w['brief_people'])}" for _, w in chosen
            )
        )
        return chosen

    # The brief named nobody this run has a photograph of. Fall back to the
    # single strongest base, as before.
    chosen = ranked[: max(1, limit)]
    best = chosen[0][1]
    logger.info(
        f"Thumbnail base: {best['file']} — "
        f"{'real photograph' if best['real_photograph'] else 'generated image'}, "
        + (
            f"shows {', '.join(best['people'])}"
            if best["people"]
            else ("shows people" if best["depicts_person"] else "no person in frame")
        )
        + f", {best['pixels']:,}px, from {best['platform']}"
    )
    return chosen
