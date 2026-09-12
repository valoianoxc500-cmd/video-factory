"""The Viral Reels Finder worker.

It polls one endpoint for two kinds of work:

  tasks         discovery searches, AI analysis, and video processing --
                anything that calls an external API or a model and so cannot
                run inside a request handler.
  publish jobs  due posts, including scheduled ones whose time has arrived.

Isolation, again
----------------
The worker serves every user, which is exactly why the owner has to travel
with the work. Each claimed row carries its `user_id`; tokens are fetched for
*that* id and decrypted with it as associated data, so a token that somehow
belonged to another account would fail to decrypt rather than post to a
stranger's profile. There is no ambient "current user" anywhere in this file.

The web deployment never sees a plaintext token: it hands out ciphertext and
this process holds the key.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from viral import analytics as vrf_analytics
from viral.accounts import TokenCipher, TokenSecurityError
from viral.adapters import AdapterContext, build_adapters, publish_to
from viral.analysis import analyse, to_new_version_inputs
from viral.explain import explain
from viral.discovery import (
    Availability,
    SearchQuery,
    SortOrder,
    default_providers,
    parse_iso8601_duration,
    sort_results,
)
from viral.processing import build_ffmpeg_command, build_plan, detect_subject_centre, probe
from viral import clipping as vrf_clipping
from viral import police_chase as chase
from viral.publishing import (
    MAX_ATTEMPTS,
    PublishRequest,
    PublishResult,
    PublishStatus,
    backoff_seconds,
)
from viral.rights import RightsError, Source, attest
from viral.scoring import VideoMetrics, new_version_probability, viral_score

logger = logging.getLogger("viral.runner")

POLL_SECONDS = 5.0
IDLE_SLEEP = 15.0


class WorkerClient:
    """The one endpoint, with the worker's bearer token on every call."""

    def __init__(self, base_url: str = "", token: str = "", client=None) -> None:
        self._base = (base_url or os.environ.get("WORKER_API_BASE", "")).rstrip("/")
        self._token = token or os.environ.get("WORKER_TOKEN", "")
        self._client = client
        if not self._base:
            raise RuntimeError("WORKER_API_BASE is not set.")
        if not self._token:
            raise RuntimeError("WORKER_TOKEN is not set.")

    def call(self, action: str, **params) -> dict:
        client = self._client
        owns = client is None
        if owns:
            client = httpx.Client(timeout=60.0, follow_redirects=True)
        try:
            response = client.post(
                f"{self._base}/api/worker/vrf",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"action": action, **params},
            )
            response.raise_for_status()
            return response.json() or {}
        finally:
            if owns:
                client.close()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def run_discover(payload: dict) -> dict:
    """Search every provider that has a lawful path, and say so about the rest."""
    query = SearchQuery(
        niche=str(payload.get("niche") or ""),
        keyword=str(payload.get("keyword") or ""),
        language=str(payload.get("language") or ""),
        max_results=int(payload.get("max_results") or 25),
        max_age_days=int(payload.get("max_age_days") or 30),
    )

    videos: list = []
    statuses: list[dict] = []
    for provider in default_providers():
        status = provider.status()
        statuses.append({
            "platform": status.platform,
            "availability": status.availability.value,
            "reason": status.reason,
        })
        if status.availability is Availability.READY:
            videos.extend(provider.search(query))

    try:
        order = SortOrder(str(payload.get("sort") or "viral_score"))
    except ValueError:
        order = SortOrder.VIRAL_SCORE

    ranked = sort_results(videos, order)
    note = ""
    if not ranked and all(s["availability"] != "ready" for s in statuses):
        note = (
            "No discovery provider is configured. YouTube is the only platform "
            "with a lawful open-search API; set YOUTUBE_API_KEY to enable it."
        )

    return {
        "videos": [v.to_record() for v in ranked],
        "providers": statuses,
        "note": note,
    }


def run_analyse(client: WorkerClient, payload: dict) -> dict:
    """Analyse a video from its public signals only.

    Two callers, two shapes. A saved discovery arrives as `source_id` and its
    metrics are already stored. A video the user added arrives as `asset_id`
    and carries only a link, so the metrics are fetched from the platform --
    which is possible for a public YouTube video and for nothing else.

    Either way this reads public signals and the thumbnail. It never copies
    the video: `analyse` takes its depth from the rights basis, and DISCOVERED
    keeps it at metadata depth.
    """
    if payload.get("asset_id"):
        return _analyse_asset(client, str(payload["asset_id"]))

    source_id = str(payload.get("source_id") or "")
    source = (client.call("source", id=source_id) or {}).get("source")
    if not source:
        raise RuntimeError("That saved video no longer exists.")

    video = {
        "platform": source.get("platform"),
        "title": source.get("title"),
        "author": source.get("author"),
        "duration_seconds": source.get("duration_seconds"),
        "views": source.get("views"),
        "likes": source.get("likes"),
        "comments": source.get("comments"),
        "followers": source.get("followers"),
    }
    # DISCOVERED keeps this at metadata depth: the video is never copied.
    attestation = attest(
        source=Source.DISCOVERED, user_id=str(source.get("user_id") or "worker"))
    result = asyncio.run(analyse(video, attestation=attestation))

    client.call("save_analysis", id=source_id, analysis=result.to_record())
    return {"analysed": True, "depth": result.depth.value}


def _analyse_asset(client: WorkerClient, asset_id: str) -> dict:
    """Read a video the user added, and score the original it came from."""
    asset = (client.call("asset", id=asset_id) or {}).get("asset")
    if not asset:
        raise RuntimeError("That video no longer exists.")

    metrics = public_metrics(
        str(asset.get("source_platform") or ""),
        str(asset.get("source_video_id") or ""),
    )

    video = {
        "platform": asset.get("source_platform"),
        "title": asset.get("title"),
        "author": asset.get("source_author"),
        "duration_seconds": metrics.duration_seconds if metrics else None,
        "views": metrics.views if metrics else None,
        "likes": metrics.likes if metrics else None,
        "comments": metrics.comments if metrics else None,
        "followers": metrics.followers if metrics else None,
    }
    attestation = attest(
        source=Source.DISCOVERED, user_id=str(asset.get("user_id") or "worker"))
    result = asyncio.run(analyse(video, attestation=attestation))

    # The Original Viral Score is a measurement, so it exists only where the
    # platform actually reported numbers. Everywhere else it stays absent
    # rather than being invented from a title.
    original = viral_score(metrics) if metrics else None
    forecast = (
        new_version_probability(original, to_new_version_inputs(result))
        if original else None
    )

    client.call(
        "save_asset_analysis",
        id=asset_id,
        score_breakdown={
            "analysis": result.to_record(),
            "original": original.to_record() if original else None,
        },
        original_viral_score=round(original.score) if original else None,
        new_version_probability=round(forecast.score) if forecast else None,
        probability_confidence=forecast.confidence.value if forecast else "",
        duration_seconds=metrics.duration_seconds if metrics else None,
    )
    return {
        "analysed": True,
        "depth": result.depth.value,
        "scored": original is not None,
    }


def run_explain(client: WorkerClient, payload: dict) -> dict:
    """Clip Analyzer: explain one video the user added, and stop there.

    Deliberately writes nothing back onto the asset. The explanation is the
    task's own result, so running it twice cannot alter a score, a rights
    record or anything the publishing path reads. Nothing here queues a
    generation: this analyses and explains, and the plan it returns is for a
    person to carry out.
    """
    asset_id = str(payload.get("asset_id") or "")
    asset = (client.call("asset", id=asset_id) or {}).get("asset")
    if not asset:
        raise RuntimeError("That video no longer exists.")

    metrics = public_metrics(
        str(asset.get("source_platform") or ""),
        str(asset.get("source_video_id") or ""),
    )

    # The processed copy if the user has already cut one, otherwise the
    # original. Either is a file they hold rights to; the analyser only ever
    # reads it.
    local = ""
    for key in ("processed_path", "storage_path"):
        candidate = str(asset.get(key) or "").strip()
        if candidate and Path(candidate).exists():
            local = candidate
            break

    duration = None
    if metrics and metrics.duration_seconds:
        duration = metrics.duration_seconds
    elif asset.get("duration_seconds"):
        duration = float(asset["duration_seconds"])

    video = {
        "platform": asset.get("source_platform"),
        "title": asset.get("title"),
        "author": asset.get("source_author"),
        "duration_seconds": duration,
        "views": metrics.views if metrics else None,
        "likes": metrics.likes if metrics else None,
        "comments": metrics.comments if metrics else None,
        "followers": metrics.followers if metrics else None,
    }

    # An unrecognised or missing rights basis is not a reason to fail an
    # analysis -- it is a reason to analyse less deeply. `attest` refuses an
    # unknown source outright, so falling back to DISCOVERED keeps the run and
    # keeps it at metadata depth, which is exactly what an unproven basis
    # should get.
    try:
        attestation = attest(
            source=str(asset.get("rights_source") or ""),
            user_id=str(asset.get("user_id") or "worker"),
            rights_holder=str(asset.get("rights_holder") or ""),
            evidence=str(asset.get("rights_evidence") or ""),
        )
    except RightsError:
        logger.info(
            f"asset {asset_id} has no usable rights basis "
            f"({asset.get('rights_source')!r}); explaining from public signals only"
        )
        attestation = attest(
            source=Source.DISCOVERED, user_id=str(asset.get("user_id") or "worker")
        )
        local = ""  # nothing may be read from the file without a basis

    result = asyncio.run(
        explain(
            video,
            attestation=attestation,
            source_path=Path(local) if local else None,
        )
    )
    record = result.to_record()
    record["asset_id"] = asset_id
    record["title"] = asset.get("title") or ""
    logger.info(
        f"explained asset {asset_id} at {result.depth} depth "
        f"({result.frames_examined} frames)"
    )
    return record


def public_metrics(platform: str, video_id: str) -> VideoMetrics | None:
    """The source video's own public numbers, where a public API offers them.

    Only YouTube does. Instagram, TikTok and Facebook return data for the
    authenticated account's own posts, which is not what this is looking at,
    so those return None and the score is reported as unavailable rather than
    guessed at.
    """
    if str(platform or "").strip().lower() != "youtube" or not video_id:
        return None

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        return None

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as http:
            response = http.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "key": api_key,
                    "part": "statistics,contentDetails,snippet",
                    "id": video_id,
                },
            )
            response.raise_for_status()
            items = response.json().get("items") or []
    except Exception as exc:
        logger.info(f"public metrics unavailable for {video_id}: {exc}")
        return None
    if not items:
        return None

    item = items[0]
    stats = item.get("statistics", {})
    followers = _channel_subscribers(
        api_key, str(item.get("snippet", {}).get("channelId") or ""))

    return VideoMetrics(
        views=_as_int(stats.get("viewCount")),
        likes=_as_int(stats.get("likeCount")),
        comments=_as_int(stats.get("commentCount")),
        # The Data API exposes no share count on this endpoint.
        shares=None,
        followers=followers,
        posted_at=_parse_time(item.get("snippet", {}).get("publishedAt")),
        duration_seconds=parse_iso8601_duration(
            item.get("contentDetails", {}).get("duration", "")),
        platform="youtube",
    )


def _channel_subscribers(api_key: str, channel_id: str) -> int | None:
    if not channel_id:
        return None
    try:
        with httpx.Client(timeout=30.0) as http:
            response = http.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"key": api_key, "part": "statistics", "id": channel_id},
            )
            response.raise_for_status()
            items = response.json().get("items") or []
    except Exception as exc:
        logger.info(f"channel statistics unavailable for {channel_id}: {exc}")
        return None
    if not items:
        return None
    return _as_int(items[0].get("statistics", {}).get("subscriberCount"))


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _upload_media(local: Path, key: str) -> str:
    """Put a file in permanent storage and return its address.

    The one place this module knows about storage. Imported here rather than
    at module scope because `worker.storage` pulls in cloud clients that a
    discovery-only run has no use for.
    """
    from worker.storage import upload_media

    return str(upload_media(local, key, content_type="video/mp4"))


def owner_media_url(platform: str, video_id: str, token: str, *, client=None) -> str:
    """The file for a video the connected account itself published.

    Only two platforms offer one:

      instagram  IG Media `media_url`. Omitted when the media contains
                 copyrighted content -- copyrighted audio on a Reel is the
                 common case -- so an empty answer here is a real outcome
                 rather than an error.
      facebook   Page video `source`.

    TikTok's Video object carries no file field at all, and YouTube's Data API
    never returns media. Both are declared metadata-only, so this is not asked
    for them.
    """
    key = str(platform or "").strip().lower()
    if key not in {"instagram", "facebook"} or not video_id:
        return ""

    owns = client is None
    http = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        if key == "instagram":
            response = http.get(
                f"https://graph.facebook.com/v21.0/{video_id}",
                params={"fields": "media_url,media_type", "access_token": token},
            )
        else:
            response = http.get(
                f"https://graph.facebook.com/v21.0/{video_id}",
                params={"fields": "source", "access_token": token},
            )
        response.raise_for_status()
        body = response.json() or {}
    except Exception as exc:
        raise RuntimeError(f"{key} did not return the file: {exc}"[:300]) from exc
    finally:
        if owns:
            http.close()

    return str(body.get("media_url") or body.get("source") or "")


def run_ingest(client: WorkerClient, cipher: TokenCipher | None, payload: dict) -> dict:
    """Import the owner's own file, then prepare it for the target platform.

    One task rather than two so a failed fetch cannot leave a processing task
    queued behind it waiting for a file that never arrived. The asset is moved
    through `fetching` and `imported` on the way, which is what the card in My
    Videos is reading.
    """
    asset_id = str(payload.get("asset_id") or "")
    asset = (client.call("asset", id=asset_id) or {}).get("asset")
    if not asset:
        raise RuntimeError("That video no longer exists.")

    platform = str(asset.get("source_platform") or "")
    user_id = str(asset.get("user_id") or "")

    def stop(detail: str, status: str = "failed") -> dict:
        client.call("update_asset_ingest", id=asset_id, status=status, detail=detail)
        return {"imported": False, "reason": detail}

    if cipher is None:
        return stop(
            "Token encryption is not configured on this worker, so the "
            "connected account cannot be used."
        )

    client.call("update_asset_ingest", id=asset_id, status="fetching",
                detail=f"Importing from your connected {platform} account.")

    try:
        token = _token_for(client, cipher, user_id, platform)
    except Exception as exc:
        return stop(str(exc)[:300])

    try:
        media_url = owner_media_url(
            platform, str(asset.get("source_video_id") or ""), token)
    except RuntimeError as exc:
        return stop(str(exc)[:300])

    if not media_url:
        # Instagram omits media_url for copyrighted content. Naming that is
        # more use than "import failed", because nothing the user does to the
        # connection will change it.
        return stop(
            f"{platform} did not return a file for this video. It returns one "
            f"only for the account's own posts, and omits it when the media "
            f"contains copyrighted content such as licensed audio.",
            status="metadata_only",
        )

    with tempfile.TemporaryDirectory(prefix="vrf_ingest_") as workdir:
        local = Path(workdir) / "source.mp4"
        try:
            _download(media_url, local)
        except Exception as exc:
            return stop(f"The file could not be downloaded: {exc}"[:300])

        remote = _upload_media(local, f"vrf/{user_id}/{asset_id}/source.mp4")

    client.call(
        "update_asset_ingest",
        id=asset_id,
        status="imported",
        detail=f"Imported from your connected {platform} account.",
        storage_path=str(remote),
    )

    # The new version is what the user is actually waiting for, so it runs
    # here rather than in a task queued behind this one.
    processed = run_process(
        client,
        {
            "asset_id": asset_id,
            "platform": payload.get("platform") or "tiktok",
            **({"auto_clip": payload["auto_clip"]}
               if isinstance(payload.get("auto_clip"), dict) else {}),
        },
    )
    return {"imported": True, **processed}


def _clip_spec_from(options: dict, source, plan) -> "vrf_clipping.ClipSpec":
    """One clip spec from the screen's options and the plan's trims.

    The range comes from the plan rather than the options so the trims keep
    their single meaning: `build_plan` has always owned them, and having two
    places compute a start time is how they drift apart.
    """
    start = float(plan.trim_start or 0.0)
    duration = float(getattr(source, "duration", 0.0) or 0.0)
    end = duration - float(plan.trim_end or 0.0) if duration else 0.0
    if end <= start:
        # No measurable duration, or trims that meet: fall back to the whole
        # remaining file and let ffmpeg run to the end.
        end = start + max(1.0, duration - start)

    return vrf_clipping.ClipSpec(
        id="clip_1",
        start=round(start, 3),
        end=round(end, 3),
        aspect=str(options.get("aspect") or vrf_clipping.DEFAULT_ASPECT),
        captions=bool(options.get("captions")),
        caption_style=str(options.get("caption_style") or vrf_clipping.DEFAULT_CAPTION_STYLE),
        focus=str(options.get("focus") or vrf_clipping.DEFAULT_FOCUS),
        quality=str(options.get("quality") or vrf_clipping.DEFAULT_QUALITY),
        speaker=str(options.get("speaker") or ""),
    )


def _build_subtitles(
    source_path: Path,
    spec: "vrf_clipping.ClipSpec",
    workdir: Path,
    payload: dict,
) -> Path | None:
    """A burned-in subtitle file for this clip, or None.

    Returns None on every failure. Captions are something the user asked to
    add to a clip; they are not the clip, and a recogniser being unavailable
    must not turn a perfectly good cut into a failed job.

    Uses the transcription path the rest of the repo already uses, so there is
    no second recogniser and no second bill.
    """
    try:
        audio = workdir / "clip_audio.wav"
        extract = subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-ss", f"{spec.start:.3f}", "-i", str(source_path),
             "-t", f"{spec.duration:.3f}",
             "-vn", "-ac", "1", "-ar", "16000", str(audio)],
            capture_output=True, text=True,
        )
        if extract.returncode != 0 or not audio.exists():
            logger.warning("[clipping] no audio to caption; continuing without")
            return None

        from core.audio_sourcer import _transcribe_with_timestamps

        language = str(payload.get("caption_language") or "en")
        words = _transcribe_with_timestamps(audio, language)

        # The audio was already cut to the clip, so the words start at zero.
        cues = vrf_clipping.caption_cues(
            words, clip_start=0.0, clip_end=spec.duration
        )
        if not cues:
            logger.warning("[clipping] transcript yielded no cues; continuing without")
            return None

        srt = workdir / "captions.srt"
        srt.write_text(vrf_clipping.cues_to_srt(cues), encoding="utf-8")
        return srt
    except Exception as exc:  # noqa: BLE001 - captions must never fail a clip
        logger.warning(
            f"[clipping] captions unavailable, continuing without: {str(exc)[:200]}"
        )
        return None


def _transcribe_for_viral_clips(
    source_path: Path,
    workdir: Path,
    language: str,
) -> list[dict]:
    """Transcribe the full source once for ranking and every output caption."""
    audio = workdir / "source_audio.wav"
    extract = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source_path),
         "-vn", "-ac", "1", "-ar", "16000", str(audio)],
        capture_output=True,
        text=True,
    )
    if extract.returncode != 0 or not audio.exists():
        raise RuntimeError("We could not hear enough speech to find standalone clips.")

    from core.audio_sourcer import _transcribe_with_timestamps

    # Auto means no translation. The existing recognizer needs a locale; use
    # the source-language hint when one exists and English as the safe default.
    requested = str(language or "auto").strip().lower()
    locale = str(requested if requested in {"en", "ar", "es"} else "en")
    words = _transcribe_with_timestamps(audio, locale)
    if not words:
        raise RuntimeError("We could not hear enough speech to find standalone clips.")
    return words


def _subtitles_from_transcript(
    words: list[dict],
    spec: "vrf_clipping.ClipSpec",
    workdir: Path,
) -> Path | None:
    """Reuse the analysis transcript; caption failure never destroys a clip."""
    try:
        cues = vrf_clipping.caption_cues(
            words,
            clip_start=spec.start,
            clip_end=spec.end,
        )
        if not cues:
            return None
        target = workdir / f"{spec.id}.srt"
        target.write_text(vrf_clipping.cues_to_srt(cues), encoding="utf-8")
        return target
    except Exception as exc:  # noqa: BLE001 - captions remain optional
        logger.warning(f"[clipping] {spec.id}: captions unavailable: {str(exc)[:160]}")
        return None


def _run_viral_clip_batch(
    client: WorkerClient,
    payload: dict,
    asset: dict,
    source_path: Path,
    source,
    workdir: Path,
    attestation,
) -> dict:
    """Find, rank, render and upload several standalone vertical moments."""
    options = payload.get("auto_clip") or {}
    language = str(options.get("language") or "auto")
    words = _transcribe_for_viral_clips(source_path, workdir, language)
    moments = vrf_clipping.select_viral_moments(
        words,
        source_duration=float(source.duration or 0.0),
        length=str(options.get("length") or "auto"),
        count=options.get("count") or "auto",
    )
    if not moments:
        raise RuntimeError("We could not find a complete standalone moment in this video.")

    requests = [
        {
            "id": moment.id,
            "start": moment.start,
            "end": moment.end,
            "aspect": "9:16",
            "captions": True,
            "caption_style": "bold",
            "focus": "auto",
            "quality": "balanced",
        }
        for moment in moments
    ]
    plan = vrf_clipping.plan_clips(
        source,
        requests,
        attestation=attestation,
        platform="tiktok",
    )
    if not plan.specs:
        raise RuntimeError("We could not turn the selected moments into valid clips.")

    rendered: dict[str, dict] = {}

    def render(spec: "vrf_clipping.ClipSpec") -> str:
        output = workdir / f"{spec.id}.mp4"
        output.unlink(missing_ok=True)
        centre = vrf_clipping.focus_centre(source_path, spec)
        subtitles = _subtitles_from_transcript(words, spec, workdir)
        command = vrf_clipping.build_clip_command(
            source_path,
            output,
            spec,
            centre=centre,
            subtitle_path=subtitles,
            has_audio=source.has_audio,
        )
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not output.exists():
            logger.error(
                f"[clipping] {spec.id} encode failed: {(result.stderr or '')[-500:]}"
            )
            raise RuntimeError(vrf_clipping.safe_clip_error(result.stderr or ""))
        measured = probe(output)
        remote = _upload_media(
            output,
            f"vrf/{asset.get('user_id')}/{asset.get('id')}/{spec.id}.mp4",
        )
        rendered[spec.id] = {
            "captions": subtitles is not None,
            "width": measured.width,
            "height": measured.height,
            "duration": measured.duration or spec.duration,
        }
        return str(remote)

    results = vrf_clipping.run_clip_batch(plan.specs, render)
    summary = vrf_clipping.batch_summary(results)
    if not summary["ok"]:
        raise RuntimeError(str(summary["message"]))

    moment_by_id = {moment.id: moment for moment in moments}
    clips: list[dict] = []
    for result in results:
        moment = moment_by_id[result.id]
        record = {
            **moment.to_record(),
            "status": result.status,
            "path": result.path,
            "error": result.error,
            "attempts": result.attempts,
            "captions": bool(rendered.get(result.id, {}).get("captions")),
        }
        clips.append(record)

    available = [clip for clip in clips if clip["status"] in {"done", "skipped"}]
    first = available[0]
    first_meta = rendered.get(str(first["id"]), {})
    client.call(
        "save_processing",
        id=str(asset.get("id") or ""),
        processed_path=str(first["path"]),
        duration_seconds=first_meta.get("duration") or first["duration"],
        width=first_meta.get("width") or 1080,
        height=first_meta.get("height") or 1920,
        processing_plan={
            "mode": "viral_clips",
            "language": language,
            "length": str(options.get("length") or "auto"),
            "requested_count": options.get("count") or "auto",
            "moments": [moment.to_record() for moment in moments],
            "warnings": plan.warnings,
        },
    )
    return {
        "processed": True,
        "mode": "viral_clips",
        "clips": clips,
        "ready": len(available),
        "failed": int(summary["failed"]),
        "message": str(summary["message"]),
        "warnings": plan.warnings,
    }


def _police_visual_analysis(source_path: Path, duration: float, workdir: Path) -> dict:
    """One bounded vision review over evenly sampled frames; never fabricates events."""
    frame_paths: list[Path] = []
    count = 10
    for index in range(count):
        at = (max(0.0, duration - .1) * index / max(1, count - 1))
        target = workdir / f"analysis_{index:02d}.jpg"
        result = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", str(source_path), "-frames:v", "1", "-vf", "scale=640:-2", str(target)], capture_output=True, text=True)
        if result.returncode == 0 and target.exists(): frame_paths.append(target)
    if not frame_paths: return {"events": [], "burned_english_captions": None}
    try:
        import clients
        stamps = [round(max(0.0, duration-.1)*i/max(1,count-1), 2) for i in range(len(frame_paths))]
        prompt = (
            "Review these evenly spaced frames from one authorized police/bodycam/dashcam video. "
            f"Frame timestamps in order: {stamps}. Detect only clearly visible events from: {sorted(chase.EVENT_LABELS)}. "
            "Do not infer unseen events. Also decide whether repeated burned-in ENGLISH subtitle text is visibly present. "
            "Return JSON with events [{seconds,label,confidence,description}], burned_english_captions true/false, caption_confidence 0..1."
        )
        value = asyncio.run(clients.review_with_vision(prompt, frame_paths, operation_label="police_chase_visual_analysis"))
        return value if isinstance(value, dict) else {"events": [], "burned_english_captions": None}
    except Exception as exc:
        logger.warning(f"[police-chase] visual analysis unavailable: {str(exc)[:160]}")
        return {"events": [], "burned_english_captions": None}


def _cta_srt(spec: "vrf_clipping.ClipSpec", text: str, placement: str, workdir: Path) -> Path | None:
    if not text: return None
    start = .2 if placement == "persistent" else max(0.0, spec.duration - 2.7)
    target = workdir / f"{spec.id}_cta.srt"
    target.write_text(
        vrf_clipping.cues_to_srt([
            vrf_clipping.CaptionCue(start, max(start + .2, spec.duration - .1), text)
        ]),
        encoding="utf-8",
    )
    return target


def _police_caption_words(words: list[dict], action: str) -> tuple[list[dict], bool]:
    if action == "generate_en": return words, bool(words)
    if action != "translate_ar" or not words: return [], False
    try:
        from core.caption_language import build_caption_words
        narration = " ".join(str(row.get("word") or "") for row in words)
        translated, ok = asyncio.run(build_caption_words(words, narration, voice_language="en", caption_language="ar"))
        return (translated, True) if ok else ([], False)
    except Exception as exc:
        logger.warning(f"[police-chase] Arabic captions unavailable: {str(exc)[:160]}")
        return [], False


def _run_police_chase_batch(client: WorkerClient, payload: dict, asset: dict, source_path: Path, source, workdir: Path, attestation) -> dict:
    options = payload.get("police_chase") or {}
    target = int(options.get("target_seconds") or 30); requested = int(options.get("count") or 1)
    retry = options.get("retry_moment")
    analysis = {"events": [], "burned_english_captions": None} if retry else _police_visual_analysis(source_path, float(source.duration or 0), workdir)
    try: words = _transcribe_for_viral_clips(source_path, workdir, "en")
    except Exception as exc:
        logger.warning(f"[police-chase] transcript unavailable: {str(exc)[:160]}"); words = []
    if isinstance(retry, dict):
        moments = [vrf_clipping.ViralMoment("chase_retry", str(retry.get("title") or "Selected chase moment"), float(retry.get("start") or 0), float(retry.get("end") or target), int(retry.get("viral_score") or 50), str(retry.get("reason") or "Selected moment"), dict(retry.get("signals") or {}))]
    else:
        moments = chase.select_chase_moments(words, list(analysis.get("events") or []), source_duration=float(source.duration or 0), target_seconds=target, count=requested)
    if not moments: raise RuntimeError("We could not verify a strong chase moment in this source.")
    burned = analysis.get("burned_english_captions") if float(analysis.get("caption_confidence") or 0) >= .65 else None
    decision = chase.caption_decision(str(options.get("caption_language") or "auto"), burned)
    caption_words, caption_ok = _police_caption_words(words, decision["action"])
    if decision["action"] == "translate_ar" and not caption_ok: decision = {"action":"none","status":"Arabic captions unavailable"}
    specs=[vrf_clipping.ClipSpec(id=m.id,start=m.start,end=m.end,aspect="9:16",captions=bool(caption_words),caption_style="bold",focus="auto",quality="balanced") for m in moments]
    plan=vrf_clipping.plan_clips(source,[s.to_record() for s in specs],attestation=attestation,platform="tiktok")
    rendered:dict[str,dict]={}; source_meta=chase.normalize_source_metadata(options.get("source_metadata")); moment_by={m.id:m for m in moments}
    def render(spec):
        output=workdir/f"{spec.id}.mp4"; subtitles=_subtitles_from_transcript(caption_words,spec,workdir) if caption_words else None
        cta=chase.choose_cta(str(options.get("cta_mode") or "auto"),str(options.get("cta_text") or ""),f"{asset.get('id')}:{spec.id}",str(options.get("cta_placement") or "end")); cta_path=_cta_srt(spec,cta["text"],cta["placement"],workdir)
        command=vrf_clipping.build_clip_command(source_path,output,spec,centre=vrf_clipping.focus_centre(source_path,spec),subtitle_path=subtitles,cta_path=cta_path,has_audio=source.has_audio)
        result=subprocess.run(command,capture_output=True,text=True)
        if result.returncode!=0 or not output.exists(): raise RuntimeError(vrf_clipping.safe_clip_error(result.stderr or ""))
        measured=probe(output); remote=_upload_media(output,f"vrf/{asset.get('user_id')}/{asset.get('id')}/{spec.id}.mp4"); rendered[spec.id]={"captions":subtitles is not None,"caption_status":decision["status"],"cta_text":cta["text"],"duration":measured.duration or spec.duration,"width":measured.width,"height":measured.height}; return str(remote)
    results=vrf_clipping.run_clip_batch(plan.specs,render); summary=vrf_clipping.batch_summary(results)
    clips=[]
    for result in results:
        moment=moment_by[result.id]; clips.append({**moment.to_record(),**result.to_record(),**rendered.get(result.id,{"caption_status":decision["status"],"cta_text":""})})
    if not summary["ok"]: raise RuntimeError(str(summary["message"]))
    available=[c for c in clips if c["status"] in {"done","skipped"}]; first=available[0]; meta=rendered.get(first["id"],{})
    client.call("save_processing",id=str(asset.get("id") or ""),processed_path=str(first["path"]),duration_seconds=meta.get("duration") or first["duration"],width=meta.get("width") or 1080,height=meta.get("height") or 1920,processing_plan={"mode":"police_chase","moments":[m.to_record() for m in moments],"source_metadata":source_meta.to_record()})
    return {"processed":True,"mode":"police_chase","clips":clips,"ready":len(available),"failed":summary["failed"],"message":summary["message"],"source_metadata":source_meta.to_record(),"burned_caption_detection":"detected" if burned is True else "not_detected" if burned is False else "unavailable"}


def run_process(client: WorkerClient, payload: dict) -> dict:
    """Format and quality work on a video the user holds rights to."""
    asset_id = str(payload.get("asset_id") or "")
    asset = (client.call("asset", id=asset_id) or {}).get("asset")
    if not asset:
        raise RuntimeError("That video no longer exists.")

    attestation = attest(
        source=str(asset.get("rights_source") or ""),
        user_id=str(asset.get("user_id") or ""),
        rights_holder=str(asset.get("rights_holder") or ""),
        evidence=str(asset.get("rights_evidence") or ""),
    )

    # A URL-added asset has no file until the ingest step fetches one, and for
    # a metadata-only platform it never will. Say which of those it is, rather
    # than failing on an empty download URL.
    ingest_status = str(asset.get("ingest_status") or "")
    if not str(asset.get("storage_path") or "").strip():
        if ingest_status == "metadata_only":
            raise RuntimeError(
                asset.get("ingest_detail")
                or "This platform provides no official route to the video file, "
                   "so there is nothing to process."
            )
        raise RuntimeError(
            "The video file has not been imported yet."
            if ingest_status in {"pending", "fetching"}
            else "This video has no source file to process."
        )

    platform = str(payload.get("platform") or "tiktok")
    with tempfile.TemporaryDirectory(prefix="vrf_process_") as workdir:
        source_path = Path(workdir) / "source.mp4"
        _download(str(asset.get("storage_path") or ""), source_path)

        source = probe(source_path)
        if isinstance(payload.get("police_chase"), dict):
            return _run_police_chase_batch(client, payload, asset, source_path, source, Path(workdir), attestation)
        auto_clip = payload.get("auto_clip")
        if isinstance(auto_clip, dict):
            return _run_viral_clip_batch(
                client,
                payload,
                asset,
                source_path,
                source,
                Path(workdir),
                attestation,
            )

        # Trims come from the caller. build_plan has always accepted them and
        # build_ffmpeg_command has always emitted them; this hands them across
        # so the clipping screen can ask for a section of a long video rather
        # than only ever the whole thing.
        plan = build_plan(
            source,
            platform,
            attestation=attestation,
            trim_start=float(payload.get("trim_start") or 0.0),
            trim_end=float(payload.get("trim_end") or 0.0),
        )
        # The clipping screen sends `clip_options` when the user chose an
        # aspect, a quality, a focus mode or captions. Without it this is the
        # original path, unchanged: an existing caller gets the same encode it
        # has always got, and none of the new code runs.
        options = payload.get("clip_options")
        output = Path(workdir) / "processed.mp4"

        if isinstance(options, dict) and options:
            spec = _clip_spec_from(options, source, plan)
            centre = vrf_clipping.focus_centre(source_path, spec)
            subtitles = (
                _build_subtitles(source_path, spec, Path(workdir), payload)
                if spec.captions else None
            )
            command = vrf_clipping.build_clip_command(
                source_path, output, spec,
                centre=centre,
                subtitle_path=subtitles,
                has_audio=source.has_audio,
            )
        else:
            centre = detect_subject_centre(source_path) if not source.is_vertical else 0.5
            command = build_ffmpeg_command(source_path, output, plan, crop_centre_x=centre)

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not output.exists():
            # The ffmpeg tail goes to the worker log; the customer gets a
            # sentence. Raising the raw stderr put a filter graph on screen.
            logger.error(f"clip encode failed: {(result.stderr or '')[-500:]}")
            raise RuntimeError(vrf_clipping.safe_clip_error(result.stderr or ""))

        # Measured before the temporary directory goes away.
        processed = probe(output)

        remote = _upload_media(
            output, f"vrf/{asset.get('user_id')}/{asset_id}/processed.mp4")

    client.call(
        "save_processing",
        id=asset_id,
        processed_path=str(remote),
        duration_seconds=processed.duration or source.duration,
        width=processed.width or source.width,
        height=processed.height or source.height,
        processing_plan={
            "platform": plan.platform,
            "steps": plan.describe(),
            "warnings": plan.warnings,
        },
    )
    return {"processed": True, "warnings": plan.warnings}


def run_collect_metrics(client: WorkerClient, cipher: TokenCipher, payload: dict) -> dict:
    """Refresh the numbers on posts this product published."""
    posts = (client.call("posts_to_measure", limit=payload.get("limit") or 25)
             or {}).get("posts") or []
    collected = 0

    with httpx.Client(timeout=30.0, follow_redirects=True) as http:
        for post in posts:
            user_id = str(post.get("user_id") or "")
            platform = str(post.get("platform") or "")
            try:
                token = _token_for(client, cipher, user_id, platform)
            except Exception as exc:
                logger.info(f"skipping metrics for {post.get('id')}: {exc}")
                continue

            snapshot = vrf_analytics.collect(
                platform, str(post.get("post_id") or ""), token, client=http)
            if snapshot is None:
                continue

            client.call(
                "record_metric",
                user_id=user_id,
                job_id=str(post.get("id")),
                platform=platform,
                views=snapshot.views,
                likes=snapshot.likes,
                comments=snapshot.comments,
                shares=snapshot.shares,
            )
            collected += 1

    return {"collected": collected, "considered": len(posts)}


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _token_for(
    client: WorkerClient,
    cipher: TokenCipher,
    user_id: str,
    platform: str,
) -> str:
    """The access token for this user's account on this platform.

    The ciphertext is bound to the owner, so a row belonging to anyone else
    raises here rather than producing a usable token.
    """
    account = (client.call("account", user_id=user_id, platform=platform)
               or {}).get("account")
    if not account:
        raise RuntimeError(f"No connected {platform} account for this user.")

    expires_at = _parse_time(account.get("token_expires_at"))
    now = datetime.now(timezone.utc)
    if expires_at and expires_at <= now + timedelta(seconds=300):
        refreshed = _refresh(client, cipher, user_id, platform, account)
        if refreshed:
            return refreshed
        raise RuntimeError(
            f"The {platform} token expired and could not be refreshed. "
            f"The account needs reconnecting."
        )

    return cipher.decrypt(
        str(account.get("access_token_encrypted") or ""),
        user_id=user_id, platform=platform,
    )


def _refresh(
    client: WorkerClient,
    cipher: TokenCipher,
    user_id: str,
    platform: str,
    account: dict,
) -> str:
    """Exchange the refresh token, storing the new pair encrypted."""
    from viral.accounts import OAUTH_PROVIDERS

    provider = OAUTH_PROVIDERS.get(platform)
    sealed = str(account.get("refresh_token_encrypted") or "")
    if provider is None or not sealed:
        return ""

    refresh_token = cipher.decrypt(sealed, user_id=user_id, platform=platform)
    client_id = os.environ.get(provider.client_id_env, "")
    client_secret = os.environ.get(provider.client_secret_env, "")
    if not client_id or not client_secret:
        return ""

    try:
        with httpx.Client(timeout=30.0) as http:
            response = http.post(provider.token_url, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            })
            response.raise_for_status()
            payload = response.json() or {}
    except Exception as exc:
        logger.warning(f"{platform} token refresh failed: {exc}")
        return ""

    access = str(payload.get("access_token") or "")
    if not access:
        return ""

    expires_in = payload.get("expires_in")
    client.call(
        "store_tokens",
        account_id=str(account.get("id")),
        access_token_encrypted=cipher.encrypt(
            access, user_id=user_id, platform=platform),
        refresh_token_encrypted=(
            cipher.encrypt(str(payload["refresh_token"]),
                           user_id=user_id, platform=platform)
            if payload.get("refresh_token") else None
        ),
        token_expires_at=(
            (datetime.now(timezone.utc)
             + timedelta(seconds=float(expires_in))).isoformat()
            if expires_in else None
        ),
    )
    return access


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def run_publish_job(client: WorkerClient, cipher: TokenCipher, job: dict) -> None:
    """Post one due job to its own owner's account, then record what happened."""
    user_id = str(job.get("user_id") or "")
    platform = str(job.get("platform") or "")
    attempts = int(job.get("attempts") or 0) + 1

    request = PublishRequest(
        user_id=user_id,
        video_id=str(job.get("asset_id") or ""),
        media_path=str(job.get("processed_path") or ""),
        caption=str(job.get("caption") or ""),
        platforms=(platform,),
        scheduled_for=(
            _parse_time(job.get("scheduled_for"))
            if job.get("mode") == "native_schedule" else None
        ),
    )

    try:
        token = _token_for(client, cipher, user_id, platform)
        account = (client.call("account", user_id=user_id, platform=platform)
                   or {}).get("account") or {}
        context = AdapterContext(
            media_url=str(job.get("processed_path") or ""),
            page_id=str(account.get("account_ref") or ""),
            ig_user_id=str(account.get("account_ref") or ""),
            title=str(job.get("caption") or "")[:100],
        )
        result = publish_to(
            platform, request, token, context,
            adapters=build_adapters(
                tiktok_direct_post=_flag("TIKTOK_DIRECT_POST_APPROVED")),
        )
    except (RightsError, TokenSecurityError) as exc:
        result = PublishResult(platform, PublishStatus.FAILED,
                               error=str(exc)[:300], retryable=False)
    except Exception as exc:
        # An account problem is the user's to fix; do not spin on it.
        result = PublishResult(platform, PublishStatus.FAILED,
                               error=str(exc)[:300], retryable=False)

    status = result.status.value
    next_attempt = None
    error = result.error

    if result.status is PublishStatus.FAILED:
        if result.retryable and attempts < MAX_ATTEMPTS:
            status = PublishStatus.QUEUED.value
            next_attempt = (
                datetime.now(timezone.utc)
                + timedelta(seconds=backoff_seconds(attempts))
            ).isoformat()
        elif result.retryable:
            error = f"{result.error} (gave up after {attempts} attempts)"

    client.call(
        "update_publish_job",
        id=str(job.get("id")),
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt,
        post_id=result.post_id or None,
        post_url=result.url or None,
        error=error,
    )
    logger.info(
        f"publish {platform} job={job.get('id')} -> {status}"
        + (f" ({error[:80]})" if error else "")
    )


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def handle_task(client: WorkerClient, cipher: "TokenCipher | None", task: dict) -> None:
    kind = str(task.get("kind") or "")
    payload = task.get("payload") or {}
    try:
        if kind == "discover":
            result = run_discover(payload)
        elif kind == "analyse":
            result = run_analyse(client, payload)
        elif kind == "ingest":
            result = run_ingest(client, cipher, payload)
        elif kind == "process":
            result = run_process(client, payload)
        elif kind == "collect_metrics":
            result = run_collect_metrics(client, cipher, payload)
        elif kind == "explain":
            result = run_explain(client, payload)
        else:
            raise RuntimeError(f"unknown task kind {kind!r}")
    except Exception as exc:
        logger.warning(f"task {task.get('id')} ({kind}) failed: {exc}")
        client.call("complete_task", id=str(task.get("id")),
                    status="failed", error=str(exc)[:500])
        return

    client.call("complete_task", id=str(task.get("id")),
                status="done", result=result)
    logger.info(f"task {task.get('id')} ({kind}) done")


def poll_once(client: WorkerClient, cipher: "TokenCipher | None") -> bool:
    """One pass. Returns True if anything was done."""
    did_work = False

    task = (client.call("claim_task") or {}).get("task")
    if task:
        handle_task(client, cipher, task)
        did_work = True

    # Publish jobs are only claimed when they can actually be carried out.
    # Claiming one without a cipher would move it out of the queue and then
    # fail it, losing the job for a reason the operator can fix.
    if cipher is not None:
        job = (client.call("claim_publish_job") or {}).get("job")
        if job:
            run_publish_job(client, cipher, job)
            did_work = True

    return did_work


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Viral Reels Finder worker")
    parser.add_argument("--once", action="store_true",
                        help="do one pass and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Only publishing needs the token key. Discovery, analysis and processing
    # never touch a stored OAuth token, so refusing to start without the key
    # took the whole worker down over a capability most tasks do not use --
    # and a queued discovery task with no worker behind it is invisible to the
    # user, who just sees "Searchingâ€¦" forever.
    #
    # Start anyway, carry the reason, and let the publish path refuse on its
    # own terms.
    cipher: TokenCipher | None
    try:
        cipher = TokenCipher()
    except TokenSecurityError as exc:
        cipher = None
        logger.warning(
            f"{exc} Discovery and analysis will still run; publishing and "
            f"account connection are disabled until VRF_TOKEN_KEY is set."
        )

    client = WorkerClient()
    logger.info(
        "viral worker ready"
        + ("" if cipher else " (publishing disabled: no VRF_TOKEN_KEY)")
    )

    if args.once:
        poll_once(client, cipher)
        return 0

    while True:
        try:
            did_work = poll_once(client, cipher)
        except KeyboardInterrupt:
            logger.info("stopping")
            return 0
        except Exception as exc:
            logger.warning(f"poll failed: {exc}")
            did_work = False
        time.sleep(POLL_SECONDS if did_work else IDLE_SLEEP)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _download(url: str, destination: Path) -> None:
    """Fetch the user's own source file."""
    if not url:
        raise RuntimeError("This video has no source URL.")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("The source must be an http(s) URL you control.")

    with httpx.Client(timeout=300.0, follow_redirects=True) as http:
        with http.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes"}


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
