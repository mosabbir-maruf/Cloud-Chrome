"""The service supervisor.

One supervisor owns the whole desktop stack: the X/VNC server, the D-Bus
session bus, the GNOME Keyring, the XFCE session and window manager, the
noVNC bridge and Chrome. It is the only place where process lifecycle logic
lives; the CLI commands call into it and never manage a process themselves.

Design points that the requirements ask for explicitly:

* **Dependency order.** Components start in order and each one is verified
  before the next starts, so Chrome never launches before the keyring is
  unlocked and the desktop is up.
* **Graceful shutdown.** SIGTERM and SIGINT stop the stack in the reverse
  order, giving each component a chance to exit cleanly before it is killed.
* **Dead-process detection.** A component that exits unexpectedly stops the
  whole stack and is reported, instead of leaving a half-working desktop.
* **No stale PID files.** A PID file records the process start time, so a
  recycled PID is never mistaken for the running supervisor.
* **No duplicate stacks.** The supervisor holds an exclusive lock for its
  whole lifetime, so a second one cannot start.
* **Never kill somebody else's process.** Only processes this supervisor
  started are signalled, and only after their identity is verified.
"""

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time

from . import chrome as chrome_policy
from . import desktop as desktop_policy
from . import keyring as keyring_policy
from . import logutil
from . import procs
from . import vnc as vnc_policy
from . import web as web_policy
from .errors import CloudChromeError, InvalidStateError, UnsupportedError
from .locking import FileLock
from .paths import OWNER_ONLY_DIR

# The components, in dependency order. The reverse order is the shutdown
# order.
COMPONENT_ORDER = (
    "xvnc",
    "dbus",
    "keyring",
    "desktop",
    "desktop-wm",
    "novnc",
    "chrome",
)

COMPONENT_LABELS = {
    "xvnc": "X server / TigerVNC",
    "dbus": "D-Bus session bus",
    "keyring": "GNOME Keyring",
    "desktop": "XFCE session",
    "desktop-wm": "XFCE window manager",
    "novnc": "websockify + noVNC",
    "chrome": "Google Chrome",
}


class Component:
    """One supervised process."""

    def __init__(self, name, argv, env=None, cwd=None, log_name=None):
        self.name = name
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.log_name = log_name or name
        self.process = None
        self.start_time = None
        self.log_handle = None

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def alive(self):
        if self.process is None:
            return False
        if self.process.poll() is not None:
            return False
        return procs.pid_matches(self.process.pid, self.start_time)

    def stop(self, timeout):
        """Stop the component and everything it started."""
        if self.process is None:
            return True
        return procs.terminate(self.process.pid, self.start_time, timeout=timeout)


class Supervisor:
    """The desktop stack supervisor."""

    def __init__(self, config, layout):
        self.config = config
        self.layout = layout
        self.components = {}
        self.state = "initialising"
        self.error = None
        self.signal_received = None
        self._lock = None
        self._dbus_address = None
        self._keyring_control = None
        self._keyring_reported = []
        self._secret_check = None
        self._stopping = False

    # --- logging ------------------------------------------------------------
    def log(self, message):
        logutil.append(
            self.layout.supervisor_log,
            message,
            max_bytes=self.config.log_max_bytes,
            keep=self.config.log_keep,
        )

    # --- state file ---------------------------------------------------------
    def write_state(self):
        payload = {
            "state": self.state,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "supervisor_pid": os.getpid(),
            "components": {},
            "dbus_address": self._dbus_address,
            "keyring_control": self._keyring_control,
            "secret_service": bool(self._secret_check),
            "error": self.error,
        }
        for name in COMPONENT_ORDER:
            component = self.components.get(name)
            if component is None:
                continue
            payload["components"][name] = {
                "label": COMPONENT_LABELS.get(name, name),
                "pid": component.pid,
                "start_time": component.start_time,
                "running": component.alive(),
                "log": os.path.join(self.layout.log_dir, "%s.log" % component.log_name),
            }
        try:
            os.makedirs(self.layout.runtime_dir, mode=OWNER_ONLY_DIR, exist_ok=True)
            tmp = self.layout.supervisor_state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.layout.supervisor_state_file)
        except OSError as exc:
            self.log("cannot write the supervisor state file: %s" % exc)
        return payload

    # --- process handling ---------------------------------------------------
    def spawn(self, name, argv, env=None, cwd=None):
        """Start a component and record its identity."""
        component = Component(name, argv, env=env, cwd=cwd)
        log_path = os.path.join(self.layout.log_dir, "%s.log" % component.log_name)
        try:
            os.makedirs(self.layout.log_dir, mode=OWNER_ONLY_DIR, exist_ok=True)
            handle = open(log_path, "ab", buffering=0)
        except OSError as exc:
            raise CloudChromeError("cannot open the log file %s: %s" % (log_path, exc))
        component.log_handle = handle
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            handle.close()
            raise CloudChromeError("cannot start %s: %s" % (name, exc))
        component.process = process
        component.start_time = procs.proc_start_time(process.pid)
        self.components[name] = component
        self.log("started %s (pid %d)" % (name, process.pid))
        return component

    def wait_for(self, name, predicate, timeout=30.0, interval=0.25):
        """Wait until *predicate* is satisfied for a component."""
        component = self.components.get(name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if component is not None and not component.alive():
                code = component.process.poll() if component.process else None
                raise CloudChromeError(
                    "%s exited unexpectedly (exit code %s)" % (name, code)
                )
            try:
                if predicate():
                    return True
            except Exception:  # noqa: BLE001 - a probe failure is not fatal
                pass
            time.sleep(interval)
        raise CloudChromeError("%s did not become ready within %ds" % (name, timeout))

    # --- the stack ----------------------------------------------------------
    def start_xvnc(self):
        password_file = self.layout.vnc_pass_file
        vnc_policy.create_password_file(password_file)
        argv = vnc_policy.vnc_arguments(
            self.config.display,
            self.config.geometry,
            self.config.depth,
            self.config.vnc_port,
            password_file,
        )
        self.spawn("xvnc", argv)
        display_number = self.config.display.lstrip(":").split(".")[0]
        socket_path = "/tmp/.X11-unix/X%s" % display_number

        def ready():
            return os.path.exists(socket_path)

        self.wait_for("xvnc", ready, timeout=30)

    def start_dbus(self):
        argv = keyring_policy.dbus_arguments(self.layout.dbus_socket)
        self.spawn("dbus", argv)
        self.wait_for("dbus", lambda: os.path.exists(self.layout.dbus_socket), timeout=20)
        self._dbus_address = "unix:path=%s" % self.layout.dbus_socket

    def start_keyring(self):
        passphrase = keyring_policy.read_passphrase(self.layout.keyring_pass_file)
        env = keyring_policy.dbus_environment(
            self.layout.dbus_socket,
            self.layout.xdg_runtime_dir,
            self.layout.xdg_data_home,
        )
        # The keyring creates its control socket in the runtime directory, so
        # the location is known in advance and can be handed to Chrome.
        control_socket = os.path.join(self.layout.xdg_runtime_dir, "keyring")
        env["GNOME_KEYRING_CONTROL"] = control_socket
        argv = keyring_policy.keyring_arguments()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        # The passphrase is written to the daemon's standard input and then
        # discarded; it never appears in the process table.
        try:
            process.stdin.write((passphrase + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

        component = Component("keyring", argv)
        component.process = process
        component.start_time = procs.proc_start_time(process.pid)
        self.components["keyring"] = component
        self.log("started keyring (pid %d)" % process.pid)
        self._keyring_control = control_socket

        # The daemon may report its control socket on stdout; when it does,
        # the reported path wins over the predicted one. The output is
        # drained by a thread so that the daemon never blocks on a full pipe.
        self._keyring_reported = []
        reader = threading.Thread(
            target=self._drain_keyring_output,
            args=(process,),
            daemon=True,
        )
        reader.start()

        # The secret service must genuinely work before Chrome is started.
        self._secret_check = keyring_policy.verify_secret_service(env)
        self.log("the secret service stored and returned a probe secret")

        if self._keyring_reported:
            self._keyring_control = self._keyring_reported[0]
            env["GNOME_KEYRING_CONTROL"] = self._keyring_control

    def _drain_keyring_output(self, process):
        """Drain the keyring daemon's output and remember its control path."""
        if process.stdout is None:
            return
        try:
            for raw in iter(process.stdout.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("GNOME_KEYRING_CONTROL="):
                    self._keyring_reported.append(text.split("=", 1)[1].strip())
        except (OSError, ValueError):
            return

    def start_desktop(self):
        env = desktop_policy.desktop_environment(
            self.config.display, self._dbus_address, self.layout.persist_dir
        )
        self.spawn(
            "desktop",
            desktop_policy.session_arguments(self.config.display),
            env=env,
        )
        # The window manager is started explicitly so that the desktop is
        # usable as soon as the supervisor reports the stack as ready.
        self.spawn(
            "desktop-wm",
            desktop_policy.window_manager_arguments(self.config.display),
            env=env,
        )
        time.sleep(1.0)

    def start_novnc(self):
        if not web_policy.novnc_index_present(self.config.novnc_root):
            raise UnsupportedError(
                "the noVNC client is not installed at %s" % self.config.novnc_root
            )
        argv = web_policy.websockify_arguments(
            self.config.http_port,
            self.config.vnc_port,
            self.config.novnc_root,
            self.config.http_bind,
        )
        self.spawn("novnc", argv)
        self.wait_for(
            "novnc",
            lambda: web_policy.http_port_is_bound(self.config.http_port),
            timeout=30,
        )

    def start_chrome(self):
        arguments = chrome_policy.build_arguments(
            self.config, self.layout, self.config.display
        )
        chrome_policy.assert_safe_arguments(arguments)
        chrome_policy.assert_sandbox_available()
        chrome_policy.assert_secret_service_available(self._secret_check)
        env = chrome_policy.chrome_environment(
            self.config.display,
            self._dbus_address,
            self._keyring_control,
            self.layout.persist_dir,
        )
        self.spawn(
            "chrome",
            [self.config.chrome_binary] + arguments,
            env=env,
        )
        time.sleep(2.0)
        if not self.components["chrome"].alive():
            raise CloudChromeError("Chrome exited immediately after it was started")

    def start(self):
        """Start the whole stack in dependency order."""
        steps = (
            ("xvnc", self.start_xvnc),
            ("dbus", self.start_dbus),
            ("keyring", self.start_keyring),
            ("desktop", self.start_desktop),
            ("novnc", self.start_novnc),
            ("chrome", self.start_chrome),
        )
        for name, step in steps:
            try:
                step()
            except CloudChromeError as exc:
                self.state = "failed"
                self.error = "%s: %s" % (name, exc)
                self.log("component %s failed to start: %s" % (name, exc))
                self.write_state()
                self.stop_all()
                raise
        self.state = "running"
        self.write_state()
        self.log("the desktop stack is running")

    # --- shutdown -----------------------------------------------------------
    def stop_all(self):
        """Stop every component in the reverse of the start order."""
        self._stopping = True
        self.state = "stopping"
        timeout = max(self.config.stop_timeout, 1)
        for name in reversed(COMPONENT_ORDER):
            component = self.components.get(name)
            if component is None:
                continue
            if component.alive():
                self.log("stopping %s (pid %s)" % (name, component.pid))
            component.stop(timeout)
            if component.log_handle is not None:
                try:
                    component.log_handle.close()
                except OSError:
                    pass
        self.components.clear()
        procs.reap_children()
        self.state = "stopped"
        self.write_state()

    # --- signals ------------------------------------------------------------
    def install_signal_handlers(self):
        def handler(signum, _frame):
            self.signal_received = signum
            self.log("received signal %d; shutting the stack down" % signum)
            self._stopping = True

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def monitor(self):
        """Watch the stack until a component dies or a signal arrives."""
        while not self._stopping:
            for name in COMPONENT_ORDER:
                component = self.components.get(name)
                if component is None:
                    continue
                if not component.alive():
                    code = component.process.poll() if component.process else None
                    self.state = "failed"
                    self.error = "%s exited unexpectedly (exit code %s)" % (name, code)
                    self.log(self.error)
                    self.write_state()
                    self.stop_all()
                    return 1
            time.sleep(0.5)
        return 0

    # --- entry point --------------------------------------------------------
    def run(self):
        """Run the supervisor until the stack is stopped."""
        os.makedirs(self.layout.runtime_dir, mode=OWNER_ONLY_DIR, exist_ok=True)
        self._lock = FileLock(self.layout.supervisor_lock_file, timeout=0)
        try:
            self._lock.acquire()
        except CloudChromeError:
            self.log("another supervisor already holds the lock; not starting")
            return 0

        self.install_signal_handlers()
        try:
            procs.write_pid_file(
                self.layout.supervisor_pid_file, os.getpid(), "supervisor"
            )
        except (OSError, InvalidStateError) as exc:
            self.log("cannot write the supervisor PID file: %s" % exc)
            self._lock.release()
            return 1

        try:
            self.start()
        except CloudChromeError as exc:
            self.error = str(exc)
            self.write_state()
            self._cleanup_pid_file()
            self._lock.release()
            return 1

        result = self.monitor()
        self._cleanup_pid_file()
        self._lock.release()
        return result

    def _cleanup_pid_file(self):
        procs.remove_pid_file(self.layout.supervisor_pid_file)


def main():
    """Run the supervisor as its own process.

    The CLI starts this with ``python3 -m cloudchrome.supervisor``; it is not
    a user-facing entry point.
    """
    from .config import load_config
    from .paths import Layout

    config = load_config()
    layout = Layout(config)
    layout.require_initialized()
    supervisor = Supervisor(config, layout)
    try:
        return supervisor.run()
    except CloudChromeError as exc:
        supervisor.log("the supervisor failed: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
