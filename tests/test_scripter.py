"""Tests for script-generation validation helpers."""

import asyncio
import copy

import pytest

import core.scripter as scripter
from prompts import (
    script_generation_prompt,
    script_revision_prompt,
    script_review_prompt,
)
from core.utils import duration_window, script_timing_profile
from core.scripter import (
    _script_word_totals,
    _script_validation_errors,
    _prompt_word_targets,
    _thumbnail_strategy_options,
    _title_banner_numbering_errors,
    _validate_thumbnail_strategy_choice,
    generate_script,
)
from core.utils import ChannelConfig


def _script_data(numbers: list[int], *, title_prefix: str = "Item") -> dict:
    sections = []
    for idx, number in enumerate(numbers, start=1):
        sections.append({
            "id": idx,
            "narration": f"This section introduces {title_prefix.lower()} {number} with more detail.",
            "slots": [
                {
                    "visual": "title_banner",
                    "prompt": f"Realistic photo of {title_prefix.lower()} {number}.",
                    "keywords": f"{title_prefix.lower()} {number} real photo",
                    "props": {
                        "title": f"{title_prefix} {number}",
                        "section_number": number,
                    },
                }
            ],
        })
    return {"sections": sections}


def _structure_errors(data: dict, *, numbering_order: str | None = None) -> list[str]:
    errors, _ = _script_validation_errors(
        data,
        numbering_order=numbering_order,
        max_visual_hold_seconds=16.0,
        crossfade=0.3,
        timing_profile=script_timing_profile("gemini-2.5-flash-tts"),
    )
    return errors


def _script_stage_config() -> ChannelConfig:
    return ChannelConfig(
        channel_name="Test",
        niche={
            "category": "Test",
            "focus": "Test",
            "audience": "Test",
            "content_style": "Test",
        },
        video={"target_duration_minutes": 1},
        video_types={
            "talk": {
                "enabled": True,
                "section_style": "clear",
                "pacing": "steady",
                "allowed_thumbnail_strategies": ["hero"],
            }
        },
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        # Pinned to the pre-density pacing so these tests keep exercising the
        # generate/review/revision control flow rather than the slot-density
        # rule. Density itself is covered by the pacing tests, which pass
        # max_visual_hold_seconds explicitly.
        rendering_defaults={
            "max_visual_hold_seconds": 16.0,
            "image_slot_min_duration": 5.0,
        },
    )


def _script_stage_plan() -> dict:
    return {
        "topic": "Test topic",
        "video_type": "talk",
        "angle": "Test angle",
        "title_format_instruction": "Use a clear title",
        "description_style_instruction": "Use a short description",
    }


def test_script_structure_rejects_authored_text_only_slide():
    errors = _structure_errors({
        "sections": [
            {
                "id": 1,
                "narration": "Sit tall and move slowly.",
                "slots": [
                    {
                        "visual": "text_only_slide",
                        "props": {"title": "Setup", "text": "Sit tall."},
                    }
                ],
            }
        ]
    })

    assert any("text_only_slide is reserved for internal AI prompt preview" in err for err in errors)


def test_script_structure_rejects_image_less_info_slide():
    errors = _structure_errors({
        "sections": [
            {
                "id": 1,
                "narration": "Pause before standing.",
                "slots": [
                    {
                        "visual": "info_slide",
                        "prompt": "",
                        "props": {"title": "Pause", "text": "Pause before standing."},
                    }
                ],
            }
        ]
    })

    assert any("info_slide requires an image prompt" in err for err in errors)


def test_script_structure_rejects_photo_backed_policy_on_non_info_slide():
    errors = _structure_errors({
        "sections": [
            {
                "id": 1,
                "narration": "Move your ankles before standing.",
                "slots": [
                    {
                        "visual": "info_card",
                        "prompt": "Simple heart icon.",
                        "visual_policy": "photo_backed_info_slide",
                        "props": {"title": "Pump", "text": "Warms knee tissues."},
                    }
                ],
            }
        ]
    })

    assert any("photo_backed_info_slide requires visual info_slide" in err for err in errors)


def _valid_generated_script() -> dict:
    return {
        "title": "Test Title",
        "video_type": "talk",
        "description": "Test description",
        "tags": ["t"],
        "hook": "Hook",
        "thumbnail_text": "TEST",
        "thumbnail_brief": "One clear hero image",
        "thumbnail_strategy": "hero",
        "sections": [
            {
                "id": 1,
                "narration": "word " * 74,
                "highlighted_keywords": ["warmup", "mobility"],
                "slots": [
                    {
                        "visual": "google_photo",
                        "prompt": "Realistic photo of older adult stretching calves at home.",
                        "keywords": "older adult stretching calves at home",
                    },
                    {
                        "visual": "stock_photo",
                        "prompt": "Realistic photo of older adult walking carefully through a hallway.",
                        "keywords": "older adult walking carefully hallway",
                    },
                    {
                        "visual": "google_photo",
                        "prompt": "Realistic photo of older adult sitting at the edge of a bed before standing.",
                        "keywords": "older adult sitting edge bed before standing",
                    },
                    {
                        "visual": "b_roll",
                        "prompt": "older adult placing both feet flat on the floor before walking",
                        "keywords": "older adult feet flat on floor before walking",
                    },
                ],
            },
            {
                "id": 2,
                "narration": "word " * 74,
                "highlighted_keywords": ["balance", "routine"],
                "transition_type": "fade",
                "slots": [
                    {
                        "visual": "google_photo",
                        "prompt": "Realistic photo of older adult standing near a chair for balance practice.",
                        "keywords": "older adult standing near chair balance practice",
                    },
                    {
                        "visual": "b_roll",
                        "prompt": "older adult stepping carefully beside a sturdy chair",
                        "keywords": "older adult stepping carefully sturdy chair",
                    },
                    {
                        "visual": "stock_photo",
                        "prompt": "Realistic photo of older adult pausing to reset posture before a short walk.",
                        "keywords": "older adult reset posture before short walk",
                    },
                    {
                        "visual": "google_photo",
                        "prompt": "Realistic photo of older adult practicing balance with a hand near the chair back.",
                        "keywords": "older adult balance hand near chair back",
                    },
                ],
            },
        ],
    }


def test_title_banner_numbering_errors_accepts_ascending_sequence():
    errors = _title_banner_numbering_errors(
        _script_data([1, 2, 3]),
        "ascending",
    )
    assert errors == []


def test_title_banner_numbering_errors_rejects_wrong_sequence():
    errors = _title_banner_numbering_errors(
        _script_data([3, 2, 1]),
        "ascending",
    )
    assert errors == [
        "title_banner section_number values must be [1, 2, 3] in section order; got [3, 2, 1]."
    ]


def test_script_prompt_includes_numbering_order_rules():
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[
            {"name": "hero", "instruction": "Make a hero thumbnail"},
            {"name": "data", "instruction": "Make a data thumbnail"},
        ],
        numbering_order="ascending",
    )
    assert "NUMBERED SECTION ORDER" in prompt
    assert "strict ascending order" in prompt
    assert 'visible title_banner text itself MUST include the matching item number' in prompt
    assert '"thumbnail_strategy"' in prompt
    assert '"hero": Make a hero thumbnail' in prompt
    assert '"data": Make a data thumbnail' in prompt
    assert '"info_slide"' in prompt
    assert '"info_card"' in prompt


def test_script_prompt_uses_tagged_sections():
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        numbering_order="ascending",
    )

    assert "<task>" in prompt
    assert "<rules>" in prompt
    assert "<schema>" in prompt
    assert "<input>" in prompt


def test_script_prompt_limits_visible_slot_hold_time():
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[
            {"name": "hero", "instruction": "Make a hero thumbnail"},
        ],
        numbering_order="ascending",
    )
    assert "ONE slot per distinct" in prompt
    assert "Every visible beat must stay under 5 seconds" in prompt
    assert "Aim for roughly 2.5-5 seconds per visible beat" in prompt
    # A long section must gain slots rather than hold one image longer, and
    # neighbouring slots must not repeat the same subject.
    assert "A long section needs MORE SLOTS, not a longer hold" in prompt
    assert "must be different from its neighbours" in prompt


def test_script_prompt_does_not_ask_llm_for_duration_fields():
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )

    assert '"estimated_duration_seconds"' not in prompt
    assert '"total_estimated_duration_seconds"' not in prompt
    assert "The system computes timing from narration words after generation." in prompt


def test_script_prompt_keeps_stage_one_focused_on_script_and_thumbnail_inputs():
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )

    assert '"thumbnail_strategy"' in prompt
    assert '"thumbnail_text"' in prompt
    assert '"thumbnail_brief"' in prompt
    assert '"title": "Video title' in prompt
    assert '"description": "YouTube description' in prompt
    assert '"tags": [' in prompt
    assert '"music_track"' not in prompt
    assert '"voice_variation"' not in prompt
    assert '"intra_transition"' not in prompt
    assert "TITLE FORMAT" in prompt
    assert "DESCRIPTION STYLE" in prompt
    assert "BACKGROUND MUSIC OPTIONS" not in prompt
    assert "VOICE VARIATIONS" not in prompt
    assert "INTRA-SECTION TRANSITIONS" not in prompt


def test_thumbnail_strategy_options_filter_to_video_type_allowed_subset():
    config = ChannelConfig(
        channel_name="Test",
        niche={
            "category": "Test",
            "focus": "Test",
            "audience": "Test",
            "content_style": "Test",
        },
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[
            {"name": "hero", "instruction": "Make a hero thumbnail"},
            {"name": "data", "instruction": "Make a data thumbnail"},
        ],
    )

    options = _thumbnail_strategy_options(config, ["hero"])

    assert options == [{"name": "hero", "instruction": "Make a hero thumbnail"}]


def test_script_prompt_excludes_disallowed_thumbnail_strategies():
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[
            {"name": "hero", "instruction": "Make a hero thumbnail"},
        ],
        numbering_order="ascending",
    )

    assert '"hero": Make a hero thumbnail' in prompt
    assert '"data": Make a data thumbnail' not in prompt


def test_validate_thumbnail_strategy_choice_rejects_disallowed_strategy():
    try:
        _validate_thumbnail_strategy_choice(
            "data",
            video_type="listicle",
            allowed_names=["hero"],
        )
    except ValueError as exc:
        assert (
            str(exc)
            == "Script selected thumbnail strategy 'data' not allowed for video type "
            "'listicle'. Allowed: hero"
        )
    else:
        raise AssertionError("Expected ValueError for disallowed thumbnail strategy")


def test_script_review_prompt_includes_numbering_order_rules():
    prompt = script_review_prompt("{}", numbering_order="ascending")
    assert "strict ascending order" in prompt
    assert "numbered banner title omits or mismatches its visible item" in prompt


def test_script_review_prompt_uses_tagged_sections():
    prompt = script_review_prompt("{}", numbering_order="ascending")
    assert "<task>" in prompt
    assert "<rules>" in prompt
    assert "<schema>" in prompt
    assert "<input>" in prompt


def test_script_revision_prompt_keeps_visual_structure_rules_simple():
    prompt = script_revision_prompt(
        script_json='{"sections": []}',
        feedback="Section 1 opener is wrong.",
        sections_range=(2, 4),
        duration_guard="Aim for about 110 words.",
    )

    assert "<task>" in prompt
    assert "<rules>" in prompt
    assert "CURRENT SCRIPT" in prompt
    assert "NEVER output text_only_slide" in prompt
    assert "Allowed visual_policy values are" in prompt
    assert "visual_policy is not the same as visual" in prompt
    assert "Keep enough slots in every revised section" in prompt


def test_generate_script_repairs_invalid_script_review_revision(monkeypatch, tmp_path):
    config = _script_stage_config()
    plan = _script_stage_plan()
    initial = _valid_generated_script()
    invalid_revision = _valid_generated_script()
    invalid_revision["sections"][0]["slots"] = [invalid_revision["sections"][0]["slots"][0]]
    repaired_revision = _valid_generated_script()
    operations = []
    saved = []

    async def fake_generate_json(*args, operation_label, **kwargs):
        operations.append(operation_label)
        if operation_label == "script_generate":
            return copy.deepcopy(initial)
        if operation_label == "script_revision":
            return copy.deepcopy(invalid_revision)
        if operation_label == "script_revision_validation_correction":
            return copy.deepcopy(repaired_revision)
        raise AssertionError(f"Unexpected operation_label: {operation_label}")

    async def fake_review_gate(**kwargs):
        revised = await kwargs["regenerate_fn"](
            kwargs["content"],
            {"feedback": "Fix the opener visual."},
        )
        return {
            "approved": True,
            "content": revised,
            "attempts": 2,
            "feedback": None,
            "scores": None,
            "flagged_for_review": False,
            "review_history": [],
        }

    monkeypatch.setattr(scripter.clients, "generate_json", fake_generate_json)
    monkeypatch.setattr(scripter, "review_gate", fake_review_gate)
    monkeypatch.setattr(scripter, "save_script", lambda ws, script: saved.append(script))

    script, result = asyncio.run(generate_script(config, plan, tmp_path))

    assert operations == [
        "script_generate",
        "script_revision",
        "script_revision_validation_correction",
    ]
    assert result["approved"] is True
    assert len(script.sections[0].slots) == 4
    assert saved


def test_script_structure_errors_reject_bad_backdrop_scene_structure():
    data = {
        "sections": [
            {
                "id": 1,
                "narration": "Notice calf tightness before you stand.",
                "slots": [
                    {
                        "visual": "stock_photo",
                        "prompt": "Realistic photo of older adult rubbing calf while standing",
                    },
                    {
                        "visual": "title_banner",
                        "prompt": "Realistic photo of older adult rubbing calf while standing",
                        "keywords": "older adult calf tightness standing photo",
                        "props": {"title": "Calf Tightness"},
                    },
                ],
            },
            {
                "id": 2,
                "narration": "Keep watching for more help.",
                "slots": [
                    {
                        "visual": "subscribe_cta",
                        "prompt": "",
                        "keywords": "",
                        "props": {"cta_text": "Subscribe"},
                    },
                    {
                        "visual": "stock_photo",
                        "prompt": "Realistic photo of older adult resting near a chair",
                    },
                ],
            },
            {
                "id": 3,
                "narration": "Wrap up.",
                "slots": [
                    {
                        "visual": "subscribe_cta",
                        "prompt": "Realistic photo of older adult smiling at home.",
                        "keywords": "older adult smiling at home photo",
                        "props": {"cta_text": "Subscribe"},
                    },
                ],
            },
        ]
    }

    errors = _structure_errors(data)

    assert "Section 1: title_banner must be the first slot in its section." in errors
    assert "Section 2 slot 1: subscribe_cta requires a background image prompt." in errors
    assert "Section 2 slot 1: subscribe_cta requires background image keywords." in errors
    assert "Script has 2 subscribe_cta sections; only one is allowed per video." in errors


def test_script_structure_errors_reject_missing_title_banner_media_fields():
    data = {
        "sections": [
            {
                "id": 1,
                "narration": "Steady your balance before you walk.",
                "slots": [
                    {
                        "visual": "title_banner",
                        "prompt": "",
                        "keywords": "",
                        "props": {"title": "Steady Before You Walk"},
                    },
                ],
            }
        ]
    }

    errors = _structure_errors(data)

    assert "Section 1 slot 1: title_banner requires a background image prompt." in errors
    assert "Section 1 slot 1: title_banner requires background image keywords." in errors


def test_generate_script_uses_single_validation_revision_loop(monkeypatch, tmp_path):
    config = _script_stage_config()
    plan = _script_stage_plan()
    invalid = _valid_generated_script()
    invalid["sections"][0]["slots"] = [
        {
            "visual": "subscribe_cta",
            "prompt": "",
            "keywords": "",
            "props": {"cta_text": "Subscribe"},
        },
        *invalid["sections"][0]["slots"],
    ]
    valid = _valid_generated_script()
    operations = []
    reviewed_content = []
    saved = []

    async def fake_generate_json(*args, operation_label, **kwargs):
        operations.append(operation_label)
        if operation_label == "script_generate":
            return copy.deepcopy(invalid)
        if operation_label == "script_validation_revision":
            assert "Script failed validation after generation." in args[0]
            assert "subscribe_cta requires a background image prompt" in args[0]
            return copy.deepcopy(valid)
        raise AssertionError(f"Unexpected operation_label: {operation_label}")

    async def fake_review_gate(**kwargs):
        reviewed_content.append(copy.deepcopy(kwargs["content"]))
        return {
            "approved": True,
            "content": kwargs["content"],
            "attempts": 1,
            "feedback": None,
            "scores": None,
            "flagged_for_review": False,
            "review_history": [],
        }

    monkeypatch.setattr(scripter.clients, "generate_json", fake_generate_json)
    monkeypatch.setattr(scripter, "review_gate", fake_review_gate)
    monkeypatch.setattr(scripter, "save_script", lambda ws, script: saved.append(script))

    script, result = asyncio.run(generate_script(config, plan, tmp_path))

    assert operations == ["script_generate", "script_validation_revision"]
    assert len(reviewed_content) == 1
    assert reviewed_content[0]["sections"][0]["estimated_duration_seconds"] > 0
    assert reviewed_content[0]["total_estimated_duration_seconds"] > 0
    assert saved
    assert script.title == "Test Title"
    assert script.description == "Test description"
    assert script.tags == ["t"]
    assert script.hook == "Hook"
    assert script.thumbnail_strategy == "hero"
    assert script.music_track == ""
    assert script.voice_variation == 0
    assert script.intra_transition == "fade"
    assert script.total_estimated_duration_seconds > 0
    assert result["approved"] is True


def test_generate_script_rejects_invalid_review_output_without_silent_normalization(monkeypatch, tmp_path):
    config = _script_stage_config()
    plan = _script_stage_plan()
    initial = _valid_generated_script()
    reviewed = _valid_generated_script()
    reviewed["sections"][0]["slots"] = [
        {
            "visual": "title_banner",
            "prompt": "",
            "keywords": "",
            "props": {"title": "Bad Banner", "section_number": 1},
        },
        *reviewed["sections"][0]["slots"],
    ]
    saved = []

    async def fake_generate_json(*args, operation_label, **kwargs):
        assert operation_label == "script_generate"
        return copy.deepcopy(initial)

    async def fake_review_gate(**kwargs):
        return {
            "approved": True,
            "content": copy.deepcopy(reviewed),
            "attempts": 1,
            "feedback": None,
            "scores": None,
            "flagged_for_review": False,
            "review_history": [],
        }

    monkeypatch.setattr(scripter.clients, "generate_json", fake_generate_json)
    monkeypatch.setattr(scripter, "review_gate", fake_review_gate)
    monkeypatch.setattr(scripter, "save_script", lambda ws, script: saved.append(script))

    with pytest.raises(ValueError, match="requires a background image prompt"):
        asyncio.run(generate_script(config, plan, tmp_path))

    assert saved == []


def test_script_review_prompt_rejects_overlong_visual_holds():
    prompt = script_review_prompt("{}", numbering_order="ascending")
    assert "longer than about 5 seconds" in prompt
    assert "add more visual slots or split the section" in prompt


def test_script_word_budget_errors_rejects_overlong_script():
    script_data = {
        "sections": [
            {"narration": "word " * 200, "slots": [{"visual": "google_photo", "prompt": "a photo", "keywords": "photo subject"}]},
            {"narration": "word " * 150, "slots": [{"visual": "google_photo", "prompt": "a photo", "keywords": "photo subject"}]},
        ]
    }

    errors, _ = _script_validation_errors(
        script_data,
        numbering_order=None,
        max_visual_hold_seconds=999.0,
        crossfade=0.3,
        timing_profile=script_timing_profile("gemini-2.5-flash-tts"),
        min_sections=2,
        min_words=103,
        max_words=140,
    )

    assert errors == [
        "script is too long: 350 words; must be at most 140"
    ]


def test_script_word_budget_errors_accepts_target_range():
    script_data = {
        "sections": [
            {"narration": "word " * 60, "slots": [{"visual": "google_photo", "prompt": "a photo", "keywords": "photo subject"}]},
            {"narration": "word " * 60, "slots": [{"visual": "google_photo", "prompt": "a photo", "keywords": "photo subject"}]},
        ]
    }

    errors, _ = _script_validation_errors(
        script_data,
        numbering_order=None,
        max_visual_hold_seconds=999.0,
        crossfade=0.3,
        timing_profile=script_timing_profile("gemini-2.5-flash-tts"),
        min_sections=2,
        min_words=103,
        max_words=140,
    )

    assert errors == []


def test_script_word_totals_counts_sections_and_words():
    script_data = {
        "sections": [
            {"narration": "word " * 80},
            {"narration": "word " * 68},
            {"narration": ""},
        ]
    }

    section_count, total_words = _script_word_totals(script_data)

    assert section_count == 3
    assert total_words == 148


def test_prompt_word_targets_match_model_aware_estimator():
    timing_profile = script_timing_profile("gemini-2.5-flash-tts")

    target_words, min_words_per_section, target_words_per_section = _prompt_word_targets(
        target_seconds=60,
        sections_range=[2, 3],
        timing_profile=timing_profile,
    )

    assert target_words == 112
    assert min_words_per_section == 37
    assert target_words_per_section == 45


def test_duration_window_is_looser_in_preview_mode():
    assert duration_window(60, preview_mode=False) == (54.0, 105)
    assert duration_window(60, preview_mode=True) == (54.0, 120)


def test_script_timing_profile_falls_back_for_unknown_model_name():
    profile = script_timing_profile("gemini-2.6-experimental-tts")

    assert profile.words_per_minute == 118
    assert profile.section_overhead_seconds == 3.5
    assert "gemini-2.6-experimental-tts" in profile.estimator_label


def test_script_visual_pacing_errors_rejects_under_slotted_section():
    timing_profile = script_timing_profile("gemini-2.5-flash-tts")
    script_data = {
        "sections": [
            {
                "id": 1,
                "narration": "word " * 70,
                "slots": [
                    {"visual": "google_photo", "prompt": "a photo", "keywords": "photo subject"},
                    {"visual": "stock_photo", "prompt": "a photo", "keywords": "photo subject"},
                ],
            }
        ]
    }

    errors, _ = _script_validation_errors(
        script_data,
        numbering_order=None,
        max_visual_hold_seconds=16.0,
        crossfade=0.3,
        timing_profile=timing_profile,
    )

    assert len(errors) == 1
    assert "Section 1" in errors[0]
    assert "2 slots" in errors[0]
    assert "16s" in errors[0]
    assert "needs at least" in errors[0]


def test_script_visual_pacing_issues_include_required_slot_budget():
    timing_profile = script_timing_profile("gemini-2.5-flash-tts")
    script_data = {
        "sections": [
            {
                "id": 2,
                "narration": "word " * 120,
                "slots": [
                    {"visual": "google_photo", "prompt": "a photo", "keywords": "photo subject"},
                    {"visual": "stock_photo", "prompt": "a photo", "keywords": "photo subject"},
                    {"visual": "b_roll"},
                ],
            }
        ]
    }

    _, issues = _script_validation_errors(
        script_data,
        numbering_order=None,
        max_visual_hold_seconds=16.0,
        crossfade=0.3,
        timing_profile=timing_profile,
    )

    assert len(issues) == 1
    assert issues[0]["section_id"] == 2
    assert issues[0]["current_slots"] == 3
    assert issues[0]["minimum_slots"] >= 4
    assert issues[0]["recommended_action"] in {"add_slots", "split_section"}
