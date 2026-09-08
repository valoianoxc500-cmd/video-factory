"""Reading player and club names out of Arabic football copy.

The Football channel writes in Arabic, and the squad-record gate could not see
it. Player names were found by looking for capitalised runs -- a Latin-script
idea -- so an Arabic topic yielded no names, no squad record was fetched, and
a planner-invented transfer went unchecked all the way to sourcing:

    "آخر أخبار إيميليانو مارتينيز بعد انتقاله من أستون فيلا إلى تشيلسي"
    ("Latest on Emiliano Martínez after his transfer from Aston Villa to
      Chelsea")

Martínez has never played for Chelsea. Nothing caught it until the image
review gate said so, by which point the script was written and the run was
dead.

Two different problems, solved differently:

  * Clubs are a closed set, so they get a lexicon. It is exact and needs no
    guessing.
  * Players are an open set, so the name is transliterated and looked up.
    API-Football matches on prefixes, which is what makes this work at all --
    a naive transliteration of مارتينيز is "martynyz", which finds nothing,
    while "martin" finds Martínez. Arabic does not write short vowels, so
    each ambiguous letter is expanded into its plausible Latin readings and
    the variants are tried in turn.
"""

from __future__ import annotations

import re
import unicodedata

# Arabic letters that map to exactly one Latin form.
_FIXED: dict[str, str] = {
    "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh",
    "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "س": "s", "ش": "sh",
    "ص": "s", "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh",
    "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m", "ن": "n",
    "ه": "h", "ة": "a", "ى": "a", "آ": "a", "ا": "a", "أ": "a",
    "ٱ": "a", "ء": "", "ئ": "e", "ؤ": "o", "پ": "p", "چ": "ch",
    "ڤ": "v", "ﭬ": "v", "ژ": "zh", "گ": "g",
}

# Letters Arabic uses for more than one vowel. Arabic omits short vowels, so
# "مارتينيز" is legitimately martinez, martiniz or martynyz -- only the first
# is the player. Each reading is tried.
_AMBIGUOUS: dict[str, tuple[str, ...]] = {
    "ي": ("i", "e", "y"),
    "و": ("o", "u", "w"),
    "إ": ("e", "i"),
}

_DIACRITICS = "".join(chr(c) for c in range(0x064B, 0x0653)) + "ـ"
_ARABIC_RE = re.compile(r"[؀-ۿ]")

# Words that are grammar, not names. Arabic prefixes them to nouns, so they
# turn up glued to the front of a club or a person.
_STOPWORDS = frozenset("""
آخر أخبار خبر بعد قبل من إلى عن على في مع هل هذا هذه ذلك التي الذي
انتقاله انتقال صفقة صفقات نادي ناديه فريق فريقه لاعب اللاعب مدرب المدرب
الدوري كأس مباراة مباريات موسم الموسم اليوم الآن سوق الانتقالات
رسميا رسمياً تقارير تقرير مصادر مصدر يريد يرغب يسعى ينتقل رحيل
""".split())

# Clubs are finite, so they are named rather than guessed. Keys are matched as
# substrings after normalisation, so the definite article does not matter.
_CLUB_LEXICON: tuple[tuple[str, str], ...] = (
    ("تشيلسي", "Chelsea"),
    ("استون فيلا", "Aston Villa"),
    ("أستون فيلا", "Aston Villa"),
    ("برشلونة", "Barcelona"),
    ("ريال مدريد", "Real Madrid"),
    ("اتلتيكو مدريد", "Atletico Madrid"),
    ("أتلتيكو مدريد", "Atletico Madrid"),
    ("مانشستر سيتي", "Manchester City"),
    ("مانشستر يونايتد", "Manchester United"),
    ("ليفربول", "Liverpool"),
    ("ارسنال", "Arsenal"),
    ("أرسنال", "Arsenal"),
    ("توتنهام", "Tottenham"),
    ("نيوكاسل", "Newcastle"),
    ("وست هام", "West Ham"),
    ("بايرن ميونخ", "Bayern Munich"),
    ("بوروسيا دورتموند", "Borussia Dortmund"),
    ("يوفنتوس", "Juventus"),
    ("انتر ميلان", "Inter Milan"),
    ("ميلان", "AC Milan"),
    ("نابولي", "Napoli"),
    ("روما", "Roma"),
    ("باريس سان جيرمان", "Paris Saint-Germain"),
    ("الهلال", "Al Hilal"),
    ("النصر", "Al Nassr"),
    ("ريفر بليت", "River Plate"),
    ("بوكا جونيورز", "Boca Juniors"),
)

# "his transfer ... to X", "moves to X", "signs for X" -- the wording that
# makes a topic assert where a player now is.
_DESTINATION_MARKERS = ("إلى", "الى", "لصفوف", "ينضم", "انضم")


def contains_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(str(text or "")))


def _strip_diacritics(text: str) -> str:
    cleaned = "".join(c for c in str(text or "") if c not in _DIACRITICS)
    return unicodedata.normalize("NFKC", cleaned)


def normalise_arabic(text: str) -> str:
    """Fold the spellings Arabic treats as interchangeable."""
    text = _strip_diacritics(text)
    for variants, canonical in (("أإآٱ", "ا"), ("ى", "ا"), ("ة", "ه"), ("ؤ", "و")):
        for ch in variants:
            text = text.replace(ch, canonical)
    return text


def transliterate(word: str, *, limit: int = 12) -> list[str]:
    """Latin readings of an Arabic word, most likely first.

    Returns several because Arabic leaves short vowels unwritten: مارتينيز is
    martinez, martiniz and martynyz on the page, and only the first is a
    footballer. Capped so a long word cannot explode combinatorially.
    """
    source = _strip_diacritics(word)
    if not source:
        return []

    readings: list[str] = [""]
    for char in source:
        if char in _AMBIGUOUS:
            options = _AMBIGUOUS[char]
        elif char in _FIXED:
            options = (_FIXED[char],)
        elif char.isalnum():
            options = (char.lower(),)
        else:
            continue
        grown: list[str] = []
        for prefix in readings:
            for option in options:
                grown.append(prefix + option)
            if len(grown) >= limit * 4:
                break
        readings = grown[: limit * 4]

    out: list[str] = []
    for reading in readings:
        forms = [reading]
        # A leading "al" is the definite article, not part of the name.
        if reading.startswith("al"):
            forms.append(reading[2:])
        # Arabic writes a long vowel where Latin writes one letter: إيميليانو
        # transliterates to "eimiliano", and the player is "Emiliano". Collapse
        # an opening vowel pair so the real spelling is among the candidates.
        if len(reading) > 3 and reading[0] in "aeiou" and reading[1] in "aeiou":
            forms.append(reading[0] + reading[2:])
        for candidate in forms:
            if len(candidate) >= 3 and candidate not in out:
                out.append(candidate)
    return out[:limit]


def clubs_in(text: str) -> list[str]:
    """Clubs named in Arabic text, in their English form."""
    haystack = normalise_arabic(text)
    found: list[str] = []
    for arabic, english in _CLUB_LEXICON:
        if normalise_arabic(arabic) in haystack and english not in found:
            found.append(english)
    return found


def destination_clubs(text: str) -> list[str]:
    """Clubs the text presents as where the player is going or has gone.

    "انتقاله من أستون فيلا إلى تشيلسي" -- from Aston Villa TO Chelsea --
    asserts Chelsea. That assertion is what has to be checked against the
    squad record before anything is written from it.
    """
    haystack = normalise_arabic(text)
    found: list[str] = []
    for arabic, english in _CLUB_LEXICON:
        club = normalise_arabic(arabic)
        position = haystack.find(club)
        if position < 0:
            continue
        preceding = haystack[max(0, position - 40) : position]
        if any(normalise_arabic(m) in preceding for m in _DESTINATION_MARKERS):
            if english not in found:
                found.append(english)
    return found


def candidate_person_words(text: str) -> list[str]:
    """Arabic words that could be part of a person's name.

    Grammar words and club names are removed; what is left is mostly names.
    Deliberately generous -- a wrong candidate costs one lookup that finds
    nothing, while a missed one costs the check entirely.
    """
    # Diacritics go, but alef forms are kept: normalising إ to ا before
    # transliteration turned "إيميليانو" into "aimiliano" instead of
    # "emiliano", and the provider recognised neither. Folding is used only
    # for comparison.
    source = _strip_diacritics(text)
    club_words: set[str] = set()
    for arabic, _ in _CLUB_LEXICON:
        club_words.update(normalise_arabic(arabic).split())
    stopwords = {normalise_arabic(s) for s in _STOPWORDS}

    words: list[str] = []
    for raw in re.findall(r"[؀-ۿ]+", source):
        folded = normalise_arabic(raw)
        if folded in club_words or folded in stopwords or len(raw) < 4:
            continue
        words.append(raw)
    return words


def person_name_candidates(text: str, *, limit: int = 4) -> list[dict]:
    """People possibly named in *text*, with every reading worth looking up.

    All readings are carried, not just the likeliest: the correct one for
    مارتينيز is "martinez", which is the *second* variant -- handing the
    lookup only the first would find nothing and the check would silently
    pass, which is the failure this module exists to close.

    Each entry also carries prefixes, because API-Football matches on those:
    "martin" finds Martínez even when no full transliteration does.
    """
    words = candidate_person_words(text)
    out: list[dict] = []
    index = 0

    while index < len(words) and len(out) < limit:
        given_word = words[index]
        family_word = words[index + 1] if index + 1 < len(words) else ""

        # A single word on its own is too weak to look up: "مارتينيز" alone
        # matched Lautaro Martínez, and "إيميليانو" alone matched an Albanian
        # called Emiliano Çela. Both parts of the name have to agree.
        if not family_word:
            break

        family = transliterate(family_word)
        given = transliterate(given_word)
        if not family or not given:
            index += 1
            continue

        search_terms = list(family)
        # Prefixes last: they match broadly, and are only usable because the
        # forename has to confirm the result anyway.
        for size in (6, 5):
            prefix = family[0][:size]
            if len(prefix) == size and prefix not in search_terms:
                search_terms.append(prefix)

        out.append(
            {
                "arabic": f"{given_word} {family_word}",
                "given": given,
                "family": family,
                "search_terms": search_terms,
                "confirm_terms": given,
            }
        )
        index += 2
    return out
