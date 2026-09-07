"""Horror narration ends on a question the viewer can answer.

The ending is what earns a comment, so the last spoken sentence is a short,
story-specific question rather than a subscribe prompt. The script is asked for
one; this is the floor under that, so the beat can never be silently missing.
"""

import json
from pathlib import Path

import pytest

from core.scripter import _ends_with_question, _ensure_closing_question
from core.utils import load_channel_config

REPO_ROOT = Path(__file__).resolve().parent.parent
FALLBACK = "ما تفسيرك لهذه القصة؟"


def _script(*narrations: str) -> dict:
    return {
        "sections": [
            {"id": i + 1, "narration": n, "slots": []}
            for i, n in enumerate(narrations)
        ]
    }


def _last(data: dict) -> str:
    return data["sections"][-1]["narration"]


# --- a real question is kept ----------------------------------------------

def test_a_story_specific_question_is_left_alone():
    """The model's own question is better than any fixed wording."""
    written = "برأيك... هل مات دي بي كوبر ليلة القفزة، أم أنه عاش واختفى بهوية جديدة؟"
    data = _script("القصة بدأت عام 1971.", written)
    _ensure_closing_question(data, fallback=FALLBACK)
    assert _last(data) == written
    assert FALLBACK not in _last(data)


@pytest.mark.parametrize(
    "ending",
    [
        "فهل كان يعرف أن المظلة معطلة؟",
        "من كان هذا الرجل حقًا؟",
        'وهل صدقت الرواية الرسمية؟"',
        "ما الذي حدث في تلك الليلة؟)",
    ],
)
def test_various_question_endings_are_recognised(ending):
    data = _script("مقدمة.", ending)
    _ensure_closing_question(data, fallback=FALLBACK)
    assert _last(data) == ending


# --- a statement gets the fallback ----------------------------------------

def test_a_statement_ending_gets_the_fallback_appended():
    data = _script("مقدمة.", "ولم يُعثر عليه قط.")
    _ensure_closing_question(data, fallback=FALLBACK)
    assert _last(data).endswith(FALLBACK)
    assert "ولم يُعثر عليه قط." in _last(data), "existing narration was discarded"


def test_the_fallback_makes_no_claim():
    """It asks for the viewer's reading, so it adds no fact to the story."""
    data = _script("مقدمة.", "انتهت القصة هنا.")
    _ensure_closing_question(data, fallback=FALLBACK)
    added = _last(data).replace("انتهت القصة هنا.", "").strip()
    assert added == FALLBACK
    assert added.endswith("؟")


def test_only_the_final_section_is_touched():
    data = _script("الأولى.", "الثانية.", "الثالثة.")
    _ensure_closing_question(data, fallback=FALLBACK)
    assert data["sections"][0]["narration"] == "الأولى."
    assert data["sections"][1]["narration"] == "الثانية."
    assert data["sections"][2]["narration"].endswith(FALLBACK)


# --- disabled and malformed cases -----------------------------------------

def test_an_empty_fallback_disables_the_behaviour():
    data = _script("مقدمة.", "بيان عادي.")
    _ensure_closing_question(data, fallback="")
    assert _last(data) == "بيان عادي."


@pytest.mark.parametrize(
    "malformed",
    [{}, {"sections": None}, {"sections": []}, {"sections": ["not a dict"]}],
)
def test_malformed_shapes_do_not_raise(malformed):
    _ensure_closing_question(malformed, fallback=FALLBACK)


def test_an_empty_final_narration_still_gets_the_question():
    data = _script("مقدمة.", "")
    _ensure_closing_question(data, fallback=FALLBACK)
    assert _last(data) == FALLBACK


# --- helper -----------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("هل حدث ذلك؟", True),
        ("Did it happen?", True),
        ("لم يحدث.", False),
        ("", False),
        ("   ", False),
    ],
)
def test_question_detection(text, expected):
    assert _ends_with_question(text) is expected


# --- channel wiring --------------------------------------------------------

def test_horror_declares_the_fallback():
    assert load_channel_config("horror_stories").closing_question_fallback == FALLBACK


def test_horror_instructs_a_story_specific_question():
    raw = json.loads(
        (REPO_ROOT / "config" / "channels" / "horror_stories.json")
        .read_text(encoding="utf-8")
    )
    instructions = raw["script_style"]["instructions"]
    assert "CLOSING QUESTION" in instructions
    assert "final spoken sentence" in instructions
    assert "specific to this case" in instructions


def test_horror_forbids_a_subscribe_style_ending():
    raw = json.loads(
        (REPO_ROOT / "config" / "channels" / "horror_stories.json")
        .read_text(encoding="utf-8")
    )
    instructions = raw["script_style"]["instructions"].lower()
    assert "do not end on a like, follow or subscribe request" in instructions


def test_the_question_may_not_introduce_new_claims():
    raw = json.loads(
        (REPO_ROOT / "config" / "channels" / "horror_stories.json")
        .read_text(encoding="utf-8")
    )
    instructions = raw["script_style"]["instructions"]
    assert "Introduce NO new fact" in instructions


def test_the_example_is_guidance_not_a_hardcoded_string():
    """The D.B. Cooper wording must live in the prompt, never in the code."""
    scripter_src = (REPO_ROOT / "core" / "scripter.py").read_text(encoding="utf-8")
    assert "دي بي كوبر" not in scripter_src


def test_football_news_is_unaffected():
    assert load_channel_config("football_news").closing_question_fallback == ""


def test_the_conditional_part_two_cta_survives():
    raw = json.loads(
        (REPO_ROOT / "config" / "channels" / "horror_stories.json")
        .read_text(encoding="utf-8")
    )
    assert any("الجزء الثاني" in r for r in raw["business_strategy"]["cta_rules"])
