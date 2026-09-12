"""The voice catalogue, discovered from the provider rather than hardcoded.

Eleven hand-written entries was the whole English and Arabic offering, and it
went stale the moment Microsoft added a voice. Edge publishes its full list,
so the catalogue is built from that and cached to disk -- one network call the
first time, none afterwards.

Two things the raw list is not: customer-facing, and safe to show whole. It
carries ~500 voices across every locale, and its names are identifiers like
`ar-EG-SalmaNeural`. So this module filters to the locales the product
supports, sorts them, and presents a person's name with an accent and a short
style note. The identifier stays internal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("aivideo")

__all__ = ["Voice", "catalogue", "voices_for", "default_voice", "refresh_catalogue"]

CACHE = Path(__file__).resolve().parent / "voices.json"

#: Locales the product offers, and what to call them. Arabic first because it
#: is the half that was thinnest -- five voices for the entire Arab world.
LOCALES: dict[str, tuple[str, str]] = {
    # locale: (language, customer-facing accent)
    "ar-EG": ("ar", "Egyptian"),
    "ar-SA": ("ar", "Gulf / Modern Standard"),
    "ar-AE": ("ar", "Emirati"),
    "ar-MA": ("ar", "Moroccan"),
    "ar-DZ": ("ar", "Algerian"),
    "ar-TN": ("ar", "Tunisian"),
    "ar-JO": ("ar", "Jordanian"),
    "ar-LB": ("ar", "Lebanese"),
    "ar-IQ": ("ar", "Iraqi"),
    "ar-KW": ("ar", "Kuwaiti"),
    "ar-QA": ("ar", "Qatari"),
    "ar-SY": ("ar", "Syrian"),
    "ar-BH": ("ar", "Bahraini"),
    "ar-LY": ("ar", "Libyan"),
    "ar-OM": ("ar", "Omani"),
    "ar-YE": ("ar", "Yemeni"),
    "en-US": ("en", "American"),
    "en-GB": ("en", "British"),
    "en-AU": ("en", "Australian"),
    "en-CA": ("en", "Canadian"),
    "en-IE": ("en", "Irish"),
}

#: Voices worth leading with, in order. Everything else follows alphabetically.
#: Chosen by listening: these are the ones that do not sound synthetic on a
#: 30-second read.
PREFERRED = (
    "ar-EG-SalmaNeural", "ar-EG-ShakirNeural",
    "ar-SA-ZariyahNeural", "ar-SA-HamedNeural",
    "ar-AE-FatimaNeural", "ar-AE-HamdanNeural",
    "en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural",
    "en-US-ChristopherNeural", "en-GB-SoniaNeural", "en-GB-RyanNeural",
)

#: A short human note per voice where we have an opinion. Absent entries fall
#: back to the accent, which is still more useful than an identifier.
NOTES: dict[str, str] = {
    "ar-EG-SalmaNeural": "Warm, clear",
    "ar-EG-ShakirNeural": "News delivery",
    "ar-SA-ZariyahNeural": "Formal, precise",
    "ar-SA-HamedNeural": "Deep, authoritative",
    "ar-AE-FatimaNeural": "Bright, friendly",
    "ar-AE-HamdanNeural": "Calm narrator",
    "en-US-AriaNeural": "Warm, natural",
    "en-US-GuyNeural": "Confident narrator",
    "en-US-JennyNeural": "Friendly, upbeat",
    "en-US-ChristopherNeural": "Deep documentary",
    "en-GB-SoniaNeural": "Crisp, composed",
    "en-GB-RyanNeural": "Measured, warm",
}


@dataclass(frozen=True)
class Voice:
    id: str
    label: str
    language: str
    locale: str
    accent: str
    gender: str
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _display_name(voice_id: str) -> str:
    """`ar-EG-SalmaNeural` -> `Salma`. Never show the identifier.

    The `Multilingual` and `Neural` suffixes are Microsoft's model naming, not
    part of anyone's name -- "WilliamMultilingual" in a voice picker reads as
    a bug.
    """
    tail = voice_id.rsplit("-", 1)[-1]
    # Repeatedly, and outermost first: the id is "...MultilingualNeural", so
    # stripping "Multilingual" before "Neural" matches nothing and leaves the
    # whole suffix in the name.
    changed = True
    while changed:
        changed = False
        for suffix in ("Neural", "Multilingual"):
            if tail.endswith(suffix) and len(tail) > len(suffix):
                tail = tail[: -len(suffix)]
                changed = True
    return tail or voice_id


def _build(raw: list[dict]) -> list[Voice]:
    out: list[Voice] = []
    for entry in raw:
        voice_id = str(entry.get("ShortName") or entry.get("Name") or "")
        locale = str(entry.get("Locale") or "")
        if locale not in LOCALES or not voice_id:
            continue
        language, accent = LOCALES[locale]
        out.append(Voice(
            id=voice_id,
            label=_display_name(voice_id),
            language=language,
            locale=locale,
            accent=accent,
            gender=str(entry.get("Gender") or "").lower() or "female",
            note=NOTES.get(voice_id, accent),
        ))

    rank = {vid: i for i, vid in enumerate(PREFERRED)}
    out.sort(key=lambda v: (rank.get(v.id, 999), v.locale, v.label))
    return out


async def refresh_catalogue() -> list[Voice]:
    """Ask Edge for its voice list and cache it. Network, called rarely."""
    import edge_tts

    raw = await edge_tts.list_voices()
    voices = _build(list(raw))
    if not voices:
        raise RuntimeError("voice discovery returned nothing usable")
    CACHE.write_text(
        json.dumps([v.to_dict() for v in voices], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    logger.info(f"[aivideo] voice catalogue refreshed: {len(voices)} voices")
    return voices


def catalogue() -> list[Voice]:
    """The cached catalogue.

    Reads the file written by `refresh_catalogue`. Returns the hand-written
    core set if the cache is missing, so the product still offers voices on a
    fresh checkout that has never run discovery.
    """
    try:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
        voices = [Voice(**row) for row in raw]
        if voices:
            return voices
    except Exception:
        pass

    return [
        Voice(
            id=vid, label=_display_name(vid),
            language=LOCALES[vid[:5]][0], locale=vid[:5],
            accent=LOCALES[vid[:5]][1], gender="female",
            note=NOTES.get(vid, ""),
        )
        for vid in PREFERRED
    ]


def voices_for(language: str) -> list[Voice]:
    return [v for v in catalogue() if v.language == language]


def default_voice(language: str) -> str:
    options = voices_for(language)
    return options[0].id if options else PREFERRED[0]
