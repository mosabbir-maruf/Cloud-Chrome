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

from . import backup as backup_module
from . import chrome as chrome_policy
from . import lifecycle as lifecycle_module
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
from .lifecycle import Lifecycle
from .limits import Limits
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


def _lifecycle(config, layout):
    """The desktop stack lifecycle, for this invocation.

    The lifecycle never takes the control lock: the caller owns it, so a
    compound operation holds the lock exactly once.
    """
    return Lifecycle(config, layout)


def _print(message=""):
    sys.stdout.write("%s\n" % message)


def _fail(message):
    sys.stderr.write("%s: %s\n" % (PROGRAM, message))


def _supervisor_running(layout):
    """Return the supervisor's PID when it is running, else None."""
    return lifecycle_module.supervisor_pid(layout)


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

    # The age identity for the encrypted backups is created once as well, so
    # that 'cloudchrome backup' works without any further preparation.
    backup_module.ensure_identity(layout)

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
def command_start(args, config):
    """Start the desktop stack."""
    layout = _layout(config)
    layout.require_initialized()
    # The state is checked before anything is started, so a broken state is
    # reported straight away instead of after the stack is half up.
    layout.validate_for_start()
    with FileLock(layout.control_lock_file, timeout=30):
        _lifecycle(config, layout).start(if_needed=args.if_needed)
    _print("the desktop stack is running")
    return EXIT_OK


def command_stop(args, config):
    """Stop the desktop stack."""
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=30):
        stopped = _lifecycle(config, layout).stop()
    if stopped:
        _print("the desktop stack is stopped")
    else:
        _print("the desktop stack is not running")
    return EXIT_OK


def command_restart(args, config):
    """Stop the desktop stack and start it again.

    The lock is taken once for the whole operation: taking it again inside
    it would be a second, independent lock acquisition, which Linux
    ``flock`` treats as a different owner and would fail.
    """
    layout = _layout(config)
    layout.require_initialized()
    layout.validate_for_start()
    with FileLock(layout.control_lock_file, timeout=30):
        lifecycle = _lifecycle(config, layout)
        lifecycle.stop()
        lifecycle.start()
    _print("the desktop stack is running")
    return EXIT_OK

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
        check(
            "state directory is owned by the runtime user",
            info.st_uid == os.getuid(),
            "uid=%d" % info.st_uid,
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
            check(
                "%s is owned by the runtime user" % label,
                info.st_uid == os.getuid(),
                "uid=%d" % info.st_uid,
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

    # The age identity must never be world readable.
    if os.path.exists(layout.identity_file):
        info = os.stat(layout.identity_file)
        check(
            "the age identity is private",
            info.st_mode & 0o077 == 0,
            "mode=%o" % (info.st_mode & 0o777),
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

    # --- the remote access surface -----------------------------------------
    check("no DevTools listener is open", _no_devtools_listener())
    check(
        "no SSH server is installed",
        not _any_listening_on(22),
        "port 22",
    )
    unexpected = _unexpected_listeners(config)
    check(
        "no Cloud-Chrome component listens on an unexpected port",
        not unexpected,
        "; ".join("%s: %s" % (port, owner) for port, owner in unexpected[:3]),
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
        # The browser process is looked up through the process table rather
        # than through the supervisor's record, because Chrome re-executes
        # itself and the recorded PID is not always the browser.
        browser = _find_browser_process(layout)
        if browser is not None:
            browser_pid, browser_argv = browser
            # Chrome rewrites its argument vector into a single string, so the
            # checks are made against the joined command line and not
            # against the individual arguments.
            command_line = " ".join(browser_argv)
            forbidden = [
                argument
                for argument in browser_argv
                if argument.split("=", 1)[0] in chrome_policy.FORBIDDEN_FLAGS
            ]
            check("Chrome runs without forbidden flags", not forbidden, ",".join(forbidden))
            check(
                "Chrome uses the libsecret password store",
                "--password-store=gnome-libsecret" in command_line,
                command_line[:120],
            )
            check(
                "Chrome uses the persistent profile",
                "--user-data-dir=%s" % layout.chrome_dir in command_line,
            )
            check("Chrome has no DevTools listener", _no_devtools_listener())
        else:
            check("the Chrome browser process was found", False)

    # --- the logs -----------------------------------------------------------
    # The logs must never carry a secret.
    secret = ""
    if os.path.exists(layout.keyring_pass_file):
        try:
            with open(layout.keyring_pass_file, "r", encoding="utf-8") as handle:
                secret = handle.read().strip()
        except OSError:
            secret = ""
    if secret:
        leaked = False
        for name in os.listdir(layout.log_dir) if os.path.isdir(layout.log_dir) else []:
            path = os.path.join(layout.log_dir, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    if secret in handle.read():
                        leaked = True
            except OSError:
                continue
        check("no log file carries the keyring passphrase", not leaked)

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


def _any_listening_on(port):
    return port in _listening_ports()


def _find_browser_process(layout):
    """Find the Chrome browser process and its command line."""
    return lifecycle_module.find_browser_process(layout)


def _no_devtools_listener(port=9222):
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        probe.close()


def _listening_ports():
    """Return the ports that are in the LISTEN state."""
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            try:
                ports.add(int(fields[1].rpartition(":")[2], 16))
            except ValueError:
                continue
    return ports


def _listening_inodes():
    """Return the socket inodes of the listening sockets."""
    inodes = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                port = int(fields[1].rpartition(":")[2], 16)
            except ValueError:
                continue
            inodes[fields[9]] = port
    return inodes


def _processes_owning_port(port):
    """Return the command lines of the processes that listen on *port*.

    The platform's own agents listen on ports too; only the processes that
    belong to Cloud-Chrome are of interest here.
    """
    inodes = {
        inode: number
        for inode, number in _listening_inodes().items()
        if number == port
    }
    owners = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        directory = "/proc/%s/fd" % entry
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            try:
                target = os.readlink(os.path.join(directory, name))
            except OSError:
                continue
            if not target.startswith("socket:["):
                continue
            inode = target[8:-1]
            if inode in inodes:
                owners.append(" ".join(procs.read_cmdline(int(entry))))
                break
    return owners


def _unexpected_listeners(config):
    """True when a Cloud-Chrome component listens on an unexpected port.

    The platform's own agents listen on ports of their own; those are not
    Cloud-Chrome's business. Only the processes that Cloud-Chrome started
    are checked.
    """
    allowed = {config.http_port, config.vnc_port}
    unexpected = []
    for port in sorted(_listening_ports()):
        if port in allowed:
            continue
        for owner in _processes_owning_port(port):
            if _is_cloudchrome_process(owner):
                unexpected.append((port, owner))
    return unexpected


def _is_cloudchrome_process(command_line):
    """True when the command line belongs to a Cloud-Chrome component."""
    markers = (
        "Xtigervnc",
        "websockify",
        "xfce4-session",
        "xfwm4",
        "gnome-keyring-daemon",
        "dbus-daemon",
        "google-chrome",
        "cloudchrome",
    )
    return any(marker in command_line for marker in markers)


# ---------------------------------------------------------------------------
# passwd
# ---------------------------------------------------------------------------
def command_passwd(args, config):
    """Rotate the VNC password.

    The password is printed only when stdout is an interactive terminal.
    Without a terminal the rotation does not happen either: a password that
    was changed and cannot be shown would lock the user out of their own
    desktop without any way to recover it.
    """
    layout = _layout(config)
    layout.require_initialized()
    if not os.path.exists(layout.vnc_pass_file):
        raise NotInitializedError("the VNC password file does not exist yet")

    if not _stdout_is_tty():
        raise CloudChromeError(
            "the VNC password is only shown on an interactive terminal, so it "
            "was not changed; run 'cloudchrome passwd' in a terminal",
            code=EXIT_UNSUPPORTED,
        )

    _path, password = vnc_policy.rotate_password_file(layout.vnc_pass_file)
    global _last_generated_password
    _last_generated_password = password
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
# ---------------------------------------------------------------------------
def command_backup(args, config):
    """Write an encrypted backup.

    The desktop stack is stopped for the snapshot and started again
    afterwards. A profile that Chrome is still writing to is not a consistent
    snapshot, and hashing it does not make it one.

    A failure of the restart is never swallowed: the command reports it and
    exits non-zero, because a backup that was taken but a desktop that never
    came back is not a clean outcome.
    """
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=60):
        lifecycle = _lifecycle(config, layout)
        was_running = lifecycle.is_running()
        if was_running:
            lifecycle.stop()
        try:
            result = backup_module.create_backup(
                layout, config, output=args.output, label=args.label
            )
        except BaseException:
            # The backup failed: the previous state is untouched, so the
            # desktop that was running is simply started again.
            _restart_or_report(lifecycle, was_running, "the backup failed")
            raise
        _restart_or_report(lifecycle, was_running, "the backup was written")
    _print("backup written: %s" % result["path"])
    _print("members:      %d" % result["members"])
    _print("sha-256:      %s" % result["sha256"])
    return EXIT_OK


def command_restore(args, config):
    """Restore an encrypted backup, as one transaction.

    The whole restore is one operation under one lock: the archive is
    validated and staged, the stack is stopped, the state is replaced, the
    restored desktop is started and checked, and only then is the restore
    reported as successful. A failure at any step puts the previous profile,
    keyring and passphrase back and returns a failure.
    """
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=60):
        result = backup_module.restore_transaction(
            _lifecycle(config, layout),
            layout,
            config,
            source=args.source,
            assume_yes=args.yes,
            limits=Limits(config.as_dict()),
        )
    _print("restored from %s" % result["source"])
    _print("members:      %d" % result["members"])
    return EXIT_OK


def _restart_or_report(lifecycle, was_running, context):
    """Start the desktop again and report a failure to start it.

    The failure is never swallowed: the caller turns it into a non-zero exit
    code.
    """
    if not was_running:
        return
    try:
        lifecycle.start()
    except CloudChromeError as exc:
        raise CloudChromeError(
            "%s, but the desktop stack could not be started again: %s"
            % (context, exc)
        ) from exc


def command_prune(args, config):
    """Delete the old encrypted backups."""
    layout = _layout(config)
    layout.require_initialized()
    with FileLock(layout.control_lock_file, timeout=60):
        result = backup_module.prune_backups(
            layout, keep=args.keep, assume_yes=args.yes, dry_run=args.dry_run
        )
    for path in result["candidates"]:
        _print("would delete %s" % path if args.dry_run else "deleted %s" % path)
    _print(
        "%d backup(s) %s, %d kept"
        % (
            len(result["deleted"]) if not args.dry_run else len(result["candidates"]),
            "would be deleted" if args.dry_run else "deleted",
            result["kept"],
        )
    )
    return EXIT_OK


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

    # Every file the runtime creates is private by default.
    os.umask(0o077)

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
