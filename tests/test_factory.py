"""Tests for factory orchestration helpers."""

import asyncio
import contextlib
import logging

from click.testing import CliRunner

import factory as factory_module
from core.planner import plan_video, record_completed_video
from core.utils import ChannelConfig, Checkpoint, Script, ScriptSection, VisualSlot, save_checkpoint
from factory import (
    _apply_settings_overrides,
    _parse_allowed_review_failures,
    _parse_stage_spec,
    _review_log_entry,
    _split_overrides,
    main,
    run_pipeline,
)
from settings import settings


def test_review_log_entry_preserves_review_history_when_feedback_included():
    result = {
        "approved": False,
        "attempts": 1,
        "flagged_for_review": True,
        "feedback": "Raw list response: 1 rejected image(s)",
        "review_history": [
            {
                "attempt": 1,
                "image_results": [{"approved": False, "severity": "error"}],
            }
        ],
    }

    entry = _review_log_entry(result, include_feedback=True)

    assert entry == {
        "approved": False,
        "attempts": 1,
        "flagged": True,
        "feedback": "Raw list response: 1 rejected image(s)",
        "review_history": [
            {
                "attempt": 1,
                "image_results": [{"approved": False, "severity": "error"}],
            }
        ],
    }


def test_run_pipeline_persists_image_review_feedback_on_success(monkeypatch, tmp_path):
    logger = logging.getLogger("video_factory_test_pipeline")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

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
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    ws = tmp_path / "workspace"
    ws.mkdir()

    script = Script(
        title="Test",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="A test instructional section.",
                slots=[
                    VisualSlot(
                        visual="ai_illustration",
                        prompt="Correct seated toe tap posture with both heels down.",
                    )
                ],
            )
        ],
    )
    (ws / "plan.json").write_text(
        '{"topic": "Test topic", "video_type": "listicle"}',
        encoding="utf-8",
    )
    (ws / "script.json").write_text(
        script.model_dump_json(indent=2),
        encoding="utf-8",
    )
    save_checkpoint(
        ws,
        Checkpoint(
            channel="test",
            workspace_dir=str(ws),
            started_at="2026-04-10T19:29:17",
        ),
    )

    review_result = {
        "approved": True,
        "attempts": 2,
        "flagged_for_review": False,
        "feedback": "First pass caught extra feet; second pass fixed anatomy.",
        "review_history": [
            {
                "attempt": 1,
                "approved": False,
                "feedback": "Instructional anatomy mismatch.",
                "image_results": [
                    {
                        "section_id": 1,
                        "sub_image_index": 1,
                        "approved": False,
                        "severity": "error",
                        "issues": ["extra feet"],
                    }
                ],
            },
            {
                "attempt": 2,
                "approved": True,
                "feedback": "Fixed.",
                "image_results": [
                    {
                        "section_id": 1,
                        "sub_image_index": 1,
                        "approved": True,
                        "severity": "ok",
                        "issues": [],
                    }
                ],
            },
        ],
    }

    async def fake_source_images(*args, **kwargs):
        return review_result

    monkeypatch.setattr("factory.setup_logging", lambda channel_slug: logger)
    monkeypatch.setattr("factory.load_channel_config", lambda channel_slug, overrides=None, language=None: config)
    monkeypatch.setattr("factory._validate_raw_images", lambda ws, script, config: None)
    monkeypatch.setattr("factory.console.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.image_sourcer.source_images", fake_source_images)

    asyncio.run(
        run_pipeline(
            "test",
            start_from="image_source",
            stop_after="image_source",
            workspace_path=ws,
        )
    )

    updated = Checkpoint.model_validate_json(
        (ws / "checkpoint.json").read_text(encoding="utf-8")
    )

    assert updated.review_log["image_review"] == {
        "approved": True,
        "attempts": 2,
        "flagged": False,
        "feedback": "First pass caught extra feet; second pass fixed anatomy.",
        "review_history": review_result["review_history"],
    }


def test_split_overrides_routes_global_settings_override():
    config_overrides, plan_overrides, settings_overrides = _split_overrides([
        "video.target_duration_minutes=1",
        "plan.video_type=listicle",
        "plan.content_family=concept_explainer",
        "gemini_tts_model=gemini-2.5-flash-tts",
    ])

    assert config_overrides == ["video.target_duration_minutes=1"]
    assert plan_overrides == {
        "video_type": "listicle",
        "content_family": "concept_explainer",
    }
    assert settings_overrides == ["gemini_tts_model=gemini-2.5-flash-tts"]


def test_apply_settings_overrides_updates_settings(monkeypatch):
    monkeypatch.setattr(settings, "gemini_tts_model", "original")

    _apply_settings_overrides(["gemini_tts_model=gemini-2.5-flash-tts"])

    assert settings.gemini_tts_model == "gemini-2.5-flash-tts"


def test_parse_allowed_review_failures_normalizes_aliases():
    # `image_review` still parses -- the worker passes it -- but is dropped:
    # it is unwaivable, so a rejected visual can no longer ship.
    assert _parse_allowed_review_failures("image_review,thumbnail,final_review") == {
        "thumbnail_review",
        "final_review",
    }
    assert _parse_allowed_review_failures("script,thumbnail,final") == {
        "script_review",
        "thumbnail_review",
        "final_review",
    }


def test_parse_stage_spec_single_stage_returns_range():
    assert _parse_stage_spec("planning") == ("planning", "planning")
    assert _parse_stage_spec("..script") == (None, "script")
    assert _parse_stage_spec("script..final_review") == ("script", "final_review")


def _planning_config() -> ChannelConfig:
    return ChannelConfig(
        channel_name="Test",
        niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
        video_types={
            "listicle": {
                "enabled": True,
                "section_style": "s",
                "pacing": "p",
                "example": "e",
                "allowed_thumbnail_strategies": ["hero"],
            }
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


def test_plan_video_does_not_preselect_thumbnail_strategy(monkeypatch):
    async def fake_generate_json(*args, **kwargs):
        return {
            "topic": "Test topic",
            "video_type": "listicle",
            "angle": "Test angle",
        }

    monkeypatch.setattr("core.planner.load_topic_history", lambda channel: [
        {"video_type": "listicle", "title_format": "a", "description_style": "a", "thumbnail_strategy": "hero"}
    ])
    monkeypatch.setattr("core.planner.clients.generate_json", fake_generate_json)

    plan = asyncio.run(plan_video(_planning_config(), "test"))

    assert "thumbnail_strategy" not in plan
    assert "thumbnail_strategy_instruction" not in plan


def test_plan_video_enriches_business_strategy(monkeypatch):
    config = ChannelConfig(
        channel_name="Test",
        niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
        video_types={
            "listicle": {
                "enabled": True,
                "section_style": "s",
                "pacing": "p",
                "example": "e",
                "allowed_thumbnail_strategies": ["hero"],
            }
        },
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        business_strategy={
            "channel_goal": "Turn each video into a funnel.",
            "video_jobs": ["Get the click.", "Move to the lead magnet."],
            "content_families": [
                {
                    "name": "concept_explainer",
                    "planning_focus": "Warning signs plus next steps",
                    "example_topics": ["7 warning signs seniors ignore"],
                    "lead_magnet": "Doctor Visit Checklist",
                    "low_ticket_offer": "Symptom Tracker",
                    "cta_angle": "Offer a checklist for the next appointment.",
                }
            ],
            "mid_ticket_offer": "30-Day Mobility System",
            "later_offers": ["Newsletter"],
            "cta_rules": ["Primary CTA is always the lead magnet."],
            "launch_strategy": "Wait before launching channel two.",
        },
    )

    async def fake_generate_json(*args, **kwargs):
        return {
            "topic": "7 warning signs seniors ignore",
            "video_type": "listicle",
            "content_family": "concept_explainer",
            "angle": "Catch circulation problems early",
        }

    monkeypatch.setattr("core.planner.load_topic_history", lambda channel: [])
    monkeypatch.setattr("core.planner.clients.generate_json", fake_generate_json)

    plan = asyncio.run(plan_video(config, "test"))

    assert plan["content_family"] == "concept_explainer"
    assert plan["lead_magnet"] == "Doctor Visit Checklist"
    assert plan["low_ticket_offer"] == "Symptom Tracker"
    assert plan["mid_ticket_offer"] == "30-Day Mobility System"
    assert plan["cta_angle"] == "Offer a checklist for the next appointment."


def test_plan_video_selects_random_content_family(monkeypatch):
    config = ChannelConfig(
        channel_name="Test",
        niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
        video_types={
            "listicle": {
                "enabled": True,
                "section_style": "s",
                "pacing": "p",
                "example": "e",
                "allowed_thumbnail_strategies": ["hero"],
            }
        },
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        business_strategy={
            "channel_goal": "Turn each video into a funnel.",
            "video_jobs": ["Get the click.", "Move to the lead magnet."],
            "content_families": [
                {
                    "name": "practical_checklist",
                    "planning_focus": "Routine",
                    "example_topics": ["Do this routine"],
                    "lead_magnet": "Mobility Starter Guide",
                    "low_ticket_offer": "7-Day Mobility Reset",
                    "cta_angle": "Offer the routine guide.",
                },
                {
                    "name": "concept_explainer",
                    "planning_focus": "Warning signs",
                    "example_topics": ["7 warning signs seniors ignore"],
                    "lead_magnet": "Doctor Visit Checklist",
                    "low_ticket_offer": "Symptom Tracker",
                    "cta_angle": "Offer a checklist for the next appointment.",
                },
            ],
            "mid_ticket_offer": "30-Day Mobility System",
            "later_offers": ["Newsletter"],
            "cta_rules": ["Primary CTA is always the lead magnet."],
            "launch_strategy": "Wait before launching channel two.",
        },
    )
    selected_family = config.business_strategy.content_families[1]

    async def fake_generate_json(prompt, *args, **kwargs):
        assert "concept_explainer" in prompt
        assert "Doctor Visit Checklist" in prompt
        assert "practical_checklist" not in prompt
        return {
            "topic": "7 warning signs seniors ignore",
            "video_type": "listicle",
            "content_family": "concept_explainer",
            "angle": "Catch circulation problems early",
        }

    monkeypatch.setattr("core.planner.load_topic_history", lambda channel: [])
    monkeypatch.setattr("core.planner.random.choice", lambda families: selected_family)
    monkeypatch.setattr("core.planner.clients.generate_json", fake_generate_json)

    plan = asyncio.run(plan_video(config, "test"))

    assert plan["content_family"] == "concept_explainer"
    assert plan["lead_magnet"] == "Doctor Visit Checklist"


def test_plan_video_can_force_content_family(monkeypatch):
    config = ChannelConfig(
        channel_name="Test",
        niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
        video_types={
            "listicle": {
                "enabled": True,
                "section_style": "s",
                "pacing": "p",
                "example": "e",
                "allowed_thumbnail_strategies": ["hero"],
            }
        },
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        business_strategy={
            "channel_goal": "Turn each video into a funnel.",
            "video_jobs": ["Get the click.", "Move to the lead magnet."],
            "content_families": [
                {
                    "name": "practical_checklist",
                    "planning_focus": "Routine",
                    "example_topics": ["Do this routine"],
                    "lead_magnet": "Mobility Starter Guide",
                    "low_ticket_offer": "7-Day Mobility Reset",
                    "cta_angle": "Offer the routine guide.",
                },
                {
                    "name": "concept_explainer",
                    "planning_focus": "Warning signs",
                    "example_topics": ["7 warning signs seniors ignore"],
                    "lead_magnet": "Doctor Visit Checklist",
                    "low_ticket_offer": "Symptom Tracker",
                    "cta_angle": "Offer a checklist for the next appointment.",
                },
            ],
            "mid_ticket_offer": "30-Day Mobility System",
            "later_offers": ["Newsletter"],
            "cta_rules": ["Primary CTA is always the lead magnet."],
            "launch_strategy": "Wait before launching channel two.",
        },
    )

    async def fake_generate_json(prompt, *args, **kwargs):
        assert "concept_explainer" in prompt
        assert "Doctor Visit Checklist" in prompt
        assert "practical_checklist" not in prompt
        return {
            "topic": "7 warning signs seniors ignore",
            "video_type": "listicle",
            "content_family": "concept_explainer",
            "angle": "Catch circulation problems early",
        }

    monkeypatch.setattr("core.planner.load_topic_history", lambda channel: [])
    monkeypatch.setattr("core.planner.clients.generate_json", fake_generate_json)

    plan = asyncio.run(
        plan_video(config, "test", override_content_family="concept_explainer")
    )

    assert plan["content_family"] == "concept_explainer"
    assert plan["low_ticket_offer"] == "Symptom Tracker"


def test_record_completed_video_persists_final_package(monkeypatch):
    saved = {}
    monkeypatch.setattr("core.planner.load_topic_history", lambda channel: [])
    monkeypatch.setattr("core.planner.save_topic_history", lambda channel, history: saved.update({"channel": channel, "history": history}))

    record_completed_video(
        "test",
        topic="Test topic",
        video_type="listicle",
        output_video="test.mp4",
        title_format="a",
        description_style="a",
        thumbnail_strategy="hero",
        content_family="concept_explainer",
    )

    assert saved["channel"] == "test"
    assert saved["history"][0]["output_video"] == "test.mp4"
    assert saved["history"][0]["thumbnail_strategy"] == "hero"
    assert saved["history"][0]["content_family"] == "concept_explainer"


def test_run_pipeline_preview_remotion_skips_heavy_stages(monkeypatch, tmp_path):
    logger = logging.getLogger("video_factory_test_preview_pipeline")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

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
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    ws = tmp_path / "workspace"
    ws.mkdir()

    script = Script(
        title="Test",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="A test instructional section.",
                slots=[
                    VisualSlot(
                        visual="google_photo",
                        prompt="Correct seated toe tap posture with both heels down.",
                        keywords="toe taps",
                    )
                ],
            )
        ],
    )
    (ws / "plan.json").write_text(
        '{"topic": "Test topic", "video_type": "listicle"}',
        encoding="utf-8",
    )
    (ws / "script.json").write_text(
        script.model_dump_json(indent=2),
        encoding="utf-8",
    )
    save_checkpoint(
        ws,
        Checkpoint(
            channel="test",
            workspace_dir=str(ws),
            started_at="2026-04-11T10:00:00",
        ),
    )

    preview_calls = {}

    def fake_write_preview_manifest(script_arg, config_arg, workspace_arg):
        preview_calls["manifest"] = tmp_path / "remotion_public" / "preview_manifest.json"
        preview_calls["manifest"].parent.mkdir(parents=True, exist_ok=True)
        preview_calls["manifest"].write_text("{}", encoding="utf-8")
        return preview_calls["manifest"]

    def fake_launch_preview():
        preview_calls["launched"] = True

    async def fail_async(*args, **kwargs):
        raise AssertionError("Heavy stage should not run in preview mode")

    def fail_sync(*args, **kwargs):
        raise AssertionError("Heavy stage should not run in preview mode")

    monkeypatch.setattr("factory.setup_logging", lambda channel_slug: logger)
    monkeypatch.setattr("factory.load_channel_config", lambda channel_slug, overrides=None, language=None: config)
    monkeypatch.setattr("factory.console.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("factory._validate_ready_images", lambda *args, **kwargs: None)
    monkeypatch.setattr("factory._validate_audio_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr("factory.generate_log_html", lambda src, dest: dest)
    monkeypatch.setattr("core.processor.process_images", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.preview_remotion.write_preview_manifest", fake_write_preview_manifest)
    monkeypatch.setattr("core.preview_remotion.launch_remotion_studio", fake_launch_preview)
    monkeypatch.setattr("core.thumbnailer.create_thumbnail", fail_async)
    monkeypatch.setattr("core.render_sections.render_sections", fail_async)
    monkeypatch.setattr("core.assembler.assemble_video", fail_sync)
    monkeypatch.setattr("factory._run_final_review", fail_async)
    monkeypatch.setattr("factory.costs.initialize_cost_tracking", lambda **kwargs: None)
    monkeypatch.setattr("factory.costs.generate_run_id", lambda: "run-1")
    monkeypatch.setattr("factory.costs.write_estimate_reports", lambda *args, **kwargs: {"total": 0})
    monkeypatch.setattr("factory.costs.shutdown_cost_tracking", lambda: None)
    monkeypatch.setattr("factory.costs.bound_context", lambda **kwargs: contextlib.nullcontext())
    monkeypatch.setattr("factory.costs.cost_summary_line", lambda report: "Cost report unavailable.")

    asyncio.run(
        run_pipeline(
            "test",
            start_from="process",
            stop_after="final_review",
            workspace_path=ws,
            preview_remotion=True,
        )
    )

    checkpoint = Checkpoint.model_validate_json(
        (ws / "checkpoint.json").read_text(encoding="utf-8")
    )

    assert preview_calls["manifest"].name == "preview_manifest.json"
    assert preview_calls["launched"] is True
    assert "preview_remotion" in checkpoint.stages_completed
    assert "render_sections" not in checkpoint.stages_completed
    assert "assemble" not in checkpoint.stages_completed
    assert "final_review" not in checkpoint.stages_completed


def test_run_pipeline_preview_remotion_auto_allows_failed_script_and_image_review(monkeypatch, tmp_path):
    logger = logging.getLogger("video_factory_test_preview_auto_review_failures")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

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
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    ws = tmp_path / "workspace"
    ws.mkdir()

    (ws / "plan.json").write_text(
        '{"topic": "Preview topic", "video_type": "listicle"}',
        encoding="utf-8",
    )
    save_checkpoint(
        ws,
        Checkpoint(
            channel="test",
            workspace_dir=str(ws),
            started_at="2026-04-19T16:50:00",
        ),
    )

    script = Script(
        title="Preview Test",
        video_type="listicle",
        sections=[ScriptSection(id=1, narration="Narration", slots=[VisualSlot(visual="google_photo")])],
    )
    script_review_result = {
        "approved": False,
        "attempts": 1,
        "flagged_for_review": True,
        "feedback": "Script review rejected",
        "scores": {"overall_watchability": 6},
    }
    image_review_result = {
        "approved": False,
        "attempts": 1,
        "flagged_for_review": True,
        "feedback": "Image review rejected",
        "review_history": [{"attempt": 1}],
    }

    preview_calls = {}
    generate_script_calls = {}

    async def fake_generate_script(config_arg, plan_arg, ws_arg, allow_review_failure, preview_mode):
        generate_script_calls["allow_review_failure"] = allow_review_failure
        generate_script_calls["preview_mode"] = preview_mode
        assert plan_arg["topic"] == "Preview topic"
        return script, script_review_result

    async def fake_source_images(*args, **kwargs):
        raise factory_module.ReviewGateError("image_review", image_review_result)

    async def fake_source_audio(script_arg, *args, **kwargs):
        return script_arg

    def fake_write_preview_manifest(script_arg, config_arg, workspace_arg):
        preview_calls["manifest"] = tmp_path / "remotion_public" / "preview_manifest.json"
        preview_calls["manifest"].parent.mkdir(parents=True, exist_ok=True)
        preview_calls["manifest"].write_text("{}", encoding="utf-8")
        return preview_calls["manifest"]

    def fake_launch_preview():
        preview_calls["launched"] = True

    monkeypatch.setattr("factory.setup_logging", lambda channel_slug: logger)
    monkeypatch.setattr("factory.load_channel_config", lambda channel_slug, overrides=None, language=None: config)
    monkeypatch.setattr("factory.console.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("factory._validate_raw_images", lambda ws, script, config: None)
    monkeypatch.setattr("factory._validate_audio_outputs", lambda ws, script: None)
    monkeypatch.setattr("factory._validate_ready_images", lambda *args, **kwargs: None)
    monkeypatch.setattr("factory.generate_log_html", lambda src, dest: dest)
    monkeypatch.setattr("factory.costs.initialize_cost_tracking", lambda **kwargs: None)
    monkeypatch.setattr("factory.costs.generate_run_id", lambda: "run-1")
    monkeypatch.setattr("factory.costs.write_estimate_reports", lambda *args, **kwargs: {"total": 0})
    monkeypatch.setattr("factory.costs.shutdown_cost_tracking", lambda: None)
    monkeypatch.setattr("factory.costs.bound_context", lambda **kwargs: contextlib.nullcontext())
    monkeypatch.setattr("factory.costs.cost_summary_line", lambda report: "Cost report unavailable.")
    monkeypatch.setattr("core.scripter.generate_script", fake_generate_script)
    monkeypatch.setattr("core.image_sourcer.source_images", fake_source_images)
    monkeypatch.setattr("core.audio_sourcer.source_audio", fake_source_audio)
    monkeypatch.setattr("core.processor.process_images", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.validator.validate_script", lambda script_arg: [])
    monkeypatch.setattr("core.validator.run_validation", lambda name, issues: None)
    monkeypatch.setattr("core.preview_remotion.write_preview_manifest", fake_write_preview_manifest)
    monkeypatch.setattr("core.preview_remotion.launch_remotion_studio", fake_launch_preview)

    asyncio.run(
        run_pipeline(
            "test",
            start_from="script",
            stop_after="final_review",
            workspace_path=ws,
            preview_remotion=True,
        )
    )

    checkpoint = Checkpoint.model_validate_json(
        (ws / "checkpoint.json").read_text(encoding="utf-8")
    )

    assert generate_script_calls == {
        "allow_review_failure": True,
        "preview_mode": True,
    }
    assert preview_calls["launched"] is True
    assert checkpoint.review_log["script_review"]["flagged"] is True
    assert checkpoint.review_log["image_review"]["flagged"] is True
    assert checkpoint.last_error is None
    assert "preview_remotion" in checkpoint.stages_completed


def test_main_passes_preview_flag_to_run_pipeline(monkeypatch):
    runner = CliRunner()
    calls = {}

    async def fake_run_pipeline(*args, **kwargs):
        calls["preview_remotion"] = kwargs["preview_remotion"]
        calls["allow_review_failures"] = kwargs["allow_review_failures"]

    monkeypatch.setattr(factory_module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(factory_module, "console", type("ConsoleStub", (), {"print": staticmethod(lambda *args, **kwargs: None)})())
    monkeypatch.setattr(factory_module, "_apply_settings_overrides", lambda overrides: None)

    result = runner.invoke(
        main,
        [
            "--channel",
            "test",
            "--preview-remotion",
            "--allow-review-failures",
            "image_review,thumbnail,final_review",
        ],
    )

    assert result.exit_code == 0
    assert calls["preview_remotion"] is True
    # image_review is stripped at parse time. Preview mode re-adds it inside
    # run_pipeline, where it is safe: that mode exports no video.
    assert calls["allow_review_failures"] == {
        "thumbnail_review",
        "final_review",
    }


def test_main_single_stage_routes_through_run_pipeline(monkeypatch):
    runner = CliRunner()
    calls = {}

    async def fake_run_pipeline(*args, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(factory_module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(factory_module, "console", type("ConsoleStub", (), {"print": staticmethod(lambda *args, **kwargs: None)})())
    monkeypatch.setattr(factory_module, "_apply_settings_overrides", lambda overrides: None)

    result = runner.invoke(
        main,
        ["--channel", "test", "--stage", "planning"],
    )

    assert result.exit_code == 0
    assert calls["start_from"] == "planning"
    assert calls["stop_after"] == "planning"
    assert calls["preview_remotion"] is False


def test_main_rejects_preview_flag_with_stage():
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["--channel", "test", "--preview-remotion", "--stage", "process"],
    )

    assert result.exit_code != 0
    assert "--preview-remotion cannot be combined with --stage" in result.output


def test_run_pipeline_allows_failed_image_review(monkeypatch, tmp_path):
    logger = logging.getLogger("video_factory_test_allow_image")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

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
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    ws = tmp_path / "workspace"
    ws.mkdir()

    script = Script(
        title="Test",
        video_type="listicle",
        sections=[ScriptSection(id=1, narration="Narration", slots=[VisualSlot(visual="google_photo")])],
    )
    (ws / "plan.json").write_text(
        '{"topic": "Test topic", "video_type": "listicle"}',
        encoding="utf-8",
    )
    (ws / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")
    save_checkpoint(
        ws,
        Checkpoint(channel="test", workspace_dir=str(ws), started_at="2026-04-12T01:00:00"),
    )

    review_result = {
        "approved": False,
        "attempts": 2,
        "feedback": "Image review rejected",
        "flagged_for_review": True,
        "review_history": [{"attempt": 1}, {"attempt": 2}],
    }

    async def fake_source_images(*args, **kwargs):
        raise factory_module.ReviewGateError("image_review", review_result)

    async def fake_source_audio(script_arg, *args, **kwargs):
        return script_arg

    monkeypatch.setattr("factory.setup_logging", lambda channel_slug: logger)
    monkeypatch.setattr("factory.load_channel_config", lambda channel_slug, overrides=None, language=None: config)
    monkeypatch.setattr("factory._validate_raw_images", lambda ws, script, config: None)
    monkeypatch.setattr("factory._validate_audio_outputs", lambda ws, script: None)
    monkeypatch.setattr("factory.console.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.image_sourcer.source_images", fake_source_images)
    monkeypatch.setattr("core.audio_sourcer.source_audio", fake_source_audio)

    asyncio.run(
        run_pipeline(
            "test",
            start_from="image_source",
            stop_after="audio_source",
            workspace_path=ws,
            allow_review_failures={"image_review"},
        )
    )

    updated = Checkpoint.model_validate_json(
        (ws / "checkpoint.json").read_text(encoding="utf-8")
    )

    assert "image_source" in updated.stages_completed
    assert updated.review_log["image_review"]["flagged"] is True
    assert updated.last_error is None


def test_run_pipeline_cancels_audio_when_image_source_hard_fails(monkeypatch, tmp_path):
    logger = logging.getLogger("video_factory_test_cancel_audio")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

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
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    ws = tmp_path / "workspace"
    ws.mkdir()

    script = Script(
        title="Test",
        video_type="listicle",
        sections=[ScriptSection(id=1, narration="Narration", slots=[VisualSlot(visual="google_photo")])],
    )
    (ws / "plan.json").write_text(
        '{"topic": "Test topic", "video_type": "listicle"}',
        encoding="utf-8",
    )
    (ws / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")
    save_checkpoint(
        ws,
        Checkpoint(channel="test", workspace_dir=str(ws), started_at="2026-04-12T01:00:00"),
    )

    audio_started = asyncio.Event()
    audio_cancelled = asyncio.Event()

    async def fake_source_images(*args, **kwargs):
        await audio_started.wait()
        raise RuntimeError("image source exploded")

    async def fake_source_audio(script_arg, *args, **kwargs):
        audio_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            audio_cancelled.set()
            raise
        return script_arg

    monkeypatch.setattr("factory.setup_logging", lambda channel_slug: logger)
    monkeypatch.setattr("factory.load_channel_config", lambda channel_slug, overrides=None, language=None: config)
    monkeypatch.setattr("factory.console.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.image_sourcer.source_images", fake_source_images)
    monkeypatch.setattr("core.audio_sourcer.source_audio", fake_source_audio)

    try:
        asyncio.run(
            run_pipeline(
                "test",
                start_from="image_source",
                stop_after="audio_source",
                workspace_path=ws,
            )
        )
    except RuntimeError as exc:
        assert "Media sourcing failed for stage(s): image_source" == str(exc)
    else:
        raise AssertionError("Expected media sourcing failure")

    assert audio_cancelled.is_set()


def test_run_pipeline_allows_failed_thumbnail_review(monkeypatch, tmp_path):
    logger = logging.getLogger("video_factory_test_allow_thumbnail")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

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
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "thumbnail.png").write_bytes(b"thumb")

    script = Script(
        title="Test",
        video_type="listicle",
        sections=[ScriptSection(id=1, narration="Narration", slots=[VisualSlot(visual="google_photo")])],
    )
    (ws / "plan.json").write_text(
        '{"topic": "Test topic", "video_type": "listicle"}',
        encoding="utf-8",
    )
    (ws / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")
    save_checkpoint(
        ws,
        Checkpoint(channel="test", workspace_dir=str(ws), started_at="2026-04-12T01:00:00"),
    )

    review_result = {
        "approved": False,
        "attempts": 2,
        "feedback": "Thumbnail review rejected",
        "flagged_for_review": True,
        "review_history": [{"attempt": 1}, {"attempt": 2}],
    }

    async def fake_create_thumbnail(*args, **kwargs):
        raise factory_module.ReviewGateError("thumbnail_review", review_result)

    monkeypatch.setattr("factory.setup_logging", lambda channel_slug: logger)
    monkeypatch.setattr("factory.load_channel_config", lambda channel_slug, overrides=None, language=None: config)
    monkeypatch.setattr("factory.console.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("factory._validate_ready_images", lambda ws, script, config: None)
    monkeypatch.setattr("factory._validate_audio_outputs", lambda ws, script: None)
    monkeypatch.setattr("core.thumbnailer.create_thumbnail", fake_create_thumbnail)
    # Takes the channel config too: expected dimensions are per-channel, since
    # a 9:16 channel publishes a vertical thumbnail.
    monkeypatch.setattr(
        "core.validator.validate_thumbnail", lambda path, config=None: []
    )
    monkeypatch.setattr("core.validator.run_validation", lambda name, issues: None)

    asyncio.run(
        run_pipeline(
            "test",
            start_from="thumbnail",
            stop_after="thumbnail",
            workspace_path=ws,
            allow_review_failures={"thumbnail_review"},
        )
    )

    updated = Checkpoint.model_validate_json(
        (ws / "checkpoint.json").read_text(encoding="utf-8")
    )

    assert "thumbnail" in updated.stages_completed
    assert updated.review_log["thumbnail_review"]["flagged"] is True
    assert updated.last_error is None


def test_run_pipeline_allows_failed_final_review(monkeypatch, tmp_path):
    logger = logging.getLogger("video_factory_test_allow_final")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

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
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    ws = tmp_path / "workspace"
    ws.mkdir()

    script = Script(
        title="Test",
        video_type="listicle",
        sections=[ScriptSection(id=1, narration="Narration", slots=[VisualSlot(visual="google_photo")])],
    )
    (ws / "plan.json").write_text(
        '{"topic": "Test topic", "video_type": "listicle"}',
        encoding="utf-8",
    )
    (ws / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")
    save_checkpoint(
        ws,
        Checkpoint(channel="test", workspace_dir=str(ws), started_at="2026-04-12T01:00:00"),
    )

    review_result = {
        "approved": False,
        "attempts": 1,
        "feedback": "Final review rejected",
        "flagged_for_review": True,
        "review_history": [{"attempt": 1}],
    }

    async def fake_final_review(script_arg, config_arg, workspace_arg, checkpoint_arg):
        checkpoint_arg.review_log["final_review"] = _review_log_entry(
            review_result,
            include_feedback=True,
        )
        checkpoint_arg.last_error = "Final review rejected"
        save_checkpoint(workspace_arg, checkpoint_arg)
        raise factory_module.ReviewGateError("final_review", review_result)

    monkeypatch.setattr("factory.setup_logging", lambda channel_slug: logger)
    monkeypatch.setattr("factory.load_channel_config", lambda channel_slug, overrides=None, language=None: config)
    monkeypatch.setattr("factory.console.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("factory._run_final_review", fake_final_review)

    asyncio.run(
        run_pipeline(
            "test",
            start_from="final_review",
            stop_after="final_review",
            workspace_path=ws,
            allow_review_failures={"final_review"},
        )
    )

    updated = Checkpoint.model_validate_json(
        (ws / "checkpoint.json").read_text(encoding="utf-8")
    )

    assert "final_review" in updated.stages_completed
    assert updated.review_log["final_review"]["flagged"] is True
    assert updated.last_error is None
