"""Stage 2b: Audio sourcing — per-section TTS + STT word timestamps.

Generates narration per-section for reliable coverage (no TTS truncation),
transcribes each section with Google Cloud STT for word-level timestamps,
and concatenates into narration_full.wav for the assembler.
"""

import asyncio
import hashlib
import json
import logging
import random
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from google.api_core.exceptions import InvalidArgument
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech

import clients
from core import caption_language, costs
from core.caption_integrity import (
    describe_drift,
    drifted_indices as _drifted_indices,
    repair_word_timestamps,
)
from core.utils import Script, ScriptSection, ChannelConfig, save_script


def contains_drift(words: list[dict]) -> bool:
    """Whether any transcript token left the Arabic script."""
    return bool(_drifted_indices(words or []))
from settings import settings, PROJECT_ROOT, ASSETS_DIR

logger = logging.getLogger("video_factory")

def _is_arabic(locale: str) -> bool:
    """Whether a resolved STT locale is an Arabic one."""
    return str(locale or "").lower().startswith("ar")


_STT_LOCALE_MAP = {
    "es": "es-US", "es-419": "es-US", "en": "en-US", "pt": "pt-BR",
    # Speech-to-Text V2 recognizes no ar-SA variant in any region. ar-EG is
    # the Modern Standard Arabic locale that is served, and it transcribes
    # MSA narration produced for ar-SA voices correctly.
    "ar": "ar-EG", "ar-SA": "ar-EG", "ar-AE": "ar-EG", "ar-XA": "ar-EG",
}

# Recognition models to try, in order, per language prefix. "long" has no
# Arabic coverage; chirp_2 does (us-central1). Falling through the list keeps
# a region/model change from breaking a language outright.
_STT_MODEL_LADDER: dict[str, tuple[str, ...]] = {
    "ar": ("chirp_2", "long", "short"),
}
_STT_DEFAULT_MODELS = ("long", "short", "chirp_2")

# Resolved (location, language) -> model, so the ladder is walked once per run.
_STT_MODEL_CACHE: dict[tuple[str, str], str] = {}


def _stt_models_for_language(stt_lang: str) -> tuple[str, ...]:
    return _STT_MODEL_LADDER.get(stt_lang.split("-")[0], _STT_DEFAULT_MODELS)


# ---------------------------------------------------------------------------
# Manifest (cache) helpers
# ---------------------------------------------------------------------------

def _audio_manifest_path(sections_dir: Path) -> Path:
    return sections_dir / "manifest.json"


def _load_audio_manifest(sections_dir: Path) -> dict[str, dict]:
    manifest_path = _audio_manifest_path(sections_dir)
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read audio manifest, regenerating: {e}")
        return {}


def _save_audio_manifest(sections_dir: Path, manifest: dict[str, dict]) -> None:
    manifest_path = _audio_manifest_path(sections_dir)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def _section_audio_fingerprint(
    narration: str, voice_prompt: str, voice_name: str, language: str,
    provider: str = "", voice_id: str = "",
) -> str:
    """Cache key for a single section's narration audio.

    Provider and voice id are in the key because switching a channel to a
    different narrator must re-speak its sections. Without them a workspace
    reused across the change keeps the old voice for every cached section and
    speaks only the new ones in the new voice, which is audible and very hard
    to attribute to a cache.
    """
    payload = {
        "narration": narration,
        "voice_prompt": voice_prompt,
        "voice_name": voice_name,
        "language": language,
        "model": settings.gemini_tts_model,
        "provider": provider,
        "voice_id": voice_id,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _wav_duration(path: Path) -> float:
    """Read WAV duration from header."""
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def _concat_wavs(wav_paths: list[Path], output_path: Path) -> None:
    """Concatenate multiple WAV files into one (must share sample format)."""
    with wave.open(str(wav_paths[0]), "rb") as first:
        params = first.getparams()

    with wave.open(str(output_path), "wb") as out:
        out.setparams(params)
        for p in wav_paths:
            with wave.open(str(p), "rb") as wf:
                out.writeframes(wf.readframes(wf.getnframes()))


# ---------------------------------------------------------------------------
# Speech-to-Text transcription with word timestamps
# ---------------------------------------------------------------------------

_MAX_CHUNK_SECONDS = 55  # STT sync limit is 60s; leave margin
_STT_MAX_WORKERS = 5     # cap concurrent STT requests to avoid rate limits


def _transcribe_single_chunk(
    client: SpeechClient,
    recognizer: str,
    config: cloud_speech.RecognitionConfig,
    chunk_raw: bytes,
    wav_path: Path,
    chunk_idx: int,
    nchannels: int,
    sampwidth: int,
    framerate: int,
    time_offset: float,
) -> list[dict]:
    """Transcribe a single audio chunk via Google Cloud STT V2.

    Returns list of {"word": str, "start": float, "end": float} with
    timestamps adjusted by time_offset.
    """
    chunk_path = wav_path.with_suffix(f".chunk{chunk_idx}.wav")
    with wave.open(str(chunk_path), "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(chunk_raw)

    content = chunk_path.read_bytes()
    request = cloud_speech.RecognizeRequest(
        recognizer=recognizer,
        config=config,
        content=content,
    )
    response = client.recognize(request=request)

    words: list[dict] = []
    for result in response.results:
        alt = result.alternatives[0] if result.alternatives else None
        if not alt:
            continue
        for w in alt.words:
            words.append({
                "word": w.word,
                "start": w.start_offset.total_seconds() + time_offset,
                "end": w.end_offset.total_seconds() + time_offset,
            })

    chunk_path.unlink(missing_ok=True)
    return words


def _transcribe_with_timestamps(
    wav_path: Path,
    language: str,
) -> list[dict]:
    """Transcribe audio via Google Cloud STT V2, return word-level timestamps.

    Splits audio into <=55s chunks to stay within sync API limits,
    then runs all chunks in parallel (capped at _STT_MAX_WORKERS) and
    merges results with time offsets.

    Returns list of {"word": str, "start": float, "end": float}.
    """
    import google.auth
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _, project = google.auth.default()
    project = settings.google_project_id or project

    location = settings.google_stt_location
    client = SpeechClient(
        client_options={"api_endpoint": f"{location}-speech.googleapis.com"},
    )

    stt_lang = _STT_LOCALE_MAP.get(language, language)
    recognizer = f"projects/{project}/locations/{location}/recognizers/_"

    def _config_for(model_name: str) -> cloud_speech.RecognitionConfig:
        return cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            language_codes=[stt_lang],
            model=model_name,
            features=cloud_speech.RecognitionFeatures(
                enable_word_time_offsets=True,
            ),
        )

    # Read WAV parameters
    with wave.open(str(wav_path), "rb") as wf:
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        sampwidth = wf.getsampwidth()
        nchannels = wf.getnchannels()
        raw = wf.readframes(n_frames)
    audio_seconds = n_frames / framerate if framerate else 0.0

    chunk_frames = int(_MAX_CHUNK_SECONDS * framerate)
    bytes_per_frame = sampwidth * nchannels

    # Build list of (chunk_idx, chunk_raw, time_offset)
    chunks: list[tuple[int, bytes, float]] = []
    for chunk_idx, start_frame in enumerate(range(0, n_frames, chunk_frames)):
        end_frame = min(start_frame + chunk_frames, n_frames)
        chunk_raw = raw[start_frame * bytes_per_frame : end_frame * bytes_per_frame]
        time_offset = start_frame / framerate
        chunks.append((chunk_idx, chunk_raw, time_offset))

    def _run_all_chunks(recognition_config: cloud_speech.RecognitionConfig) -> list[dict]:
        results_by_idx: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=_STT_MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _transcribe_single_chunk,
                    client, recognizer, recognition_config,
                    chunk_raw, wav_path, idx,
                    nchannels, sampwidth, framerate, time_offset,
                ): idx
                for idx, chunk_raw, time_offset in chunks
            }
            for future in as_completed(futures):
                results_by_idx[futures[future]] = future.result()
        merged: list[dict] = []
        for idx in range(len(chunks)):
            merged.extend(results_by_idx[idx])
        return merged

    # Walk the model ladder: a model/locale/region combination that the API
    # rejects outright (InvalidArgument) is skipped rather than failing the run.
    cache_key = (location, stt_lang)
    candidates = (
        (_STT_MODEL_CACHE[cache_key],)
        if cache_key in _STT_MODEL_CACHE
        else _stt_models_for_language(stt_lang)
    )

    last_error: Exception | None = None
    drifted_fallback: list[dict] | None = None
    for model in candidates:
        try:
            all_words = _run_all_chunks(_config_for(model))
        except InvalidArgument as e:
            last_error = e
            logger.warning(
                f"STT model '{model}' rejected {stt_lang} in {location}: "
                f"{str(e).strip()[:160]}"
            )
            continue

        # An Arabic transcript containing Persian/Urdu letters means the
        # recognizer changed language mid-audio. Prefer another model over
        # accepting it -- but keep the result, because the ladder may have
        # nothing better and a repaired transcript still beats no timings.
        if _is_arabic(stt_lang) and contains_drift(all_words):
            logger.warning(
                f"STT model '{model}' drifted out of Arabic: "
                f"{describe_drift(all_words)}"
            )
            if drifted_fallback is None:
                drifted_fallback = all_words
            # Do not cache a model that produced drift.
            _STT_MODEL_CACHE.pop(cache_key, None)
            continue

        _STT_MODEL_CACHE[cache_key] = model
        logger.info(
            f"STT transcribed {len(all_words)} words in {len(chunks)} chunk(s) "
            f"(model={model}, lang={stt_lang})"
        )
        costs.record_stt_cost(
            model=model,
            audio_seconds=audio_seconds,
            operation="stt_transcribe",
        )
        return all_words

    if drifted_fallback is not None:
        # Every model that ran drifted. Hand the best one back so the caller
        # can repair it against the narration rather than losing STT timings
        # altogether.
        logger.warning(
            f"every STT model drifted out of Arabic for {stt_lang}; "
            f"returning the transcript for repair against the narration"
        )
        return drifted_fallback

    raise RuntimeError(
        f"No Speech-to-Text model supports {stt_lang} in {location} "
        f"(tried {', '.join(candidates)}): {last_error}"
    )


def _estimate_word_timestamps(
    narration: str,
    audio_duration: float,
) -> list[dict]:
    """Derive word timings from the narration text and the measured duration.

    Deterministic fallback for when Speech-to-Text is unavailable. The audio is
    a reading of exactly this text, so spreading it proportionally to word
    length (plus a fixed per-word cost for the gap between words) tracks real
    speech far better than an even split, and the phrase boundaries the
    subtitle component derives from it stay on the narration.
    """
    words = narration.split()
    if not words or audio_duration <= 0:
        return []

    per_word_cost = 1.0  # constant "slot" per word, in character-equivalents
    weights = [len(w) + per_word_cost for w in words]
    total_weight = sum(weights)

    timestamps: list[dict] = []
    cursor = 0.0
    for word, weight in zip(words, weights):
        span = audio_duration * weight / total_weight
        end = min(cursor + span, audio_duration)
        timestamps.append({"word": word, "start": cursor, "end": end})
        cursor = end

    # Absorb float drift into the final word so captions end with the audio.
    timestamps[-1]["end"] = audio_duration
    return timestamps


def _transcribe_section(
    section: ScriptSection,
    wav_path: Path,
    language: str,
    cumulative_offset: float,
) -> None:
    """Transcribe a single section's WAV and set its timestamps and duration.

    Word timestamps are shifted by cumulative_offset so they represent
    absolute time in the final concatenated narration.
    """
    section_dur = _wav_duration(wav_path)
    script_word_count = len(section.narration.split())

    try:
        words = _transcribe_with_timestamps(wav_path, language)
        if script_word_count and len(words) < script_word_count * 0.5:
            raise RuntimeError(
                f"STT returned {len(words)} words but narration has "
                f"{script_word_count} "
                f"({100 * len(words) // script_word_count}% coverage)"
            )
    except Exception as exc:
        logger.warning(
            f"Section s{section.id:03d}: STT unusable, deriving timings from "
            f"narration text instead: {exc}"
        )
        words = _estimate_word_timestamps(section.narration, section_dur)

    # Last line of defence for the captions. Every STT model may have drifted,
    # or drift may survive a model that otherwise looked fine, and corrupted
    # text must never reach the screen: repair the affected span against the
    # narration, and derive every timing from the text if it cannot be
    # anchored. The audio is untouched either way.
    if _is_arabic(_STT_LOCALE_MAP.get(language, language)) and contains_drift(words):
        logger.warning(
            f"Section s{section.id:03d}: {describe_drift(words)}"
        )
        words, replaced = repair_word_timestamps(words, section.narration)
        if not replaced or contains_drift(words):
            logger.warning(
                f"Section s{section.id:03d}: caption repair could not clear the "
                f"drift; deriving every timing from the narration text"
            )
            words = _estimate_word_timestamps(section.narration, section_dur)

    if script_word_count and not words:
        raise RuntimeError(
            f"Section {section.id}: no word timestamps could be produced"
        )

    # Shift timestamps to absolute narration time
    for w in words:
        w["start"] += cumulative_offset
        w["end"] += cumulative_offset

    section.word_timestamps = words
    section.actual_duration_seconds = section_dur

    logger.info(
        f"  Section s{section.id:03d}: {len(words)} words, "
        f"{section_dur:.1f}s (offset {cumulative_offset:.1f}s)"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def _apply_caption_language(script: Script, config: ChannelConfig) -> None:
    """Rewrite each section's caption words into the configured language.

    A no-op unless the channel sets `caption_language` to something other than
    the voice language. Per section rather than per video so a failed
    translation costs one section's captions rather than the whole track, and
    so each section keeps its own measured span.

    Never raises: captions falling back to the spoken language is a
    recoverable outcome, and losing the caption track is not.
    """
    caption = str(getattr(config, "caption_language", "") or "").strip().lower()[:2]
    voice = str(config.voice.language or "").strip().lower()[:2]
    if not caption or caption == voice:
        return

    logger.info(
        f"Caption language {caption!r} differs from narration {voice!r}; "
        f"translating captions and aligning them to narration timings"
    )

    translated_sections = 0
    for section in script.sections:
        words = section.word_timestamps or []
        if not words:
            continue
        try:
            new_words, translated = await caption_language.build_caption_words(
                words,
                section.narration,
                voice_language=voice,
                caption_language=caption,
            )
        except Exception as exc:
            logger.error(
                f"  Section s{section.id:03d}: caption translation failed "
                f"({exc}); keeping narration-language captions"
            )
            continue
        if translated:
            section.word_timestamps = new_words
            translated_sections += 1

    if translated_sections:
        logger.info(
            f"Captions written in {caption!r} for {translated_sections}/"
            f"{len(script.sections)} sections"
        )
    else:
        logger.warning(
            f"No section captions could be translated to {caption!r}; "
            f"the caption track stays in {voice!r}"
        )


async def source_audio(
    script: Script,
    config: ChannelConfig,
    workspace: Path,
) -> Script:
    """Generate all audio for the video.

    1. Generate narration per-section (no TTS truncation)
    2. Transcribe each section for word-level timestamps
    3. Concatenate into narration_full.wav for the assembler
    4. Source and trim background music
    5. Build transition SFX track at section boundaries

    Returns the updated Script with actual durations and word timestamps.
    """
    sections_dir = workspace / "audio" / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = workspace / "audio"
    manifest = _load_audio_manifest(sections_dir)

    # ── Select voice prompt variation for this video ──────────────
    variations = config.voice.voice_prompt_variations
    if variations:
        idx = min(script.voice_variation, len(variations) - 1)
        effective_voice_prompt = variations[idx]
        logger.info(f"Voice variation {idx}: {effective_voice_prompt.split('.')[0]}")
    else:
        effective_voice_prompt = config.voice.voice_prompt
    logger.info(f"Voice prompt selected: {effective_voice_prompt[:80]}...")

    # ── Generate TTS per section (concurrent) ────────────────────
    logger.info(
        f"Generating TTS per section "
        f"({len(script.sections)} sections, concurrent)"
    )

    async def _tts_one(section: ScriptSection) -> Path:
        section_path = sections_dir / f"section_{section.id:03d}.wav"
        fp = _section_audio_fingerprint(
            section.narration, effective_voice_prompt,
            config.voice.voice_name, config.voice.language,
            config.voice.provider, config.voice.voice_id,
        )
        cached = manifest.get(f"section_{section.id}")
        cache_valid = (
            section_path.exists()
            and cached is not None
            and cached.get("fingerprint") == fp
        )

        if cache_valid:
            logger.info(f"  Section s{section.id:03d} TTS cached")
            costs.record_cache_hit(
                provider="workspace_cache",
                service="tts",
                model=settings.gemini_tts_model,
                operation="tts_section_generate",
                cache_kind="workspace_manifest",
                notes=[f"Section {section.id} reused generated narration audio."],
            )
            return section_path

        result = None
        for attempt in range(2):
            result = await clients.generate_speech(
                text=section.narration,
                output_path=section_path,
                voice_prompt=effective_voice_prompt,
                language=config.voice.language,
                voice_name=config.voice.voice_name,
                operation_label="tts_section_generate",
                provider=config.voice.provider,
                voice_id=config.voice.voice_id,
            )
            if result is not None:
                break
            if attempt == 0:
                logger.warning(f"  Section s{section.id:03d} TTS failed, retrying...")

        if result is None:
            raise RuntimeError(
                f"TTS failed for section {section.id} after 2 attempts"
            )

        manifest[f"section_{section.id}"] = {
            "fingerprint": fp,
            "file": section_path.name,
        }
        return section_path

    section_wavs = list(await asyncio.gather(*[_tts_one(s) for s in script.sections]))

    # ── Transcribe each section and set timestamps ────────────────
    cumulative_offset = 0.0
    for section, wav_path in zip(script.sections, section_wavs):
        stt_cache_key = f"stt_{section.id}"
        cached_stt = manifest.get(stt_cache_key)
        section_fp = manifest.get(f"section_{section.id}", {}).get("fingerprint")

        if (cached_stt
                and cached_stt.get("fingerprint") == section_fp
                and cached_stt.get("words") is not None):
            words = cached_stt["words"]
            section_dur = _wav_duration(wav_path)
            section.word_timestamps = words if words else None
            section.actual_duration_seconds = section_dur
            costs.record_cache_hit(
                provider="workspace_cache",
                service="speech_to_text",
                model="long",
                operation="stt_transcribe",
                cache_kind="workspace_manifest",
                reconciliation_supported=False,
                notes=[f"Section {section.id} reused cached STT timestamps."],
            )
            logger.info(
                f"  Section s{section.id:03d}: cached STT "
                f"({len(words)} words, {section_dur:.1f}s)"
            )
        else:
            _transcribe_section(section, wav_path, config.voice.language, cumulative_offset)
            manifest[stt_cache_key] = {
                "fingerprint": section_fp,
                "words": section.word_timestamps,
            }

        cumulative_offset += section.actual_duration_seconds

    logger.info(
        "Section durations from STT: "
        + ", ".join(
            f"s{s.id}={s.actual_duration_seconds:.1f}s"
            for s in script.sections
        )
    )

    # ── Captions in a language the narrator is not speaking ────────
    #
    # Runs after transcription and before anything reads the timings, so the
    # rest of the pipeline sees one caption track and does not need to know
    # which language it is in. When the two languages match this is a no-op
    # and the measured STT timings are used exactly as before.
    await _apply_caption_language(script, config)

    # ── Concatenate into narration_full.wav ────────────────────────
    narration_path = audio_dir / "narration_full.wav"
    _concat_wavs(section_wavs, narration_path)
    total_duration = _wav_duration(narration_path)
    script.total_estimated_duration_seconds = total_duration
    logger.info(f"Full narration: {total_duration:.1f}s ({len(section_wavs)} sections)")

    # ── Source background music (always trim to current duration) ─
    music_path = audio_dir / "background_music.mp3"
    _source_background_music(
        script=script,
        config=config,
        output_path=music_path,
    )

    # ── Build transition SFX track ────────────────────────────────
    transitions_path = audio_dir / "transitions.wav"
    section_durations = [
        s.actual_duration_seconds or s.estimated_duration_seconds
        for s in script.sections
    ]
    _build_transition_track(
        section_durations=section_durations,
        total_duration=total_duration,
        output_path=transitions_path,
        sfx_boundary_coverage=config.rendering_defaults.sfx_boundary_coverage,
        sfx_pool=config.video.sfx_pool,
    )

    # ── Per-scene ambience and cues ───────────────────────────────
    # Two extra layers, rendered full-length so the assembler only gains two
    # inputs. Never fatal: a run without ambience is a worse video, a run that
    # died sourcing ambience is no video.
    if config.image_sourcing.scene_audio:
        try:
            from core.audio_director import build_scene_audio

            plan = await build_scene_audio(script, workspace, total_duration)
            logger.info(
                f"Scene audio: {sum(1 for c in plan.cues if c.sourced)}"
                f"/{len(plan.cues)} cues across {len(plan.ambience)} scene(s)"
            )
        except Exception as exc:
            logger.warning(f"Scene audio unavailable: {exc}")

    _save_audio_manifest(sections_dir, manifest)
    save_script(workspace, script)

    return script


# ---------------------------------------------------------------------------
# Background music
# ---------------------------------------------------------------------------

def _source_background_music(
    script: Script,
    config: ChannelConfig,
    output_path: Path,
) -> bool:
    """Source background music: manually placed -> bundled library -> narration only."""
    if output_path.exists():
        logger.info("Background music already present, re-trimming to match duration")
        _trim_music(output_path, script.total_estimated_duration_seconds)
        return True

    music_dir = ASSETS_DIR / "music"
    tracks = sorted(music_dir.glob("*.mp3"))

    # Filter to channel's music pool if configured
    if tracks and config.video.music_pool:
        pool_set = {name.lower() for name in config.video.music_pool}
        pool_tracks = [t for t in tracks if t.stem.lower() in pool_set]
        if pool_tracks:
            tracks = pool_tracks
            logger.info(f"Music pool: {len(tracks)} tracks for channel")

    if tracks:
        if script.music_track:
            match = [t for t in tracks if t.stem == script.music_track]
            if not match:
                available = [t.stem for t in tracks]
                raise ValueError(
                    f"AI-selected music track '{script.music_track}' not found. "
                    f"Available: {available}"
                )
            chosen = match[0]
            logger.info(f"Background music (AI-selected): {chosen.name}")
        else:
            idx = int(hashlib.md5(script.title.encode()).hexdigest(), 16) % len(tracks)
            chosen = tracks[idx]
            logger.info(f"Background music: {chosen.name}")
        shutil.copy2(chosen, output_path)
        _trim_music(output_path, script.total_estimated_duration_seconds)
        return True

    logger.info("No background music available -- video will use narration only")
    return False


# ---------------------------------------------------------------------------
# Transition SFX
# ---------------------------------------------------------------------------

def _build_transition_track(
    section_durations: list[float],
    total_duration: float,
    output_path: Path,
    sfx_boundary_coverage: float = 0.70,
    sfx_pool: list[str] | None = None,
) -> bool:
    """Build a WAV track with transition SFX placed at section boundaries.

    Places short whoosh/riser sounds centered at the boundary between sections.
    ~70% of boundaries get a transition sound (randomized) to avoid predictability.

    `sfx_pool` names the stems this channel may use. It exists because the SFX
    directories are shared: without it, adding a horror one-shot to the assets
    tree would start dropping it into every other channel's videos. An empty
    pool keeps the original behaviour of using every bundled transition clip.
    """
    sfx_dirs = [ASSETS_DIR / "sfx" / "transitions", ASSETS_DIR / "sfx" / "horror"]
    pool = sorted(
        p for d in sfx_dirs if d.exists() for p in d.glob("*.mp3")
    )
    if sfx_pool:
        wanted = {name.lower() for name in sfx_pool}
        pool = [p for p in pool if p.stem.lower() in wanted]
        missing = wanted - {p.stem.lower() for p in pool}
        if missing:
            logger.warning(
                f"SFX in the channel pool are missing from assets/sfx/: "
                f"{', '.join(sorted(missing))}"
            )
    else:
        # No pool declared: keep the historical set only, so newly added
        # channel-specific one-shots cannot leak into an existing channel.
        pool = [p for p in pool if p.parent.name == "transitions"]
    if not pool:
        logger.info("No transition SFX available for this channel, skipping")
        return False

    num_boundaries = len(section_durations) - 1
    if num_boundaries <= 0:
        return False

    # Decide which boundaries get transitions
    boundary_indices = list(range(num_boundaries))
    n_transitions = int(len(boundary_indices) * sfx_boundary_coverage)
    if n_transitions <= 0:
        return False
    chosen = sorted(random.sample(boundary_indices, min(n_transitions, len(boundary_indices))))

    # Calculate timestamp of each section boundary
    boundary_times: list[float] = []
    cursor = 0.0
    for dur in section_durations[:-1]:
        cursor += dur
        boundary_times.append(cursor)

    # Build the transitions track by placing SFX clips in a silence buffer
    sample_rate = 24000
    total_frames = int(total_duration * sample_rate)
    buffer = np.zeros(total_frames, dtype=np.int32)

    for idx in chosen:
        if idx >= len(boundary_times):
            continue
        center = boundary_times[idx]
        clip_path = random.choice(pool)

        # Decode clip to raw PCM via ffmpeg
        try:
            result = subprocess.run(
                [
                    settings.ffmpeg_path, "-y",
                    "-i", str(clip_path),
                    "-ar", str(sample_rate),
                    "-ac", "1",
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "pipe:1",
                ],
                capture_output=True,
                check=True,
            )
        except Exception as e:
            logger.warning(f"Failed to decode SFX {clip_path.name}: {e}")
            continue

        clip = np.frombuffer(result.stdout, dtype=np.int16)
        clip_duration = len(clip) / sample_rate

        # Center the clip at the boundary timestamp
        start_frame = max(0, int((center - clip_duration / 2) * sample_rate))
        end_frame = min(start_frame + len(clip), total_frames)
        buffer[start_frame:end_frame] += clip[:end_frame - start_frame]

    # Clamp to int16 range and write WAV
    output = np.clip(buffer, -32768, 32767).astype(np.int16)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(output.tobytes())

    logger.info(
        f"Transition SFX track: {len(chosen)} effects placed "
        f"across {num_boundaries} boundaries"
    )
    return True


def _trim_music(music_path: Path, total_duration: float) -> None:
    """Trim and fade out background music to match video duration."""
    if total_duration <= 0:
        return
    trimmed = music_path.with_name("background_music_trimmed.mp3")
    try:
        subprocess.run(
            [
                settings.ffmpeg_path, "-y",
                "-i", str(music_path),
                "-t", str(total_duration),
                "-af", f"afade=t=out:st={max(0, total_duration - 3):.1f}:d=3",
                str(trimmed),
            ],
            capture_output=True,
            check=True,
        )
        trimmed.replace(music_path)
    except Exception as e:
        logger.warning(f"Background music trim failed: {e}")
