"""The Character Bible: who is in this story, locked down before anything is drawn.

This module exists because of a specific, observed failure. An Animated
Stories run generated twenty-one scenes whose prompts all said "the SAME
character as the reference sheet", and the reviewer rejected every batch:
*"every image features a different character"*. Two things were wrong, and
both are fixed here.

First, there was no description. The character was derived from a `character`
field that no plan and no script ever set, so it resolved to the empty string
and every prompt asked for "the main character" -- a phrase with no face, no
clothes and no colours in it. A model cannot hold constant something it was
never told.

Second, a description is only worth anything if it is *identical* every time.
Re-describing a character per scene, even from the same source, produces
twenty-one paraphrases and twenty-one people. So the description here is
written once, frozen as a string, and pasted verbatim into every prompt that
mentions that character. `locked_description` returns the same characters in
the same order for the life of a run; that is its entire job.

Each character also gets a stable ID (`char_01`). The ID is what a scene
prompt, a reference sheet filename and a consistency review all agree on, so
"which character is this scene supposed to contain" is never inferred from a
name that the script may have spelled three ways.

Nothing here is reachable from Football, Horror Stories, True Stories or
Quote Studio.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field

logger = logging.getLogger("video_factory")

#: Fields that make a character recognisable. Every one is mandatory in the
#: locked description: leaving any of them to the model is where drift starts.
LOCKED_FIELDS = (
    "face",
    "hair",
    "clothing",
    "colours",
    "accessories",
    "proportions",
    "expression",
)


@dataclass
class Character:
    """One recurring character, frozen."""

    id: str
    name: str
    role: str = "secondary"  # "main" | "secondary"
    face: str = ""
    hair: str = ""
    clothing: str = ""
    colours: str = ""
    accessories: str = "none"
    proportions: str = ""
    expression: str = ""
    #: Reference sheet filename, once drawn.
    sheet: str = ""

    def to_record(self) -> dict:
        return asdict(self)

    def is_complete(self) -> bool:
        return all(str(getattr(self, f, "") or "").strip() for f in LOCKED_FIELDS)


def locked_description(character: Character) -> str:
    """The exact words used for this character in every prompt, forever.

    Assembled in a fixed field order and never re-phrased. Two scenes that
    both contain `char_01` receive byte-identical text describing them, which
    is the only reason a text-to-image model draws them the same way twice.
    """
    return (
        f"[{character.id}] {character.name} -- "
        f"FACE: {character.face}. "
        f"HAIR: {character.hair}. "
        f"CLOTHING: {character.clothing}. "
        f"COLOURS: {character.colours}. "
        f"ACCESSORIES: {character.accessories}. "
        f"PROPORTIONS: {character.proportions}. "
        f"DEFAULT EXPRESSION: {character.expression}."
    )


#: Appended wherever a character appears. States what may change and what may
#: not, because "same character" alone is read as a style note.
IDENTITY_LOCK = (
    "IDENTITY LOCK -- this is the single most important instruction. The "
    "character above must be drawn with exactly the same face, the same head "
    "shape, the same hair, the same clothing, the same colours, the same "
    "accessories and the same body proportions as described. Do not redesign, "
    "restyle, age, recolour or replace them. Do not change their outfit "
    "between scenes. ONLY the pose, the facial expression, the action, the "
    "camera angle, the environment and the lighting may change."
)


#: Appended only when the character's reference sheet is actually attached to
#: the generation call. Kept out of the stored prompt on purpose: a prompt that
#: claims an attached image when none was sent describes a picture the model
#: cannot see, which is worse than not mentioning one.
REFERENCE_LOCK = (
    "The attached image is the official reference sheet for the character(s) "
    "named above. It is the authority on their appearance: copy the face, head "
    "shape, hair, clothing, colours, accessories and proportions from it "
    "exactly, including skin tone and any facial shading it shows. Where the "
    "written description and the reference sheet could be read differently, "
    "follow the reference sheet. Draw the same character in the new scene "
    "described above -- do not redraw the reference sheet itself, and do not "
    "copy its plain background, its multiple views or its head close-ups."
)

#: `[char_01]` markers, as `locked_description` writes them.
_ID_PATTERN = re.compile(r"\[(char_\d+)\]")


def character_ids_in(text: str) -> list[str]:
    """The character IDs a locked prompt names, in order, without repeats.

    The bound scene prompt is the only thing the generator call has in hand at
    the point where a reference sheet could be attached, so the cast has to be
    readable back out of it. `locked_description` writes the ID in brackets for
    exactly this reason.
    """
    seen: list[str] = []
    for match in _ID_PATTERN.finditer(str(text or "")):
        char_id = match.group(1)
        if char_id not in seen:
            seen.append(char_id)
    return seen


@dataclass
class CharacterBible:
    """The full cast, by ID."""

    characters: list[Character] = field(default_factory=list)

    def main(self) -> Character | None:
        for c in self.characters:
            if c.role == "main":
                return c
        return self.characters[0] if self.characters else None

    def by_id(self, char_id: str) -> Character | None:
        for c in self.characters:
            if c.id == char_id:
                return c
        return None

    def to_record(self) -> dict:
        return {
            "characters": [c.to_record() for c in self.characters],
            "count": len(self.characters),
        }

    def cast_summary(self) -> str:
        """Every character's locked description, for a prompt that needs all of them."""
        return "\n".join(locked_description(c) for c in self.characters)


# ── deriving the cast ────────────────────────────────────────────────

_PROMPT = """<task>
Read this story and design its cast for a stick-figure animated series.
</task>

<story>
{story}
</story>

<rules>
- Identify every character who appears in more than one beat. At most {limit}.
- Exactly one character has role "main". The rest are "secondary".
- Invent concrete, specific, VISUAL detail. This is a design document, not a
  summary: another artist must be able to draw the same character from it
  without seeing your drawing.
- Name actual colours ("mustard yellow hoodie", not "colourful top").
- Every field must be filled. Never write "unknown", "varies" or "any".
- accessories may be "none", but must be stated.
- Keep each field under 20 words.
- Do not describe the scene, the story or the mood. Describe the person.
</rules>

Return ONLY JSON:
{{"characters": [
  {{"name": "...", "role": "main",
    "face": "...", "hair": "...", "clothing": "...", "colours": "...",
    "accessories": "...", "proportions": "...", "expression": "..."}}
]}}"""

#: Filled in when the model omits a field. A specific default is better than
#: an empty one: an empty field silently removes a lock, a default keeps it.
_FALLBACKS = {
    "face": "large round white head, simple black dot eyes, thick expressive "
            "black eyebrows, small simple mouth",
    "hair": "short flat black hair",
    "clothing": "plain long-sleeved top and plain trousers",
    "colours": "navy top, dark grey trousers",
    "accessories": "none",
    "proportions": "thin stick-figure limbs, small torso, head roughly one "
                   "third of total height",
    "expression": "neutral and alert",
}


def _story_digest(script, plan: dict | None, limit: int = 4000) -> str:
    """The narration, which is where the characters actually are."""
    parts: list[str] = []
    if plan:
        for key in ("topic", "angle"):
            value = str(plan.get(key) or "").strip()
            if value:
                parts.append(value)
    for section in getattr(script, "sections", []) or []:
        text = str(getattr(section, "narration", "") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)[:limit]


def _parse(raw: str) -> list[dict]:
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    entries = data.get("characters") if isinstance(data, dict) else data
    return entries if isinstance(entries, list) else []


def bible_from_entries(entries: list[dict], *, limit: int = 4) -> CharacterBible:
    """Build a bible from raw dicts, filling gaps and assigning IDs.

    Separated from the model call so the assembly rules -- IDs, exactly one
    main, no empty locked fields -- can be tested without a network.
    """
    characters: list[Character] = []
    for index, entry in enumerate(entries[:limit], start=1):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or f"Character {index}").strip()
        character = Character(
            id=f"char_{index:02d}",
            name=name,
            role=str(entry.get("role") or "secondary").strip().lower(),
        )
        for field_name in LOCKED_FIELDS:
            value = str(entry.get(field_name) or "").strip()
            setattr(character, field_name, value or _FALLBACKS[field_name])
        characters.append(character)

    if not characters:
        characters = [
            Character(id="char_01", name="The main character", role="main",
                      **_FALLBACKS)
        ]

    # Exactly one main. A cast with two leads gives the scene prompts two
    # candidates for "the character", which is drift by another route.
    mains = [c for c in characters if c.role == "main"]
    if len(mains) != 1:
        for c in characters:
            c.role = "secondary"
        characters[0].role = "main"

    return CharacterBible(characters=characters)


async def build_bible(
    script,
    plan: dict | None = None,
    *,
    generate_text,
    limit: int = 4,
) -> CharacterBible:
    """Design the cast from the story. Never raises; always returns a bible.

    A failed or unparseable response degrades to a single fully-specified
    default character rather than to nothing, because "no description" is the
    exact condition this module was written to eliminate.
    """
    story = _story_digest(script, plan)
    if not story.strip():
        logger.warning("[character] no narration to design a cast from")
        return bible_from_entries([], limit=limit)

    try:
        raw = await generate_text(
            _PROMPT.format(story=story, limit=limit),
            operation_label="character_bible",
            response_mime_type="application/json",
        )
        entries = _parse(raw)
    except Exception as exc:
        logger.warning(f"[character] cast design failed: {str(exc)[:200]}")
        entries = []

    bible = bible_from_entries(entries, limit=limit)
    logger.info(
        f"[character] bible: "
        + ", ".join(f"{c.id}={c.name}" for c in bible.characters)
    )
    return bible


# ── prompts ──────────────────────────────────────────────────────────

def sheet_prompt(character: Character, style: str, exclusions: str) -> str:
    """The reference sheet for one character."""
    return (
        f"Character reference sheet for {character.name}. "
        "Three views of the SAME character side by side on a plain neutral "
        "background: front view, three-quarter view, and side view, plus two "
        "small head close-ups showing a neutral expression and a frightened "
        "expression. Identical head shape, identical clothing, identical "
        "colours and identical proportions in every view.\n\n"
        f"{locked_description(character)}\n\n"
        # The sheet is what the consistency review compares every scene
        # against, so anything it invents becomes a rule no scene was told
        # about. A run was rejected for scenes having "plain white faces and
        # lacking the skin tone and blush details shown in the reference
        # sheets" -- neither of which the description mentions. The scenes were
        # faithful to the text; the sheet had added detail on its own.
        "Draw ONLY what the description above states. Do not add skin tone, "
        "blush, freckles, make-up, patterns, logos, extra clothing layers or "
        "accessories that are not listed. Anything you invent here becomes a "
        "rule every later scene has to match.\n\n"
        f"{IDENTITY_LOCK}\n\n"
        f"{style} {exclusions}"
    )


def scene_prompt_for(
    beat: str,
    characters: list[Character],
    style: str,
    exclusions: str,
    *,
    setting: str = "",
) -> str:
    """One scene, with every character in it locked to the bible."""
    action = " ".join(str(beat or "").split())
    where = f" {setting.strip()}" if setting and setting.strip() else ""
    cast = "\n".join(locked_description(c) for c in characters)
    return (
        f"{action}{where}\n\n"
        f"CHARACTERS IN THIS SCENE:\n{cast}\n\n"
        f"{IDENTITY_LOCK}\n\n"
        f"{style} {exclusions}"
    )


def characters_in_beat(beat: str, bible: CharacterBible) -> list[Character]:
    """Which characters a beat mentions, main first.

    Falls back to the main character rather than to nobody: a scene with no
    named character still shows one, and leaving the cast empty is what
    produced unlabelled prompts in the first place.
    """
    text = " ".join(str(beat or "").lower().split())
    found = [
        c for c in bible.characters
        if c.name and re.search(
            r"\b" + re.escape(c.name.split()[0].lower()) + r"\b", text
        )
    ]
    main = bible.main()
    if main and main not in found:
        found.insert(0, main)
    return found or ([main] if main else [])
