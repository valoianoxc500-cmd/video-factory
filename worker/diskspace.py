"""Reclaiming disk space before a render, in order of increasing intrusiveness.

The worker used to prune only `workspace/`, which assumes the disk is full of
its own leftovers. That assumption can be wrong: the worker shares a machine,
and a render can be blocked with the workspace directory nearly empty. So
reclamation is tiered -- run the cheapest, safest step, check whether that was
enough, and only escalate if it was not.

What is never touched, at any tier:

  * anything already uploaded to object storage is irrelevant here, but the
    local copy of the newest runs is kept anyway for debugging;
  * `assets/`, `config/`, `data/`, `.git/`, the virtualenv, and Remotion's
    `node_modules` -- deleting those breaks the pipeline rather than freeing
    scratch space;
  * any file inside a run that is currently being rendered.

Every tier is best-effort: a file held open by another process is skipped, not
fatal. Reclaiming space must never be the thing that fails a job.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("worker.disk")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories whose contents are regenerable but which are NOT ours: package
# manager caches and the OS temp area. Cleared only as a last resort, and only
# when the worker would otherwise refuse to run at all.
_CACHE_OPT_OUT = os.environ.get("WORKER_CLEAN_CACHES", "1").lower() in ("0", "false", "no")

# Temp entries younger than this may belong to a running process.
_TEMP_MIN_AGE_HOURS = float(os.environ.get("WORKER_TEMP_MIN_AGE_HOURS", "24"))

# Recovery copies of old runs are a manual debugging aid; they are not the
# durable copy of anything.
_RECOVERY_MAX_AGE_DAYS = float(os.environ.get("WORKER_RECOVERY_MAX_AGE_DAYS", "3"))


@dataclass
class Reclamation:
    """What a cleanup pass managed to free."""

    freed_bytes: int = 0
    actions: list[str] = field(default_factory=list)

    @property
    def freed_gb(self) -> float:
        return self.freed_bytes / 1e9

    def record(self, label: str, size: int) -> None:
        if size <= 0:
            return
        self.freed_bytes += size
        self.actions.append(f"{label} ({size / 1e6:.0f} MB)")
        logger.info(f"reclaimed {label}: {size / 1e6:.0f} MB")


def free_gb(path: Path | None = None) -> float:
    return shutil.disk_usage(path or REPO_ROOT).free / 1e9


def _size_of(path: Path) -> int:
    """Bytes under `path`, ignoring anything that cannot be read."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _remove(path: Path) -> int:
    """Delete a file or tree. Returns bytes freed; never raises."""
    try:
        size = _size_of(path)
    except OSError:
        return 0
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(f"could not remove {path}: {exc}")
        return 0
    # rmtree(ignore_errors) can leave a partial tree behind; report what went.
    return size if not path.exists() else max(0, size - _size_of(path))


# ── tier 1: our own finished runs ─────────────────────────────────

def prune_workspaces(keep: int, exclude: Path | None = None) -> Reclamation:
    """Delete all but the newest `keep` run directories."""
    out = Reclamation()
    root = REPO_ROOT / "workspace"
    if keep < 1 or not root.exists():
        return out

    runs = sorted(
        (p for p in root.glob("*_*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in runs[keep:]:
        if exclude and stale == exclude:
            continue  # the run happening right now
        out.record(f"workspace {stale.name}", _remove(stale))
    return out


def prune_recovery(max_age_days: float = _RECOVERY_MAX_AGE_DAYS) -> Reclamation:
    """Drop recovery copies older than `max_age_days`.

    `workspace_recovery/` holds hand-copied runs kept while debugging a past
    failure. Nothing reads it automatically, and the finished video of any run
    worth keeping is already in object storage.
    """
    out = Reclamation()
    root = REPO_ROOT / "workspace_recovery"
    if not root.exists():
        return out

    cutoff = time.time() - max_age_days * 86400
    for entry in root.iterdir():
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        out.record(f"recovery {entry.name}", _remove(entry))
    return out


# ── tier 2: intermediates inside runs we are keeping ──────────────

# Scratch produced during a render. The finished video, thumbnail, script and
# metadata live alongside these and are deliberately absent from this list.
_INTERMEDIATE_DIRS = (
    "sections",       # per-section MP4s, concatenated into the final video
    "frames",         # Remotion frame dumps
    "tmp",
    "temp",
    "_ffmpeg",
    "audio_chunks",
)


def strip_intermediates(exclude: Path | None = None) -> Reclamation:
    """Remove render scratch from completed runs, keeping their output.

    A finished run needs its MP4, thumbnail and JSON to stay inspectable; it
    does not need the per-section renders that were concatenated into that MP4.
    Those are the bulk of a run's footprint.
    """
    out = Reclamation()
    root = REPO_ROOT / "workspace"
    if not root.exists():
        return out

    for run in root.glob("*_*"):
        if not run.is_dir() or (exclude and run == exclude):
            continue
        # Only strip runs that actually produced a video -- an incomplete run
        # may still be mid-flight, and its scratch is not scratch yet.
        #
        # Deliberately a top-level glob, not rglob: the finished video sits at
        # the run root, while `sections/` fills with per-section MP4s as the
        # render proceeds. Matching recursively would read a run that had
        # rendered its first section as finished and delete the scratch out
        # from under the render still using it.
        if not any(run.glob("*.mp4")):
            continue
        for name in _INTERMEDIATE_DIRS:
            target = run / name
            if target.is_dir():
                out.record(f"{run.name}/{name}", _remove(target))
    return out


# ── tier 3: caches that are not ours ──────────────────────────────

def _cache_dirs() -> list[tuple[str, Path]]:
    local = os.environ.get("LOCALAPPDATA")
    home = Path.home()
    candidates: list[tuple[str, Path | None]] = [
        ("pip cache", Path(local) / "pip" / "Cache" if local else home / ".cache" / "pip"),
        ("npm cache", Path(local) / "npm-cache" if local else home / ".npm"),
    ]
    return [(label, path) for label, path in candidates if path and path.is_dir()]


def clear_caches() -> Reclamation:
    """Empty package-manager caches.

    These belong to pip and npm, not to this project, which is why they are the
    last resort rather than routine housekeeping. They hold downloaded archives
    that are re-fetched on demand, so clearing them costs bandwidth on the next
    install and nothing else. Set WORKER_CLEAN_CACHES=0 to disable.
    """
    out = Reclamation()
    if _CACHE_OPT_OUT:
        logger.info("cache cleaning disabled by WORKER_CLEAN_CACHES=0")
        return out

    for label, path in _cache_dirs():
        freed = 0
        for entry in path.iterdir():
            freed += _remove(entry)
        out.record(label, freed)
    return out


def clear_stale_temp(min_age_hours: float = _TEMP_MIN_AGE_HOURS) -> Reclamation:
    """Delete OS temp entries older than `min_age_hours`.

    ffmpeg and Remotion both stage work in the system temp directory and do not
    always clean up after a crash. The age floor is what keeps this from
    deleting scratch belonging to a process that is still running -- including
    this one.
    """
    out = Reclamation()
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp")
    if not temp.is_dir():
        return out

    cutoff = time.time() - min_age_hours * 3600
    freed = 0
    for entry in temp.iterdir():
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        freed += _remove(entry)
    out.record(f"stale temp (>{min_age_hours:.0f}h)", freed)
    return out


# ── the escalating pass ───────────────────────────────────────────

def ensure_free(
    target_gb: float,
    *,
    keep_workspaces: int,
    exclude: Path | None = None,
) -> tuple[float, list[str]]:
    """Reclaim until `target_gb` is free, or until nothing is left to reclaim.

    Returns the free space afterwards and what was done. Tiers stop as soon as
    the target is met, so a healthy worker only ever runs the first one.
    """
    actions: list[str] = []

    tiers = (
        ("workspaces", lambda: prune_workspaces(keep_workspaces, exclude=exclude)),
        ("recovery copies", prune_recovery),
        ("render intermediates", lambda: strip_intermediates(exclude=exclude)),
        ("stale temp files", clear_stale_temp),
        ("package caches", clear_caches),
    )

    for label, run_tier in tiers:
        current = free_gb()
        if current >= target_gb:
            return current, actions
        logger.info(
            f"{current:.1f} GB free, need {target_gb:.1f} GB -- reclaiming {label}"
        )
        try:
            result = run_tier()
        except Exception as exc:  # never let cleanup fail a job
            logger.warning(f"cleanup tier {label!r} failed: {exc}")
            continue
        actions.extend(result.actions)

    return free_gb(), actions
