"""Pipeline stage: turn verified match moments into rendered 9:16 clips.

How a clip reaches the screen
-----------------------------
The renderer already prefers a video over a still whenever
`workspace/videos/raw/section_NNN_MM.mp4` exists for a slot. So this stage
does not touch the renderer or the image sourcer at all: it cuts clips, names
them for the slot they belong to, and drops them in that directory. A slot
with a clip renders as footage; a slot without one is sourced as a photo
exactly as before.

That is also the fallback. Every failure path here -- no footage, no
alignment, a moment in an unaligned half, a bad cut -- simply leaves the file
absent, and the photo pipeline covers that slot. This stage can degrade the
video but cannot fail the run.

Narration sync
--------------
A moment's clip must appear while the narration is talking about it. Sections
are matched to moments by the minute the narration states (the channel's
script instructions require naming it), falling back to match order when no
minute is written. The clip then takes the section's first slot, which is the
beat that introduces the moment.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from core.footage import (
    ClipWindow,
    FootageAvailability,
    MatchMoment,
    clip_window_for,
    cut_clips,
    discover_local_footage,
    probe_duration,
    resolve_availability,
)
from core.footage_align import load_alignment, minute_to_video_seconds

logger = logging.getLogger("video_factory")


class FootageAnalysisError(RuntimeError):
    """Supplied footage could not be analysed, so the run must not proceed."""

# Arabic-Indic digits appear in Arabic narration alongside Latin ones.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


@dataclass
class FootagePlacement:
    moment: MatchMoment
    section_id: int
    slot_index: int          # 1-based, matching the renderer's file naming
    clip_path: Path


def _minutes_in(text: str) -> set[int]:
    """Match minutes named in a narration line, in either digit set."""
    normalised = (text or "").translate(_ARABIC_DIGITS)
    found = set()
    # Not \b...\b: an English ordinal ("61st", "23rd") has no word boundary
    # between the digits and the suffix, so \b would miss exactly the phrasing
    # a match report uses most. Digit-adjacency is the real constraint.
    for raw in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", normalised):
        value = int(raw)
        if 1 <= value <= 120:
            found.add(value)
    return found


def match_moments_to_sections(
    moments: list[MatchMoment],
    sections: list,
) -> dict[int, MatchMoment]:
    """Map section id -> the moment that section narrates.

    Prefers an explicit minute in the narration, because that is unambiguous.
    Unclaimed moments are then handed to the remaining sections in match
    order, so a script that omits the minute still gets its clips in sequence.
    """
    assigned: dict[int, MatchMoment] = {}
    taken: set[int] = set()

    for section in sections:
        minutes = _minutes_in(getattr(section, "narration", ""))
        for index, moment in enumerate(moments):
            if index in taken:
                continue
            if moment.minute in minutes:
                assigned[section.id] = moment
                taken.add(index)
                break

    leftovers = [m for i, m in enumerate(moments) if i not in taken]
    for section in sections:
        if not leftovers:
            break
        if section.id in assigned:
            continue
        assigned[section.id] = leftovers.pop(0)
    return assigned


def analysis_gate(footage_dir: Path | None) -> tuple[bool, str]:
    """Whether supplied footage analysed successfully enough to generate.

    Returns (ok, reason). With no footage directory configured this is a
    no-op pass: the engine is then a photo pipeline and there is nothing to
    analyse. With footage present but no confident event in any file, it
    fails, so the caller can refuse to start generation rather than build a
    video around a guess.
    """
    if footage_dir is None:
        return True, "no footage directory configured; photo pipeline"
    clips = discover_local_footage(footage_dir)
    if not clips:
        return True, f"no footage files in {footage_dir}; photo pipeline"

    from core.clip_analysis import analyse_clip

    reasons = []
    for source in clips:
        result = analyse_clip(source)
        if result.ok:
            return True, (
                f"{source.name}: {len(result.events)} event(s) via {result.detector}"
            )
        reasons.append(f"{source.name}: {result.reason}")
        # A full-match recording is analysed by alignment, not clip analysis.
        if load_alignment(source, duration_seconds=probe_duration(source)) is not None:
            return True, f"{source.name}: full-match alignment present"
    return False, "; ".join(reasons)


def build_match_clips(
    *,
    workspace: Path,
    script,
    moments: list[MatchMoment],
    footage_dir: Path | None,
    allow_licensed_stock: bool,
    target_size: tuple[int, int] = (1080, 1920),
    fps: int = 30,
) -> list[FootagePlacement]:
    """Cut every moment that authorized, aligned footage actually covers.

    Returns the placements made. An empty list means the whole video falls
    back to photos, which is a supported outcome, not an error.
    """
    availability: FootageAvailability = resolve_availability(
        footage_dir=footage_dir,
        allow_licensed_stock=allow_licensed_stock,
    )
    logger.info(f"match footage: {availability.reason}")

    # `moments` comes from the verified match timeline and drives the
    # full-match alignment path. Clip mode does not need it: a standalone clip
    # carries its own event timestamps, so the run continues to the analysis
    # below even when the match itself could not be identified.
    if not moments and not availability.has_local:
        logger.info("no verified match moments and no footage; stage skipped")
        return []

    discovered: list = []
    if not availability.has_local:
        # Experimental discovery runs only when the operator supplied nothing
        # and only when MATCH_FOOTAGE_MODE=experimental. It never overrides
        # operator footage, and it cannot run with the variable unset.
        discovered = _discover_supplementary_clips(
            workspace=workspace, moments=moments
        )
        if not discovered:
            return []
        availability = FootageAvailability(
            local_clips=[c.local_path for c in discovered if c.local_path],
            licensed_enabled=availability.licensed_enabled,
            reason=f"experimental discovery: {len(discovered)} reusable clip(s)",
        )
        if not availability.has_local:
            return []

    # Clip mode: a standalone clip has no match clock, so it is analysed for
    # its own event timestamps and kickoff alignment is not consulted at all.
    # Full-match recordings carry a kickoff sidecar instead and fall through
    # to the alignment path below, unchanged.
    clip_mode = _analyse_standalone_clips(availability.local_clips)
    if clip_mode:
        return _place_analysed_clips(
            workspace=workspace,
            script=script,
            analysed=clip_mode,
            target_size=target_size,
            fps=fps,
            availability=availability,
        )

    # Align each supplied file, then resolve every moment against the first
    # file that can actually place it.
    aligned = []
    for source in availability.local_clips:
        duration = probe_duration(source)
        alignment = load_alignment(source, duration_seconds=duration)
        if alignment is not None:
            aligned.append((source, alignment, duration))
    if not aligned:
        logger.info(
            "footage was supplied but none of it carries an alignment "
            "sidecar; falling back to the photo pipeline"
        )
        return []

    assignments = match_moments_to_sections(moments, list(script.sections))
    if not assignments:
        logger.info("no section could be matched to a moment; using photos")
        return []

    windows: list[ClipWindow] = []
    pending: list[tuple[int, MatchMoment]] = []
    for section_id, moment in sorted(assignments.items()):
        for source, alignment, duration in aligned:
            offset = minute_to_video_seconds(moment.minute, alignment)
            if offset is None:
                continue
            located = MatchMoment(
                label=moment.label,
                minute=moment.minute,
                description=moment.description,
                footage_offset_seconds=offset,
            )
            window = clip_window_for(located, duration)
            if window is None:
                continue
            start, length = window
            windows.append(
                ClipWindow(
                    moment=located,
                    source=source,
                    start_seconds=start,
                    duration_seconds=length,
                    # Named for the slot the renderer will look for: the first
                    # slot of the section that narrates this moment.
                    output_name=f"section_{section_id:03d}_01.mp4",
                )
            )
            pending.append((section_id, located))
            break
        else:
            logger.info(
                f"moment {moment.label} at {moment.minute}' could not be "
                f"placed in any supplied footage; it will use photos"
            )

    if not windows:
        return []

    videos_raw = workspace / "videos" / "raw"
    produced = cut_clips(windows, videos_raw, target_size=target_size, fps=fps)
    produced_names = {p.name for p in produced}

    placements = [
        FootagePlacement(
            moment=moment,
            section_id=section_id,
            slot_index=1,
            clip_path=videos_raw / f"section_{section_id:03d}_01.mp4",
        )
        for section_id, moment in pending
        if f"section_{section_id:03d}_01.mp4" in produced_names
    ]

    _write_manifest(workspace, placements, availability)
    logger.info(
        f"match footage: {len(placements)} of {len(moments)} moment(s) rendered "
        f"from authorized footage; the rest use photos"
    )
    return placements


def _analyse_standalone_clips(sources: list[Path]) -> list[tuple[Path, list, float]]:
    """Analyse each supplied file for its own event timestamps.

    Returns [(source, events, duration)] for files that are standalone clips.
    A file whose sidecar declares a kickoff instead of events is not a clip
    and is excluded, so full-match recordings keep using alignment.
    """
    from core.clip_analysis import analyse_clip

    analysed: list[tuple[Path, list, float]] = []
    for source in sources:
        result = analyse_clip(source)
        if not result.ok:
            logger.info(
                f"clip analysis found no confident event in {source.name}: "
                f"{result.reason}"
            )
            continue
        analysed.append((source, result.events, probe_duration(source)))
        logger.info(
            f"clip mode: {source.name} -> "
            f"{[f'{e.label}@{e.seconds:.1f}s' for e in result.events]} "
            f"via {result.detector}"
        )
    return analysed


def _place_analysed_clips(
    *,
    workspace: Path,
    script,
    analysed: list[tuple[Path, list, float]],
    target_size: tuple[int, int],
    fps: int,
    availability: FootageAvailability,
) -> list[FootagePlacement]:
    """Cut analysed clip events into the script's sections, in order."""
    sections = list(script.sections)
    if not sections:
        return []

    windows: list[ClipWindow] = []
    pending: list[tuple[int, MatchMoment]] = []
    section_index = 0
    for source, events, duration in analysed:
        for event in events:
            if section_index >= len(sections):
                break
            moment = MatchMoment(
                label=event.label,
                minute=0,
                description=event.description or event.label,
                footage_offset_seconds=event.seconds,
            )
            window = clip_window_for(moment, duration)
            if window is None:
                logger.info(
                    f"{event.label} at {event.seconds:.1f}s has no usable "
                    f"window in {source.name}; that beat uses photos"
                )
                continue
            start, length = window
            section_id = sections[section_index].id
            section_index += 1
            windows.append(
                ClipWindow(
                    moment=moment,
                    source=source,
                    start_seconds=start,
                    duration_seconds=length,
                    output_name=f"section_{section_id:03d}_01.mp4",
                )
            )
            pending.append((section_id, moment))

    if not windows:
        return []

    videos_raw = workspace / "videos" / "raw"
    produced = {p.name for p in cut_clips(
        windows, videos_raw, target_size=target_size, fps=fps
    )}
    placements = [
        FootagePlacement(
            moment=moment,
            section_id=section_id,
            slot_index=1,
            clip_path=videos_raw / f"section_{section_id:03d}_01.mp4",
        )
        for section_id, moment in pending
        if f"section_{section_id:03d}_01.mp4" in produced
    ]
    _write_manifest(workspace, placements, availability)
    logger.info(f"clip mode: {len(placements)} moment clip(s) cut")
    return placements


def _discover_supplementary_clips(*, workspace: Path, moments: list[MatchMoment]) -> list:
    """Experimental online discovery. Returns [] whenever the mode is off.

    Discovered clips have no match-clock alignment, so they are only ever
    usable where a sidecar is also supplied for them. In practice this means
    the mode surfaces candidates and provenance for review rather than
    silently producing event-accurate clips.
    """
    try:
        from core.footage_discovery import discover_for_query, is_enabled

        if not is_enabled():
            return []
        query = (moments[0].description if moments else "").strip() or "football match"
        logger.info(f"experimental footage discovery for {query!r}")
        result = discover_for_query(query, workspace / "footage" / "discovered")
        _write_discovery_report(workspace, result)
        if not result.clips:
            logger.info(
                "experimental discovery found no reusable clip; using the "
                "photo pipeline"
            )
        return result.clips
    except Exception as exc:
        logger.warning(f"experimental discovery failed ({exc}); using photos")
        return []


def _write_discovery_report(workspace: Path, result) -> None:
    """Every candidate and why it was kept or dropped, for audit."""
    report = {
        "reason": result.reason,
        "accepted": [c.as_record() for c in result.clips],
        "rejected": result.rejected,
    }
    (workspace / "footage_discovery.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_manifest(
    workspace: Path,
    placements: list[FootagePlacement],
    availability: FootageAvailability,
) -> None:
    """Record what was used, so a finished video's footage is auditable."""
    manifest = {
        "sources": [str(p) for p in availability.local_clips],
        "licensed_stock_enabled": availability.licensed_enabled,
        "reason": availability.reason,
        "clips": [
            {
                "section_id": p.section_id,
                "slot_index": p.slot_index,
                "minute": p.moment.minute,
                "label": p.moment.label,
                "footage_offset_seconds": p.moment.footage_offset_seconds,
                "file": p.clip_path.name,
            }
            for p in placements
        ],
    }
    out = workspace / "footage_manifest.json"
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
