"""Video Factory — Main orchestrator + CLI entry point.

Usage:
    python factory.py --channel demo_channel
    python factory.py --channel demo_channel --stage script
    python factory.py --channel demo_channel --fixtures replay
    python factory.py --channel demo_channel --set video.target_duration_minutes=1
    python factory.py --channel demo_channel --preview-remotion --set video.target_duration_minutes=1
    python factory.py --channel demo_channel --preview-remotion --allow-review-failures thumbnail,final_review
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel

from core.reviewer import ReviewGateError
import prompts
from core import costs
from tools.log_viewer import generate_image_source_html_report, generate_log_html
from core.planner import plan_video, record_completed_video
from core.utils import (
    ChannelConfig,
    Checkpoint,
    Script,
    create_workspace,
    find_latest_workspace,
    load_channel_config,
    load_checkpoint,
    load_script,
    save_checkpoint,
    setup_logging,
    find_output_video,
    parse_override_value,
)
from settings import settings

console = Console()


def _output_video_name(script: Script) -> str:
    """Build descriptive video filename: YYYY-MM-DD_HHMMSS_title-slug.mp4"""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = re.sub(r"[^\w\s-]", "", script.title.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:60]
    return f"{timestamp}_{slug}.mp4"


def _output_video_path(workspace: Path, script: Script) -> Path:
    """Build a unique descriptive output path inside the workspace."""
    candidate = workspace / _output_video_name(script)
    if not candidate.exists():
        return candidate

    suffix = 2
    while True:
        next_candidate = candidate.with_name(f"{candidate.stem}-{suffix}{candidate.suffix}")
        if not next_candidate.exists():
            return next_candidate
        suffix += 1


# All pipeline stages in order
STAGES = [
    "planning",
    "script",
    "image_source",
    "audio_source",
    "process",
    "render_sections",
    "assemble",
    "thumbnail",
    "final_review",
]

logger = logging.getLogger("video_factory")

_ALLOWED_REVIEW_FAILURE_GATES = {
    "script_review",
    "image_review",
    "thumbnail_review",
    "final_review",
}
_REVIEW_FAILURE_GATE_ALIASES = {
    "script": "script_review",
    "image": "image_review",
    "thumbnail": "thumbnail_review",
    "final": "final_review",
}


def _review_log_entry(
    result: dict,
    *,
    include_scores: bool = False,
    include_feedback: bool = False,
) -> dict:
    entry = {
        "approved": result.get("approved", False),
        "attempts": result.get("attempts", 0),
        "flagged": result.get("flagged_for_review", False),
    }
    if include_scores:
        entry["scores"] = result.get("scores")
    if include_feedback:
        entry["feedback"] = result.get("feedback")
        if result.get("review_history") is not None:
            entry["review_history"] = result["review_history"]
    return entry


# Hex highlights the reviewer can be asked about by name. A vision model judges
# "red" far more reliably than "#E23B3B", so the name leads and the hex backs it
# up. Anything unlisted is described by hex alone rather than guessed at.
_HIGHLIGHT_COLOUR_NAMES = {
    "#ffe500": "yellow",
    "#ffd23f": "yellow",
    "#ffff00": "yellow",
    "#e23b3b": "red",
    "#ff0000": "red",
    "#ff6b35": "orange",
    "#00d4ff": "cyan",
}


def _caption_highlight_name(config: ChannelConfig) -> str:
    """Describe this channel's active-word highlight for the review gate."""
    raw = str(
        (config.style.subtitle_highlight or {}).get("color", "")
    ).strip().lower()
    if not raw:
        return "yellow"
    name = _HIGHLIGHT_COLOUR_NAMES.get(raw)
    return f"{name} ({raw})" if name else raw


def _parse_allowed_review_failures(raw: str | None) -> set[str]:
    if not raw:
        return set()

    allowed: set[str] = set()
    for item in raw.split(","):
        token = item.strip().lower()
        if not token:
            continue
        gate_name = _REVIEW_FAILURE_GATE_ALIASES.get(token, token)
        if gate_name not in _ALLOWED_REVIEW_FAILURE_GATES:
            valid = ", ".join(sorted(_ALLOWED_REVIEW_FAILURE_GATES | set(_REVIEW_FAILURE_GATE_ALIASES)))
            raise click.BadParameter(
                f"Unknown review gate '{token}'. Valid values: {valid}",
                param_hint="--allow-review-failures",
            )
        allowed.add(gate_name)
    return allowed


def _log_allowed_review_failure(gate_name: str) -> None:
    logger.warning(
        f"[{gate_name}] Continuing despite failed review because "
        f"--allow-review-failures includes {gate_name}"
    )



def _next_report_path(ws: Path, stage: str) -> Path:
    """Return an incremental report path like reports/image_source_1.json."""
    reports_dir = ws / "reports"
    reports_dir.mkdir(exist_ok=True)
    n = 1
    while (reports_dir / f"{stage}_{n}.json").exists():
        n += 1
    return reports_dir / f"{stage}_{n}.json"



def _validate_raw_images(workspace: Path, script: Script, config: ChannelConfig) -> None:
    from core.validator import validate_raw_images, run_validation
    run_validation("raw_images", validate_raw_images(workspace, script, config))


def _validate_ready_images(workspace: Path, script: Script, config: ChannelConfig) -> None:
    from core.validator import validate_ready_images, run_validation
    run_validation("ready_images", validate_ready_images(workspace, script, config))


def _validate_audio_outputs(workspace: Path, script: Script) -> None:
    from core.validator import validate_audio, run_validation
    run_validation("audio", validate_audio(workspace, script))


async def run_pipeline(
    channel_slug: str,
    *,
    start_from: str | None = None,
    stop_after: str | None = None,
    overrides: list[str] | None = None,
    plan_overrides: dict[str, str] | None = None,
    workspace_path: Path | None = None,
    preview_remotion: bool = False,
    allow_review_failures: set[str] | None = None,
    language: str | None = None,
) -> None:
    """Run the full video factory pipeline."""
    logger = setup_logging(channel_slug)
    allowed_review_failures = set(allow_review_failures or ())
    if preview_remotion:
        allowed_review_failures.update({"script_review", "image_review"})

    # A language variant swaps the narration language, the voice and the
    # script instructions together; they cannot move independently.
    config = load_channel_config(
        channel_slug, overrides=overrides, language=language
    )
    logger.info(
        f"Channel: {config.channel_name} (script language: {config.language})"
    )

    # Explicit workspace path overrides all auto-detection
    if workspace_path:
        ws = workspace_path
        checkpoint = load_checkpoint(ws)
        if checkpoint:
            logger.info(f"Using workspace: {ws.name}")
        else:
            raise RuntimeError(f"No checkpoint in workspace: {ws}")
    else:
        # Starting mid-pipeline requires an existing workspace
        needs_existing_ws = start_from and start_from != STAGES[0]

        checkpoint = None
        if needs_existing_ws:
            ws = find_latest_workspace(channel_slug)
            if ws:
                checkpoint = load_checkpoint(ws)
                if checkpoint:
                    logger.info(f"Resuming workspace: {ws.name}")
                else:
                    if start_from and start_from != STAGES[0]:
                        raise RuntimeError(
                            f"No checkpoint in latest workspace — can't start from '{start_from}'"
                        )
                    logger.warning("No checkpoint found, starting fresh")
                    ws = create_workspace(channel_slug)
            else:
                if start_from and start_from != STAGES[0]:
                    raise RuntimeError("No workspace found — can't start mid-pipeline")
                logger.warning("No workspace found, starting fresh")
                ws = create_workspace(channel_slug)
        else:
            ws = create_workspace(channel_slug)

    if not checkpoint:
        checkpoint = Checkpoint(
            channel=channel_slug,
            started_at=datetime.now().isoformat(),
            workspace_dir=str(ws),
        )
    if not checkpoint.run_id:
        checkpoint.run_id = costs.generate_run_id()
    save_checkpoint(ws, checkpoint)

    fixture_mode = "off"
    try:
        from clients import get_mode
        fixture_mode = get_mode().value
    except Exception:
        fixture_mode = "off"
    costs.initialize_cost_tracking(
        workspace=ws,
        run_id=checkpoint.run_id,
        channel=channel_slug,
        fixture_mode=fixture_mode,
    )

    # Per-workspace log file (feeds the HTML viewer)
    ws_log_path = ws / "pipeline.log"
    ws_handler = logging.FileHandler(ws_log_path, encoding="utf-8")
    ws_handler.setLevel(logging.DEBUG)
    ws_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(ws_handler)
    estimate_report: dict[str, Any] | None = None
    outputs_finalized = False

    completed = set(checkpoint.stages_completed)
    stage_start_time: dict[str, datetime] = {}

    # A checkpoint written before completion became atomic can claim
    # image_source is done while a required beat has no file. Resuming such a
    # run skipped sourcing and failed the same validation every time, with no
    # way forward short of deleting the workspace. Re-open the stage instead:
    # it is the only stage that can repair the assets.
    if "image_source" in completed:
        try:
            _validate_raw_images(ws, load_script(ws), config)
        except FileNotFoundError:
            pass  # no script on disk yet; nothing to check against
        except Exception as stale:
            logger.warning(
                f"checkpoint claims image_source is complete but its assets are "
                f"not on disk ({stale}); re-running the stage"
            )
            completed.discard("image_source")
            checkpoint.stages_completed = list(completed)
            save_checkpoint(ws, checkpoint)

    # Build the set of stages within the requested range
    start_idx = STAGES.index(start_from) if start_from else 0
    stop_idx = STAGES.index(stop_after) if stop_after else len(STAGES) - 1
    stages_in_range = set(STAGES[start_idx:stop_idx + 1])

    def should_run(stage: str) -> bool:
        return stage in stages_in_range and stage not in completed

    def start_stage(stage: str) -> None:
        stage_start_time[stage] = datetime.now()
        checkpoint.current_stage = stage
        save_checkpoint(ws, checkpoint)
        logger.info(f"[stage] {stage} started")

    def complete_stage(stage: str, ended: datetime | None = None) -> None:
        completed.add(stage)
        checkpoint.stages_completed = list(completed)
        checkpoint.current_stage = stage
        checkpoint.last_error = None
        started = stage_start_time.get(stage)
        if started:
            ended = ended or datetime.now()
            duration = (ended - started).total_seconds()
            checkpoint.stage_timings[stage] = {
                "started": started.isoformat(),
                "completed": ended.isoformat(),
                "duration_seconds": round(duration, 1),
            }
            logger.info(f"[stage] {stage} completed in {duration:.1f}s")
        save_checkpoint(ws, checkpoint)

    def _finalize_total_duration() -> None:
        if checkpoint.started_at:
            total = (datetime.now() - datetime.fromisoformat(checkpoint.started_at)).total_seconds()
            checkpoint.total_duration_seconds = round(total, 1)
            save_checkpoint(ws, checkpoint)

    def _finalize_outputs() -> dict[str, Any] | None:
        nonlocal estimate_report, outputs_finalized
        if outputs_finalized:
            return estimate_report
        outputs_finalized = True

        _finalize_total_duration()
        try:
            estimate_report = costs.write_estimate_reports(ws, checkpoint.model_dump())
        except Exception as cost_err:
            logger.error(f"Cost report generation failed: {cost_err}")

        try:
            ws_handler.flush()
        except Exception as flush_err:
            logger.error(f"Pipeline log flush failed: {flush_err}")
        if ws_handler in logger.handlers:
            logger.removeHandler(ws_handler)
        try:
            ws_handler.close()
        except Exception as close_err:
            logger.error(f"Pipeline log close failed: {close_err}")

        try:
            html_path = generate_log_html(ws_log_path, ws / "pipeline.html")
            logger.info(f"Log viewer: {html_path}")
        except Exception as html_err:
            logger.error(f"Log viewer generation failed: {html_err}")

        if estimate_report:
            logger.info(f"Cost report: {ws / 'reports' / 'cost_estimate.json'}")

        try:
            costs.shutdown_cost_tracking()
        except Exception as shutdown_err:
            logger.error(f"Cost tracker shutdown failed: {shutdown_err}")
        return estimate_report

    def should_stop(stage: str) -> bool:
        stopping = stop_after and stage == stop_after
        if stopping:
            _finalize_total_duration()
        return bool(stopping and stage == "planning")

    def fail_pipeline(message: str) -> None:
        logger.error(message)
        checkpoint.last_error = message
        save_checkpoint(ws, checkpoint)
        _finalize_outputs()
        raise RuntimeError(message)

    if should_run("planning"):
        start_stage("planning")
        console.print(Panel("Stage 0: Planning", style="bold cyan"))

        override_type = (plan_overrides or {}).get("video_type")
        override_content_family = (plan_overrides or {}).get("content_family")
        override_topic = (plan_overrides or {}).get("topic")
        with costs.bound_context(stage="planning"):
            plan = await plan_video(
                config,
                channel_slug,
                override_type=override_type,
                override_content_family=override_content_family,
                override_topic=override_topic,
            )
        checkpoint.topic = plan["topic"]
        checkpoint.video_type = plan["video_type"]

        # Establish what is actually true about the topic right now. The
        # scripter already consumes plan["research_context"]; without this it
        # writes from training data and can describe a completed transfer as a
        # possible one.
        from core.researcher import research_topic, save_research
        with costs.bound_context(stage="planning"):
            research = await research_topic(
                topic=plan["topic"],
                angle=plan.get("angle", ""),
                config=config,
            )
        # Match Analysis engines anchor the script to a verified scoreline and
        # event timeline before the general brief. Gated on the channel
        # declaring match_footage, so engines without it -- Football News --
        # never reach this and are unaffected.
        match_brief = ""
        if getattr(config, "match_footage", None) and config.match_footage.get(
            "enabled"
        ):
            from core.match_data import fetch_match_facts, research_brief

            with costs.bound_context(stage="planning"):
                facts = await fetch_match_facts(plan["topic"])
            match_brief = research_brief(facts)
            plan["match_facts"] = {
                "identified": facts.identified,
                "label": facts.label,
                "provider": facts.provider,
                "note": facts.note,
                "moments": [
                    {"label": m.label, "minute": m.minute, "description": m.description}
                    for m in facts.to_moments()
                ],
            }
            logger.info(
                f"match analysis: "
                f"{facts.label or 'unidentified'} "
                f"({len(facts.major_events())} major event(s))"
            )

        # Engine sub-mode. Horror uses it to decide how truth is handled --
        # a true case is sourced and claim-tagged, folklore is attributed,
        # fiction is never dressed as documentary. Engines that do not set
        # plan.story_type (Football News) get an empty prefix and are
        # unaffected.
        story_type = str(plan.get("story_type") or "").strip()
        story_directive = (
            f"STORY TYPE: {story_type}\n"
            "Follow the story-type rules in the channel's script instructions "
            "exactly. Do not present fiction, folklore or an unverified claim "
            "as an established fact.\n"
            if story_type
            else ""
        )

        if research.get("brief"):
            plan["research_context"] = (
                story_directive
                + (f"{match_brief}\n\n" if match_brief else "")
                + research["brief"]
            )
            plan["research_entities"] = research.get("key_entities", [])
            save_research(ws, research)
        else:
            # No verified brief. Silence here is dangerous: the model fills the
            # gap from training data and states old or invented transfers as
            # today's news, which is exactly the failure research exists to
            # prevent. Say so explicitly instead.
            plan["research_context"] = story_directive + (
                "NO VERIFIED CURRENT INFORMATION IS AVAILABLE for this topic.\n"
                "You therefore MUST NOT state any specific recent transfer, "
                "scoreline, injury, appointment or sacking as fact, and MUST NOT "
                "present anything as breaking news, 'today', or 'the latest'.\n"
                "Write the video as durable background instead: established "
                "history, rules, and long-settled facts about the subject that do "
                "not depend on this week's events. Prefer facts that were already "
                "true a year ago and will still be true next year."
            )
            plan["research_entities"] = []
            if research.get("rejected_reason"):
                logger.warning(
                    f"Scripting without a verified brief: "
                    f"{research['rejected_reason']}"
                )

        (ws / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        complete_stage("planning")
        if should_stop("planning"):
            logger.info("Stopped after planning")
            _finalize_outputs()
            return
    else:
        plan = json.loads((ws / "plan.json").read_text(encoding="utf-8"))

    if should_run("script"):
        start_stage("script")
        console.print(Panel("Stage 1: Script Generation + Review", style="bold cyan"))
        from core.scripter import generate_script
        try:
            with costs.bound_context(stage="script"):
                script, review_result = await generate_script(
                    config,
                    plan,
                    ws,
                    allow_review_failure="script_review" in allowed_review_failures,
                    preview_mode=preview_remotion,
                )
        except ReviewGateError as e:
            checkpoint.review_log["script_review"] = _review_log_entry(
                e.result,
                include_scores=True,
                include_feedback=True,
            )
            checkpoint.last_error = str(e)
            save_checkpoint(ws, checkpoint)
            _finalize_outputs()
            raise
        from core.validator import validate_script, run_validation
        run_validation("script", validate_script(script))
        checkpoint.review_log["script_review"] = _review_log_entry(
            review_result,
            include_scores=True,
            include_feedback=not review_result.get("approved", False),
        )
        if not review_result.get("approved", False):
            _log_allowed_review_failure("script_review")
            checkpoint.last_error = None
        complete_stage("script")
        if should_stop("script"):
            logger.info("Stopped after script")
            _finalize_outputs()
            return
    else:
        script = load_script(ws)

    # Match footage, before media sourcing so that a slot backed by an
    # authorized clip is skipped by the image sourcer rather than sourced
    # twice. Gated on the channel declaring match_footage, so Football News
    # never reaches it. Every failure inside leaves the slot without a clip,
    # which the photo pipeline then covers.
    if (
        should_run("image_source")
        and getattr(config, "match_footage", None)
        and config.match_footage.enabled
    ):
        # Imported before the try so the handler below can name it even if the
        # rest of the stage fails to import.
        from core.footage_stage import FootageAnalysisError

        try:
            from core.footage import MatchMoment
            from core.footage_stage import analysis_gate, build_match_clips

            moments = [
                MatchMoment(
                    label=m.get("label", ""),
                    minute=int(m.get("minute", 0)),
                    description=m.get("description", ""),
                )
                for m in (plan.get("match_facts", {}) or {}).get("moments", [])
            ]
            # MATCH_FOOTAGE_DIR wins over the channel config so an operator
            # can point a run at a folder without editing JSON.
            footage_dir_raw = (
                os.environ.get("MATCH_FOOTAGE_DIR", "").strip()
                or (config.match_footage.footage_dir or "").strip()
            )
            footage_path = Path(footage_dir_raw) if footage_dir_raw else None
            # Supplied footage must analyse successfully before the run
            # continues. Building a video around a guessed timestamp is worse
            # than stopping, so this raises rather than degrading.
            gate_ok, gate_reason = analysis_gate(footage_path)
            logger.info(f"clip analysis gate: {gate_reason}")
            if not gate_ok:
                # Deliberately not swallowed by the handler below: the run is
                # supposed to stop when supplied footage cannot be analysed,
                # rather than quietly producing a photo video the operator did
                # not ask for.
                raise FootageAnalysisError(
                    f"match footage was supplied but no event could be "
                    f"identified in it ({gate_reason}). Declare the timestamp "
                    f"in a sidecar (event_offset_seconds) or enable the vision "
                    f"provider with CLIP_ANALYSIS_VISION=1."
                )

            placements = build_match_clips(
                workspace=ws,
                script=script,
                moments=moments,
                footage_dir=footage_path,
                allow_licensed_stock=config.match_footage.allow_licensed_stock,
                target_size=tuple(config.video.resolution),
                fps=config.video.fps,
            )
            if placements:
                console.print(
                    Panel(
                        f"Match footage: {len(placements)} moment clip(s) cut "
                        f"from authorized footage",
                        style="bold cyan",
                    )
                )
        except FootageAnalysisError:
            raise
        except Exception as exc:
            # Footage is an enhancement. Losing it must never cost the run.
            logger.warning(
                f"match footage stage failed ({exc}); continuing with the "
                f"photo pipeline"
            )

    run_images = should_run("image_source")
    run_audio = should_run("audio_source")

    if run_images or run_audio:
        console.print(Panel("Stage 2: Media Sourcing (parallel)", style="bold cyan"))
        if run_images:
            start_stage("image_source")
        if run_audio:
            start_stage("audio_source")

        stage_end_time: dict[str, datetime] = {}

        async def _timed(name: str, coro):
            with costs.bound_context(stage=name):
                result = await coro
            stage_end_time[name] = datetime.now()
            return result

        tasks = []
        if run_images:
            from core.image_sourcer import source_images
            tasks.append(("image_source", _timed("image_source", source_images(script, config, ws))))
        if run_audio:
            from core.audio_sourcer import source_audio
            tasks.append(("audio_source", _timed("audio_source", source_audio(script, config, ws))))

        task_objects = {
            stage_name: asyncio.create_task(coro, name=stage_name)
            for stage_name, coro in tasks
        }
        pending = set(task_objects.values())
        failed_stages: list[str] = []

        def _validate_stage_outputs(stage: str) -> None:
            """Check a sourcing stage's required assets are actually on disk.

            Reads `script` from the enclosing scope on purpose: audio_source
            returns a rebuilt script, and image_source mutates its own in
            place, so both validations must see the current one.
            """
            if stage == "image_source":
                _validate_raw_images(ws, script, config)
            elif stage == "audio_source":
                _validate_audio_outputs(ws, script)

        def _complete_when_outputs_exist(stage: str) -> bool:
            """Mark a stage complete only if its outputs survived validation.

            A stage used to be recorded as complete and validated afterwards.
            When validation then failed, the checkpoint already said the stage
            was done -- so every resume skipped the work and failed at the same
            line, with `last_error` cleared by the completion that preceded it.
            """
            try:
                _validate_stage_outputs(stage)
            except Exception as validation_error:
                logger.error(
                    f"{stage} finished with incomplete output: {validation_error}"
                )
                checkpoint.last_error = str(validation_error)
                checkpoint.current_stage = stage
                save_checkpoint(ws, checkpoint)
                failed_stages.append(stage)
                return False
            complete_stage(stage, ended=stage_end_time.get(stage))
            return True

        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            hard_failure = False
            for finished in done:
                stage_name = next(
                    name for name, task in task_objects.items()
                    if task is finished
                )
                try:
                    result = finished.result()
                except asyncio.CancelledError:
                    continue
                except Exception as result:
                    if stage_name == "image_source" and isinstance(result, ReviewGateError):
                        checkpoint.review_log["image_review"] = _review_log_entry(
                            result.result,
                            include_feedback=True,
                        )
                        if "image_review" in allowed_review_failures:
                            _log_allowed_review_failure("image_review")
                            checkpoint.last_error = None
                            # Waiving the review does not waive the assets:
                            # the beats still have to exist.
                            if not _complete_when_outputs_exist(stage_name):
                                hard_failure = True
                            continue
                    logger.error(f"{stage_name} failed: {result}")
                    checkpoint.last_error = str(result)
                    failed_stages.append(stage_name)
                    hard_failure = True
                    continue

                if stage_name == "image_source":
                    checkpoint.review_log["image_review"] = _review_log_entry(
                        result,
                        include_feedback=True,
                    )
                elif stage_name == "audio_source" and isinstance(result, Script):
                    script = result
                if not _complete_when_outputs_exist(stage_name):
                    hard_failure = True

            if hard_failure and pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                pending.clear()

        if failed_stages:
            fail_pipeline("Media sourcing failed for stage(s): " + ", ".join(failed_stages))

        # Both stages validated their own outputs before being marked
        # complete, so reaching here means the assets are on disk.

        if should_stop("image_source") or should_stop("audio_source"):
            logger.info("Stopped after sourcing")
            _finalize_outputs()
            return
    else:
        script = load_script(ws)

    if should_run("process"):
        start_stage("process")
        console.print(Panel("Stage 3: Image Processing", style="bold cyan"))
        from core.processor import process_images
        process_images(ws, config, tuple(config.video.resolution))
        _validate_ready_images(ws, script, config)
        complete_stage("process")
        if should_stop("process"):
            logger.info("Stopped after process")
            _finalize_outputs()
            return

    if preview_remotion:
        start_stage("preview_remotion")
        console.print(Panel("Stage 3b: Full Video Preview (Remotion Studio)", style="bold cyan"))
        _validate_ready_images(ws, script, config)
        _validate_audio_outputs(ws, script)
        from core.preview_remotion import launch_remotion_studio, write_preview_manifest

        manifest_path = write_preview_manifest(script, config, ws)
        launch_remotion_studio()
        logger.info(f"Preview manifest: {manifest_path}")
        complete_stage("preview_remotion")
        logger.info("Stopped after preview_remotion")
        _finalize_outputs()
        return

    # ── Thumbnail generation (overlapped with render+assemble) ─────
    # Thumbnail only needs images/ready/ (from process stage), not the video.
    # Launch it early and await the result before final_review.
    thumbnail_task = None
    if should_run("thumbnail"):
        start_stage("thumbnail")
        console.print(Panel("Stage 5: Thumbnail Generation + Review (background)", style="bold cyan"))
        from core.thumbnailer import create_thumbnail

        async def _run_thumbnail_task():
            # The thumbnail runs concurrently with section rendering. If this
            # task raises while the render is still in flight, the failure
            # tears down the render too -- a run died with the sections
            # cancelled mid-frame and Remotion reporting 404s on staged images,
            # when the only real problem was a thumbnail the review gate
            # disliked. When the gate is allowed to fail, return the error
            # instead of raising so the render is never disturbed by it.
            with costs.bound_context(stage="thumbnail"):
                try:
                    return await create_thumbnail(script, config, ws)
                except ReviewGateError as exc:
                    if "thumbnail_review" in allowed_review_failures:
                        return exc
                    raise

        thumbnail_task = asyncio.create_task(_run_thumbnail_task())

    # ── Render sections stage (Remotion SectionComposition per section) ─
    if should_run("render_sections"):
        start_stage("render_sections")
        console.print(Panel("Stage 3b: Section Rendering (Remotion)", style="bold cyan"))
        from core.render_sections import render_sections
        await render_sections(script, config, ws)
        complete_stage("render_sections")
        if should_stop("render_sections"):
            if thumbnail_task:
                thumbnail_task.cancel()
            logger.info("Stopped after render_sections")
            _finalize_outputs()
            return

    if should_run("assemble"):
        start_stage("assemble")
        console.print(Panel("Stage 4: Video Assembly", style="bold cyan"))
        from core.assembler import assemble_video
        _validate_ready_images(ws, script, config)
        _validate_audio_outputs(ws, script)
        output_path = _output_video_path(ws, script)
        named = assemble_video(script, config, ws, output_path)
        if not named.exists():
            fail_pipeline(f"Video assembly did not produce {named.name}")
        logger.info(f"Output: {named.name}")
        from core.validator import validate_video, run_validation
        run_validation("video", validate_video(named, config))
        complete_stage("assemble")
        if should_stop("assemble"):
            if thumbnail_task:
                thumbnail_task.cancel()
            logger.info("Stopped after assemble")
            _finalize_outputs()
            return

    # ── Await thumbnail result ──────────────────────────────────────
    if thumbnail_task:
        thumbnail_allowed_failure = False
        try:
            thumb_result = await thumbnail_task
            # An allowed gate failure comes back as a value rather than an
            # exception, so the render could not be caught in its blast radius.
            if isinstance(thumb_result, ReviewGateError):
                raise thumb_result
        except ReviewGateError as e:
            checkpoint.review_log["thumbnail_review"] = _review_log_entry(
                e.result,
                include_feedback=True,
            )
            if "thumbnail_review" not in allowed_review_failures:
                checkpoint.last_error = str(e)
                save_checkpoint(ws, checkpoint)
                _finalize_outputs()
                raise
            _log_allowed_review_failure("thumbnail_review")
            checkpoint.last_error = None
            thumbnail_allowed_failure = True
        from core.validator import validate_thumbnail, run_validation
        run_validation("thumbnail", validate_thumbnail(ws / "thumbnail.png", config))
        if not thumbnail_allowed_failure:
            checkpoint.review_log["thumbnail_review"] = _review_log_entry(thumb_result)
        complete_stage("thumbnail")
        if should_stop("thumbnail"):
            logger.info("Stopped after thumbnail")
            _finalize_outputs()
            return

    if should_run("final_review"):
        start_stage("final_review")
        console.print(Panel("Stage 6: Final Package Review", style="bold cyan"))
        final_review_passed = True
        try:
            with costs.bound_context(stage="final_review"):
                await _run_final_review(script, config, ws, checkpoint)
        except ReviewGateError:
            if "final_review" not in allowed_review_failures:
                _finalize_outputs()
                raise
            _log_allowed_review_failure("final_review")
            checkpoint.last_error = None
            save_checkpoint(ws, checkpoint)
            final_review_passed = False
        if final_review_passed:
            record_completed_video(
                channel_slug,
                checkpoint.topic,
                checkpoint.video_type,
                find_output_video(ws).name,
                title_format=plan["title_format"],
                description_style=plan["description_style"],
                thumbnail_strategy=script.thumbnail_strategy,
                content_family=plan.get("content_family", ""),
                narrative_hook_idx=plan.get("narrative_hook_idx", ""),
            )
        complete_stage("final_review")
        if should_stop("final_review"):
            logger.info("Stopped after final review")
            _finalize_outputs()
            return

    estimate_report = _finalize_outputs()

    timing_lines = []
    for stage, timing in checkpoint.stage_timings.items():
        timing_lines.append(f"  {stage}: {timing['duration_seconds']}s")
    if checkpoint.total_duration_seconds:
        timing_lines.append(f"  TOTAL: {checkpoint.total_duration_seconds}s")
    timing_summary = "\n".join(timing_lines) if timing_lines else "  (no timing data)"
    cost_summary = costs.cost_summary_line(estimate_report) if estimate_report else "Cost report unavailable."

    console.print(Panel(
        f"[bold green]Pipeline complete![/bold green]\n"
        f"Workspace: {ws}\n\n"
        f"[bold]Stage Timings:[/bold]\n{timing_summary}\n\n"
        f"[bold]Cost Summary:[/bold]\n{cost_summary}\n\n"
        f"Review log: {json.dumps(checkpoint.review_log, indent=2)}",
        style="green",
    ))


async def _run_final_review(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
    checkpoint: Checkpoint,
) -> None:
    """Stage 6: Extract frames and run Gate #4."""
    import cv2
    from core.reviewer import review_gate

    video_path = find_output_video(workspace)
    thumbnail_path = workspace / "thumbnail.png"
    frames_dir = workspace / "frames"
    frames_dir.mkdir(exist_ok=True)

    frame_paths = []
    if video_path.exists():
        cap = cv2.VideoCapture(str(video_path))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        n_frames = min(12, max(8, total_frames // 100))
        indices = [int(i * total_frames / n_frames) for i in range(n_frames)]

        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                frame_path = frames_dir / f"frame_{idx:06d}.jpg"
                cv2.imwrite(str(frame_path), frame)
                frame_paths.append(frame_path)
        cap.release()

    review_images = []
    if thumbnail_path.exists():
        review_images.append(thumbnail_path)
    review_images.extend(frame_paths)

    narration_summary = "\n\n".join(
        f"Section {section.id}: {section.narration[:300]}..."
        for section in script.sections[:5]
    )
    narration_summary += f"\n\n... ({len(script.sections)} sections total)"

    def review_prompt(_content):
        return prompts.package_review_prompt(
            title=script.title,
            description=script.description,
            tags=script.tags,
            video_type=script.video_type,
            narration_summary=narration_summary,
            # The gate judges captions and subject matter against this
            # channel's own spec. Hardcoding Football News' yellow highlight
            # rejected a Horror package for using the red its config asks for.
            caption_highlight=_caption_highlight_name(config),
            subject_domain=config.niche.category,
        )

    try:
        result = await review_gate(
            content=None,
            review_prompt_fn=review_prompt,
            system_instruction=prompts.package_review_system(),
            max_attempts=config.review_thresholds.final_review_max_attempts,
            gate_name="final_review",
            image_paths=review_images,
        )
    except ReviewGateError as e:
        checkpoint.review_log["final_review"] = _review_log_entry(
            e.result,
            include_feedback=True,
        )
        checkpoint.last_error = str(e)
        save_checkpoint(workspace, checkpoint)
        try:
            costs.write_estimate_reports(workspace, checkpoint.model_dump())
        except Exception as cost_err:
            logger.error(f"Cost report generation failed: {cost_err}")
        raise

    checkpoint.review_log["final_review"] = _review_log_entry(
        result,
        include_feedback=True,
    )
    save_checkpoint(workspace, checkpoint)


def _parse_stage_spec(spec: str | None) -> tuple[str | None, str | None]:
    """Parse --stage value into (start, stop).

    Returns:
        start: first stage to run (None = first stage)
        stop:  last stage to run (None = final_review)
    """
    if not spec:
        return None, "final_review"

    if ".." in spec:
        left, right = spec.split("..", 1)
        start = left.strip() or None
        stop = right.strip() or STAGES[-1]
        if stop not in STAGES:
            raise click.BadParameter(f"Unknown stage: {stop}", param_hint="--stage")
        if start and start not in STAGES:
            raise click.BadParameter(f"Unknown stage: {start}", param_hint="--stage")
        return start, stop

    if spec not in STAGES:
        raise click.BadParameter(f"Unknown stage: {spec}", param_hint="--stage")
    return spec, spec


def _split_overrides(overrides: list[str]) -> tuple[list[str], dict[str, str], list[str]]:
    """Separate plan.*, global settings, and channel config overrides."""
    config_overrides = []
    plan_overrides: dict[str, str] = {}
    settings_overrides: list[str] = []
    for ov in overrides:
        if ov.startswith("plan."):
            key, value = ov.split("=", 1)
            plan_overrides[key[5:]] = value
        elif "=" in ov and ov.split("=", 1)[0] in type(settings).model_fields:
            settings_overrides.append(ov)
        else:
            config_overrides.append(ov)
    return config_overrides, plan_overrides, settings_overrides


def _apply_settings_overrides(overrides: list[str]) -> None:
    for ov in overrides:
        key, raw_value = ov.split("=", 1)
        value = parse_override_value(raw_value)
        setattr(settings, key, value)
        logger.info(f"Settings override: {key}={value}")


@click.command()
@click.option("--channel", required=True, help="Channel slug (e.g. demo_channel)")
@click.option("--language", default=None,
              help="Script language for narration and captions (e.g. ar, en). "
                   "Must be one the channel declares in language_variants.")
@click.option("--stage", "stage_spec", default=None, help="Run specific stage(s): 'script', 'process..thumbnail', '..script'")
@click.option("--workspace", type=click.Path(exists=True, file_okay=False), default=None, help="Target a specific workspace (skips auto-detection)")
@click.option("--fixtures", type=click.Choice(["record", "replay"]), default=None, help="Record or replay API responses via .fixtures/")
@click.option("--set", "overrides", multiple=True, help="Override config (e.g. --set video.target_duration_minutes=1)")
@click.option("--preview-remotion", is_flag=True, help="Run through process, then open a full-video Remotion Studio preview instead of rendering/exporting")
@click.option(
    "--allow-review-failures",
    default="",
    help="Comma-separated review gates to continue after max retries (e.g. image_review,thumbnail,final_review)",
)
def main(channel, language, stage_spec, workspace, fixtures, overrides,
         preview_remotion, allow_review_failures):
    """Video Factory — Autonomous YouTube video pipeline."""
    if fixtures:
        from clients import set_mode
        set_mode(fixtures, channel_slug=channel)

    if preview_remotion and stage_spec is not None:
        raise click.BadParameter(
            "--preview-remotion cannot be combined with --stage",
            param_hint="--preview-remotion",
        )

    start_stage, stop_stage = _parse_stage_spec(stage_spec)
    config_overrides, plan_overrides, settings_overrides = _split_overrides(list(overrides))
    allowed_review_failures = _parse_allowed_review_failures(allow_review_failures)
    _apply_settings_overrides(settings_overrides)

    workspace_path = Path(workspace) if workspace else None

    console.print(Panel(
        "[bold]Video Factory[/bold]\n"
        f"Channel: {channel}",
        style="bold blue",
    ))

    try:
        asyncio.run(run_pipeline(
            channel_slug=channel,
            start_from=start_stage,
            stop_after=stop_stage,
            overrides=config_overrides,
            plan_overrides=plan_overrides,
            workspace_path=workspace_path,
            preview_remotion=preview_remotion,
            allow_review_failures=allowed_review_failures,
            language=language,
        ))
    except Exception:
        failed_ws = workspace_path or find_latest_workspace(channel)
        if failed_ws:
            failed_log = failed_ws / "pipeline.log"
            failed_checkpoint = load_checkpoint(failed_ws)
            if failed_checkpoint:
                try:
                    costs.write_estimate_reports(failed_ws, failed_checkpoint.model_dump())
                except Exception:
                    pass
            if failed_log.exists():
                try:
                    generate_log_html(failed_log, failed_ws / "pipeline.html")
                except Exception:
                    pass
        try:
            costs.shutdown_cost_tracking()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        console.print(Panel(str(e), title="Pipeline Failed", style="bold red"))
        sys.exit(1)
