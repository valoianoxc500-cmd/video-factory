"""A football video has to end on purpose.

Run 99db2deb's script delivered the winning goal and then stopped. Nothing
asked the viewer anything, so nothing invited a comment. The requirement is
that the last section closes with a short question built from this match --
and, just as importantly, that the question does not smuggle in a fact the
research never retrieved. A made-up next fixture with a question mark on the
end is still a made-up fixture.
"""

import json
from pathlib import Path

import pytest

import prompts
from core.scripter import _end_hook_errors, _script_validation_errors
from core.utils import ChannelConfig


def _sections(final_narration: str):
    return [
        {"id": 1, "narration": "الشوط الأول كان متكافئا بين الفريقين."},
        {"id": 2, "narration": final_narration},
    ]


def _errors(final_narration: str):
    return _end_hook_errors(_sections(final_narration))


# ── the failure that shipped ─────────────────────────────────────────

def test_a_script_that_just_stops_is_rejected():
    problems = _errors("وبهذا حسم ريال مدريد المباراة بهدفين مقابل هدف.")
    assert problems
    assert "ends on a statement" in problems[0]


def test_a_closing_question_is_accepted():
    assert _errors(
        "وبهذا حسم ريال مدريد المباراة. برأيك من كان أفضل لاعب في اللقاء؟"
    ) == []


def test_an_english_closing_question_is_accepted():
    assert _errors("Madrid took it 2-1. Who was your man of the match?") == []


def test_a_subscribe_line_is_not_an_end_hook():
    problems = _errors("نتيجة رائعة. اشترك في القناة لمزيد من الأخبار؟")
    assert any("not an end hook" in p for p in problems)


def test_a_follow_cta_in_english_is_not_an_end_hook():
    problems = _errors("Great result. Follow for more, and what did you think?")
    assert any("not an end hook" in p for p in problems)


def test_a_question_earlier_in_the_section_does_not_count():
    """The hook is the ending, not a rhetorical aside in the middle."""
    problems = _errors(
        "هل كان التبديل هو المفتاح؟ " + ("لقد سيطر ريال مدريد على المباراة تماما. " * 8)
    )
    assert problems, "a mid-section question was accepted as the ending"


def test_an_empty_or_missing_final_section_is_not_a_hook_error():
    """Emptiness is somebody else's error; do not double-report it."""
    assert _end_hook_errors([]) == []
    assert _end_hook_errors([{"id": 1, "narration": "   "}]) == []


# ── it is only on where it belongs ───────────────────────────────────

def test_the_requirement_is_off_unless_the_channel_asks_for_it():
    sections = _sections("It simply ends here.")
    errors, _ = _script_validation_errors(
        {"sections": sections},
        numbering_order=None, max_visual_hold_seconds=5.0, crossfade=0.3,
        timing_profile=None,
    )
    assert not any("ends on a statement" in e for e in errors)


def test_football_asks_for_it_and_the_story_channels_do_not():
    def style(slug):
        return ChannelConfig.model_validate(json.loads(
            Path(f"config/channels/{slug}.json").read_text(encoding="utf-8")
        )).script_style

    assert style("football_news").require_end_hook is True
    for slug in ("horror_stories", "true_stories", "animated_stories"):
        assert style(slug).require_end_hook is False, slug


# ── what the model is told ───────────────────────────────────────────

def test_the_hook_must_come_from_this_match():
    rules = prompts.end_hook_rules("ar")
    assert "Build it from THIS match" in rules
    assert "would fit any football video" in rules


def test_the_hook_may_not_invent_a_fixture_or_a_statistic():
    rules = prompts.end_hook_rules("ar")
    assert "future fixture" in rules
    assert "research context does not contain" in rules
    assert "statistic, score or record that is not in" in rules


def test_the_hook_is_not_a_subscribe_line():
    rules = prompts.end_hook_rules("en")
    assert "follow for more" in rules
    assert "it is not the" in rules


def test_an_arabic_run_asks_for_spoken_arabic():
    arabic = prompts.end_hook_rules("ar")
    assert "natural spoken Arabic" in arabic
    assert "not a literal translation" in arabic
    assert "natural spoken English" in prompts.end_hook_rules("en")


def test_the_hook_gets_its_own_visual():
    rules = prompts.end_hook_rules("ar")
    assert "own last visual slot" in rules
    assert "sourcing rules as every other slot" in rules


def test_the_rules_are_absent_unless_the_channel_opts_in():
    common = dict(
        topic="t", video_type="news", angle="a", channel_name="c",
        audience="x", language="ar", target_duration_minutes=1,
        sections_range=[2, 3], section_style="s", pacing="p",
        style_prompt_suffix="", title_format_instruction="",
        description_style_instruction="",
        thumbnail_strategies=[{"name": "face", "instruction": "a face"}],
    )
    assert "END HOOK (required)" not in prompts.script_generation_prompt(**common)
    assert "END HOOK (required)" in prompts.script_generation_prompt(
        **common, end_hook=True
    )
