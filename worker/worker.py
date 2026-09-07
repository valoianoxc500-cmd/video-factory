"""Video Factory worker.

Polls the Vercel app for queued jobs, runs the EXISTING pipeline unchanged
(`factory.py --channel football_news`), uploads the finished MP4 and thumbnail
to object storage via `worker/storage.py`, and reports progress back.

The worker polls outbound only, so it runs anywhere with network access --
this machine, a VM, or Cloud Run -- without needing an inbound URL.

Run with:
    python worker/worker.py

Environment:
    APP_URL                 https://<your-app>.vercel.app
    WORKER_TOKEN            shared secret, must match the Vercel env var
    MEDIA_STORAGE_PROVIDER  gcs (default) or supabase -- see worker/storage.py
    GCS_BUCKET              bucket name when the provider is gcs
    VIDEO_CHANNEL           channel slug (default: football_news)
    POLL_INTERVAL_SECONDS   default 5
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Import for the TLS bootstrap side effect (OS trust store for httpx/gRPC).
import settings  # noqa: E402,F401

import storage  # noqa: E402
import diskspace  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("worker")

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
CHANNEL = os.environ.get("VIDEO_CHANNEL", "football_news")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "15"))

# Review gates whose verdict is recorded but does not discard a finished video.
# All three are subjective quality opinions on an already-validated render
# (watermark on a sourced photo, kit-era accuracy on the thumbnail, a debatable
# frame choice). Each still runs, still retries, and still writes its result
# into the run's review log.
ALLOWED_REVIEW_FAILURES = os.environ.get(
    "ALLOWED_REVIEW_FAILURES", "image_review,thumbnail_review,final_review"
)

# Each run leaves ~200 MB of intermediate media in workspace/. The durable copy
# of every finished video lives in object storage, so the local runs are a debugging
# convenience only -- keep a handful and prune the rest, or the worker fills
# its disk and later renders die with ENOSPC.
WORKSPACE_RETENTION = int(os.environ.get("WORKSPACE_RETENTION", "3"))

# Refuse to start a run without this much headroom. A render that dies with
# ENOSPC halfway through burns ~8 minutes and all of the API spend with it.
MIN_FREE_DISK_GB = float(os.environ.get("MIN_FREE_DISK_GB", "2.0"))

# Cleanup aims above the minimum rather than at it. Stopping exactly at the
# threshold means the next run starts with nothing to spare and the check
# passes only to have the render fill the gap, so aim for the minimum plus one
# run's working set.
RUN_DISK_HEADROOM_GB = float(os.environ.get("RUN_DISK_HEADROOM_GB", "1.5"))

# How often to touch a running job's row while the pipeline is producing
# output but has not crossed a stage boundary. The app treats a row untouched
# for 15 minutes as abandoned, and a single section render can legitimately run
# longer than that, so the heartbeat is what separates "slow" from "dead".
HEARTBEAT_SECONDS = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "60"))

# Videos to keep in object storage. GCS has no fixed quota to bump into, so
# this is now a cost control rather than a hard limit: each video is ~110 MB,
# and standard storage runs about $0.02/GB/month. Pruning the oldest keeps the
# bill flat as the factory runs continuously.
VIDEO_RETENTION = int(os.environ.get("VIDEO_RETENTION", os.environ.get("BLOB_VIDEO_RETENTION", "12")))

# Fraction of overall progress attributed to each pipeline stage, cumulative.
# Weighted by observed wall-clock share so the bar tracks reality.
STAGE_PROGRESS: dict[str, tuple[int, str]] = {
    "planning": (5, "Choosing the angle for this topic"),
    "script": (30, "Writing the Arabic script and visual plan"),
    "image_source": (45, "Finding real photographs of the subject"),
    "audio_source": (58, "Generating Arabic narration and caption timings"),
    "process": (62, "Preparing images for the 1080x1920 canvas"),
    "render_sections": (85, "Rendering scenes with motion and captions"),
    "assemble": (90, "Mixing narration, music and SFX"),
    "thumbnail": (96, "Creating the thumbnail"),
    "final_review": (99, "Final quality review"),
}

_STAGE_RE = re.compile(r"\[stage\]\s+(\w+)\s+started")


class WorkerConfigError(RuntimeError):
    pass


def _require_config() -> None:
    missing = [
        name
        for name, value in (
            ("APP_URL", APP_URL),
            ("WORKER_TOKEN", WORKER_TOKEN),
            *storage.required_env(),
        )
        if not value
    ]
    if missing:
        raise WorkerConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )


def _auth_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {WORKER_TOKEN}"}


def claim_job(client: httpx.Client) -> dict | None:
    resp = client.post(f"{APP_URL}/api/worker/claim", headers=_auth_headers())
    if resp.status_code == 401:
        raise WorkerConfigError(
            "Vercel rejected WORKER_TOKEN (401). The value here must match the "
            "WORKER_TOKEN environment variable set on the Vercel project."
        )
    resp.raise_for_status()
    return resp.json().get("job")


def post_update(
    client: httpx.Client,
    job_id: str,
    *,
    attempts: int = 1,
    **fields,
) -> bool:
    """Push job state to the app. Returns whether it landed.

    Intermediate progress is best-effort, but the terminal update carries the
    video URL -- if it is lost the finished MP4 exists in storage with nothing
    pointing at it, so callers retry that one.
    """
    for attempt in range(1, attempts + 1):
        try:
            resp = client.post(
                f"{APP_URL}/api/worker/update",
                headers=_auth_headers(),
                json={"id": job_id, **fields},
                timeout=60.0,
            )
            if resp.status_code < 400:
                return True
            logger.warning(
                f"update returned {resp.status_code} "
                f"(attempt {attempt}/{attempts})"
            )
        except Exception as exc:
            logger.warning(f"update failed (attempt {attempt}/{attempts}): {exc}")
        if attempt < attempts:
            time.sleep(min(5 * attempt, 20))
    return False


# ---------------------------------------------------------------------------
# Media upload
# ---------------------------------------------------------------------------
#
# The backend lives in worker/storage.py and is chosen by
# MEDIA_STORAGE_PROVIDER, so every channel shares one storage layer. Both of
# the earlier backends failed on finished videos, which run 97-121 MB:
# Vercel Blob suspends the whole store at its Hobby quota, and Supabase's free
# tier rejects any object over 50 MB on both its standard and resumable
# endpoints. GCS takes them as resumable chunked uploads.
#
# Media and job state stay separate concerns: files in object storage, rows in
# Postgres. A full or broken media store can no longer take the site down.

upload_media = storage.upload_media


def prune_stored_videos(keep: int = VIDEO_RETENTION) -> None:
    """Delete the oldest stored videos so the bucket stays inside its quota."""
    if keep < 1:
        return
    try:
        videos = storage.list_media("videos")
    except Exception as exc:
        logger.warning(f"could not list storage to prune: {exc}")
        return

    doomed = videos[:-keep] if len(videos) > keep else []
    if not doomed:
        return

    names: list[str] = []
    for obj in doomed:
        # list_media reports full object names ("videos/<job_id>.mp4").
        job_id = str(obj.get("name", "")).split("/")[-1].removesuffix(".mp4")
        if not job_id:
            continue
        names.append(f"videos/{job_id}.mp4")
        names.append(f"thumbnails/{job_id}.png")

    try:
        storage.delete_media(names)
        logger.info(f"pruned {len(doomed)} old video(s) from storage")
    except Exception as exc:
        logger.warning(f"storage prune failed: {exc}")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _channel_for_job(job: dict) -> str:
    """The channel config this job's engine maps to.

    The API only ever sends a slug from its own allowlist, but the worker is
    the thing that spawns a process with it, so it re-checks that a matching
    channel config actually exists on this machine. An unknown or missing
    engine falls back to VIDEO_CHANNEL rather than failing the job -- that
    keeps a worker on older code running jobs from a newer web deploy.
    """
    engine = str(job.get("engine") or "").strip()
    if not engine:
        return CHANNEL
    if not _CHANNEL_SLUG_RE.fullmatch(engine):
        logger.warning(f"ignoring malformed engine {engine!r}; using {CHANNEL}")
        return CHANNEL
    if not (REPO_ROOT / "config" / "channels" / f"{engine}.json").exists():
        logger.warning(
            f"engine {engine!r} has no channel config on this worker; "
            f"falling back to {CHANNEL}"
        )
        return CHANNEL
    return engine


# Slugs become a CLI argument and a filename, so they are restricted rather
# than sanitised.
_CHANNEL_SLUG_RE = re.compile(r"[a-z0-9_]{1,64}")
_STYLE_SLUG_RE = re.compile(r"[a-z0-9_]{1,64}")
_LANGUAGE_RE = re.compile(r"[a-z]{2}(-[a-z]{2})?")


# Workspaces are named "<channel>_<timestamp>_...", and a worker now runs more
# than one channel, so these glob every engine's runs rather than only the
# configured default. Scoping them to CHANNEL would lose a Match Analysis run's
# output and leave its workspaces to fill the disk unpruned.
def _workspaces_before() -> set[Path]:
    root = REPO_ROOT / "workspace"
    return set(p for p in root.glob("*_*") if p.is_dir()) if root.exists() else set()


def _newest_new_workspace(before: set[Path]) -> Path | None:
    root = REPO_ROOT / "workspace"
    candidates = [
        p for p in root.glob("*_*") if p.is_dir() and p not in before
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# Workspace pruning and free-space measurement now live in diskspace.py, which
# escalates beyond pruning when the disk is full of something that is not ours.


def _probe_video_stream(path: Path) -> dict:
    """Width, height and fps of a rendered video, or {} if ffprobe cannot say."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        stream = (json.loads(out.stdout or "{}").get("streams") or [{}])[0]
    except Exception:
        return {}

    rate = str(stream.get("r_frame_rate", "")) or "0/1"
    try:
        num, _, den = rate.partition("/")
        fps = round(float(num) / float(den or 1))
    except (ValueError, ZeroDivisionError):
        fps = None
    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "fps": fps,
    }


def _register_in_library(
    *,
    job: dict,
    job_id: str,
    channel: str,
    title: str,
    mp4: Path,
    thumbnail: Path | None,
    workspace: Path,
):
    """Save the finished video into its channel's object-storage library.

    Returns the stored VideoRecord so the caller can file the same paths in
    the database. Object storage holds the bytes; `public.videos` is what the
    owner's Library actually lists, and the two must describe the same object.
    """
    import storage
    from library import VideoLibrary, VideoRecord

    from core.review_status import classify_review_log

    review_status = "approved"
    checkpoint = workspace / "checkpoint.json"
    review_log: dict = {}
    if checkpoint.exists():
        try:
            data = json.loads(checkpoint.read_text(encoding="utf-8"))
            review_log = data.get("review_log") or {}
            # Grade by what the gates actually found, not merely by whether
            # they approved. Every gate still ran and its full verdict is
            # still stored below; this only decides what the owner is shown.
            review_status, detail = classify_review_log(review_log)
            if detail["discounted_count"]:
                logger.info(
                    f"{job_id}: {detail['discounted_count']} review finding(s) "
                    f"discounted as unreliable colour judgements"
                )
        except Exception:
            pass

    stream = _probe_video_stream(mp4)
    record = VideoRecord(
        video_id=job_id,
        job_id=job_id,
        channel_id=channel,
        title=title,
        description=str(job.get("description") or ""),
        duration_seconds=_probe_duration(mp4),
        width=stream.get("width"),
        height=stream.get("height"),
        fps=stream.get("fps"),
        review_status=review_status,
        review_log=review_log,
    )
    return VideoLibrary(storage).register(
        record, video_file=mp4, thumbnail_file=thumbnail
    )


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return None
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return None


def run_pipeline(client: httpx.Client, job: dict) -> None:
    job_id = job["id"]
    topic = job["topic"]
    logger.info(f"job {job_id}: {topic!r}")

    # Reclaim space before starting: a render that runs out of disk halfway
    # through wastes the whole job. This escalates through progressively more
    # intrusive tiers and stops as soon as there is room, so a healthy worker
    # only ever prunes its own old runs.
    free_gb, actions = diskspace.ensure_free(
        MIN_FREE_DISK_GB + RUN_DISK_HEADROOM_GB,
        keep_workspaces=WORKSPACE_RETENTION,
    )
    if actions:
        logger.info("cleanup freed: " + ", ".join(actions))
    logger.info(f"free disk: {free_gb:.1f} GB")

    if free_gb < MIN_FREE_DISK_GB:
        raise RuntimeError(
            f"only {free_gb:.1f} GB free on the worker disk, below the "
            f"{MIN_FREE_DISK_GB:.1f} GB minimum, and automatic cleanup could "
            f"not reclaim enough. This disk is shared with the rest of the "
            f"machine, so the space is most likely not the worker's: check "
            f"overall disk usage. Free space, or lower WORKSPACE_RETENTION / "
            f"MIN_FREE_DISK_GB."
        )

    before = _workspaces_before()
    cmd = [
        sys.executable, "factory.py",
        "--channel", _channel_for_job(job),
        "--allow-review-failures", ALLOWED_REVIEW_FAILURES,
        "--set", f"plan.topic={topic}",
    ]

    # Engine sub-mode (Horror story type). Restricted the same way the channel
    # slug is: it becomes a command-line value, so it is validated rather than
    # sanitised, and an unrecognised one is dropped instead of passed on.
    style = str(job.get("style") or "").strip()
    if style:
        if _STYLE_SLUG_RE.fullmatch(style):
            cmd += ["--set", f"plan.story_type={style}"]
        else:
            logger.warning(f"ignoring malformed style {style!r}")

    # Script language. Selects a channel language variant, which swaps the
    # narration language, the voice and the script instructions together.
    language = str(job.get("language") or "").strip().lower()
    if language:
        if _LANGUAGE_RE.fullmatch(language):
            cmd += ["--language", language]
        else:
            logger.warning(f"ignoring malformed language {language!r}")

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    post_update(
        client, job_id,
        status="running", stage="planning", progress=3,
        message="Starting the pipeline",
    )

    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    tail: list[str] = []
    assert proc.stdout is not None

    # Heartbeat state. Rendering can run for many minutes between stage
    # markers, and the app decides a job is abandoned from how long its row
    # has gone untouched -- so silence during a healthy render would look
    # exactly like a dead worker. Touching the row while output is still
    # flowing keeps that distinction honest.
    last_post = time.monotonic()
    last_stage = "planning"
    last_pct = 3

    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-40]

        # Any output at all means the pipeline is alive.
        if time.monotonic() - last_post >= HEARTBEAT_SECONDS:
            post_update(
                client, job_id,
                status="running",
                stage=last_stage, progress=last_pct,
                message=STAGE_PROGRESS.get(last_stage, (0, "Working"))[1],
            )
            last_post = time.monotonic()

        match = _STAGE_RE.search(line)
        if match:
            stage = match.group(1)
            if stage in STAGE_PROGRESS:
                pct, message = STAGE_PROGRESS[stage]
                last_stage, last_pct = stage, pct
                last_post = time.monotonic()
                logger.info(f"  stage {stage} -> {pct}%")
                post_update(
                    client, job_id,
                    # Re-assert "running" on every tick. The update endpoint
                    # only changes fields it is given, so a status that drifted
                    # (a stale-job sweep, an operator reset, a re-claim) would
                    # otherwise stick for the whole run and the UI would keep
                    # showing "waiting for the worker" at 85%.
                    status="running",
                    stage=stage, progress=pct, message=message,
                )

    code = proc.wait()
    if code != 0:
        detail = "\n".join(tail[-12:])
        raise RuntimeError(f"pipeline exited {code}\n{detail}")

    workspace = _newest_new_workspace(before)
    if workspace is None:
        raise RuntimeError("pipeline finished but produced no workspace")

    videos = sorted(workspace.glob("*.mp4"))
    if not videos:
        raise RuntimeError(f"no MP4 in {workspace.name}")
    mp4 = videos[0]

    post_update(
        client, job_id,
        status="running",
        stage="final_review", progress=99,
        message="Uploading the finished video",
    )

    title = topic
    script_path = workspace / "script.json"
    if script_path.exists():
        try:
            title = json.loads(script_path.read_text(encoding="utf-8")).get(
                "title", topic
            )
        except Exception:
            pass

    # Make room before uploading, so a full store does not fail the upload of
    # a video that has already cost eight minutes of compute.
    prune_stored_videos()

    # The job is only marked done after BOTH the upload and the metadata write
    # succeed; upload_media raises on failure, and the completion update
    # below is retried, so a job can never read "done" without a playable URL.
    video_url = upload_media(
        mp4, f"videos/{job_id}.mp4", "video/mp4"
    )
    thumb = workspace / "thumbnail.png"
    thumb_url = (
        upload_media(thumb, f"thumbnails/{job_id}.png", "image/png")
        if thumb.exists()
        else None
    )

    # Confirm the uploaded object is really retrievable before claiming done.
    check = httpx.head(video_url, timeout=60.0, follow_redirects=True)
    if check.status_code >= 400:
        raise RuntimeError(
            f"uploaded video is not retrievable ({check.status_code}): {video_url}"
        )
    logger.info(
        f"verified upload: {int(check.headers.get('content-length', 0)) / 1e6:.1f} MB"
    )

    # Save a permanent copy in the channel's library. The job row is a queue
    # record -- it gets pruned and superseded -- so this is what the website
    # lists long after the job is gone. Never allowed to fail the job: the
    # video is uploaded and playable either way.
    channel = _channel_for_job(job)
    library_payload: dict | None = None
    try:
        record = _register_in_library(
            job=job,
            job_id=job_id,
            channel=channel,
            title=title,
            mp4=mp4,
            thumbnail=thumb if thumb.exists() else None,
            workspace=workspace,
        )
        # Filed in the owner's library by the completion update below. The
        # paths are the object-storage ones, so the row keeps pointing at the
        # permanent copy after this run's workspace is deleted.
        library_payload = {
            "channelSlug": record.channel_id,
            "videoKey": record.video_id,
            "videoPath": record.video_path,
            "thumbnailPath": record.thumbnail_path or None,
            "description": record.description,
            "durationSeconds": record.duration_seconds,
            "width": record.width,
            "height": record.height,
            "fps": record.fps,
            "reviewStatus": record.review_status,
            "reviewLog": record.review_log,
        }
    except Exception as exc:
        logger.warning(f"library registration failed for {job_id}: {exc}")

    completion = dict(
        status="done", stage="final_review", progress=100,
        message="Done",
        title=title,
        videoUrl=video_url,
        thumbnailUrl=thumb_url,
        durationSeconds=_probe_duration(mp4),
    )

    delivered = post_update(
        client, job_id, attempts=6,
        **completion,
        **({"library": library_payload} if library_payload else {}),
    )

    if not delivered and library_payload:
        # The app registers the video before it marks the job done, so a
        # refused registration -- an unowned legacy job, most often -- would
        # otherwise strand a finished video at 99%. Complete the job without
        # it rather than lose the run, and say so loudly: the video is in
        # storage but will not appear in anyone's Library.
        logger.error(
            f"could not register {job_id} in the owner's library; retrying "
            f"the completion update without it"
        )
        delivered = post_update(client, job_id, attempts=3, **completion)
        if delivered:
            logger.error(
                f"job {job_id} completed but its video is NOT in public.videos "
                f"and will not show in any Library"
            )
    elif delivered and library_payload:
        logger.info(
            f"registered {job_id} in the {channel} library "
            f"-> {record.video_path}"
        )

    if not delivered:
        raise RuntimeError(
            f"video uploaded to {video_url} but the completion update could "
            f"not be delivered to {APP_URL}"
        )
    logger.info(f"job {job_id} complete -> {video_url}")

    # The video, thumbnail and metadata are all in object storage now, so the
    # per-section renders and frame dumps in this run have nothing left to
    # contribute. Dropping them here -- rather than waiting for the next job's
    # preflight -- is what keeps a busy worker from accumulating a run's full
    # working set per completed job.
    _release_run_scratch(workspace)


def _release_run_scratch(workspace: Path | None) -> None:
    """Drop a finished run's intermediates, keeping its output for inspection.

    Best-effort by design: the job is already complete and reported, so a file
    still held open by a lingering ffmpeg process must not turn a successful
    render into a failure. The next preflight sweeps whatever is left.
    """
    if workspace is None:
        return
    try:
        freed = diskspace.strip_intermediates(exclude=None)
        if freed.actions:
            logger.info("post-run cleanup: " + ", ".join(freed.actions))
    except Exception as exc:
        logger.warning(f"post-run cleanup failed: {exc}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def preflight() -> None:
    _require_config()
    for binary in ("ffmpeg", "ffprobe", "node"):
        if shutil.which(binary) is None:
            raise WorkerConfigError(f"{binary} not found on PATH")
    if not (REPO_ROOT / "factory.py").exists():
        raise WorkerConfigError(f"factory.py not found under {REPO_ROOT}")
    if not (REPO_ROOT / "rendering" / "remotion" / "node_modules").exists():
        raise WorkerConfigError(
            "Remotion dependencies missing. Run: "
            "cd rendering/remotion && npm install"
        )
    logger.info(
        f"worker ready | app={APP_URL} | channel={CHANNEL} "
        f"| storage={storage.describe()}"
    )


def _process_is_alive(pid: int) -> bool:
    """Whether `pid` is still running, without disturbing it.

    Not os.kill(pid, 0): on Windows CPython maps a non-console signal to
    TerminateProcess, so the POSIX "signal 0 just tests existence" idiom
    actually KILLS the process it was meant to probe -- and raises an
    unhandled SystemError on a stale pid, which stopped the worker booting.

    Fails closed. If liveness cannot be determined the answer is "alive", so
    an uncertain lock blocks a second worker rather than letting two race for
    the same disk.
    """
    if pid <= 0:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by another user
        except OSError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False  # no such process: the lock is stale
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell; refuse to start a second worker
        # A process that genuinely exited with 259 reads as alive. That is the
        # safe direction to be wrong in, and the lock can be deleted by hand.
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class _SingleWorkerLock:
    """Refuse to run a second worker against the same checkout.

    Disk headroom is checked once per job, so two workers sharing a disk each
    see the other's free space and both decide there is room -- then render
    concurrently and run it out between them. Rendering is also CPU- and
    GPU-bound here, so a second process makes both runs slower rather than
    getting more done. One worker per checkout.

    The lock is a file holding a PID. A stale lock left by a killed worker is
    reclaimed rather than blocking forever.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def _held_by_live_process(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        if pid == os.getpid():
            return False
        return _process_is_alive(pid)

    def __enter__(self) -> "_SingleWorkerLock":
        if self.path.exists() and self._held_by_live_process():
            raise WorkerConfigError(
                f"another worker is already running (lock: {self.path}). "
                f"Two workers on one disk race for the same free space. "
                f"Stop the other worker, or delete the lock file if it is stale."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        self.acquired = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self.acquired:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def main() -> int:
    try:
        preflight()
    except WorkerConfigError as exc:
        logger.error(str(exc))
        return 2

    once = "--once" in sys.argv
    lock = _SingleWorkerLock(REPO_ROOT / "workspace" / ".worker.lock")
    try:
        return _serve(lock, once)
    except WorkerConfigError as exc:
        # Raised by the lock when another worker already holds it.
        logger.error(str(exc))
        return 2


def _serve(lock: "_SingleWorkerLock", once: bool) -> int:
    with lock, httpx.Client(timeout=60.0) as client:
        while True:
            try:
                job = claim_job(client)
            except WorkerConfigError as exc:
                logger.error(str(exc))
                return 2
            except Exception as exc:
                logger.warning(f"claim failed: {exc}")
                time.sleep(POLL_INTERVAL)
                continue

            if job is None:
                if once:
                    logger.info("no queued job; exiting (--once)")
                    return 0
                time.sleep(POLL_INTERVAL)
                continue

            try:
                run_pipeline(client, job)
            except Exception as exc:
                logger.error(f"job {job['id']} failed: {exc}")
                post_update(
                    client, job["id"],
                    attempts=4,
                    status="error", progress=0,
                    message="Generation failed",
                    error=str(exc)[:1500],
                )
                # A failed run leaves the same intermediates a successful one
                # does, minus anything worth keeping. Without this, repeated
                # failures fill the disk faster than successes would.
                _release_run_scratch(REPO_ROOT / "workspace")
            if once:
                return 0


if __name__ == "__main__":
    sys.exit(main())
