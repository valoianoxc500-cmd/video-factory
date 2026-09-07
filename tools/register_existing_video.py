"""Register an already-rendered workspace video into its channel library.

For videos produced before the library existed, or any run whose upload step
did not happen. Re-runs nothing and calls no AI API -- it reads the finished
MP4, thumbnail and checkpoint from the workspace, probes the file for its real
duration and resolution, and saves the same three objects the worker would.

    python tools/register_existing_video.py workspace/horror_stories_... [--overwrite]

Idempotent: a video already in the library is left alone unless --overwrite.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "worker"))

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
logger = logging.getLogger("register")


def _load_worker_env() -> None:
    """Load worker/.env so the storage backend is configured as the worker has it."""
    env_path = REPO_ROOT / "worker" / ".env"
    if not env_path.exists():
        return
    import os

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    data = json.loads(out.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    rate = str(stream.get("r_frame_rate", "0/1"))
    try:
        num, _, den = rate.partition("/")
        fps = round(float(num) / float(den or 1))
    except (ValueError, ZeroDivisionError):
        fps = None
    duration = data.get("format", {}).get("duration")
    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "fps": fps,
        "duration_seconds": float(duration) if duration else None,
    }


def _find_video(workspace: Path) -> Path:
    candidates = [
        p for p in workspace.glob("*.mp4")
        if p.stem != "output" and not p.stem.startswith("clip")
    ]
    if not candidates:
        raise FileNotFoundError(f"no finished MP4 in {workspace}")
    return max(candidates, key=lambda p: p.stat().st_size)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--video-id", default="", help="defaults to the run id")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise SystemExit(f"not a workspace: {workspace}")

    _load_worker_env()
    # Imported for the TLS bootstrap side effect, exactly as worker.py does:
    # this host re-signs TLS locally, and google.auth ships its own CA bundle,
    # so without it the token request fails CERTIFICATE_VERIFY_FAILED.
    import settings  # noqa: F401
    import storage
    from library import VideoLibrary, VideoRecord

    checkpoint_path = workspace / "checkpoint.json"
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_path.exists() else {}
    )
    script_path = workspace / "script.json"
    script = (
        json.loads(script_path.read_text(encoding="utf-8"))
        if script_path.exists() else {}
    )

    channel = checkpoint.get("channel") or workspace.name.rsplit("_", 4)[0]
    video_id = args.video_id or checkpoint.get("run_id") or workspace.name
    # Workspace names carry dots and other characters the layout forbids.
    video_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in video_id)

    review_log = checkpoint.get("review_log") or {}
    flagged = [
        name for name, entry in review_log.items()
        if isinstance(entry, dict) and entry.get("flagged")
    ]
    review_status = "flagged: " + ", ".join(sorted(flagged)) if flagged else "approved"

    video = _find_video(workspace)
    thumbnail = workspace / "thumbnail.png"
    probe = _probe(video)

    created_at = checkpoint.get("started_at") or datetime.fromtimestamp(
        video.stat().st_mtime, tz=timezone.utc
    ).isoformat()

    record = VideoRecord(
        video_id=video_id,
        job_id=checkpoint.get("run_id") or video_id,
        channel_id=channel,
        title=script.get("title") or video.stem,
        description=script.get("description", ""),
        created_at=created_at,
        duration_seconds=probe["duration_seconds"],
        width=probe["width"],
        height=probe["height"],
        fps=probe["fps"],
        review_status=review_status,
        review_log=review_log,
    )

    logger.info(f"channel   : {record.channel_id}")
    logger.info(f"video id  : {record.video_id}")
    logger.info(f"title     : {record.title}")
    logger.info(f"video     : {video.name} ({video.stat().st_size / 1e6:.1f} MB)")
    logger.info(f"resolution: {record.resolution} @ {record.fps}fps, "
                f"{record.duration_seconds:.1f}s")
    logger.info(f"storage   : {storage.describe()}")

    library = VideoLibrary(storage)
    if not args.overwrite and library.is_registered(record.channel_id, record.video_id):
        logger.info("already in the library; nothing to do")
        return 0

    saved = library.register(
        record,
        video_file=video,
        thumbnail_file=thumbnail if thumbnail.exists() else None,
        overwrite=args.overwrite,
    )
    logger.info(f"video     -> {saved.video_url}")
    if saved.thumbnail_url:
        logger.info(f"thumbnail -> {saved.thumbnail_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
