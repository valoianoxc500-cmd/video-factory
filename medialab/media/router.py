"""Which sources to ask, for what kind of subject.

Asking all twelve providers for every beat would be slower, ruder to the free
archives, and worse: a generic lifestyle beat searched against the Met returns
oil paintings, and a named footballer searched against Pexels returns a model
in a generic kit. The right source depends on the subject, so the subject is
classified first.

Classification is keyword-based and deliberately so. It runs per beat, has to
be instant and free, and the cost of being wrong is small -- the stock lane is
appended to almost every route, so a misrouted beat still reaches the general
libraries. A model call here would add latency and spend to a decision that
keywords get right nearly all of the time.
"""

from __future__ import annotations

import re

__all__ = ["classify", "route_for", "core_query", "ROUTES", "SUBJECTS"]

#: Words that describe how a picture was taken rather than what is in it.
#:
#: Stock libraries match loosely and cope with these. Archives do not: NASA's
#: index AND-matches every term, so "Great Barrier Reef satellite view"
#: returns nothing at all while "Great Barrier Reef" returns twelve satellite
#: images -- because everything in that collection is a satellite view and
#: nobody writes it in the title.
_VIEWPOINT = frozenset({
    "view", "views", "shot", "shots", "photo", "photos", "photograph",
    "photographs", "image", "images", "picture", "pictures", "footage",
    "closeup", "close-up", "close", "up", "macro", "wide", "angle", "scene",
    "background", "drone", "aerial", "satellite", "overhead", "looking",
    "showing", "of", "the", "a", "an", "from", "at", "in", "on",
})

#: Subject -> the ordered provider lanes for it. Order matters: the
#: aggregator searches them concurrently but ranks earlier lanes higher on a
#: tie, so the specialist source wins when both have something usable.
ROUTES: dict[str, tuple[str, ...]] = {
    "generic": ("pexels", "pixabay", "unsplash", "openverse"),
    "sports": ("commons", "openverse", "archive", "pexels", "pixabay"),
    "space": ("nasa", "commons", "openverse", "pexels", "pixabay"),
    "ocean": ("noaa", "commons", "openverse", "pexels", "pixabay"),
    "weather": ("noaa", "commons", "openverse", "pexels", "pixabay"),
    "history": ("loc", "archive", "smithsonian", "commons", "europeana"),
    "art": ("met", "smithsonian", "europeana", "commons", "openverse"),
    "named": ("commons", "openverse", "archive", "loc", "pexels"),
}

SUBJECTS = tuple(ROUTES)

#: Checked in this order, so the more specific subject wins. "Real Madrid
#: stadium" is sports, not architecture; "Apollo 11 launch" is space, not
#: history, because NASA has the actual footage.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("space", re.compile(
        r"\b(nasa|satellite|orbit(al|ing)?|spacecraft|astronaut|rocket|"
        r"space ?station|iss|mars|lunar|moon landing|galaxy|nebula|telescope|"
        r"hubble|jwst|solar system|planet|asteroid|comet|earth from space)\b",
        re.I)),
    ("sports", re.compile(
        r"\b(football|soccer|stadium|fifa|uefa|premier league|la ?liga|"
        r"serie a|bundesliga|champions league|world cup|match|kick ?off|"
        r"goalkeeper|striker|midfielder|penalty|pitch side|basketball|nba|"
        r"tennis|olympic|athlete|cricket|rugby|formula 1|f1|grand prix)\b",
        re.I)),
    ("ocean", re.compile(
        r"\b(ocean|reef|coral|marine|sea ?floor|underwater|whale|shark|"
        r"plankton|tide|coastal|estuary|fishery|noaa)\b", re.I)),
    ("weather", re.compile(
        r"\b(hurricane|typhoon|cyclone|tornado|storm|blizzard|drought|"
        r"flood|wildfire|climate|weather|rainfall|temperature record)\b",
        re.I)),
    # "portrait of" is deliberately absent: a portrait of a named person is
    # a person, and belongs in the archives lane, not the museum one. Only
    # words that mean an artwork qualify here.
    ("art", re.compile(
        r"\b(painting|painted|sculpture|museum|gallery|artefact|artifact|"
        r"renaissance|baroque|impressionis|still life|ceramic|tapestry|"
        r"manuscript|fresco|exhibit|oil on canvas)\b", re.I)),
    ("history", re.compile(
        r"\b(histor(y|ical)|archive|archival|century|ancient|medieval|"
        r"world war|civil war|revolution|great depression|depression era|"
        r"vintage|1[0-9]{3}s?|20[0-2][0-9]s|dynasty|empire|colonial)\b",
        re.I)),
)

#: A capitalised multi-word name that is not a sentence opener. Weak on its
#: own, which is why it only applies when nothing more specific matched.
_NAMED = re.compile(r"(?<!^)\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+)")


def classify(intent: str, *, says: str = "") -> str:
    """The subject lane for one beat's visual intent."""
    text = f"{intent} {says}".strip()
    if not text:
        return "generic"
    for subject, pattern in _PATTERNS:
        if pattern.search(text):
            return subject
    if _NAMED.search(intent):
        return "named"
    return "generic"


def core_query(query: str) -> str:
    """The subject of a query, with the photographic modifiers taken out.

    Used only as a second attempt for a source that returned nothing on the
    full query. Intent is not lost by this: the vision ranker downstream is
    what decides whether a candidate actually shows the beat, and it is
    stricter than any search string.

    Returns "" when nothing meaningful survives, which means "do not retry".
    """
    kept = [w for w in str(query or "").split() if w.lower().strip(",.") not in _VIEWPOINT]
    trimmed = " ".join(kept)
    return trimmed if trimmed and trimmed.lower() != query.strip().lower() else ""


def route_for(intent: str, *, says: str = "", available: set[str] | None = None) -> list[str]:
    """Provider names to search for this beat, best lane first.

    Unavailable providers are dropped rather than skipped later, so the
    concurrency budget is spent on sources that can actually answer. The
    general stock libraries are appended to every route as a floor: a
    specialist lane that finds nothing must not leave the beat with nothing.
    """
    subject = classify(intent, says=says)
    order = list(ROUTES.get(subject, ROUTES["generic"]))
    for fallback in ROUTES["generic"]:
        if fallback not in order:
            order.append(fallback)
    if available is None:
        return order
    return [name for name in order if name in available]
