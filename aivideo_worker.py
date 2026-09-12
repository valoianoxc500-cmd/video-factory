"""Entry point for the AI Video Maker worker.

    python aivideo_worker.py            # poll continuously
    python aivideo_worker.py --once     # one job, for a smoke test

Its own process and its own queue, beside the video worker and the Viral Reels
worker rather than inside either. Three products, three loops: a job wedged in
one cannot stop the others being claimed, which is the isolation the product
brief asks for and the reason this is not another channel on `jobs`.

Recovery is the other half. Each job owns a directory under `workspace/`, the
pipeline checkpoints every stage into it, and a job whose worker died is
reclaimed by `ai_video_claim_job` after a stale window and resumes from the
last completed stage. A restart costs the stage that was interrupted, never
the script, the narration or the footage that already succeeded.

Environment it needs:
    APP_URL / WORKER_API_BASE   the deployment's base URL
    WORKER_TOKEN                the shared worker token
    PEXELS_API_KEY              footage (Pixabay is the fallback)
    AI_GATEWAY_API_KEY          the script model
"""

# Imported first, for the side effect: loads .env and installs the OS trust
# store for httpx and aiohttp. Edge TTS speaks to Microsoft over TLS through
# aiohttp, and without this it fails with CERTIFICATE_VERIFY_FAILED wherever
# TLS is intercepted -- which looks exactly like "the voice provider is down".
import settings  # noqa: F401

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from aivideo import pipeline

REPO_ROOT = Path(__file__).resolve().parent
WORKSPACE = REPO_ROOT / "workspace" / "aivideo"
MUSIC = REPO_ROOT / "assets" / "music"

APP_URL = (os.environ.get("APP_URL") or os.environ.get("WORKER_API_BASE") or "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
POLL_SECONDS = float(os.environ.get("AIVIDEO_POLL_SECONDS", "6"))

#: How many times one job may be resumed before it is called done-for. Bounded
#: so a job that fails the same way forever stops costing money, and generous
#: enough that a provider having a bad ten minutes does not end it.
MAX_ATTEMPTS = int(os.environ.get("AIVIDEO_MAX_ATTEMPTS", "3"))

logger = logging.getLogger("aivideo.worker")


class WorkerConfigError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {"authorization": f"Bearer {WORKER_TOKEN}"}


def claim(client: httpx.Client) -> dict | None:
    resp = client.post(f"{APP_URL}/api/worker/aivideo/claim", headers=_headers())
    if resp.status_code == 401:
        raise WorkerConfigError(
            "the deployment rejected WORKER_TOKEN; it must match the value set "
            "on the web project"
        )
    resp.raise_for_status()
    return resp.json().get("job")


def report(client: httpx.Client, job_id: str, **fields) -> None:
    """Push job state. Best effort: a lost update must not fail a render."""
    try:
        client.post(
            f"{APP_URL}/api/worker/aivideo/update",
            headers=_headers(),
            json={"id": job_id, **fields},
            timeout=30.0,
        )
    except Exception as exc:
        logger.warning(f"could not report progress: {type(exc).__name__}: {exc}")


def job_directory(job_id: str) -> Path:
    return WORKSPACE / job_id


def run_job(client: httpx.Client, job: dict) -> None:
    job_id = str(job["id"])
    spec = dict(job.get("spec") or {})
    spec.setdefault("topic", job.get("topic", ""))
    directory = job_directory(job_id)
    attempts = int(job.get("attempts") or 1)

    def progress(label: str, percent: int) -> None:
        report(client, job_id, status="running", message=label, progress=percent,
               stage=pipeline.load_state(directory).stage)

    started = time.monotonic()
    try:
        state = asyncio.run(
            pipeline.run(
                spec,
                directory,
                music_library=MUSIC if MUSIC.exists() else None,
                on_progress=progress,
            )
        )
    except pipeline.TerminalFailure as exc:
        # Nothing a retry would change. Say so plainly, without naming a
        # provider or a stage.
        logger.error(f"job {job_id} cannot be completed: {exc}")
        report(
            client, job_id, status="error", progress=0,
            message="", error=_customer_message(exc, terminal=True),
        )
        return
    except pipeline.RecoverableFailure as exc:
        will_retry = attempts < MAX_ATTEMPTS
        logger.warning(
            f"job {job_id} stopped ({exc}); "
            f"{'requeueing' if will_retry else 'out of attempts'}"
        )
        report(
            client, job_id,
            status="queued" if will_retry else "error",
            message="Still working — picking this back up" if will_retry else "",
            error="" if will_retry else _customer_message(exc, terminal=False),
        )
        return
    except Exception as exc:
        will_retry = attempts < MAX_ATTEMPTS
        logger.exception(f"job {job_id} hit an unexpected error: {exc}")
        report(
            client, job_id,
            status="queued" if will_retry else "error",
            message="Still working — picking this back up" if will_retry else "",
            error="" if will_retry else
                  "This video could not be finished. Please try again.",
        )
        return

    elapsed = time.monotonic() - started
    video = Path(state.output)
    logger.info(
        f"job {job_id} finished in {elapsed:.0f}s "
        f"(providers: {', '.join(state.providers_used) or 'none'}, "
        f"fallbacks: {len(state.fallbacks)})"
    )

    # Upload through the existing media path so this product stores files the
    # same way every other one does.
    video_url, thumb_url = _publish(video, Path(state.thumbnail) if state.thumbnail else None)

    report(
        client, job_id,
        status="done", progress=100, stage="", message="Ready", error="",
        video_url=video_url, thumbnail_url=thumb_url,
        duration_actual=round(state.narration_seconds, 2),
        cost_usd=round(state.cost_usd, 4),
        providers_used=state.providers_used,
        fallbacks=state.fallbacks,
    )


def _customer_message(exc: Exception, *, terminal: bool) -> str:
    """Turn an engine failure into something a customer can act on.

    Never the exception text: that names providers, HTTP statuses and stage
    identifiers, none of which mean anything to the person who asked for a
    video about Dubai.
    """
    detail = str(exc).lower()
    if "no topic" in detail:
        return "Add a topic and try again."
    if "footage" in detail or "stock provider" in detail:
        return (
            "We couldn't find good footage for this idea. Try describing it "
            "in more visual terms — a place, an object, an action."
        )
    if "disk space" in detail:
        return "We're low on space right now. Your progress is saved — try again shortly."
    if terminal:
        return "This idea couldn't be turned into a video. Try rewording it."
    return "This video didn't finish. Your progress is saved — try again."


def _publish(video: Path, thumbnail: Path | None) -> tuple[str, str]:
    """Store the finished artifacts and return their URLs.

    Reuses `worker/storage.py`, the same media layer the other products
    upload through, so there is one storage backend to configure and one place
    that knows about buckets.
    """
    sys.path.insert(0, str(REPO_ROOT / "worker"))
    import storage  # noqa: E402

    video_url = storage.upload_media(video, "videos")
    thumb_url = ""
    if thumbnail and thumbnail.exists():
        try:
            thumb_url = storage.upload_media(thumbnail, "thumbnails")
        except Exception as exc:
            # A missing thumbnail costs a grid tile, not the video.
            logger.warning(f"thumbnail upload failed: {type(exc).__name__}: {exc}")
    return video_url, thumb_url


def serve(once: bool) -> int:
    if not APP_URL or not WORKER_TOKEN:
        raise WorkerConfigError(
            "APP_URL and WORKER_TOKEN must be set (worker/.env is read by "
            "settings.py)"
        )
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    logger.info(f"AI Video Maker worker polling {APP_URL} every {POLL_SECONDS:.0f}s")

    with httpx.Client(timeout=60.0) as client:
        while True:
            try:
                job = claim(client)
            except WorkerConfigError:
                raise
            except Exception as exc:
                logger.warning(f"claim failed: {type(exc).__name__}: {exc}")
                time.sleep(POLL_SECONDS)
                continue

            if job is None:
                if once:
                    logger.info("no queued job; exiting (--once)")
                    return 0
                time.sleep(POLL_SECONDS)
                continue

            run_job(client, job)
            if once:
                return 0


def _quiet_windows_ssl_teardown() -> None:
    """Use the selector loop on Windows so TLS sockets close quietly.

    The default Proactor loop tears down aiohttp's TLS transports after the
    loop has gone, and logs a "Fatal error on SSL transport ... Event loop is
    closed" traceback for a job that already succeeded. In a worker log that
    reads like a crash.

    Safe here because nothing in this product uses asyncio subprocesses --
    FFmpeg is invoked with blocking `subprocess.run` -- which is the one thing
    the selector loop cannot do on Windows.
    """
    if sys.platform != "win32":
        return
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is not None:
        asyncio.set_event_loop_policy(policy())


def main() -> int:
    _quiet_windows_ssl_teardown()
    parser = argparse.ArgumentParser(description="AI Video Maker worker")
    parser.add_argument("--once", action="store_true", help="run one job and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return serve(args.once)
    except WorkerConfigError as exc:
        logger.error(str(exc))
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
