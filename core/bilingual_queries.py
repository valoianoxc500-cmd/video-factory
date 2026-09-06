"""Build image searches in both scripts, so the language of the narration
never limits which real photographs can be found.

The problem this solves
-----------------------
On the Flight 19 run the rescue pass searched with the Arabic video title:

    'الرحلة 19 archival photograph'

Flight 19's entire photographic record is catalogued in English -- "Flight 19",
"TBM Avenger", "NAS Fort Lauderdale", "Martin PBM Mariner". The Arabic title is
the right label for the *video* and the wrong key for the *archive*, and mixing
the two scripts in one query is worse than either alone: search engines tokenise
them separately and the Arabic half matches nothing in an English photo caption.

So queries are built per-script and never mixed. English proper nouns lead,
because that is how historical material is indexed; Arabic queries run too,
because Arabic-language sources hold material English ones do not -- regional
press, local place names, and cases that were never written up in English.

This is orthogonal to the script language. An Arabic video still searches in
English, and an English video still searches in Arabic.
"""

from __future__ import annotations

import re

# Script detection. A "Latin" token carries the English-language name of a
# person, unit, aircraft or place; an "Arabic" token carries its local name.
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")

# Words that are never the subject of an archive search and only dilute it.
_STOPWORDS = frozenset("""
a an the of in on at to for and or with from by is are was were
photo photograph photographs image images picture pictures archival archive
historical vintage old real actual footage shot scene view
""".split())

MAX_QUERY_CHARS = 120


def script_of(text: str) -> str:
    """'arabic', 'latin', 'mixed' or 'none' for a piece of text."""
    has_ar = bool(_ARABIC.search(text or ""))
    has_la = bool(_LATIN.search(text or ""))
    if has_ar and has_la:
        return "mixed"
    if has_ar:
        return "arabic"
    if has_la:
        return "latin"
    return "none"


def is_single_script(text: str) -> bool:
    """Whether a query stays in one script, which is what search engines want."""
    return script_of(text) in ("arabic", "latin")


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,،;:/|]+", str(text or "")) if t]


def latin_terms(*sources: str) -> list[str]:
    """Latin-script terms from the given text, order preserved, deduped.

    Proper nouns and model designations -- "Flight 19", "TBM Avenger",
    "Fort Lauderdale" -- survive; connective words and the words every archive
    query already implies are dropped.
    """
    out: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for token in _tokens(source):
            cleaned = token.strip("\"'()[].,")
            if not cleaned or not _LATIN.search(cleaned):
                continue
            if _ARABIC.search(cleaned):
                continue
            if cleaned.lower() in _STOPWORDS:
                continue
            if cleaned.lower() in seen:
                continue
            seen.add(cleaned.lower())
            out.append(cleaned)
    return out


def arabic_terms(*sources: str) -> list[str]:
    """Arabic-script terms from the given text, order preserved, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for token in _tokens(source):
            cleaned = token.strip("\"'()[].,،؟!")
            if not cleaned or not _ARABIC.search(cleaned):
                continue
            if _LATIN.search(cleaned):
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _trim(terms: list[str], suffix: str = "") -> str:
    query = " ".join(terms)
    if suffix:
        query = f"{query} {suffix}".strip()
    return " ".join(query.split())[:MAX_QUERY_CHARS].strip()


def build_bilingual_queries(
    *,
    keywords: str = "",
    title: str = "",
    narration: str = "",
    entities: list[str] | None = None,
    suffixes: tuple[str, ...] = ("archival photograph", "historical photograph"),
) -> list[str]:
    """Single-script queries for one beat, English first.

    English leads because historical material is indexed under its original
    names. Arabic follows rather than being dropped: regional sources hold
    photographs that English-language archives never catalogued.
    """
    entities = [e for e in (entities or []) if str(e).strip()]

    en_core = latin_terms(keywords, *entities, title, narration)
    ar_core = arabic_terms(keywords, *entities, title)

    queries: list[str] = []

    # The slot's own English terms are the most specific thing available.
    if en_core:
        queries.append(_trim(en_core[:8]))
        for suffix in suffixes:
            queries.append(_trim(en_core[:5], suffix))

    # Latin-script entities on their own, for when the slot's extra words are
    # what is failing to match.
    en_entities = latin_terms(*entities)
    if en_entities:
        queries.append(_trim(en_entities[:5]))
        queries.append(_trim(en_entities[:4], suffixes[0] if suffixes else ""))

    # Arabic, never mixed with the above.
    if ar_core:
        queries.append(_trim(ar_core[:8]))
        queries.append(_trim(ar_core[:5], "صورة أرشيفية"))

    ordered: list[str] = []
    seen: set[str] = set()
    for q in queries:
        if not q or len(q) < 3:
            continue
        if not is_single_script(q):
            continue          # never ship a mixed-script query
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(q)
    return ordered
