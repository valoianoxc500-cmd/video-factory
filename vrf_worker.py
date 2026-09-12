"""Entry point for the Viral Reels Finder worker.

Run alongside the video worker, not instead of it -- they poll different
queues and share nothing:

    python vrf_worker.py            # poll continuously
    python vrf_worker.py --once     # one pass, for a smoke test

Environment it needs:
    WORKER_API_BASE   the deployment's base URL
    WORKER_TOKEN      the shared worker token
    VRF_TOKEN_KEY     the AES key that reads stored OAuth tokens
    YOUTUBE_API_KEY   discovery (optional; without it discovery reports why)
"""

# Imported for the side effect, before anything opens a connection: it loads
# .env and installs the OS trust store for httpx and gRPC. Without it every
# outbound HTTPS call fails with CERTIFICATE_VERIFY_FAILED wherever TLS is
# intercepted, which is how this worker failed silently -- it could not reach
# the deployment to claim a task, so searches queued forever.
import settings  # noqa: F401

import logging
from pathlib import Path

from viral.runner import main

# Imported as `worker.singleton`, NOT by putting `worker/` on sys.path.
#
# That path insertion used to sit here, and it silently broke every clipping
# job: `worker/` has no `__init__.py`, so with the directory itself first on
# sys.path `import worker` resolved to `worker/worker.py` -- a module -- and
# shadowed the namespace package. `viral/runner.py` then failed on
# `from worker.storage import upload_media` with "'worker' is not a package",
# but only at the very end of a run, after the clip had been downloaded,
# reframed and encoded. The work was done and then thrown away.
try:
    from worker.singleton import (
        RESTART_GRACE_SECONDS,
        AlreadyRunningError,
        SingleInstanceLock,
    )
except ImportError:  # pragma: no cover - depends on sys.path ordering
    # Something ahead of us has already put `worker/` on sys.path -- several
    # test modules and worker/run_worker.py do -- which makes `import worker`
    # resolve to worker/worker.py and shadows the package. Importing the
    # module directly is correct in exactly that situation, and the package
    # import above stays the path production actually takes.
    from singleton import (
        RESTART_GRACE_SECONDS,
        AlreadyRunningError,
        SingleInstanceLock,
    )

# The worker credentials live in worker/.env, which the video worker already
# loads through its own launcher. Reading the same file here means both workers
# are configured in one place, rather than this one depending on environment
# variables an operator has to remember to export -- forgetting them was
# indistinguishable from the worker not running at all.
_WORKER_ENV = Path(__file__).resolve().parent / "worker" / ".env"

# WORKER_API_BASE is this worker's name for what worker/.env calls APP_URL.
_ALIASES = {"APP_URL": "WORKER_API_BASE"}


def _load_worker_env(path: Path = _WORKER_ENV) -> int:
    """Fill in anything the real environment has not already set."""
    import os

    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        for name in {key, _ALIASES.get(key, key)}:
            # A real environment variable always wins, so a container or a
            # one-off run can still override the file.
            if name and name not in os.environ:
                os.environ[name] = value
                loaded += 1
    return loaded


def vrf_lock() -> SingleInstanceLock:
    """The lock that keeps this checkout to one Viral Reels Finder worker.

    Its own lock, not the video worker's: the two poll different queues and
    are meant to run side by side. What must not happen is two of *this* one,
    which is what was running here -- both claiming from the same task queue,
    which has no compare-and-set, so both could take the same task and publish
    it twice.
    """
    return SingleInstanceLock(
        Path(__file__).resolve().parent / "workspace" / ".vrf_worker.lock",
        name="Viral Reels Finder worker",
    )


if __name__ == "__main__":
    _load_worker_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        lock = vrf_lock().acquire(RESTART_GRACE_SECONDS)
    except AlreadyRunningError as exc:
        logging.getLogger("vrf_worker").error(str(exc))
        raise SystemExit(2)

    try:
        code = main()
    finally:
        lock.release()
    raise SystemExit(code)
