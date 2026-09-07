"""Entry point that loads worker/.env, then starts the worker loop.

Keeps worker credentials out of the shell history and out of the repo:
worker/.env is gitignored.
"""

import os
import sys
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path) -> int:
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment always wins, so a container can override the file.
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


if __name__ == "__main__":
    count = load_env_file(WORKER_DIR / ".env")
    if count:
        print(f"loaded {count} value(s) from worker/.env")
    sys.path.insert(0, str(WORKER_DIR))
    from worker import main

    sys.exit(main())
