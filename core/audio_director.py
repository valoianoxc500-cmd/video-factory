"""Per-scene sound: what each section should sound like, and where it comes from.

The problem this solves
-----------------------
The existing audio is one music bed looped under the whole video plus a whoosh
at most section boundaries. That is fine, and it is also the same for every
video and every scene inside it. A section describing a storm at sea and the
one describing a police interview room get identical sound.

This reads each section and gives it its own ambience and its own cues, then
renders them as two more full-length layers the assembler mixes in. It does
not touch narration, the music bed, or the transition track -- those keep
working exactly as they did, and this is additive and behind a config flag.

Where sound comes from, and in what order
-----------------------------------------
1. **Freesound**, first, for ambience. A room tone or a rain bed that was
   actually recorded sounds like a place; a generated one usually sounds like
   an idea of a place. Recordings are also free, and generation is not.
2. **ElevenLabs**, when Freesound has nothing close enough, and for specific
   one-off cues no catalogue holds -- "a chair scraping back on a stone floor,
   once, close" is a prompt, not a search query.

Not repeating itself
--------------------
Two mechanisms, because they fail differently. A `used` set retires each
Freesound id and each generation prompt for the rest of the run, so no sound
lands twice. And each section's queries come from that section's own text, so
two sections do not ask the same question in the first place.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("video_factory")

#: Layer levels, relative to narration at 1.0. Ambience sits well under the
#: voice -- it is a place, not a subject -- while a deliberate cue is allowed
#: to be heard.
AMBIENCE_VOLUME = 0.16
SFX_VOLUME = 0.34

#: Ambience fades so a scene change does not click.
AMBIENCE_FADE_SECONDS = 1.2
#: A cue is ducked in and out over this long, which is enough to stop a click
#: without smearing a transient.
SFX_FADE_SECONDS = 0.08

#: Generation is metered, so a run is capped. Freesound covers the rest.
MAX_GENERATED_CUES = 8

#: An ambience bed shorter than this is a sound effect, not a bed.
MIN_AMBIENCE_SECONDS = 8.0


@dataclass
class Cue:
    """One sound, and where it sits."""

    kind: str                      # "ambience" | "sfx"
    section_id: int
    #: Seconds from the start of the video.
    start: float
    duration: float
    #: What to search Freesound for.
    query: str = ""
    #: What to generate if searching fails. Written as a description.
    prompt: str = ""
    path: Path | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def sourced(self) -> bool:
        return self.path is not None and self.path.exists()


@dataclass
class AudioPlan:
    cues: list[Cue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ambience(self) -> list[Cue]:
        return [c for c in self.cues if c.kind == "ambience"]

    @property
    def sfx(self) -> list[Cue]:
        return [c for c in self.cues if c.kind == "sfx"]

    def to_record(self) -> dict:
        return {
            "cues": [
                {
                    "kind": c.kind,
                    "section_id": c.section_id,
                    "start": round(c.start, 2),
                    "duration": round(c.duration, 2),
                    "query": c.query,
                    "prompt": c.prompt,
                    "sourced": c.sourced,
                    "source": c.provenance.get("platform", ""),
                    "licence": c.provenance.get("licence", ""),
                    "attribution": c.provenance.get("attribution", ""),
                }
                for c in self.cues
            ],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Reading the script
# ---------------------------------------------------------------------------

_PLAN_SCHEMA = """Respond with JSON only:
{
  "scenes": [
    {
      "section_id": 1,
      "ambience": "3-6 words naming the PLACE this scene happens in, as a
                   sound: 'empty stone corridor', 'heavy rain on canvas',
                   'crowded stadium concourse'. Not a mood, not music.",
      "ambience_prompt": "one sentence describing that same ambience, for a
                          generator, if no recording exists",
      "sfx": [
        {
          "at": 0.0,
          "query": "2-4 words naming ONE discrete sound heard in this scene",
          "prompt": "one sentence describing that sound, close and single"
        }
      ]
    }
  ]
}
Rules:
- One ambience per scene. Different scenes must get different ambiences.
- At most two sfx per scene, and only where the narration implies a specific
  sound. A scene with nothing to hear gets an empty list.
- `at` is seconds from the start of that scene, never past its end.
- Name what is physically audible. Never name music, score, or a mood."""


def build_plan_prompt(script, sections: list[dict]) -> str:
    lines = [
        "Give each scene of this narration its own ambience and sound cues.",
        "",
        f"Title: {getattr(script, 'title', '')}",
        "",
    ]
    for section in sections:
        lines.append(
            f"Scene {section['id']} ({section['duration']:.0f}s): "
            f"{section['text'][:600]}"
        )
    lines += ["", _PLAN_SCHEMA]
    return "\n".join(lines)


def _sections_of(script) -> list[dict]:
    out: list[dict] = []
    start = 0.0
    for section in getattr(script, "sections", []) or []:
        duration = float(
            getattr(section, "actual_duration_seconds", 0.0)
            or getattr(section, "estimated_duration_seconds", 0.0)
            or 0.0
        )
        out.append({
            "id": int(getattr(section, "id", len(out) + 1)),
            "text": str(getattr(section, "narration", "") or ""),
            "start": start,
            "duration": duration,
        })
        start += duration
    return out


_STOPWORDS = frozenset("""
a an the of in on at to from with and or for by as is are was were be been this
that these those it its his her their there here into onto over under above
""".split())


def _fallback_ambience(text: str) -> str:
    """A query from the section's own words, when no model is available."""
    words = [
        word for word in re.findall(r"[a-zA-Z]{3,}", text.lower())
        if word not in _STOPWORDS
    ]
    return " ".join(words[:3])


async def plan_audio(script, *, model_call=None) -> AudioPlan:
    """Read the script and decide what each scene should sound like.

    The model is asked once for the whole script so it can keep the scenes
    distinct from one another. Without one, each scene still gets an ambience
    query drawn from its own text -- weaker, but still per-scene rather than
    one bed for everything.
    """
    sections = _sections_of(script)
    plan = AudioPlan()
    if not sections:
        return plan

    payload: dict = {}
    if model_call is None:
        try:
            import clients

            model_call = clients.generate_json
        except Exception:
            model_call = None

    if model_call is not None:
        try:
            payload = await model_call(
                build_plan_prompt(script, sections),
                system_instruction=(
                    "You design the sound of a documentary scene. Name what is "
                    "physically audible in the place being described. Never "
                    "name music or a mood."
                ),
                operation_label="scene_audio_plan",
            ) or {}
        except Exception as exc:
            logger.info(f"scene audio plan unavailable, using fallback: {exc}")
            payload = {}

    by_id = {
        int(scene.get("section_id") or 0): scene
        for scene in (payload.get("scenes") or [])
        if isinstance(scene, dict)
    }

    for section in sections:
        if section["duration"] <= 0:
            continue
        scene = by_id.get(section["id"], {})

        query = str(scene.get("ambience") or "").strip()
        if not query:
            query = _fallback_ambience(section["text"])
            plan.notes.append(f"scene {section['id']}: ambience from its own words")
        if query:
            plan.cues.append(Cue(
                kind="ambience",
                section_id=section["id"],
                start=section["start"],
                duration=section["duration"],
                query=query,
                prompt=str(scene.get("ambience_prompt") or query).strip(),
            ))

        for entry in (scene.get("sfx") or [])[:2]:
            if not isinstance(entry, dict):
                continue
            cue_query = str(entry.get("query") or "").strip()
            if not cue_query:
                continue
            try:
                at = max(0.0, float(entry.get("at") or 0.0))
            except (TypeError, ValueError):
                at = 0.0
            # A cue past the end of its own scene would land under the next one.
            at = min(at, max(0.0, section["duration"] - 0.5))
            plan.cues.append(Cue(
                kind="sfx",
                section_id=section["id"],
                start=section["start"] + at,
                duration=min(4.0, section["duration"]),
                query=cue_query,
                prompt=str(entry.get("prompt") or cue_query).strip(),
            ))

    return plan


# ---------------------------------------------------------------------------
# Sourcing
# ---------------------------------------------------------------------------

async def source_cues(
    plan: AudioPlan,
    destination: Path,
    *,
    client: httpx.AsyncClient | None = None,
    freesound=None,
    elevenlabs=None,
    max_generated: int = MAX_GENERATED_CUES,
) -> AudioPlan:
    """Find or generate a file for every cue, never using one twice."""
    from core.providers.audio import ElevenLabsSfxProvider, FreesoundProvider

    freesound = freesound if freesound is not None else FreesoundProvider()
    elevenlabs = elevenlabs if elevenlabs is not None else ElevenLabsSfxProvider()
    destination.mkdir(parents=True, exist_ok=True)

    owns = client is None
    http = client or httpx.AsyncClient(timeout=90.0, follow_redirects=True)

    used: set[str] = set()
    generated = 0

    try:
        for index, cue in enumerate(plan.cues, start=1):
            wanted = MIN_AMBIENCE_SECONDS if cue.kind == "ambience" else 0.0
            item = None

            if freesound.status().usable and cue.query:
                try:
                    results = await freesound.search(
                        cue.query,
                        client=http,
                        limit=12,
                        min_duration=wanted,
                        max_duration=0.0 if cue.kind == "ambience" else 12.0,
                    )
                except Exception as exc:
                    logger.info(f"freesound search failed for {cue.query!r}: {exc}")
                    results = []
                # The first result this run has not already used.
                item = next(
                    (r for r in results if f"freesound:{r.provider_id}" not in used),
                    None,
                )

            target = destination / f"{cue.kind}_{index:03d}.mp3"

            if item is not None:
                try:
                    response = await http.get(item.url)
                    response.raise_for_status()
                    target.write_bytes(response.content)
                except Exception as exc:
                    logger.info(f"freesound download failed for {cue.query!r}: {exc}")
                    item = None
                else:
                    used.add(f"freesound:{item.provider_id}")
                    cue.path = target
                    cue.provenance = {
                        **item.to_provenance(),
                        "title": item.title,
                        "credit": item.credit_line(),
                        "cue": cue.query,
                    }
                    continue

            # Nothing recorded fits. Generate it, within the run's budget.
            fingerprint = f"eleven:{hashlib.md5(cue.prompt.encode()).hexdigest()}"
            if (
                elevenlabs.status().usable
                and cue.prompt
                and generated < max_generated
                and fingerprint not in used
            ):
                try:
                    audio = await elevenlabs.generate(
                        cue.prompt,
                        client=http,
                        duration_seconds=(
                            min(22.0, cue.duration) if cue.kind == "ambience" else 3.0
                        ),
                        loop=cue.kind == "ambience",
                    )
                except Exception as exc:
                    logger.info(f"generation failed for {cue.prompt[:40]!r}: {exc}")
                    continue
                target.write_bytes(audio)
                used.add(fingerprint)
                generated += 1
                cue.path = target
                cue.provenance = {
                    **elevenlabs.describe(cue.prompt, loop=cue.kind == "ambience"),
                    "cue": cue.query,
                }

        sourced = sum(1 for c in plan.cues if c.sourced)
        plan.notes.append(
            f"{sourced}/{len(plan.cues)} cues sourced, {generated} generated"
        )
        return plan
    finally:
        if owns:
            await http.aclose()


# ---------------------------------------------------------------------------
# Rendering the layers
# ---------------------------------------------------------------------------

def _run_ffmpeg(args: list[str]) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"audio layer render failed: {(result.stderr or '')[-300:]}")
        return False
    return True


def render_layer(
    cues: list[Cue],
    total_duration: float,
    output_path: Path,
    *,
    fade: float,
    loop_to_fill: bool,
) -> bool:
    """Place every sourced cue on one silent bed of the right length.

    One file per layer rather than one per cue: the assembler then gains a
    single input, and the timing is baked in here where it can be reasoned
    about, rather than spread across a filtergraph built somewhere else.
    """
    placed = [cue for cue in cues if cue.sourced]
    if not placed or total_duration <= 0:
        return False

    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []

    # A silent bed guarantees the layer is exactly as long as the video, so a
    # short final cue cannot truncate the mix.
    inputs += ["-f", "lavfi", "-t", f"{total_duration:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]

    for index, cue in enumerate(placed, start=1):
        inputs += ["-i", str(cue.path)]
        length = max(0.5, min(cue.duration, total_duration - cue.start))
        chain = []
        if loop_to_fill:
            # An ambience bed is usually shorter than its scene.
            chain.append("aloop=loop=-1:size=2e+09")
        chain.append(f"atrim=0:{length:.3f}")
        chain.append("asetpts=PTS-STARTPTS")
        if fade > 0 and length > fade * 2:
            chain.append(f"afade=t=in:st=0:d={fade:.3f}")
            chain.append(f"afade=t=out:st={length - fade:.3f}:d={fade:.3f}")
        chain.append(f"adelay={int(cue.start * 1000)}|{int(cue.start * 1000)}")
        filters.append(f"[{index}:a]{','.join(chain)}[c{index}]")
        labels.append(f"[c{index}]")

    filters.append(
        f"[0:a]{''.join(labels)}amix=inputs={len(labels) + 1}:"
        f"duration=first:normalize=0[out]"
    )

    return _run_ffmpeg([
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
        "-t", f"{total_duration:.3f}",
        "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
        str(output_path),
    ])


def save_audio_provenance(plan: AudioPlan, workspace: Path) -> Path:
    """The source, licence and credit for every sound in the video."""
    out = workspace / "audio_provenance.json"
    out.write_text(
        json.dumps(plan.to_record(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def required_credits(plan: AudioPlan) -> list[str]:
    """Credit lines the description must carry, deduplicated."""
    seen: list[str] = []
    for cue in plan.cues:
        credit = str(cue.provenance.get("credit") or "")
        if credit and credit not in seen:
            seen.append(credit)
    return seen


async def build_scene_audio(
    script,
    workspace: Path,
    total_duration: float,
    *,
    client: httpx.AsyncClient | None = None,
) -> AudioPlan:
    """Plan, source and render the two per-scene layers.

    Returns the plan whether or not anything was sourced, so the caller can
    record what was attempted. A failure here costs the video its ambience,
    never the video.
    """
    audio_dir = workspace / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    plan = await plan_audio(script)
    if not plan.cues:
        return plan

    await source_cues(plan, audio_dir / "cues", client=client)

    render_layer(
        plan.ambience, total_duration, audio_dir / "ambience.wav",
        fade=AMBIENCE_FADE_SECONDS, loop_to_fill=True,
    )
    render_layer(
        plan.sfx, total_duration, audio_dir / "scene_sfx.wav",
        fade=SFX_FADE_SECONDS, loop_to_fill=False,
    )

    save_audio_provenance(plan, workspace)
    return plan
