"""The two stages that make an Animated Story: character, then animation.

Both are no-ops unless the channel declares `animation.enabled`, which only
Animated Stories does. Football, Horror Stories and True Stories run the
pipeline exactly as they did.

`character` runs before scene artwork. It draws one reference sheet and then
rewrites every slot's prompt to restate that same character, which is the only
reason twelve independently generated images look like one series.

`animation` runs after scene artwork and after image_review, so only pictures
that passed the gate are ever animated -- paying to animate a rejected frame
is the expensive version of shipping it. Clips are written where the renderer
already looks for per-beat video (`videos/section_XXX_YY.mp4`) and the slot is
marked `b_roll`, so the existing Remotion path plays them with no new
rendering code.

A scene is never marked animated without a playable file. On failure the
generated still is preserved and the beat renders as a still, which is a
complete scene rather than a gap.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import httpx

import clients
from core import animated_stories as anim
from core import character_bible as bible_mod
from core.animated_stories import AnimationBudget
from core.utils import ChannelConfig, Script
from settings import settings

logger = logging.getLogger("video_factory")


def enabled(config: ChannelConfig) -> bool:
    """Whether this channel animates at all."""
    return bool(getattr(config, "animation", None) and config.animation.enabled)


async def build_character_sheet(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
    *,
    plan: dict | None = None,
) -> Path | None:
    """Design the cast, draw a reference sheet each, and lock every prompt.

    Returns the main character's sheet, or None when the channel does not
    animate.

    The order matters. The bible is written *first*, from the narration, so
    there is a concrete description to draw from; the sheets are drawn from
    that description; and every scene prompt then restates the identical
    locked text. The previous version drew a sheet from an empty string and
    asked the scenes to match "the main character", which is why a real run
    produced twenty-one different people.
    """
    if not enabled(config):
        return None

    sheet_dir = workspace / "character"
    sheet_dir.mkdir(parents=True, exist_ok=True)

    bible = await bible_mod.build_bible(
        script, plan, generate_text=clients.generate_text
    )

    # One sheet per character. Secondary characters recur too, and a cast
    # whose supporting roles drift is only marginally better than one whose
    # lead does.
    # The sheet is what every scene is drawn from and judged against, so it has
    # to come out of the same generator, under the same style contract, as the
    # scenes themselves. It did not: sheets were drawn on the channel's
    # *fallback* model while scenes were drawn on its primary one, and the two
    # models do not share a house style. A real run produced a polished
    # animated-feature character on the sheet and stick figures in the scenes,
    # and the reviewer rejected eight scenes for not matching a sheet that was
    # itself off-style. The locked description is unchanged either way -- only
    # the renderer of it is.
    sourcing = config.image_sourcing
    sheet_model = (
        str(getattr(sourcing, "generation_model", "") or "").strip()
        or sourcing.generated_fallback_model
    )
    style = anim.STICK_FIGURE_STYLE
    channel_style = str(
        getattr(sourcing, "illustration_style_prompt_suffix", "") or ""
    ).strip()
    if channel_style:
        style = f"{style} {channel_style}"

    main_sheet: Path | None = None
    for character in bible.characters:
        sheet_path = sheet_dir / f"{character.id}_sheet.png"
        prompt = bible_mod.sheet_prompt(
            character, style, anim.STYLE_EXCLUSIONS
        )
        logger.info(
            f"[character] sheet for {character.id} ({character.name}) "
            f"on {sheet_model}"
        )
        result = await clients.generate_scene_image(
            prompt,
            sheet_path,
            model=sheet_model,
            aspect_ratio="16:9",
            target_size=(1920, 1080),
            operation_label="character_sheet",
        )
        if result is None or not sheet_path.exists():
            if character.role == "main":
                raise RuntimeError(
                    f"character sheet for {character.id} produced no image"
                )
            # A missing secondary sheet is survivable: the locked description
            # still governs the scenes.
            logger.warning(f"[character] no sheet for {character.id}")
            continue
        character.sheet = sheet_path.name
        if character.role == "main":
            main_sheet = sheet_path

    # Bind every beat to the cast it contains. The locked text is pasted in
    # verbatim -- identical bytes in every scene that shares a character.
    bound = 0
    for section in script.sections:
        for slot in section.slots:
            beat = str(slot.prompt or slot.keywords or "").strip()
            if not beat:
                continue
            cast = bible_mod.characters_in_beat(beat, bible)
            slot.prompt = bible_mod.scene_prompt_for(
                beat, cast, anim.STICK_FIGURE_STYLE, anim.STYLE_EXCLUSIONS
            )
            slot.props = {
                **(slot.props or {}),
                "character_ids": [c.id for c in cast],
            }
            bound += 1

    (sheet_dir / "character_bible.json").write_text(
        json.dumps(
            {**bible.to_record(), "bound_slots": bound},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        f"[character] {len(bible.characters)} character(s) locked; "
        f"{bound} scene prompt(s) bound"
    )
    return main_sheet


async def review_character_consistency(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
    *,
    image_paths: list[Path],
) -> dict:
    """Hard gate: does every scene still show the character from the sheet?

    Compares generated scenes against the reference sheets and names the
    files that drifted. The caller re-sources those beats and asks again --
    a scene whose character changed is a defect, not a stylistic variation,
    and shipping it is what makes a deck of frames stop reading as one story.

    Returns `{"passed": bool, "rejected": [filename], "reason": str}`. A
    review that cannot run returns `passed=True`: an unavailable reviewer
    must not silently fail every video, and the image_review gate still
    applies.
    """
    if not enabled(config) or not image_paths:
        return {"passed": True, "rejected": [], "reason": "not applicable"}

    bible_path = workspace / "character" / "character_bible.json"
    if not bible_path.exists():
        return {"passed": True, "rejected": [], "reason": "no bible"}

    record = json.loads(bible_path.read_text(encoding="utf-8"))
    bible = bible_mod.CharacterBible(
        characters=[bible_mod.Character(**c) for c in record.get("characters", [])]
    )

    sheet_dir = workspace / "character"
    sheets = [
        sheet_dir / c.sheet for c in bible.characters
        if c.sheet and (sheet_dir / c.sheet).exists()
    ]
    if not sheets:
        return {"passed": True, "rejected": [], "reason": "no reference sheet"}

    # Sheets first so the reviewer reads them as the reference, then the
    # scenes in the order the filenames are reported back.
    scenes = list(image_paths)
    prompt = (
        "<task>\nThe FIRST "
        f"{len(sheets)} image(s) are character reference sheets. Every image "
        "after them is a scene from the same animated story.\n\n"
        "Judge ONLY whether each scene shows the SAME character(s) as the "
        "reference sheets: same face, same head shape, same hair, same "
        "clothing, same colours, same accessories, same body proportions.\n"
        "</task>\n\n"
        "<cast>\n" + bible.cast_summary() + "\n</cast>\n\n"
        "<rules>\n"
        "- Pose, expression, action, camera angle, environment and lighting "
        "are ALLOWED to differ. Do not reject for those.\n"
        "- Reject a scene when the character's identity changed: a different "
        "face, different clothing, different colours, or a different art "
        "style (3D render, photographic, realistic human).\n"
        "- Judge each scene independently.\n"
        "</rules>\n\n"
        "Scene filenames in order: "
        + ", ".join(p.name for p in scenes) + "\n\n"
        'Return ONLY JSON: {"rejected": ["filename", ...], "reason": "one sentence"}'
    )

    try:
        result = await clients.review_with_vision(
            prompt,
            sheets + scenes,
            operation_label="character_consistency_review",
            # The reviewer sometimes answers with the bare array of rejected
            # filenames instead of the object it was asked for. That is a
            # readable verdict, and it used to crash the whole stage here.
            list_key="rejected",
        )
    except Exception as exc:
        logger.warning(
            f"[character_review] could not run: {str(exc)[:200]}; "
            f"leaving the decision to image_review"
        )
        return {"passed": True, "rejected": [], "reason": "reviewer unavailable"}

    # `list_key` guarantees a mapping, and an empty one means the response was
    # neither an object nor an array -- a verdict that cannot be read, which is
    # the same situation as a reviewer that could not run. image_review still
    # applies to every one of these frames.
    if not isinstance(result, dict) or not result:
        logger.warning(
            "[character_review] no readable verdict; "
            "leaving the decision to image_review"
        )
        return {"passed": True, "rejected": [], "reason": "unreadable verdict"}

    raw_rejected = result.get("rejected")
    if not isinstance(raw_rejected, list):
        raw_rejected = []
    # Filtered against the scenes actually reviewed, so a hallucinated or
    # mis-shaped name can only ever drop out -- never widen the gate, and never
    # name a file this run does not own.
    valid = {p.name for p in scenes}
    rejected = [
        str(name) for name in raw_rejected
        if str(name) in valid
    ]
    reason = str(result.get("reason") or "").strip()

    if rejected:
        logger.warning(
            f"[character_review] REJECTED {len(rejected)} of {len(scenes)} "
            f"scene(s): {reason}"
        )
    else:
        logger.info(f"[character_review] all {len(scenes)} scene(s) consistent")

    return {"passed": not rejected, "rejected": rejected, "reason": reason}


def _playable(path: Path) -> float:
    """Duration of a real video file, or 0.0 when it is not one.

    A byte count is not proof: a truncated download and a refusal both land as
    a file. ffprobe is what says the renderer can actually play it.
    """
    if not path.exists() or path.stat().st_size < 1024:
        return 0.0
    try:
        out = subprocess.run(
            [settings.ffmpeg_path.replace("ffmpeg", "ffprobe"),
             "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float((out.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


async def animate_scenes(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
) -> dict | None:
    """Animate every scene, locally by default. Returns the timeline manifest.

    Local Remotion motion is the product, not the fallback. The paid
    image-to-video model is reached only for beats whose motion compositing
    cannot fake, and only up to `max_ai_clips` and the AI motion budget --
    normally zero or one clip in a short video.

    The paid model being unavailable is not a failure here. Every scene still
    animates and the run continues; only the escalations are dropped.
    """
    if not enabled(config):
        return None

    from core.providers.animation import FalImageToVideoProvider

    settings_a = config.animation
    provider = FalImageToVideoProvider(model=settings_a.model)
    status = provider.status()
    allow_ai = bool(status.usable)
    if not allow_ai:
        # Deliberately a warning, not an error: the video is still complete.
        logger.warning(
            f"[animation] paid model unavailable ({status.reason}); "
            f"every scene animates locally"
        )

    ready_dir = workspace / "images" / "ready"
    raw_dir = workspace / "images" / "raw"
    videos_dir = workspace / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    scenes = anim.scenes_from_script(script)
    # The ceiling is the *emergency* budget now, not the animation budget:
    # local motion costs nothing, so this bounds only the exceptions.
    budget = AnimationBudget(
        ceiling_usd=float(settings_a.ai_motion_budget_usd),
        cost_per_clip_usd=float(settings_a.cost_per_clip_usd),
    )
    anim.plan_scenes(
        scenes, budget,
        min_clip_seconds=settings_a.min_clip_seconds,
        max_clip_seconds=settings_a.max_clip_seconds,
        max_ai_clips=int(settings_a.max_ai_clips),
        allow_ai=allow_ai,
    )

    # Slot lookup by the same label the renderer and sourcing both use.
    slots_by_label: dict[str, object] = {}
    for section in script.sections:
        visible = [s for s in section.slots if s.visual != "text_overlay"]
        for index, slot in enumerate(visible, start=1):
            slots_by_label[f"section_{section.id:03d}_{index:02d}"] = slot

    animated = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(provider.TIMEOUT, connect=15.0)
    ) as client:
        for scene in scenes:
            label = f"section_{scene.section_id:03d}_{_index_in_section(scenes, scene):02d}"
            source = _existing(ready_dir / f"{label}.png", raw_dir / f"{label}.jpg",
                               raw_dir / f"{label}.png", ready_dir / f"{label}.jpg")
            scene.source_image = str(source) if source else ""
            slot = slots_by_label.get(label)

            # Every scene carries its free recipe into the render, including
            # the ones about to be escalated: if the paid clip fails, the beat
            # still moves.
            if slot is not None and scene.local_motion:
                slot.props = {**(slot.props or {}),
                              "local_motion": scene.local_motion}

            if scene.kind != "clip":
                # The normal path. Nothing is generated and nothing is spent.
                scene.animation_provider = "remotion_local"
                scene.animation_model = "local_motion"
                scene.generation_status = "local" if scene.local_motion else "still"
                if not scene.local_motion:
                    scene.kind = "still"
                continue

            scene.animation_provider = provider.name
            scene.animation_model = provider.model
            if not source:
                # No image to animate from -- but the beat still renders,
                # because the recipe above is already on the slot.
                scene.kind = "local" if scene.local_motion else "still"
                scene.generation_status = scene.kind
                scene.error = "no scene image to animate"
                continue

            out = videos_dir / f"{label}.mp4"
            motion = anim.motion_prompt(scene.beat)
            for attempt in range(1, int(settings_a.max_attempts_per_scene) + 1):
                scene.attempts = attempt
                try:
                    data = await provider.animate(
                        source, motion, client=client,
                        seconds=min(settings_a.max_clip_seconds, scene.duration),
                        resolution=settings_a.resolution,
                    )
                    out.write_bytes(data)
                    duration = _playable(out)
                    if duration <= 0.2:
                        raise RuntimeError(
                            f"returned file is not a playable video ({duration:.2f}s)"
                        )
                    scene.clip_path = str(out)
                    scene.generation_status = "done"
                    # Hand it to the existing per-beat video path. The local
                    # recipe is dropped from the slot here: the clip is the
                    # motion now, and leaving both would animate a video.
                    if slot is not None:
                        slot.visual = "b_roll"
                        slot.props = {
                            k: v for k, v in (slot.props or {}).items()
                            if k != "local_motion"
                        }
                    animated += 1
                    logger.info(
                        f"[animation] scene {scene.index} ({label}): "
                        f"{duration:.1f}s clip"
                    )
                    break
                except Exception as exc:
                    scene.error = str(exc)[:200]
                    logger.warning(
                        f"[animation] scene {scene.index} attempt {attempt} "
                        f"failed: {scene.error}"
                    )
                    out.unlink(missing_ok=True)
                    if attempt < settings_a.max_attempts_per_scene:
                        budget.charge(retry=True)
            else:
                # Every attempt failed. The beat falls back to the free
                # recipe already on its slot, so it still moves -- a failed
                # escalation costs money, not motion.
                scene.kind = "local" if scene.local_motion else "still"
                scene.generation_status = "failed_fell_back_to_local"
                logger.warning(
                    f"[animation] scene {scene.index} could not be animated; "
                    f"falling back to local motion"
                )

    record = anim.timeline_record(scenes, budget)
    (workspace / "animation_manifest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        f"[animation] {record['local_scenes']} scene(s) animated locally at $0, "
        f"{animated} paid clip(s), "
        f"{record['still_scenes']} still(s), "
        f"${record['animation_spend_usd']:.3f} of "
        f"${record['ceiling_usd']:.2f} emergency budget"
    )
    return record


def _index_in_section(scenes, scene) -> int:
    """1-based position of this scene within its own section."""
    same = [s for s in scenes if s.section_id == scene.section_id]
    return same.index(scene) + 1


def _existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None
