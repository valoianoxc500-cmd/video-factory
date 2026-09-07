"""Stage 4: Video assembly â€” section concat + audio mix.

Assembles the final video from pre-rendered section clips (produced by
render_sections via Remotion SectionComposition). Two-phase pipeline:
  1. Concatenate section clips with xfade transitions
  2. Audio mix + video overlays (single FFmpeg pass)
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.utils import Script, ChannelConfig
from settings import settings, PROJECT_ROOT

logger = logging.getLogger("video_factory")

COLOR_FILTERS: dict[str, tuple[float, ...]] = {}
_IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)


def _build_color_filter(name: str, opacity: float = 1.0) -> str | None:
    """Build a colorchannelmixer string, blended toward identity by opacity."""
    matrix = COLOR_FILTERS.get(name)
    if not matrix:
        return None
    opacity = max(0.0, min(1.0, opacity))
    blended = tuple(i * (1 - opacity) + m * opacity for i, m in zip(_IDENTITY, matrix))
    values = ":".join(f"{v:.3f}" for v in blended)
    return f"colorchannelmixer={values}"


# â”€â”€ Transition name mapping â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# style.json uses friendly names; FFmpeg xfade uses internal names.
#
# `dissolve` is a real xfade mode, but it is a random-pixel dissolve: on a
# 1080x1920 photo cut it reads as harsh static rather than a blend, and it does
# not match the smooth dissolve the Remotion path renders inside a section.
# Both sides of a boundary should feel the same, so it resolves to a blend.
_TRANSITION_MAP = {"crossfade": "fade", "dissolve": "fade"}


def _parse_section_id(path: Path) -> int | None:
    """Extract the section ID from a filename like section_003.mp4."""
    parts = path.stem.split("_")
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return None


# â”€â”€ Duration probe â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _probe_duration(path: Path) -> float:
    """Get actual duration of a media file via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.warning(f"ffprobe failed for {path.name}, estimating duration")
        return 0.0
    return float(result.stdout.strip())


# â”€â”€ Crossfade concatenation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _xfade_concat(
    clip_paths: list[Path],
    output_path: Path,
    crossfade_dur: float,
    fps: int,
    transitions: list[str] | str = "fade",
) -> Path:
    """Concatenate clips with xfade transitions (per-boundary or uniform)."""
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], output_path)
        return output_path

    # Normalize to per-boundary list
    num_boundaries = len(clip_paths) - 1
    if isinstance(transitions, str):
        trans_list = [transitions] * num_boundaries
    else:
        trans_list = list(transitions)
        while len(trans_list) < num_boundaries:
            trans_list.append("fade")

    # Get actual durations
    durations = [_probe_duration(p) for p in clip_paths]

    # Ensure crossfade doesn't exceed any clip duration
    min_dur = min(durations) if durations else 0
    xfade_dur = min(crossfade_dur, min_dur * 0.5) if min_dur > 0 else crossfade_dur

    if xfade_dur < 0.05:
        # Durations too short for xfade â€” use concat demuxer instead
        return _concat_demuxer_fallback(clip_paths, output_path)

    # Build xfade filter chain
    inputs = []
    for p in clip_paths:
        inputs.extend(["-i", str(p)])

    filter_parts = []
    current_label = "[0:v]"
    cumulative_offset = 0.0

    for i in range(1, len(clip_paths)):
        t = trans_list[i - 1]
        # "cut" = near-zero crossfade (1 frame)
        dur = 1.0 / fps if t == "cut" else xfade_dur
        t_name = "fade" if t == "cut" else t

        offset = cumulative_offset + durations[i - 1] - dur
        if offset < 0:
            offset = 0
        out_label = f"[v{i}]" if i < len(clip_paths) - 1 else "[vout]"
        next_input = f"[{i}:v]"

        filter_parts.append(
            f"{current_label}{next_input}xfade=transition={t_name}"
            f":duration={dur:.3f}:offset={offset:.3f}{out_label}"
        )
        current_label = out_label
        cumulative_offset = offset

    filter_complex = ";".join(filter_parts)

    cmd = [
        settings.ffmpeg_path, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        # The mux step copies this stream rather than re-encoding, so these
        # are the delivered video's settings. Encoding at cq 10 with no
        # ceiling produced a 16 Mbps, 119 MB file for one minute, which the
        # object store rejected outright as too large.
        *_delivery_video_encode(),
        "-an",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.warning(f"xfade concat failed, falling back to demuxer: {result.stderr[-300:]}")
        return _concat_demuxer_fallback(clip_paths, output_path)

    return output_path


def _concat_demuxer_fallback(clip_paths: list[Path], output_path: Path) -> Path:
    """Fallback: concat demuxer (hard cuts, no transitions)."""
    list_file = output_path.parent / f"{output_path.stem}_concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in clip_paths:
            safe = str(p).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    cmd = [
        settings.ffmpeg_path, "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        # Same as the xfade path: this stream is copied into the final mux,
        # so it must carry the delivery bitrate ceiling.
        *_delivery_video_encode(),
        "-an",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    list_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"Concat demuxer fallback also failed: {result.stderr[-300:]}")

    return output_path


# â”€â”€ Audio loudness normalization â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# YouTube recommends -14 LUFS integrated loudness, -1.5 dBTP true peak.
_LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"



def _delivery_video_encode() -> list[str]:
    """Video encoder args for the delivered MP4.

    Capped VBR rather than pure constant-quality: a hard ceiling keeps a busy
    scene from ballooning the file, which matters when the output is uploaded
    to metered object storage on every run.
    """
    return [
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-rc", "vbr",
        "-cq", str(settings.video_quality_cq),
        "-maxrate", settings.video_max_bitrate,
        "-bufsize", settings.video_buffer_size,
        "-pix_fmt", "yuv420p",
    ]


def _build_audio_filter(
    narration_idx: int | None,
    music_idx: int | None,
    music_volume: float,
    video_duration: float,
    transitions_idx: int | None = None,
    sfx_volume: float = 0.15,
    ambience_idx: int | None = None,
    ambience_volume: float = 0.16,
    scene_sfx_idx: int | None = None,
    scene_sfx_volume: float = 0.34,
) -> tuple[str | None, str]:
    """Build audio filter chain with loudness normalization.

    Returns (filter_part_or_None, audio_map_label).

    Up to five layers, each kept as its own input and its own volume so a mix
    can be reasoned about and changed one layer at a time: narration, the
    music bed, the transition track, and -- when the channel enables scene
    audio -- per-scene ambience and per-scene cues. The two scene layers
    arrive already timed and faded, so all that is needed here is a level.
    """
    parts: list[str] = []

    if narration_idx is not None and music_idx is not None:
        # Constant-volume mix: narration + music at their set levels.
        parts.append(
            f"[{music_idx}:a]aloop=loop=-1:size=2e+09,atrim=0:{video_duration:.3f},"
            f"volume={music_volume}[mus];"
            f"[{narration_idx}:a]apad=pad_dur=2[nar];"
            f"[nar][mus]amix=inputs=2:duration=longest:normalize=0[nar_mus]"
        )
        mix_label = "[nar_mus]"
    elif narration_idx is not None:
        parts.append(f"[{narration_idx}:a]anull[nar_mus]")
        mix_label = "[nar_mus]"
    elif music_idx is not None:
        parts.append(
            f"[{music_idx}:a]aloop=loop=-1:size=2e+09,atrim=0:{video_duration:.3f},"
            f"volume={music_volume}[nar_mus]"
        )
        mix_label = "[nar_mus]"
    else:
        return None, ""

    # Every remaining layer is trimmed to length, levelled, and mixed in one
    # pass. One amix rather than a chain of them: repeated pairwise mixing
    # attenuates the earlier layers each time.
    extra: list[tuple[int, float, str]] = []
    if transitions_idx is not None:
        extra.append((transitions_idx, sfx_volume, "sfx"))
    if ambience_idx is not None:
        extra.append((ambience_idx, ambience_volume, "amb"))
    if scene_sfx_idx is not None:
        extra.append((scene_sfx_idx, scene_sfx_volume, "scenesfx"))

    if extra:
        labels = []
        for index, volume, name in extra:
            parts.append(
                f"[{index}:a]atrim=0:{video_duration:.3f},volume={volume}[{name}]"
            )
            labels.append(f"[{name}]")
        parts.append(
            f"{mix_label}{''.join(labels)}"
            f"amix=inputs={len(labels) + 1}:duration=longest:normalize=0,"
            f"{_LOUDNORM}[aout]"
        )
    else:
        parts.append(f"{mix_label}{_LOUDNORM}[aout]")

    return ";".join(parts), "[aout]"


# â”€â”€ Audio mix + visual effects â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _mix_audio_and_effects(
    video_path: Path,
    output_path: Path,
    narration_path: Path | None,
    music_path: Path | None,
    music_volume: float,
    effects: dict[str, Any],
    video_duration: float,
    target_size: tuple[int, int],
    transitions_path: Path | None = None,
    sfx_volume: float = 0.15,
    ambience_path: Path | None = None,
    ambience_volume: float = 0.16,
    scene_sfx_path: Path | None = None,
    scene_sfx_volume: float = 0.34,
) -> Path:
    """Merge audio mix and video overlays into one FFmpeg command."""
    color_filter_name = effects.get("color_filter")
    color_filter_opacity = effects.get("color_filter_opacity", 1.0)
    color_filter_value = _build_color_filter(color_filter_name, color_filter_opacity) if color_filter_name else None
    has_audio = any(
        path and path.exists()
        for path in (
            narration_path, music_path, transitions_path,
            ambience_path, scene_sfx_path,
        )
    )

    # â”€â”€ Resolve overlay video files â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    overlay_configs = effects.get("overlays", [])
    overlays = []
    for ov in overlay_configs:
        ov_path = PROJECT_ROOT / ov["path"]
        if ov_path.exists():
            overlays.append({
                "resolved_path": ov_path,
                "blend_mode": ov.get("blend_mode", "screen"),
                "opacity": ov.get("opacity", 0.3),
            })
        else:
            logger.warning(f"Overlay not found, skipping: {ov['path']}")

    has_overlays = bool(overlays)

    # â”€â”€ Build inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    inputs = ["-i", str(video_path)]
    next_idx = 1
    narration_idx = None
    music_idx = None

    transitions_idx = None

    if narration_path and narration_path.exists():
        inputs.extend(["-i", str(narration_path)])
        narration_idx = next_idx
        next_idx += 1

    if music_path and music_path.exists():
        inputs.extend(["-i", str(music_path)])
        music_idx = next_idx
        next_idx += 1

    if transitions_path and transitions_path.exists():
        inputs.extend(["-i", str(transitions_path)])
        transitions_idx = next_idx
        next_idx += 1

    # The two per-scene layers arrive already timed and faded by the audio
    # director, so they need an input slot and a level and nothing else.
    ambience_idx = None
    if ambience_path and ambience_path.exists():
        inputs.extend(["-i", str(ambience_path)])
        ambience_idx = next_idx
        next_idx += 1

    scene_sfx_idx = None
    if scene_sfx_path and scene_sfx_path.exists():
        inputs.extend(["-i", str(scene_sfx_path)])
        scene_sfx_idx = next_idx
        next_idx += 1

    overlay_start_idx = next_idx
    for ov in overlays:
        inputs.extend([
            "-stream_loop", "-1",
            "-t", f"{video_duration:.3f}",
            "-i", str(ov["resolved_path"]),
        ])

    # â”€â”€ Build filter_complex â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    filter_complex_parts = []
    map_args = []
    w, h = target_size

    if has_overlays:
        # Build overlay blend chain
        if color_filter_value:
            filter_complex_parts.append(f"[0:v]{color_filter_value}[cf]")
            current_label = "[cf]"
        else:
            current_label = "[0:v]"
        for i, ov in enumerate(overlays):
            ov_idx = overlay_start_idx + i
            ov_label = f"[ov{i}]"
            is_last = (i == len(overlays) - 1)
            out_label = "[vout]" if is_last else f"[blend{i}]"

            # Convert overlay to planar RGB for correct blend math
            filter_complex_parts.append(
                f"[{ov_idx}:v]scale={w}:{h},format=gbrp,setpts=PTS-STARTPTS{ov_label}"
            )
            # Convert main video to planar RGB before blending
            filter_complex_parts.append(
                f"{current_label}format=gbrp[main_rgb{i}]"
            )
            # Blend in RGB space; convert back to yuv420p on last overlay
            blend_out = f",format=yuv420p{out_label}" if is_last else out_label
            filter_complex_parts.append(
                f"[main_rgb{i}]{ov_label}"
                f"blend=all_mode={ov['blend_mode']}:all_opacity={ov['opacity']}{blend_out}"
            )
            current_label = out_label

        v_enc = _delivery_video_encode()

        if has_audio:
            audio_filt, audio_map = _build_audio_filter(
                narration_idx, music_idx, music_volume, video_duration,
                transitions_idx, sfx_volume=sfx_volume,
                ambience_idx=ambience_idx, ambience_volume=ambience_volume,
                scene_sfx_idx=scene_sfx_idx, scene_sfx_volume=scene_sfx_volume,
            )
            if audio_filt:
                filter_complex_parts.append(audio_filt)
            map_args = ["-map", "[vout]", "-map", audio_map]

            filter_complex = ";".join(filter_complex_parts)
            cmd = [
                settings.ffmpeg_path, "-y",
                *inputs,
                "-filter_complex", filter_complex,
                *map_args,
                *v_enc,
                "-c:a", "aac", "-b:a", settings.audio_bitrate,
                str(output_path),
            ]
        else:
            # Overlays only, no audio
            filter_complex = ";".join(filter_complex_parts)
            cmd = [
                settings.ffmpeg_path, "-y",
                *inputs,
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                *v_enc,
                "-an",
                str(output_path),
            ]

    elif has_audio:
        # Audio (+ optional color filter), no overlays
        fc_parts = []
        if color_filter_value:
            fc_parts.append(f"[0:v]{color_filter_value}[vout]")
        v_map = "[vout]" if color_filter_value else "0:v"
        v_enc = (_delivery_video_encode() if color_filter_value
                 else ["-c:v", "copy"])

        audio_filt, a_map = _build_audio_filter(
            narration_idx, music_idx, music_volume, video_duration,
            transitions_idx, sfx_volume=sfx_volume,
            ambience_idx=ambience_idx, ambience_volume=ambience_volume,
            scene_sfx_idx=scene_sfx_idx, scene_sfx_volume=scene_sfx_volume,
        )
        if audio_filt:
            fc_parts.append(audio_filt)

        if fc_parts:
            cmd = [
                settings.ffmpeg_path, "-y",
                *inputs,
                "-filter_complex", ";".join(fc_parts),
                "-map", v_map, "-map", a_map,
                *v_enc,
                "-c:a", "aac", "-b:a", settings.audio_bitrate,
                str(output_path),
            ]
        else:
            cmd = [
                settings.ffmpeg_path, "-y",
                *inputs,
                "-map", "0:v", "-map", a_map,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", settings.audio_bitrate,
                str(output_path),
            ]
    else:
        if color_filter_value:
            # Color filter only, no overlays or audio
            cmd = [
                settings.ffmpeg_path, "-y",
                *inputs,
                "-vf", color_filter_value,
                *_delivery_video_encode(), "-an",
                str(output_path),
            ]
        else:
            # No overlays, no audio, no color filter â€” stream-copy, but still
            # remux so the output is progressive-download friendly.
            cmd = [
                settings.ffmpeg_path, "-y",
                "-i", str(video_path),
                "-c", "copy",
                str(output_path),
            ]

    # Put the moov atom in front of the media data. Without this a browser
    # has to download the entire file before it can start playing, which for a
    # ~100 MB short means the <video> element simply gives up.
    # Metadata relocation only: the encoded streams are untouched.
    if cmd and cmd[-1] == str(output_path):
        cmd = cmd[:-1] + ["-movflags", "+faststart", str(output_path)]

    logger.info(f"Final encode: watermarks={'yes' if has_overlays else 'no'}, audio={'yes' if has_audio else 'no'}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Final encode failed: {result.stderr[-500:]}")

    return output_path


# â”€â”€ Main assembly â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def assemble_video(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
    output_path: Path,
) -> Path:
    """Assemble the final video from section clips + audio.

    Returns the path to the output MP4.
    """
    style = config.style

    # Default transition from channel pool for sections that omit transition_type
    pool = style.video.get("transition_pool", ["fade"])
    default_transition = _TRANSITION_MAP.get(pool[0], pool[0])
    logger.info("Per-section transitions from script")

    audio_dir = workspace / "audio"
    clips_dir = workspace / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    final_path = output_path

    w, h = config.video.resolution
    fps = config.video.fps
    transition_dur = config.video.transition_duration_seconds
    music_volume = config.video.background_music_volume

    # â”€â”€ Phase 1: Load pre-rendered section clips â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Only include sections that exist in the current script to avoid
    # stale clips from earlier pipeline runs in the same workspace.
    sections_dir = workspace / "videos" / "sections"
    script_ids = {s.id for s in script.sections}
    section_clip_paths = sorted(
        p for p in sections_dir.glob("section_*.mp4")
        if _parse_section_id(p) in script_ids
    )

    if not section_clip_paths:
        raise RuntimeError(
            "No section clips found in videos/sections/. "
            "Run render_sections stage first."
        )

    logger.info(f"Concatenating {len(section_clip_paths)} sections")

    # â”€â”€ Phase 2: Inter-section xfade concatenation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Build per-section transition list from LLM values
    sorted_sections = sorted(script.sections, key=lambda s: s.id)
    section_transitions = []
    for i in range(1, len(sorted_sections)):
        t = sorted_sections[i].transition_type
        section_transitions.append(_TRANSITION_MAP.get(t, t) if t else default_transition)

    video_only = clips_dir / "video_only.mp4"
    inter_dur = transition_dur
    _xfade_concat(section_clip_paths, video_only, inter_dur, fps, transitions=section_transitions)

    # â”€â”€ Phase 3: Audio + effects â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    narration_path = audio_dir / "narration_full.wav"
    music_path = audio_dir / "background_music.mp3"
    transitions_path = audio_dir / "transitions.wav"
    video_duration = _probe_duration(video_only)
    sfx_volume = config.rendering_defaults.sfx_volume

    # Written by the audio director when the channel enables scene audio.
    # Absent otherwise, and the mix is then exactly what it was before.
    ambience_path = audio_dir / "ambience.wav"
    scene_sfx_path = audio_dir / "scene_sfx.wav"

    _mix_audio_and_effects(
        video_path=video_only,
        output_path=final_path,
        narration_path=narration_path if narration_path.exists() else None,
        music_path=music_path if music_path.exists() else None,
        music_volume=music_volume,
        effects=style.effects,
        video_duration=video_duration,
        target_size=(w, h),
        transitions_path=transitions_path if transitions_path.exists() else None,
        sfx_volume=sfx_volume,
        ambience_path=ambience_path if ambience_path.exists() else None,
        scene_sfx_path=scene_sfx_path if scene_sfx_path.exists() else None,
    )

    # â”€â”€ Cleanup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        shutil.rmtree(clips_dir)
    except Exception as e:
        logger.warning(f"Cleanup of clips dir failed: {e}")

    logger.info(f"Video assembled: {final_path.name}")
    return final_path
