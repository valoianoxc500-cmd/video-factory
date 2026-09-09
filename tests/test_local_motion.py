"""Local animation: what it plans, and what it refuses to escalate.

The point of these tests is the money. Local motion is free and the paid model
is not, so the behaviour worth pinning down is that ordinary beats never reach
the paid path and that the recipe is deterministic enough for Remotion to
render the same frames twice.
"""

import pytest

from core.local_motion import (
    MotionRecipe,
    needs_ai_motion,
    plan_local_motion,
)


# ── every beat gets motion ───────────────────────────────────────────

@pytest.mark.parametrize("beat", [
    "",
    "   ",
    "a quiet room",
    "an unremarkable corridor at noon",
])
def test_every_beat_gets_a_usable_recipe(beat):
    """A frozen frame is the one outcome this path must never ship."""
    recipe = plan_local_motion(beat)
    assert isinstance(recipe, MotionRecipe)
    assert recipe.camera
    assert recipe.subject
    assert 0.0 <= recipe.intensity <= 1.0


def test_the_recipe_is_deterministic():
    """Remotion renders frames out of order and retries them."""
    a = plan_local_motion("rain against the window", 3)
    b = plan_local_motion("rain against the window", 3)
    assert a.to_record() == b.to_record()


def test_the_seed_varies_between_beats():
    a = plan_local_motion("rain against the window", 1)
    b = plan_local_motion("smoke under the door", 2)
    assert a.seed != b.seed


# ── effects come from the beat's own words ───────────────────────────

@pytest.mark.parametrize("beat,effect", [
    ("heavy rain on the roof", "rain"),
    ("snow settles on the path", "snow"),
    ("smoke curls under the door", "smoke"),
    ("the fire burns low", "fire"),
    ("dust hangs in the air", "dust"),
])
def test_elements_are_detected(beat, effect):
    assert effect in plan_local_motion(beat).effects


def test_multiple_elements_stack():
    recipe = plan_local_motion("rain and smoke over the burning car")
    assert {"rain", "smoke", "fire"} <= set(recipe.effects)


def test_fog_is_layered_last():
    """Fog is volume, and it belongs over the weather it sits in."""
    recipe = plan_local_motion("rain and fog across the field")
    assert recipe.effects[-1] == "fog"


def test_substrings_do_not_trigger_effects():
    """'ash' inside 'crash' is how a quiet interior beat starts raining."""
    recipe = plan_local_motion("the crash echoed and he tried to restrain her")
    assert "dust" not in recipe.effects
    assert "rain" not in recipe.effects


def test_lighting_is_detected():
    assert plan_local_motion("the bulb flickers").lighting == "flicker"
    assert plan_local_motion("headlights sweep the wall").lighting == "sweep"


# ── escalation is rare and argued for ────────────────────────────────

@pytest.mark.parametrize("beat", [
    "he stands at the window",
    "she leans toward the desk",
    "the room is silent",
    "he stares at the door, terrified",
    "rain falls on the empty street",
])
def test_ordinary_beats_are_never_escalated(beat):
    needs, _ = needs_ai_motion(beat)
    assert needs is False


@pytest.mark.parametrize("beat", [
    "he runs down the hall and climbs the stairs",
    "she walks across the room and picks up the knife",
])
def test_stacked_articulated_motion_is_escalated(beat):
    needs, reason = needs_ai_motion(beat)
    assert needs is True
    assert reason


def test_a_travel_verb_with_a_reaction_stays_local():
    """'runs, terrified' is a reaction beat: a push-in sells it."""
    needs, reason = needs_ai_motion("he runs, terrified")
    assert needs is False
    assert "reaction" in reason


def test_an_empty_beat_is_never_escalated():
    needs, reason = needs_ai_motion("")
    assert needs is False
    assert reason == "empty beat"


def test_the_reason_is_always_reportable():
    """'why did this scene cost fifteen cents' answers from the manifest."""
    for beat in ("he walks and climbs", "a quiet room", ""):
        _, reason = needs_ai_motion(beat)
        assert isinstance(reason, str) and reason


# ── pacing ───────────────────────────────────────────────────────────

def test_long_holds_move_the_camera_less():
    short = plan_local_motion("she leans toward the desk", 0, seconds=3.0)
    long = plan_local_motion("she leans toward the desk", 0, seconds=8.0)
    assert long.intensity < short.intensity


def test_direction_alternates_between_neighbours():
    """Consecutive scenes panning the same way read as one long drift."""
    directions = [plan_local_motion("a room", i).direction for i in range(4)]
    assert len(set(directions)) == 4
