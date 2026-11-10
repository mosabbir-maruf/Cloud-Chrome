"""Advisory file locks.

Two locks are used:

* the *control* lock serialises the mutating CLI commands (``start``,
  ``stop``, ``restart``, ``backup``, ``restore``, ``prune``) so that two of
  them never run at the same time;
* the *supervisor* lock is held by the running supervisor for its whole
  lifetime, which is what makes a second supervisor impossible.

The locks are advisory ``flock`` locks on a file. A lock is released when the
process holding it exits, so a crashed command cannot wedge the runtime.
"""

import errno
import fcntl
import os
import time

from .errors import BusyError


class FileLock:
    """An exclusive advisory lock on a file."""

    def __init__(self, path, timeout=0.0, poll=0.2):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fd = None

    def acquire(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise BusyError(
                        "another Cloud-Chrome operation is already running; "
                        "wait for it to finish and try again"
                    )
                time.sleep(self.poll)

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def is_locked(path):
    """True when another process holds the lock on *path*."""
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
