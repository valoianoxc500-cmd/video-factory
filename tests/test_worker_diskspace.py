"""Disk reclamation keeps what matters and drops what does not.

The dangerous failure here is not "cleanup freed too little" -- that just
blocks a job with a clear message. It is "cleanup deleted a finished video",
which destroys work silently. So most of these assert what survives.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "worker"))

import diskspace  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A workspace tree that looks like several finished runs."""
    monkeypatch.setattr(diskspace, "REPO_ROOT", tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def make_run(name: str, *, with_video: bool = True, mtime: float | None = None):
        run = workspace / name
        (run / "sections").mkdir(parents=True)
        (run / "frames").mkdir(parents=True)
        (run / "sections" / "s1.mp4").write_bytes(b"x" * 5000)
        (run / "frames" / "f1.png").write_bytes(b"x" * 5000)
        (run / "script.json").write_text('{"title": "t"}', encoding="utf-8")
        if with_video:
            (run / "final.mp4").write_bytes(b"x" * 1000)
            (run / "thumbnail.png").write_bytes(b"x" * 500)
        if mtime is not None:
            import os

            os.utime(run, (mtime, mtime))
        return run

    return workspace, make_run


def test_pruning_keeps_the_newest_runs(fake_repo):
    workspace, make_run = fake_repo
    for i, name in enumerate(["run_a", "run_b", "run_c", "run_d", "run_e"]):
        make_run(name, mtime=1_700_000_000 + i * 1000)

    diskspace.prune_workspaces(keep=2)

    survivors = sorted(p.name for p in workspace.iterdir() if p.is_dir())
    assert survivors == ["run_d", "run_e"], "pruning kept the wrong runs"


def test_pruning_never_touches_the_running_job(fake_repo):
    workspace, make_run = fake_repo
    for i, name in enumerate(["old_1", "old_2", "old_3"]):
        make_run(name, mtime=1_700_000_000 + i)
    current = workspace / "old_1"  # oldest, so normally first to go

    diskspace.prune_workspaces(keep=1, exclude=current)

    assert current.exists(), "cleanup deleted the run that was in progress"


def test_keeping_zero_is_treated_as_a_mistake(fake_repo):
    """keep=0 would wipe every local run; refuse rather than obey."""
    workspace, make_run = fake_repo
    make_run("run_a")
    diskspace.prune_workspaces(keep=0)
    assert (workspace / "run_a").exists()


def test_stripping_intermediates_preserves_the_finished_video(fake_repo):
    workspace, make_run = fake_repo
    run = make_run("run_a")

    diskspace.strip_intermediates()

    assert (run / "final.mp4").exists(), "the finished video was deleted"
    assert (run / "thumbnail.png").exists(), "the thumbnail was deleted"
    assert (run / "script.json").exists(), "the metadata was deleted"
    assert not (run / "sections").exists(), "section renders were not reclaimed"
    assert not (run / "frames").exists(), "frame dumps were not reclaimed"


def test_stripping_skips_a_run_with_no_final_video_yet(fake_repo):
    """A run mid-render already has section MP4s but no final video.

    Regression: the finished-run check used rglob, so the first rendered
    section made an in-flight run look complete and its scratch was deleted
    while the render was still writing to it.
    """
    workspace, make_run = fake_repo
    run = make_run("in_flight", with_video=False)
    assert list(run.glob("sections/*.mp4")), "fixture should have section renders"

    diskspace.strip_intermediates()

    assert (run / "sections").exists(), "scratch of an unfinished run was deleted"
    assert (run / "frames").exists()


def test_stripping_skips_the_excluded_run(fake_repo):
    workspace, make_run = fake_repo
    run = make_run("current")

    diskspace.strip_intermediates(exclude=run)

    assert (run / "sections").exists()


def test_recovery_copies_are_dropped_only_when_old(fake_repo, tmp_path):
    import os
    import time

    recovery = tmp_path / "workspace_recovery"
    recovery.mkdir()
    fresh = recovery / "fresh_run"
    fresh.mkdir()
    (fresh / "video.mp4").write_bytes(b"x" * 100)
    stale = recovery / "stale_run"
    stale.mkdir()
    (stale / "video.mp4").write_bytes(b"x" * 100)
    old = time.time() - 10 * 86400
    os.utime(stale, (old, old))

    diskspace.prune_recovery(max_age_days=3)

    assert fresh.exists(), "a recent recovery copy was deleted"
    assert not stale.exists(), "an old recovery copy was kept"


def test_reclamation_reports_what_it_freed(fake_repo):
    workspace, make_run = fake_repo
    # Names must contain an underscore: run directories are <engine>_<stamp>,
    # and the sweep globs for that shape.
    for i, name in enumerate(["run_a", "run_b", "run_c"]):
        make_run(name, mtime=1_700_000_000 + i)

    result = diskspace.prune_workspaces(keep=1)

    assert result.freed_bytes > 0
    assert result.actions, "cleanup freed space but reported nothing"


def test_escalation_stops_once_there_is_room(fake_repo, monkeypatch):
    """A healthy worker must not reach the cache-clearing tier."""
    called: list[str] = []

    monkeypatch.setattr(diskspace, "free_gb", lambda *a, **k: 50.0)
    monkeypatch.setattr(
        diskspace, "clear_caches",
        lambda: called.append("caches") or diskspace.Reclamation(),
    )
    monkeypatch.setattr(
        diskspace, "clear_stale_temp",
        lambda *a, **k: called.append("temp") or diskspace.Reclamation(),
    )

    free, actions = diskspace.ensure_free(2.0, keep_workspaces=3)

    assert free == 50.0
    assert called == [], "cleanup escalated despite ample free space"
    assert actions == []


def test_escalation_reaches_later_tiers_when_still_short(fake_repo, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(diskspace, "free_gb", lambda *a, **k: 0.1)
    monkeypatch.setattr(
        diskspace, "clear_stale_temp",
        lambda *a, **k: called.append("temp") or diskspace.Reclamation(),
    )
    monkeypatch.setattr(
        diskspace, "clear_caches",
        lambda: called.append("caches") or diskspace.Reclamation(),
    )

    diskspace.ensure_free(2.0, keep_workspaces=3)

    assert called == ["temp", "caches"], "cleanup did not escalate when short"


def test_a_failing_tier_does_not_abort_the_rest(fake_repo, monkeypatch):
    """Cleanup must never be the reason a job fails."""
    called: list[str] = []
    monkeypatch.setattr(diskspace, "free_gb", lambda *a, **k: 0.1)

    def boom():
        raise OSError("permission denied")

    monkeypatch.setattr(diskspace, "prune_recovery", boom)
    monkeypatch.setattr(
        diskspace, "clear_caches",
        lambda: called.append("caches") or diskspace.Reclamation(),
    )

    free, _ = diskspace.ensure_free(2.0, keep_workspaces=3)

    assert called == ["caches"], "a failing tier stopped later tiers"
    assert isinstance(free, float)


def test_cache_cleaning_can_be_disabled(monkeypatch):
    monkeypatch.setattr(diskspace, "_CACHE_OPT_OUT", True)
    result = diskspace.clear_caches()
    assert result.freed_bytes == 0
    assert result.actions == []


def test_temp_cleanup_leaves_recent_entries(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("TEMP", str(tmp_path))
    recent = tmp_path / "recent.tmp"
    recent.write_bytes(b"x" * 100)
    old = tmp_path / "old.tmp"
    old.write_bytes(b"x" * 100)
    stamp = time.time() - 48 * 3600
    os.utime(old, (stamp, stamp))

    diskspace.clear_stale_temp(min_age_hours=24)

    assert recent.exists(), "temp cleanup deleted a file a process may still hold"
    assert not old.exists(), "temp cleanup kept a stale file"
