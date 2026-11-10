"""The command-line interface.

``bin/cloudchrome`` is the only entry point; every command below is a
sub-command of it. The commands never manage a process themselves: they call
the supervisor, which owns the desktop stack's lifecycle.

Exit codes are documented in :mod:`cloudchrome.errors` and in docs/USAGE.md.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from . import chrome as chrome_policy
from . import keyring as keyring_policy
from . import logutil
from . import procs
from . import vnc as vnc_policy
from . import web as web_policy
from .config import load_config
from .errors import (
    EXIT_BUSY,
    EXIT_FAILURE,
    EXIT_INVALID_STATE,
    EXIT_NOT_INITIALIZED,
    EXIT_NOT_RUNNING,
    EXIT_OK,
    EXIT_UNSUPPORTED,
    EXIT_USAGE,
    CloudChromeError,
    NotInitializedError,
    NotRunningError,
    UsageError,
)
from .locking import FileLock, is_locked
from .paths import Layout, OWNER_ONLY_DIR, assert_safe_file, ensure_dir
from .version import __version__

PROGRAM = "cloudchrome"

DESCRIPTION = """\
Cloud-Chrome — a remote Chrome desktop for GitHub Codespaces.

One command starts the desktop; the browser reaches it through the forwarded
noVNC port. The VNC password is the only secret this tool ever prints, and
only when the terminal is interactive.
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _stdout_is_tty():
    return sys.stdout.isatty()


def _layout(config):
    return Layout(config)


def _print(message=""):
    sys.stdout.write("%s\n" % message)


def _fail(message):
    sys.stderr.write("%s: %s\n" % (PROGRAM, message))


def _supervisor_running(layout):
    """Return the supervisor's PID when it is running, else None."""
    record = procs.read_pid_file(layout.supervisor_pid_file)
    if record is None:
        return None
    if procs.pid_matches(record["pid"], record["start_time"]):
        return record["pid"]
    return None


def _read_state(layout):
    try:
        with open(layout.supervisor_state_file, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _library_path():
    """The directory holding the cloudchrome package."""
    home = os.environ.get("CLOUDCHROME_HOME")
    if home:
        return os.path.join(home, "lib")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
def command_init(args, config):
    """Create the persistent state. Silent, non-interactive and idempotent."""
    layout = _layout(config)
    layout.initialize()

    # The keyring passphrase is generated once and never overwritten, so a
    # rebuild keeps the existing keyring usable.
    keyring_policy.create_passphrase_file(layout.keyring_pass_file)
    vnc_policy.create_password_file(layout.vnc_pass_file)

    os.makedirs(layout.log_dir, mode=OWNER_ONLY_DIR, exist_ok=True)
    logutil.append(
        layout.supervisor_log,
        "state initialised at %s" % layout.persist_dir,
        max_bytes=config.log_max_bytes,
        keep=config.log_keep,
    )
    if args.verbose:
        _print("initialised %s" % layout.persist_dir)
    return EXIT_OK


# ---------------------------------------------------------------------------
# start / stop / restart
# ---------------------------------------------------------------------------
def _start_supervisor(config, layout, if_needed=False):
    """Start the supervisor as a detached process and wait for it."""
    running = _supervisor_running(layout)
    if running is not None:
        if if_needed:
            _print("the desktop stack is already running (supervisor pid %d)" % running)
            return EXIT_OK
        raise CloudChromeError(
            "the desktop stack is already running (supervisor pid %d); "
            "use 'cloudchrome restart' to replace it" % running,
            code=EXIT_BUSY,
        )

    env = dict(os.environ)
    lib_path = _library_path()
    env["PYTHONPATH"] = (
        lib_path + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else lib_path
    )
    env["CLOUDCHROME_HOME"] = os.path.dirname(os.path.abspath(lib_path))

    log_path = os.path.join(layout.log_dir, "supervisor.log")
    try:
        os.makedirs(layout.log_dir, mode=OWNER_ONLY_DIR, exist_ok=True)
        handle = open(log_path, "ab", buffering=0)
    except OSError as exc:
        raise CloudChromeError("cannot open %s: %s" % (log_path, exc))

    try:
        subprocess.Popen(
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
        raise CloudChromeError("cannot start the supervisor: %s" % exc)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = _read_state(layout)
        if state:
            if state.get("state") == "running":
                _print("the desktop stack is running")
                return EXIT_OK
            if state.get("state") == "failed":
                raise CloudChromeError(
                    "the desktop stack failed to start: %s" % state.get("error")
                )
        time.sleep(0.5)
    raise CloudChromeError("the supervisor did not report a ready stack in time")


def command_start(args, config):
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=30):
        return _start_supervisor(config, layout, if_needed=args.if_needed)


def command_stop(args, config):
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=30):
        pid = _supervisor_running(layout)
        if pid is None:
            _print("the desktop stack is not running")
            return EXIT_OK
        record = procs.read_pid_file(layout.supervisor_pid_file) or {}
        logutil.append(
            layout.supervisor_log,
            "stop requested for supervisor pid %d" % pid,
            max_bytes=config.log_max_bytes,
            keep=config.log_keep,
        )
        procs.terminate(
            pid, record.get("start_time"), timeout=max(config.stop_timeout, 1)
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _supervisor_running(layout) is None:
                break
            time.sleep(0.2)
        procs.remove_pid_file(layout.supervisor_pid_file)
        _print("the desktop stack is stopped")
    return EXIT_OK


def command_restart(args, config):
    layout = _layout(config)
    layout.require_initialized()
    command_stop(args, config)
    with FileLock(layout.control_lock_file, timeout=30):
        return _start_supervisor(config, layout)

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def command_status(args, config):
    layout = _layout(config)
    if not os.path.isdir(layout.persist_dir):
        _print("state:      not initialised (%s)" % layout.persist_dir)
        return EXIT_NOT_INITIALIZED

    pid = _supervisor_running(layout)
    state = _read_state(layout)
    _print("version:    %s" % __version__)
    _print("state dir:  %s" % layout.persist_dir)
    if pid is None:
        _print("supervisor: not running")
    else:
        _print("supervisor: running (pid %d)" % pid)

    if state:
        _print("stack:      %s" % state.get("state", "unknown"))
        for name in sorted(state.get("components", {})):
            entry = state["components"][name]
            _print(
                "  %-12s %-8s pid=%s"
                % (name, "running" if entry.get("running") else "stopped", entry.get("pid"))
            )
        if state.get("error"):
            _print("error:      %s" % state["error"])
    elif pid is None:
        _print("stack:      unknown (no supervisor state file)")

    _print("noVNC port: %d%s" % (
        config.http_port,
        " (bound)" if web_policy.http_port_is_bound(config.http_port) else " (not bound)",
    ))
    rfb = web_policy.check_rfb_is_loopback_only(config.vnc_port)
    if rfb["listening"]:
        _print(
            "RFB port:   %d (loopback only)" % config.vnc_port
            if not rfb["non_loopback"]
            else "RFB port:   %d (EXPOSED on a non-loopback address)" % config.vnc_port
        )
    else:
        _print("RFB port:   %d (not listening)" % config.vnc_port)
    return EXIT_OK


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------
def command_logs(args, config):
    layout = _layout(config)
    if not os.path.isdir(layout.log_dir):
        _print("no logs yet")
        return EXIT_OK
    names = sorted(
        name for name in os.listdir(layout.log_dir) if name.endswith(".log")
    )
    if args.component:
        if args.component not in names:
            raise UsageError("no log named %s" % args.component)
        names = [args.component]
    for name in names:
        _print("===== %s =====" % name)
        for line in logutil.read_tail(
            os.path.join(layout.log_dir, name), args.lines
        ):
            _print(line)
    return EXIT_OK


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def command_doctor(args, config):
    """Diagnostics. Never prints a secret."""
    layout = _layout(config)
    problems = []

    def check(name, ok, detail=""):
        _print("%-42s %s%s" % (name, "ok" if ok else "FAIL", "  " + detail if detail else ""))
        if not ok:
            problems.append(name)
        return ok

    _print("Cloud-Chrome %s — diagnostics" % __version__)
    _print("")

    # --- identity and privileges -------------------------------------------
    check("runs as an unprivileged user", os.getuid() != 0, "uid=%d" % os.getuid())
    sudo = shutil.which("sudo")
    if sudo:
        result = subprocess.run(
            [sudo, "-n", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        check("no blanket sudo rule", b"ALL" not in result.stdout)

    # --- persistent state ---------------------------------------------------
    if os.path.isdir(layout.persist_dir):
        info = os.stat(layout.persist_dir)
        check(
            "state directory is private",
            not info.st_mode & 0o077,
            "mode=%o" % (info.st_mode & 0o777),
        )
    else:
        check("state directory exists", False, layout.persist_dir)

    for path, label in (
        (layout.keyring_pass_file, "keyring passphrase file"),
        (layout.vnc_pass_file, "VNC password file"),
    ):
        if os.path.exists(path):
            info = os.stat(path)
            check(
                "%s is private" % label,
                info.st_mode & 0o077 == 0 and info.st_size > 0,
                "mode=%o size=%d" % (info.st_mode & 0o777, info.st_size),
            )
        else:
            check("%s exists" % label, False)

    if os.path.exists(layout.vnc_pass_file):
        status = vnc_policy.password_file_status(layout.vnc_pass_file)
        check(
            "VNC password file is a valid 8-byte key",
            status.get("valid"),
            "size=%s" % status.get("size"),
        )

    # --- the stack ----------------------------------------------------------
    pid = _supervisor_running(layout)
    check("supervisor is running", pid is not None, "pid=%s" % pid if pid else "")

    rfb = web_policy.check_rfb_is_loopback_only(config.vnc_port)
    check(
        "RFB port %d is loopback only" % config.vnc_port,
        not rfb.get("non_loopback"),
        "addresses=%s" % ",".join(rfb.get("addresses", [])) if rfb.get("listening") else "not listening",
    )
    check(
        "noVNC port %d is bound" % config.http_port,
        web_policy.http_port_is_bound(config.http_port),
    )

    if pid is not None:
        state = _read_state(layout) or {}
        check(
            "the secret service is usable",
            bool(state.get("secret_service")),
            "",
        )
        components = state.get("components", {})
        check(
            "Chrome is running",
            bool(components.get("chrome", {}).get("running")),
        )
        chrome_pid = components.get("chrome", {}).get("pid")
        if chrome_pid:
            argv = procs.read_cmdline(chrome_pid)
            forbidden = [
                argument
                for argument in argv
                if argument.split("=", 1)[0] in chrome_policy.FORBIDDEN_FLAGS
            ]
            check("Chrome runs without forbidden flags", not forbidden, ",".join(forbidden))
            check(
                "Chrome uses the libsecret password store",
                any(argument.startswith("--password-store=gnome-libsecret") for argument in argv),
            )
            check("Chrome has no DevTools listener", _no_devtools_listener())

    # --- the host-level limitation -----------------------------------------
    # The 32-bit compatibility entry point is not closed by the container's
    # seccomp filter, and the only control over it is a host-level sysctl.
    # This is stated here so that it is never forgotten.
    ia32 = "/proc/sys/abi/ia32_emulation"
    if os.path.exists(ia32):
        try:
            with open(ia32, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError:
            value = "unreadable"
        _print(
            "%-42s %s"
            % ("ia32_emulation (host-level control)", "not closable in-container (%s)" % value)
        )
    else:
        _print(
            "%-42s %s"
            % (
                "ia32_emulation (host-level control)",
                "not exposed; a host-level control is required",
            )
        )
    _print(
        "%-42s %s"
        % (
            "syscall isolation",
            "PARTIAL: the int 0x80 compatibility path stays reachable",
        )
    )

    _print("")
    if problems:
        _fail("%d check(s) failed: %s" % (len(problems), ", ".join(problems)))
        return EXIT_FAILURE
    _print("all checks passed")
    return EXIT_OK


def _no_devtools_listener(port=9222):
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# passwd
# ---------------------------------------------------------------------------
def command_passwd(args, config):
    """Rotate the VNC password.

    The password is printed only when stdout is an interactive terminal. The
    command is safe to repeat: the previous file is replaced atomically and
    the sessions that are already authenticated stay authenticated.
    """
    layout = _layout(config)
    layout.require_initialized()
    if not os.path.exists(layout.vnc_pass_file):
        raise NotInitializedError("the VNC password file does not exist yet")

    _path, password = vnc_policy.rotate_password_file(layout.vnc_pass_file)
    global _last_generated_password
    _last_generated_password = password
    if not _stdout_is_tty():
        _fail(
            "the VNC password was rotated but is not printed: standard output is "
            "not an interactive terminal"
        )
        return EXIT_UNSUPPORTED
    _print(password)
    return EXIT_OK


# The password generated by the last rotation. It is held in memory only for
# the duration of the command and never written to a log.
_last_generated_password = None


# ---------------------------------------------------------------------------
# url
# ---------------------------------------------------------------------------
def command_url(args, config):
    """Print the URL of the noVNC client."""
    url = None
    forwarding = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_URL")
    if forwarding:
        url = forwarding
        if not url.endswith("/"):
            url += "/"
        url += "vnc.html?autoconnect=1&resize=scale"
    else:
        url = "http://localhost:%d/vnc.html" % config.http_port
    _print(url)
    return EXIT_OK


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------
def command_version(args, config):
    _print("cloudchrome %s" % __version__)
    return EXIT_OK


# ---------------------------------------------------------------------------
# backup / restore / prune
#
# The encrypted backup, the transactional restore and the pruning arrive in
# Phase 3. Until then the commands exist, validate their arguments and
# report clearly that the feature is not implemented yet, rather than
# pretending to work.
# ---------------------------------------------------------------------------
def _backup_not_available():
    raise CloudChromeError(
        "the encrypted backup is not implemented in this release; it arrives "
        "with Phase 3",
        code=EXIT_UNSUPPORTED,
    )


def command_backup(args, config):
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=60):
        _backup_not_available()


def command_restore(args, config):
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=60):
        _backup_not_available()


def command_prune(args, config):
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=60):
        _backup_not_available()


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s " + __version__
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init = subparsers.add_parser("init", help="create the persistent state (silent)")
    init.add_argument(
        "--verbose", action="store_true", help="print what was created"
    )
    init.set_defaults(handler=command_init)

    start = subparsers.add_parser("start", help="start the desktop stack")
    start.add_argument(
        "--if-needed",
        action="store_true",
        help="do nothing when the stack is already running",
    )
    start.set_defaults(handler=command_start)

    stop = subparsers.add_parser("stop", help="stop the desktop stack")
    stop.set_defaults(handler=command_stop)

    restart = subparsers.add_parser("restart", help="stop and start the desktop stack")
    restart.set_defaults(handler=command_restart)

    status = subparsers.add_parser("status", help="show the state of the runtime")
    status.set_defaults(handler=command_status)

    logs = subparsers.add_parser("logs", help="show the runtime logs")
    logs.add_argument("--component", help="only this component's log")
    logs.add_argument("--lines", type=int, default=100, help="how many lines")
    logs.set_defaults(handler=command_logs)

    doctor = subparsers.add_parser("doctor", help="run the diagnostics")
    doctor.set_defaults(handler=command_doctor)

    passwd = subparsers.add_parser("passwd", help="rotate the VNC password")
    passwd.set_defaults(handler=command_passwd)

    url = subparsers.add_parser("url", help="print the noVNC URL")
    url.set_defaults(handler=command_url)

    version = subparsers.add_parser("version", help="print the version")
    version.set_defaults(handler=command_version)

    backup = subparsers.add_parser("backup", help="write an encrypted backup")
    backup.add_argument("--output", help="write to this file instead of the default")
    backup.add_argument("--label", help="a label stored in the manifest")
    backup.set_defaults(handler=command_backup)

    restore = subparsers.add_parser("restore", help="restore an encrypted backup")
    restore.add_argument("--source", help="the backup file to restore")
    restore.add_argument(
        "--yes", action="store_true", help="do not ask for confirmation"
    )
    restore.set_defaults(handler=command_restore)

    prune = subparsers.add_parser("prune", help="delete old encrypted backups")
    prune.add_argument(
        "--keep", type=int, required=True, help="how many backups to keep"
    )
    prune.add_argument(
        "--yes", action="store_true", help="do not ask for confirmation"
    )
    prune.add_argument(
        "--dry-run", action="store_true", help="only show what would be deleted"
    )
    prune.set_defaults(handler=command_prune)

    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_USAGE

    config = load_config()
    try:
        return args.handler(args, config)
    except UsageError as exc:
        _fail(str(exc))
        return EXIT_USAGE
    except NotInitializedError as exc:
        _fail(str(exc))
        return EXIT_NOT_INITIALIZED
    except NotRunningError as exc:
        _fail(str(exc))
        return EXIT_NOT_RUNNING
    except CloudChromeError as exc:
        _fail(str(exc))
        if exc.hint:
            _fail(exc.hint)
        return exc.code
    except KeyboardInterrupt:
        _fail("interrupted")
        return 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
