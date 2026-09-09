"""Local, free animation for Animated Stories scenes.

Every scene is animated here first, by Remotion, for nothing. A paid
image-to-video clip is an exception this path has to argue for, not the normal
way a scene is made -- so the question each beat is asked is not "should this
be animated?" (all of them are) but "is there motion here that compositing
genuinely cannot fake?".

What local animation actually does, honestly:

    camera      pan, push-in, pull-out, drift, handheld sway
    subject     breathing bob, lean, sway, recoil, shake, punch-in
    elements    rain, snow, smoke, fire, embers, sparks, dust, fog
    light       flicker, sweep, pulse, temperature shift, vignette

What it does NOT do, and cannot from a single still: articulated limbs. A
walk cycle, a head turn, a hand picking something up -- those need the pixels
to move relative to each other, and no camera transform or particle overlay
produces them. `needs_ai_motion` exists to name exactly those beats, and it is
deliberately strict: a story that escalates every beat has simply moved the
cost back to where it was.

Nothing here imports a channel config or touches Football, Horror or True
Stories. It is a pure function from a beat's words to a recipe.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# ── vocabularies ─────────────────────────────────────────────────────
#
# Matched against the beat's own words. Kept as data rather than buried in
# branches so a recipe can be explained -- and tested -- by the terms that
# produced it.

_WEATHER = {
    "rain": ("rain", "raining", "rainy", "downpour", "drizzle", "storm",
             "thunderstorm", "shower"),
    "snow": ("snow", "snowing", "snowfall", "blizzard", "sleet"),
    "fog": ("fog", "foggy", "mist", "misty", "haze"),
}

_ELEMENTS = {
    "fire": ("fire", "flame", "flames", "burning", "blaze", "bonfire",
             "candle", "torch", "ember"),
    "smoke": ("smoke", "smoking", "smoulder", "smolder", "steam", "vapour",
              "vapor", "exhaust"),
    "sparks": ("spark", "sparks", "electric", "electrical", "short circuit",
               "flicker of current"),
    "dust": ("dust", "sand", "ash", "debris", "pollen", "motes"),
}

_LIGHT = {
    "flicker": ("flicker", "flickering", "lamp", "bulb", "fluorescent",
                "candle", "torch", "lightning", "strobe"),
    "sweep": ("headlight", "headlights", "searchlight", "torchlight",
              "beam", "spotlight", "passing car"),
    "pulse": ("heartbeat", "pulsing", "throb", "alarm", "siren", "glow"),
}

#: Beats whose motion is a reaction rather than travel. These read well from a
#: still: a recoil, a shake and a fast push-in are all camera and subject
#: transforms, and they are what most story beats actually need.
_REACTIONS = (
    "gasp", "gasps", "flinch", "flinches", "recoil", "recoils", "startled",
    "shocked", "shock", "freeze", "freezes", "stares", "staring", "realises",
    # "jumps" is deliberately absent: it is a startle in "jumps at the noise"
    # and locomotion in "jumps the fence", and matching it here made every
    # beat containing it read as a reaction. `jolt` and `startled` carry the
    # startle sense without the collision.
    "realizes", "screams", "scream", "jolt", "spins round",
    "turns cold", "panic", "panics", "terrified", "afraid", "trembling",
    "shaking", "breathes", "breathing",
)

#: Subject is moving, but in a way a push-in plus a sway sells convincingly:
#: the camera does the travelling, not the legs.
_SOFT_MOTION = (
    "approaches", "approaching", "nears", "steps toward", "steps towards",
    "leans", "leaning", "reaches", "turns", "looks", "watches", "waits",
    "stands", "sits", "kneels", "rises", "drifts", "floats", "sways",
)

#: Articulated, multi-limb or multi-actor motion. This is the only list that
#: can send a beat to the paid model, so it stays narrow and concrete.
_HARD_MOTION = (
    "walks", "walking", "runs", "running", "sprints", "sprinting", "chases",
    "chasing", "climbs", "climbing", "jumps over", "leaps", "leaping",
    "dances", "dancing", "fights", "fighting", "throws", "throwing",
    "catches", "swings", "swinging", "crawls", "crawling", "falls down",
    "stumbles", "wrestles", "pushes open", "pulls open", "hands over",
    "picks up", "puts down", "opens the door", "shuts the door",
)


def _words(beat: str) -> str:
    return " ".join(str(beat or "").lower().split())


def _hits(text: str, terms) -> list[str]:
    """Terms present in `text`, matched on word boundaries.

    Substring matching would fire "ash" inside "crash" and "rain" inside
    "restrain", which is how a quiet interior beat ends up raining.
    """
    found = []
    for term in terms:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, text):
            found.append(term)
    return found


@dataclass
class MotionRecipe:
    """Everything Remotion needs to animate one still, locally.

    Serialised straight into the scene's render props, so the field names are
    the contract with `LocalAnimatedScene.tsx`.
    """

    #: Camera move over the whole beat.
    camera: str = "parallax"
    direction: str = "left"
    #: How hard the camera works, 0..1. Slow beats get less.
    intensity: float = 0.5
    #: Subject transform layered over the camera.
    subject: str = "breathe"
    #: Overlay effects, rendered in order.
    effects: list[str] = field(default_factory=list)
    #: Lighting treatment over everything.
    lighting: str = "none"
    #: Deterministic seed so a re-render is frame-identical.
    seed: int = 0
    #: Why this recipe -- carried into the manifest so a run can be read back.
    reasons: list[str] = field(default_factory=list)

    def to_record(self) -> dict:
        return asdict(self)


def _seed_for(beat: str, index: int) -> int:
    """A stable seed from the beat itself.

    Remotion must render the same frame the same way every time, including on
    a retry, so nothing here may come from a clock or `random`.
    """
    total = index * 2654435761
    for char in _words(beat):
        total = (total * 31 + ord(char)) & 0xFFFFFFFF
    return total & 0xFFFF


def needs_ai_motion(beat: str) -> tuple[bool, str]:
    """Whether this beat has motion local compositing cannot fake.

    Returns the decision and the reason, because "why did this scene cost
    fifteen cents" has to be answerable from the manifest alone.
    """
    text = _words(beat)
    if not text:
        return False, "empty beat"

    hard = _hits(text, _HARD_MOTION)
    if not hard:
        return False, "no articulated motion"

    # A single locomotion verb on its own is still usually sellable with a
    # push-in and a sway -- the camera travels instead of the legs. Escalate
    # when the beat stacks articulated motion, which is where a static frame
    # visibly fails.
    if len(hard) == 1 and _hits(text, _REACTIONS):
        return False, f"{hard[0]!r} reads as a reaction beat"
    if len(hard) >= 2:
        return True, f"multiple articulated actions ({', '.join(hard[:3])})"
    return True, f"articulated motion ({hard[0]!r})"


def plan_local_motion(beat: str, index: int = 0, *, seconds: float = 4.0) -> MotionRecipe:
    """The free recipe for one beat.

    Always returns something usable. A beat with no recognisable cue still
    gets a camera move and a breathing subject, because a completely static
    frame is the one outcome this path does not ship.
    """
    text = _words(beat)
    recipe = MotionRecipe(seed=_seed_for(beat, index))

    # Elements and weather. A beat can carry more than one -- rain and
    # lightning belong together -- so these accumulate.
    for name, terms in {**_WEATHER, **_ELEMENTS}.items():
        hit = _hits(text, terms)
        if hit:
            recipe.effects.append(name)
            recipe.reasons.append(f"{name} from {hit[0]!r}")

    # Fog reads as atmosphere rather than particles; keep it last so it sits
    # over the weather it belongs with.
    if "fog" in recipe.effects:
        recipe.effects.remove("fog")
        recipe.effects.append("fog")

    for name, terms in _LIGHT.items():
        hit = _hits(text, terms)
        if hit:
            recipe.lighting = name
            recipe.reasons.append(f"{name} light from {hit[0]!r}")
            break

    # Subject and camera. Reactions punch in; soft motion drifts; anything
    # else breathes.
    if _hits(text, _REACTIONS):
        recipe.subject = "recoil"
        recipe.camera = "zoom_focus"
        recipe.intensity = 0.85
        recipe.reasons.append("reaction beat: push in")
    elif _hits(text, _SOFT_MOTION):
        recipe.subject = "sway"
        recipe.camera = "drift"
        recipe.intensity = 0.6
        recipe.reasons.append("soft motion: drift")
    elif _hits(text, _HARD_MOTION):
        # Still animated locally when it was not escalated: the camera
        # travels with the action, which is the honest substitute.
        recipe.subject = "lean"
        recipe.camera = "parallax"
        recipe.intensity = 0.9
        recipe.reasons.append("travel beat: camera tracks")
    else:
        recipe.reasons.append("no motion cue: breathing hold")

    # Direction alternates so consecutive scenes do not all pan the same way,
    # which is what makes a carousel of stills read as one long drift.
    recipe.direction = ("left", "right", "up", "down")[index % 4]

    # A long hold needs less camera per second or it arrives at the edge of
    # the frame early and sits there.
    if seconds >= 6:
        recipe.intensity = round(recipe.intensity * 0.7, 3)

    return recipe
