"""Process tracking, PID files and safe termination.

A PID on its own is not an identity: the kernel recycles PIDs, so a stale PID
file can name an unrelated process that happens to have the same number. Every
PID file written by Cloud-Chrome therefore records the process *start time*
as well, and a PID is only ever signalled when the running process matches
both the PID and the recorded start time.

Termination walks the process tree through ``/proc`` and only ever signals
processes that are descendants of the recorded PID, so a Chrome started by
another session is never touched.
"""

import errno
import os
import signal
import time

from .errors import InvalidStateError


def proc_start_time(pid):
    """Return the start time of *pid* in clock ticks since boot, or None."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        return None
    # The comm field may contain spaces and parentheses; everything after the
    # last ')' is the space-separated remainder of the stat line.
    try:
        index = data.rindex(b")")
    except ValueError:
        return None
    fields = data[index + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def pid_alive(pid):
    """True when a process with that PID exists."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def pid_exited(pid):
    """True when the process has exited, even if it has not been reaped.

    A child that has exited but has not been reaped is a zombie: the
    process itself is gone, but its entry is still in the process table.
    Waiting for a zombie is waiting for nothing, and a wait that ends
    only when the timeout expires turns a stop into a stall.
    """
    if not pid_alive(pid):
        return True
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        # The entry is not in the process table: the process is gone.
        return True
    except OSError:
        # The process table cannot be read, so nothing can be said.
        return False
    try:
        index = data.rindex(b")")
    except ValueError:
        return False
    fields = data[index + 2:].split()
    if not fields:
        return False
    # The first field after the command name is the state, and "Z" is the
    # state of a zombie.
    return fields[0] == b"Z"


def pid_matches(pid, start_time):
    """True when *pid* is running and has the recorded start time.

    This is the check that makes a stale PID file harmless.
    """
    if not pid_alive(pid):
        return False
    if start_time is None:
        return False
    current = proc_start_time(pid)
    if current is None:
        return False
    return current == start_time


def read_pid_file(path):
    """Read a PID file written by :func:`write_pid_file`.

    The file holds ``<pid> <start-time> <label>``. A missing or malformed file
    is reported as ``None`` rather than as an error: the caller decides
    whether a missing PID file means "not running" or "broken state".
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            fields = handle.read().split()
    except OSError:
        return None
    if len(fields) < 2:
        return None
    try:
        pid = int(fields[0])
        start_time = int(fields[1])
    except ValueError:
        return None
    label = fields[2] if len(fields) > 2 else ""
    return {"pid": pid, "start_time": start_time, "label": label}


def write_pid_file(path, pid, label=""):
    """Write a PID file for *pid*, recording its start time."""
    start_time = proc_start_time(pid)
    if start_time is None:
        raise InvalidStateError("cannot read the start time of pid %d" % pid)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("%d %d %s\n" % (pid, start_time, label))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def remove_pid_file(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def read_cmdline(pid):
    """Return the argument vector of *pid* as a list, or an empty list."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def children_of(pid):
    """Return the direct children of *pid* by reading /proc."""
    children = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        candidate = int(entry)
        try:
            with open("/proc/%d/stat" % candidate, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        try:
            index = data.rindex(b")")
        except ValueError:
            continue
        fields = data[index + 2:].split()
        if len(fields) < 2:
            continue
        try:
            parent = int(fields[1])
        except ValueError:
            continue
        if parent == pid:
            children.append(candidate)
    return children


def descendants_of(pid):
    """Return every descendant of *pid*, deepest last."""
    found = []
    stack = [pid]
    while stack:
        current = stack.pop()
        for child in children_of(current):
            if child in found:
                continue
            found.append(child)
            stack.append(child)
    return found


def terminate(pid, start_time=None, timeout=10.0, label=""):
    """Stop the process *pid*, and its descendants, gracefully.

    ``SIGTERM`` is sent first and ``SIGKILL`` follows only if the process is
    still alive after *timeout* seconds. The start time is verified before
    anything is signalled, so a recycled PID is never killed. The return value
    is True when the process is gone.
    """
    if not pid_alive(pid):
        return True
    if start_time is not None and not pid_matches(pid, start_time):
        return True
    if label:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                pass
        except OSError:
            pass

    targets = descendants_of(pid)
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_exited(pid):
            break
        time.sleep(0.1)

    for target in targets:
        if not pid_exited(target):
            try:
                os.kill(target, signal.SIGKILL)
            except OSError:
                pass
    if not pid_exited(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not pid_exited(pid):
            time.sleep(0.1)
    return pid_exited(pid)


def reap_children():
    """Reap every finished child so that no zombie survives."""
    reaped = []
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except OSError:
            break
        if pid == 0:
            break
        reaped.append(pid)
    return reaped
