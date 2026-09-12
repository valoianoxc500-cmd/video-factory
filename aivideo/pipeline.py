"""The staged, resumable run.

Six stages, each of which writes its result to the job directory and records
itself in `state.json` before the next one starts:

    script -> footage -> voice -> captions -> render -> finalize

Resume is the whole design. A worker restart, a transient FFmpeg failure or a
provider outage should cost the stage that failed and nothing before it --
never the script that was already written or the footage already on disk,
which are the two expensive things in a run. `run()` is idempotent: calling it
again on the same directory picks up from the first incomplete stage.

Stage failures are graded, not uniform. Music is optional and its failure is
logged and skipped. Captions are near-optional and fall back to even timings.
Footage tolerates losing individual beats. Only an empty script, no footage at
all, no narration, or a render that will not validate can end a run -- and each
of those genuinely leaves nothing to ship.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from aivideo import compose, footage, script as script_stage, subtitles, voice
from aivideo.cost import CostLedger
from aivideo.spec import VideoSpec, normalise_spec

logger = logging.getLogger("aivideo")

STAGES = ("script", "footage", "voice", "captions", "render", "finalize")

#: What the customer is told while each stage runs. No provider names, no
#: stage identifiers, no status codes -- this is the entire vocabulary the UI
#: is allowed to show.
PROGRESS = {
    "script": ("Writing script", 15),
    "footage": ("Finding footage", 40),
    "voice": ("Creating voice", 60),
    "captions": ("Adding captions", 72),
    "render": ("Rendering", 88),
    "finalize": ("Finalizing", 96),
}
STARTING = ("Preparing your video", 5)

#: Free room required before rendering. A render that runs out of disk halfway
#: leaves a corrupt file and a confusing error; checking first turns that into
#: a clear, recoverable stop.
_MIN_FREE_BYTES = 2 * 1024 ** 3


class TerminalFailure(RuntimeError):
    """Producing a valid video is genuinely impossible for this request."""


class RecoverableFailure(RuntimeError):
    """Something transient. The checkpoint is intact; resuming may work."""


@dataclass
class JobState:
    completed: list[str] = field(default_factory=list)
    stage: str = ""
    script_text: str = ""
    search_terms: list[str] = field(default_factory=list)
    #: Narration beats with their visual intent. The footage stage judges
    #: candidates against `shows`, which is why relevance improved over
    #: searching the raw keyword line.
    beats: list[dict] = field(default_factory=list)
    clips: list[str] = field(default_factory=list)
    clip_windows: list[list] = field(default_factory=list)
    #: Extra ranked, de-duplicated visuals for the on-screen slots a video
    #: needs beyond one per beat.
    reserve_clips: list[str] = field(default_factory=list)
    reserve_windows: list[list] = field(default_factory=list)
    #: One provenance record per visual: provider, source page, creator,
    #: licence and the credit line, if the licence asks for one.
    attribution: list[dict] = field(default_factory=list)
    missing_terms: list[str] = field(default_factory=list)
    narration_seconds: float = 0.0
    providers_used: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    #: Itemised, so "$0.0007" can be shown to be one script call rather than
    #: an unexplained number. Everything else in the pipeline is free.
    cost_items: list[dict] = field(default_factory=list)
    output: str = ""
    thumbnail: str = ""
    error: str = ""

    def done(self, stage: str) -> bool:
        return stage in self.completed

    def finish(self, stage: str) -> None:
        if stage not in self.completed:
            self.completed.append(stage)

    def note_fallback(self, detail: str) -> None:
        if detail not in self.fallbacks:
            self.fallbacks.append(detail)


def _state_path(directory: Path) -> Path:
    return directory / "state.json"


def load_state(directory: Path) -> JobState:
    path = _state_path(directory)
    if not path.exists():
        return JobState()
    try:
        return JobState(**json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        # A corrupt checkpoint must not strand the job forever. Starting over
        # costs one run; refusing to parse costs the customer their video.
        logger.warning(f"[aivideo] unreadable checkpoint ({exc}); starting fresh")
        return JobState()


def save_state(directory: Path, state: JobState) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(directory).with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(_state_path(directory))


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return _MIN_FREE_BYTES


def _all_visuals(state: "JobState") -> list[Path]:
    """Every distinct visual available, beat picks first then reserves."""
    return [Path(p) for p in [*state.clips, *state.reserve_clips]]


def _window_for(state: "JobState", index: int) -> tuple[float, float]:
    windows = [*state.clip_windows, *state.reserve_windows]
    if 0 <= index < len(windows) and windows[index]:
        return float(windows[index][0]), float(windows[index][1])
    return 0.0, 4.0


def _slot_plan(
    state: "JobState", duration: float, spec: VideoSpec
) -> list[tuple[int, Path, int]]:
    """(visual index, source, how many times it has been used already).

    Distinct visuals are spent before any of them is shown twice, so a video
    only repeats once it has genuinely run out of footage.
    """
    visuals = [(i, p) for i, p in enumerate(_all_visuals(state)) if p.exists()]
    if not visuals:
        return []
    plan = []
    for slot in range(compose.slots_needed(duration, spec)):
        index, source = visuals[slot % len(visuals)]
        plan.append((index, source, slot // len(visuals)))
    return plan


def _grouped(items: list[dict]) -> list[dict]:
    """One cost line per label, summing the calls behind it."""
    order: list[str] = []
    rolled: dict[str, dict] = {}
    for item in items:
        label = str(item.get("label") or "other")
        if label not in rolled:
            order.append(label)
            rolled[label] = {"label": label, "model": item.get("model", ""), "calls": 0, "usd": 0.0}
        row = rolled[label]
        row["calls"] += 1
        row["usd"] = round(row["usd"] + float(item.get("usd") or 0), 6)
        for field_name in ("input_tokens", "output_tokens", "images"):
            if field_name in item:
                row[field_name] = row.get(field_name, 0) + int(item[field_name] or 0)
    return [rolled[label] for label in order]


def _later_window(source: Path, start: float, span: float, occurrence: int) -> float:
    """Where to cut a clip that is being shown for the second or third time.

    Steps further into the source so the repeat is different footage. Falls
    back to the original in-point when the source is too short to move --
    showing the same seconds again is still better than cutting past the end
    into black.
    """
    try:
        info = compose.probe(source)
        total = float((info.get("format") or {}).get("duration") or 0)
    except Exception:
        total = 0.0
    if total <= 0:
        return start
    shifted = start + span * occurrence
    return shifted if shifted + span <= total else start


def pick_music(spec: VideoSpec, library: Path | None) -> Path | None:
    """A track we are licensed to use, or None.

    `auto` picks deterministically from whatever the deployment shipped, so a
    given job always sounds the same on resume. Absent library, absent track
    and an unreadable directory all mean "no music", never an error: music is
    the most optional thing in the video.
    """
    if spec.music == "none" or library is None:
        return None
    try:
        tracks = sorted(
            p for p in library.glob("*")
            if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"}
        )
    except Exception:
        return None
    if not tracks:
        return None
    if spec.music not in {"auto", ""}:
        named = [t for t in tracks if t.stem == spec.music]
        if named:
            return named[0]
    return tracks[abs(hash(spec.topic)) % len(tracks)]


async def run(
    raw_spec: dict,
    directory: Path,
    *,
    music_library: Path | None = None,
    on_progress=None,
) -> JobState:
    """Generate one video, resuming whatever is already done.

    `on_progress(label, percent)` is called at each stage boundary with the
    customer-facing wording only.
    """
    spec = normalise_spec(raw_spec)
    if not spec.topic:
        raise TerminalFailure("no topic was provided")

    directory.mkdir(parents=True, exist_ok=True)
    state = load_state(directory)
    started = time.monotonic()

    def progress(stage: str) -> None:
        state.stage = stage
        save_state(directory, state)
        if on_progress:
            label, percent = PROGRESS.get(stage, STARTING)
            try:
                on_progress(label, percent)
            except Exception:
                pass          # progress reporting must never fail a render

    if on_progress and not state.completed:
        on_progress(*STARTING)

    # ── script ───────────────────────────────────────────────────────
    ledger = CostLedger()
    if not state.done("script"):
        progress("script")
        try:
            text, beats = await script_stage.write_script(
                spec.topic,
                duration_seconds=spec.duration_seconds,
                language=spec.language,
                ledger=ledger,
            )
        except Exception as exc:
            raise RecoverableFailure(f"script stage: {exc}") from exc
        state.script_text = text
        state.beats = [
            {"says": b.says, "shows": b.shows, "terms": b.terms} for b in beats
        ]
        # Kept alongside the beats so an older resume path, and the reporting
        # that reads it, still work.
        state.search_terms = [b.terms[0] for b in beats if b.terms]
        # The only metered call in the pipeline. Added to whatever a previous
        # attempt already spent, so a resumed job reports its true total rather
        # than only the last attempt's.
        state.cost_usd = round(state.cost_usd + ledger.total_usd, 6)
        state.cost_items = ledger.summary()["items"]
        state.finish("script")
        save_state(directory, state)
    # Everything the script stage spent is now banked in `state`, so the
    # ledger starts empty again for the stages that follow and a resumed job
    # cannot bill the same call twice.
    ledger = CostLedger()

    # ── footage ──────────────────────────────────────────────────────
    clips_dir = directory / "clips"
    if not state.done("footage"):
        progress("footage")
        try:
            beats = [
                script_stage.Beat(
                    says=b.get("says", ""),
                    shows=b.get("shows", ""),
                    terms=list(b.get("terms") or []),
                )
                for b in state.beats
            ] or [script_stage.Beat(shows=t, terms=[t]) for t in state.search_terms]

            reserves: list = []
            clips, missing = await footage.gather(
                beats,
                clips_dir,
                portrait=spec.aspect_ratio == "9:16",
                ledger=ledger,
                reserves=reserves,
            )
        except footage.NoFootage as exc:
            raise TerminalFailure(str(exc)) from exc
        except Exception as exc:
            raise RecoverableFailure(f"footage stage: {exc}") from exc

        # Ranking is a metered vision call per beat and is usually the largest
        # line in a generation; reporting the script call alone understated a
        # video's cost by roughly an order of magnitude. Grouped by label
        # rather than listed per call, because seventeen identical rows is not
        # an itemisation -- but grouped, not merged, so a generated visual
        # does not vanish inside the ranking line.
        state.cost_usd = round(state.cost_usd + ledger.total_usd, 6)
        state.cost_items = [*state.cost_items, *_grouped(ledger.summary()["items"])]
        state.clips = [str(c.path) for c in clips]
        # The shot-aligned window each clip should be cut from, kept beside
        # the path so a resumed render does not have to re-detect shots.
        state.clip_windows = [list(c.window or (0.0, 4.0)) for c in clips]
        # Runner-up visuals for the slots beyond one per beat. Showing these
        # is what keeps the back half of a video from replaying the front.
        state.reserve_clips = [str(c.path) for c in reserves]
        state.reserve_windows = [list(c.window or (0.0, 4.0)) for c in reserves]
        # Where every frame came from and on what basis. Kept with the job
        # because a licence condition that is only met at selection time is
        # not met at all -- if anyone asks later why a shot was used, this is
        # the answer.
        state.attribution = [c.provenance() for c in (*clips, *reserves)]
        state.missing_terms = missing
        state.providers_used = sorted({c.provider for c in clips})
        if missing:
            state.note_fallback(
                f"{len(missing)} beat(s) had no footage; neighbouring clips held longer"
            )
        state.finish("footage")
        save_state(directory, state)

    # ── voice ────────────────────────────────────────────────────────
    narration_path = directory / "narration.mp3"
    words_path = directory / "words.json"
    if not state.done("voice"):
        progress("voice")
        try:
            narration = await voice.synthesize(
                state.script_text, spec.voice, narration_path,
                rate=spec.voice_rate, volume=1.0,
            )
        except voice.VoiceUnavailable as exc:
            raise RecoverableFailure(f"voice stage: {exc}") from exc

        words_path.write_text(
            json.dumps([w.__dict__ for w in narration.words], ensure_ascii=False),
            encoding="utf-8",
        )
        state.narration_seconds = narration.duration
        # Recorded as a line rather than omitted: "$0.00 (edge)" is what shows
        # the narration really is free.
        ledger.record_free("voice", narration.provider)
        state.cost_items = [*state.cost_items, *ledger.summary()["items"][-1:]]
        if not narration.has_timings:
            state.note_fallback("caption timings estimated from the script")
        state.finish("voice")
        save_state(directory, state)

    duration = state.narration_seconds or float(spec.duration_seconds)

    # ── captions ─────────────────────────────────────────────────────
    caption_path = directory / "captions.ass"
    if not state.done("captions"):
        progress("captions")
        try:
            words = [
                voice.Word(**w)
                for w in json.loads(words_path.read_text(encoding="utf-8"))
            ]
            built = subtitles.build_ass(
                words, spec.captions, spec.size, caption_path,
                language=spec.language,
            )
            if built is None and spec.captions.enabled:
                state.note_fallback("captions were skipped")
        except Exception as exc:
            # A caption failure is not worth losing the video over.
            logger.warning(f"[aivideo] captions failed ({exc}); rendering without")
            state.note_fallback("captions were skipped after an error")
            caption_path.unlink(missing_ok=True)
        state.finish("captions")
        save_state(directory, state)

    # ── render ───────────────────────────────────────────────────────
    output = directory / "video.mp4"
    if not state.done("render"):
        progress("render")
        if free_bytes(directory) < _MIN_FREE_BYTES:
            raise RecoverableFailure(
                "not enough free disk space to render; freeing space and "
                "resuming will continue from here"
            )

        normalised_dir = directory / "normalised"
        normalised: list[Path] = []
        # One cut per on-screen slot. A video always needs more slots than it
        # has beats, and cycling the identical cut is what made the second
        # half look like the first: the same shots, in the same order, again.
        #
        # The slots beyond one-per-beat are filled first from the reserves --
        # runner-up candidates the footage stage already ranked and
        # de-duplicated -- and only after those run out by re-cutting a
        # source at a later in-point.
        for slot, (index, source, occurrence) in enumerate(
            _slot_plan(state, duration, spec)
        ):
            target = normalised_dir / f"n_{index:02d}_{occurrence}.mp4"
            if target.exists() and target.stat().st_size > 10_000:
                normalised.append(target)        # kept across a resume
                continue
            window = _window_for(state, index)
            start = float(window[0])
            span = min(spec.clip_seconds, float(window[1]))
            if occurrence:
                start = _later_window(source, start, span, occurrence)
            try:
                normalised.append(
                    compose.normalise_clip(source, target, spec, span, start)
                )
            except Exception as exc:
                # Exactly the behaviour the product promises: drop this clip,
                # keep the video.
                logger.warning(
                    f"[aivideo] clip {source.name} could not be prepared "
                    f"({type(exc).__name__}); dropping it"
                )
                state.note_fallback(f"replaced an unusable clip ({source.name})")

        if not normalised:
            raise TerminalFailure("none of the downloaded footage could be prepared")

        try:
            visual = compose.build_visual_track(
                normalised, directory / "visual.mp4", duration, spec
            )
            music = pick_music(spec, music_library)
            try:
                compose.render(
                    visual=visual, narration=narration_path, output=output,
                    spec=spec,
                    captions=caption_path if caption_path.exists() else None,
                    music=music, duration=duration,
                )
            except compose.RenderFailed:
                if music is None:
                    raise
                # Music is the likeliest thing in the mux to be malformed.
                logger.warning("[aivideo] render failed with music; retrying silent")
                state.note_fallback("rendered without background music")
                compose.render(
                    visual=visual, narration=narration_path, output=output,
                    spec=spec,
                    captions=caption_path if caption_path.exists() else None,
                    music=None, duration=duration,
                )
        except compose.RenderFailed as exc:
            raise RecoverableFailure(f"render stage: {exc}") from exc

        state.finish("render")
        save_state(directory, state)

    # ── finalize ─────────────────────────────────────────────────────
    if not state.done("finalize"):
        progress("finalize")
        ok, detail = compose.validate_output(output)
        if not ok:
            # Reopen render rather than shipping it: the file on disk is not a
            # video, whatever the previous stage believed.
            state.completed = [s for s in state.completed if s != "render"]
            save_state(directory, state)
            raise RecoverableFailure(f"final validation: {detail}")

        thumb = compose.poster_frame(output, directory / "thumbnail.jpg")
        state.output = str(output)
        state.thumbnail = str(thumb) if thumb else ""
        state.finish("finalize")
        state.stage = ""
        save_state(directory, state)

    logger.info(
        f"[aivideo] finished in {time.monotonic() - started:.0f}s "
        f"(providers: {', '.join(state.providers_used) or 'none'}; "
        f"fallbacks: {len(state.fallbacks)})"
    )
    return state
