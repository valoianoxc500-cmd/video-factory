"""What the customer chose, and the vocabulary the engine speaks.

AI Video Maker is deliberately a separate package from `core/`. The channel
pipeline in `core/` is built around one hard promise -- that a documentary
visual is evidence of the named event it sits under -- and everything in it,
from the relevance gate to the verified cards, exists to keep that promise.
This product makes a different and much simpler one: relevant, good-looking
footage under a coherent script. Mixing the two would drag the Football
machinery into a general faceless-video maker that does not need it, and would
couple two products that must be able to fail independently.

Shapes and defaults live here so the web form, the worker and the renderer all
agree on one vocabulary, and so a preference row read back from Supabase months
later still validates.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

__all__ = [
    "Language", "AspectRatio", "CaptionStyle", "VideoSpec",
    "CAPTION_PRESETS", "VOICES", "DURATIONS", "ASPECT_SIZES",
    "voices_for", "preset", "normalise_spec",
]

Language = Literal["en", "ar"]
AspectRatio = Literal["9:16", "16:9", "1:1"]

#: Durations the customer may pick, in seconds.
DURATIONS = (30, 45, 60, 90)

#: Output pixel size per aspect ratio. 1080-wide vertical is the short-form
#: standard; the others match it on the long edge so one render profile covers
#: all three.
ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}

# ── voices ───────────────────────────────────────────────────────────
#
# Edge TTS names. Free, keyless, and good enough that paying for narration is
# not the default. `VOICES` is the catalogue the UI renders and the engine
# validates against; adding ElevenLabs later means adding entries with a
# different `provider`, not changing anything that reads this.

VOICES: tuple[dict, ...] = (
    # English
    {"id": "en-US-AriaNeural", "label": "Aria", "language": "en",
     "gender": "female", "provider": "edge", "note": "Warm, natural"},
    {"id": "en-US-GuyNeural", "label": "Guy", "language": "en",
     "gender": "male", "provider": "edge", "note": "Confident narrator"},
    {"id": "en-US-JennyNeural", "label": "Jenny", "language": "en",
     "gender": "female", "provider": "edge", "note": "Friendly, upbeat"},
    {"id": "en-US-ChristopherNeural", "label": "Christopher", "language": "en",
     "gender": "male", "provider": "edge", "note": "Deep documentary"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia", "language": "en",
     "gender": "female", "provider": "edge", "note": "British"},
    {"id": "en-GB-RyanNeural", "label": "Ryan", "language": "en",
     "gender": "male", "provider": "edge", "note": "British"},
    # Arabic
    {"id": "ar-EG-SalmaNeural", "label": "سلمى", "language": "ar",
     "gender": "female", "provider": "edge", "note": "مصري · واضح"},
    {"id": "ar-EG-ShakirNeural", "label": "شاكر", "language": "ar",
     "gender": "male", "provider": "edge", "note": "مصري · إخباري"},
    {"id": "ar-SA-ZariyahNeural", "label": "زارية", "language": "ar",
     "gender": "female", "provider": "edge", "note": "فصحى"},
    {"id": "ar-SA-HamedNeural", "label": "حامد", "language": "ar",
     "gender": "male", "provider": "edge", "note": "فصحى · عميق"},
    {"id": "ar-AE-FatimaNeural", "label": "فاطمة", "language": "ar",
     "gender": "female", "provider": "edge", "note": "خليجي"},
)


def voices_for(language: str) -> list[dict]:
    return [v for v in VOICES if v["language"] == language]


def default_voice(language: str) -> str:
    options = voices_for(language)
    return options[0]["id"] if options else VOICES[0]["id"]


# ── caption presets ──────────────────────────────────────────────────
#
# Each preset is a complete CaptionStyle, so the UI can render an honest
# preview from the same numbers the renderer will use. `font` names a family
# the font resolver knows how to find on this machine (see subtitles.py); the
# Arabic-capable families are marked so an Arabic run never silently falls back
# to something that cannot shape Arabic.

CAPTION_PRESETS: dict[str, dict] = {
    "clean": {
        "label": "Clean", "font": "inter", "size": 64, "weight": "bold",
        "text_color": "#FFFFFF", "highlight_color": "#FFFFFF",
        "outline_color": "#000000", "outline_width": 4,
        "background": "none", "position": "bottom", "words_per_line": 4,
        "uppercase": False,
    },
    "bold": {
        "label": "Bold", "font": "montserrat", "size": 76, "weight": "black",
        "text_color": "#FFFFFF", "highlight_color": "#FFE500",
        "outline_color": "#000000", "outline_width": 7,
        "background": "none", "position": "center", "words_per_line": 3,
        "uppercase": True,
    },
    "viral": {
        "label": "Viral", "font": "montserrat", "size": 82, "weight": "black",
        "text_color": "#FFFFFF", "highlight_color": "#00E676",
        "outline_color": "#000000", "outline_width": 8,
        "background": "none", "position": "center", "words_per_line": 3,
        "uppercase": True,
    },
    "minimal": {
        "label": "Minimal", "font": "inter", "size": 52, "weight": "medium",
        "text_color": "#FFFFFF", "highlight_color": "#FFFFFF",
        "outline_color": "#000000", "outline_width": 2,
        "background": "none", "position": "bottom", "words_per_line": 6,
        "uppercase": False,
    },
    "boxed": {
        "label": "Boxed", "font": "inter", "size": 60, "weight": "bold",
        "text_color": "#FFFFFF", "highlight_color": "#FFD600",
        "outline_color": "#000000", "outline_width": 0,
        "background": "#000000CC", "position": "bottom", "words_per_line": 5,
        "uppercase": False,
    },
    "karaoke": {
        "label": "Karaoke", "font": "montserrat", "size": 70, "weight": "bold",
        "text_color": "#FFFFFF", "highlight_color": "#FF3D71",
        "outline_color": "#000000", "outline_width": 6,
        "background": "none", "position": "center", "words_per_line": 4,
        "uppercase": False,
    },
    "cinematic": {
        "label": "Cinematic", "font": "playfair", "size": 56, "weight": "medium",
        "text_color": "#F5F0E6", "highlight_color": "#E0C070",
        "outline_color": "#000000", "outline_width": 3,
        "background": "none", "position": "bottom", "words_per_line": 5,
        "uppercase": False,
    },
}

#: Families that can shape Arabic. An Arabic run is moved onto one of these
#: whatever preset was chosen, because a preset that cannot draw joined Arabic
#: produces disconnected letters rather than an error -- silently, in the
#: finished MP4, where nobody sees it until a customer does.
ARABIC_CAPABLE_FONTS = ("cairo", "tajawal", "notoarabic", "arial", "tahoma")
ARABIC_DEFAULT_FONT = "cairo"


@dataclass
class CaptionStyle:
    preset: str = "clean"
    font: str = "inter"
    size: int = 64
    weight: str = "bold"
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFE500"
    outline_color: str = "#000000"
    outline_width: int = 4
    background: str = "none"
    position: str = "bottom"          # top | center | bottom
    words_per_line: int = 4
    uppercase: bool = False
    enabled: bool = True

    @classmethod
    def from_preset(cls, name: str, language: str = "en") -> "CaptionStyle":
        data = dict(CAPTION_PRESETS.get(name) or CAPTION_PRESETS["clean"])
        data.pop("label", None)
        style = cls(preset=name if name in CAPTION_PRESETS else "clean", **data)
        return style.for_language(language)

    def for_language(self, language: str) -> "CaptionStyle":
        """Force an Arabic-capable font, and never upper-case Arabic.

        Arabic has no letter case, so `uppercase` is meaningless there; leaving
        it on is harmless for the text but signals a style built for Latin,
        and the font swap is the part that actually matters.
        """
        if language != "ar":
            return self
        if self.font not in ARABIC_CAPABLE_FONTS:
            self.font = ARABIC_DEFAULT_FONT
        self.uppercase = False
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VideoSpec:
    """One generation request, fully resolved."""

    topic: str
    language: Language = "en"
    duration_seconds: int = 30
    aspect_ratio: AspectRatio = "9:16"
    voice: str = ""
    voice_rate: float = 1.0           # 0.5 – 2.0
    voice_volume: float = 1.0         # 0.0 – 1.0
    music: str = "auto"               # auto | none | <track id>
    music_volume: float = 0.12
    clip_seconds: float = 3.5         # how fast footage switches
    transition: str = "none"          # none | fade
    fit_mode: str = "cover"           # cover | contain
    quality: str = "high"             # high | balanced
    captions: CaptionStyle = field(default_factory=CaptionStyle)

    @property
    def size(self) -> tuple[int, int]:
        return ASPECT_SIZES.get(self.aspect_ratio, ASPECT_SIZES["9:16"])

    def to_dict(self) -> dict:
        data = asdict(self)
        data["captions"] = self.captions.to_dict()
        return data


def _clamp(value, low, high, default):
    try:
        return min(high, max(low, type(default)(value)))
    except (TypeError, ValueError):
        return default


def normalise_spec(raw: dict) -> VideoSpec:
    """Turn whatever the client sent into a spec the engine can trust.

    Every field is clamped or replaced rather than rejected. This runs on the
    worker, behind an authenticated API, on a row that may have been written by
    an older version of the form -- failing the job because a stored preference
    has `duration: 35` would be a worse outcome than rounding it.
    """
    raw = raw or {}
    language: Language = "ar" if str(raw.get("language")) == "ar" else "en"

    duration = _clamp(raw.get("duration_seconds", 30), 15, 180, 30)
    if duration not in DURATIONS:
        duration = min(DURATIONS, key=lambda d: abs(d - duration))

    aspect = str(raw.get("aspect_ratio") or "9:16")
    if aspect not in ASPECT_SIZES:
        aspect = "9:16"

    voice = str(raw.get("voice") or "")
    if voice not in {v["id"] for v in voices_for(language)}:
        voice = default_voice(language)

    caption_raw = dict(raw.get("captions") or {})
    style = CaptionStyle.from_preset(
        str(caption_raw.get("preset") or "clean"), language
    )
    # Explicit overrides win over the preset, which is what "Advanced" edits do.
    for key in (
        "font", "size", "weight", "text_color", "highlight_color",
        "outline_color", "outline_width", "background", "position",
        "words_per_line", "uppercase", "enabled",
    ):
        if key in caption_raw and caption_raw[key] is not None:
            setattr(style, key, caption_raw[key])
    style.size = _clamp(style.size, 24, 140, 64)
    style.outline_width = _clamp(style.outline_width, 0, 16, 4)
    style.words_per_line = _clamp(style.words_per_line, 1, 12, 4)
    if style.position not in {"top", "center", "bottom"}:
        style.position = "bottom"
    style = style.for_language(language)

    return VideoSpec(
        topic=str(raw.get("topic") or "").strip()[:400],
        language=language,
        duration_seconds=duration,
        aspect_ratio=aspect,  # type: ignore[arg-type]
        voice=voice,
        voice_rate=_clamp(raw.get("voice_rate", 1.0), 0.5, 2.0, 1.0),
        voice_volume=_clamp(raw.get("voice_volume", 1.0), 0.0, 1.0, 1.0),
        music=str(raw.get("music") or "auto"),
        music_volume=_clamp(raw.get("music_volume", 0.12), 0.0, 1.0, 0.12),
        clip_seconds=_clamp(raw.get("clip_seconds", 3.5), 1.5, 10.0, 3.5),
        transition="fade" if str(raw.get("transition")) == "fade" else "none",
        fit_mode="contain" if str(raw.get("fit_mode")) == "contain" else "cover",
        quality="balanced" if str(raw.get("quality")) == "balanced" else "high",
        captions=style,
    )


def preset(name: str) -> dict:
    return dict(CAPTION_PRESETS.get(name) or CAPTION_PRESETS["clean"])
