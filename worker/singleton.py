"""One instance per worker, enforced by the operating system.

Both workers used to guard themselves with a PID file: read it, check whether
that process is alive, then write your own PID. Two workers launched in the
same second both read a file nobody held, both decided they were clear, and
both wrote -- the second overwriting the first. That is exactly how this
checkout ended up running two `run_worker.py` processes, with the lock file
naming the one that started *second*.

Check-then-write cannot be fixed by checking harder; the gap between the check
and the write is the bug. So the lock is taken by the kernel instead:

  * Windows uses `msvcrt.locking` (LockFile under the hood)
  * everything else uses `fcntl.flock`

Both are atomic against every other process on the machine, and both are
released by the OS when the holder exits -- crash, kill, or power loss
included. There is no stale lock to reclaim and nothing to clean up after a
reboot, which is what the PID file needed a liveness probe for.

The holder's PID is written into the file for humans reading a log. It is
never used to decide whether the lock is held: the kernel already knows. It
lives at byte 0 while the lock is taken on a byte far past the end of the
text, because a Windows lock is mandatory -- a second process must still be
able to read the PID in order to name it in an error message.

Do not reintroduce a PID liveness probe. Besides being the racy half of the
old design, the obvious way to write one is a trap: `os.kill(pid, 0)` is the
POSIX idiom for "does this process exist", but on Windows CPython maps any
non-console signal to TerminateProcess -- so the probe kills the process it
was asked about, and raises SystemError on a stale PID. That cost this worker
a boot failure once already. The kernel lock needs no probe at all.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# How long a starting worker waits for a lock before refusing. Covers the gap
# between a stopped worker's process object reporting exit and Windows
# actually dropping its file locks, so `stop; start` works.
RESTART_GRACE_SECONDS = 5.0

# Locking a byte this far out keeps the mandatory Windows lock clear of the
# PID text at the start of the file, so a process that loses the race can
# still read who won.
_LOCK_BYTE_OFFSET = 1 << 20


class AlreadyRunningError(RuntimeError):
    """Another process already owns this worker's lock."""


def _try_lock(fd: int) -> bool:
    """Take an exclusive, non-blocking lock on one byte. False if held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _holder_pid(path: Path) -> str:
    """Whoever wrote the file last, for the error message. Best effort."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    pid = text.split("\x00", 1)[0].strip()
    return pid or "unknown"


class SingleInstanceLock:
    """Refuse to start when another instance of this worker is running.

    Used as a context manager. Acquiring is atomic, so two processes racing
    from the same launcher cannot both win:

        with SingleInstanceLock(path, name="video worker"):
            ...

    Raises AlreadyRunningError if the lock is held. The message names the
    lock file and the PID that wrote it, because the operator's next question
    is always "held by what?".
    """

    def __init__(self, path: Path, *, name: str = "worker") -> None:
        self.path = Path(path)
        self.name = name
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """Whether this object currently owns the lock."""
        return self._fd is not None

    def acquire(self, wait_seconds: float = 0.0) -> "SingleInstanceLock":
        """Take the lock, or raise AlreadyRunningError.

        `wait_seconds` retries briefly before giving up. Windows drops a dead
        process's file locks a moment *after* the process object reports it
        has exited, so restarting a worker straight after stopping the old one
        can otherwise meet a lock nobody holds. Retrying an atomic lock is
        still atomic -- this widens the window for a legitimate restart, not
        for a second worker.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(wait_seconds, 0.0)

        while True:
            # Opened without truncating: truncating first would blank the PID
            # of a live holder before we know whether we can have the lock.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                if _try_lock(fd):
                    break
            except Exception:
                os.close(fd)
                raise
            os.close(fd)

            if time.monotonic() >= deadline:
                holder = _holder_pid(self.path)
                raise AlreadyRunningError(
                    f"another {self.name} is already running "
                    f"(pid {holder}, lock: {self.path}). "
                    f"Only one may run against this checkout: two share the "
                    f"same workspace directory and the same free disk. Stop "
                    f"the running one before starting another."
                )
            time.sleep(0.1)

        # The lock is ours; record who holds it. Padded with NULs so a longer
        # previous PID cannot leave a stale digit behind.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}".encode("utf-8").ljust(32, b"\x00"))
        except OSError:
            pass  # diagnostics only -- never a reason to refuse to start

        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        # The file is deliberately left behind. Deleting it races with the
        # next process opening it, and an empty lock file costs nothing.

    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
