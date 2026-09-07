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

from pathlib import Path

from viral.runner import main

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


if __name__ == "__main__":
    _load_worker_env()
    raise SystemExit(main())
