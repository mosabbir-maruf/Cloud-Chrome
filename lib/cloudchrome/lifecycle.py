"""The desktop stack lifecycle.

This module owns the start and the stop of the desktop stack, and the health
checks that say whether a running stack is usable. The CLI and the backup
transaction both go through it, so that:

* there is exactly one implementation of the lifecycle, and
* a test can replace it with one that fails on demand, which is how the
  failure paths of the restore are exercised.

The lifecycle never takes the control lock. The caller owns it: a compound
operation holds the lock once, and the operations inside it use this module
directly. That is what keeps a nested lock acquisition, which Linux
``flock`` treats as a different file description, from failing.
"""

import os
import time

from . import keyring as keyring_policy
from . import logutil
from . import procs
from .errors import CloudChromeError, InvalidStateError


def supervisor_pid(layout):
    """The PID of the running supervisor, or None.

    The PID file records the process start time as well as the PID, so a
    recycled PID is never mistaken for the running supervisor.
    """
    record = procs.read_pid_file(layout.supervisor_pid_file)
    if record is None:
        return None
    if procs.pid_matches(record["pid"], record["start_time"]):
        return record["pid"]
    return None


class Lifecycle:
    """Start, stop and inspect the desktop stack."""

    def __init__(self, config, layout):
        self.config = config
        self.layout = layout

    # --- inspection ---------------------------------------------------------
    def supervisor_pid(self):
        """The PID of the running supervisor, or None."""
        return supervisor_pid(self.layout)

    def is_running(self):
        return self.supervisor_pid() is not None

    def state(self):
        """The supervisor's own state, or None when there is none."""
        import json

        try:
            with open(self.layout.supervisor_state_file, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    # --- lifecycle ----------------------------------------------------------
    def stop(self, timeout=None):
        """Stop the stack.

        Returns True when a stack was running and has been stopped, and False
        when there was nothing to stop. A stack that does not stop within the
        timeout is reported as a failure rather than left half up.
        """
        pid = self.supervisor_pid()
        if pid is None:
            procs.remove_pid_file(self.layout.supervisor_pid_file)
            return False
        record = procs.read_pid_file(self.layout.supervisor_pid_file) or {}
        logutil.append(
            self.layout.supervisor_log,
            "stop requested for supervisor pid %d" % pid,
            max_bytes=self.config.log_max_bytes,
            keep=self.config.log_keep,
        )
        timeout = timeout or max(self.config.stop_timeout, 1)
        procs.terminate(pid, record.get("start_time"), timeout=timeout)
        deadline = time.monotonic() + timeout + 5
        while time.monotonic() < deadline:
            if self.supervisor_pid() is None:
                break
            time.sleep(0.2)
        if self.supervisor_pid() is not None:
            raise CloudChromeError(
                "the desktop stack did not stop within %ds" % timeout
            )
        procs.remove_pid_file(self.layout.supervisor_pid_file)
        return True

    def start(self, if_needed=False, timeout=180):
        """Start the stack and wait until it reports itself ready.

        Raises :class:`CloudChromeError` when the stack does not come up.
        The error is never swallowed by the caller, so a failed restart is
        always visible in the exit code.
        """
        running = self.supervisor_pid()
        if running is not None:
            if if_needed:
                return
            raise CloudChromeError(
                "the desktop stack is already running (supervisor pid %d); "
                "use 'cloudchrome restart' to replace it" % running,
                code=8,
            )

        self.layout.validate_for_start()

        env = dict(os.environ)
        library = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = (
            library + os.pathsep + env["PYTHONPATH"]
            if env.get("PYTHONPATH")
            else library
        )
        env["CLOUDCHROME_HOME"] = os.path.dirname(library)

        log_path = os.path.join(self.layout.log_dir, "supervisor.log")
        try:
            os.makedirs(self.layout.log_dir, mode=0o700, exist_ok=True)
            handle = open(log_path, "ab", buffering=0)
        except OSError as exc:
            raise CloudChromeError("cannot open %s: %s" % (log_path, exc)) from exc

        # The state file of a previous stack still says "running". It is
        # removed first, so that the wait below cannot be satisfied by a
        # state that belongs to a stack that is no longer there.
        try:
            os.unlink(self.layout.supervisor_state_file)
        except OSError:
            pass

        try:
            import subprocess
            import sys

            process = subprocess.Popen(
                [sys.executable, "-m", "cloudchrome.supervisor"],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            handle.close()
            raise CloudChromeError("cannot start the supervisor: %s" % exc) from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state:
                # The state must belong to the supervisor that was just
                # started: a state left by an earlier stack says nothing
                # about this one.
                if state.get("supervisor_pid") != process.pid:
                    time.sleep(0.2)
                    continue
                if state.get("state") == "running":
                    return
                if state.get("state") == "failed":
                    raise CloudChromeError(
                        "the desktop stack failed to start: %s" % state.get("error")
                    )
            time.sleep(0.5)
        raise CloudChromeError("the supervisor did not report a ready stack in time")

    # --- health -------------------------------------------------------------
    def health(self):
        """Check that the running stack is usable.

        The returned dictionary has a ``healthy`` flag and a ``problems``
        list. The checks are the ones that decide whether a restored state
        can be used: the supervisor is running, Chrome is running against the
        persistent profile, and the secret service answers.
        """
        problems = []
        state = self.state()
        if not self.is_running():
            problems.append("the supervisor is not running")
        elif not state or state.get("state") != "running":
            problems.append(
                "the supervisor reports %s"
                % (state or {}).get("state", "an unknown state")
            )

        browser = find_browser_process(self.layout)
        if browser is None:
            problems.append("Chrome is not running against the persistent profile")
        else:
            _pid, command_line = browser
            if "--password-store=gnome-libsecret" not in command_line:
                problems.append("Chrome does not use the libsecret password store")

        if not self._secret_service_usable():
            problems.append("the secret service is not usable")

        return {"healthy": not problems, "problems": problems}

    def _secret_service_usable(self):
        """True when a secret can be stored and read back."""
        state = self.state() or {}
        if state.get("secret_service"):
            return True
        try:
            env = keyring_policy.dbus_environment(
                self.layout.dbus_socket,
                self.layout.xdg_runtime_dir,
                self.layout.xdg_data_home,
            )
            keyring_policy.verify_secret_service(env, timeout=15)
            return True
        except Exception:  # noqa: BLE001 - the check reports, it does not raise
            return False


def find_browser_process(layout):
    """Find the Chrome browser process and its command line.

    Chrome rewrites its argument vector into a single string, so the process
    is found by its profile directory and the checks are made against the
    joined command line.
    """
    marker = "--user-data-dir=%s" % layout.chrome_dir
    for entry in sorted(os.listdir("/proc")):
        if not entry.isdigit():
            continue
        pid = int(entry)
        argv = procs.read_cmdline(pid)
        if not argv:
            continue
        command_line = " ".join(argv)
        if marker not in command_line:
            continue
        if "--type=" in command_line:
            continue
        return pid, command_line
    return None
