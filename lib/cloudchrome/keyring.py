"""D-Bus and the GNOME Keyring secret service.

Chrome is started with ``--password-store=gnome-libsecret``. That only works
when a real Secret Service is reachable, so the runtime brings up its own
private session bus and its own keyring, unlocks it with a passphrase that
lives in the persistent state directory, and then *verifies* that a secret can
actually be stored and read back.

The verification matters: a keyring daemon that started but cannot serve
would otherwise let Chrome fall back to storing passwords in plaintext.
Cloud-Chrome fails closed instead.
"""

import os
import shutil
import subprocess

from . import logutil
from .errors import UnsupportedError
from .paths import OWNER_ONLY_FILE, assert_safe_file, write_private_file

KEYRING_SERVICE = "org.freedesktop.Secret.Service"
KEYRING_PATH = "/org/freedesktop/secrets"


def generate_passphrase():
    """Return a fresh, random keyring passphrase."""
    import base64
    import os as _os

    return base64.b32encode(_os.urandom(32)).decode("ascii").rstrip("=").lower()


def create_passphrase_file(path):
    """Create the keyring passphrase file with mode 0600 if it is missing."""
    if os.path.exists(path):
        assert_safe_file(path, OWNER_ONLY_FILE, "the keyring passphrase file")
        return path
    write_private_file(path, generate_passphrase() + "\n", OWNER_ONLY_FILE)
    return path


def read_passphrase(path):
    """Read the keyring passphrase. It is registered as a secret at once."""
    assert_safe_file(path, OWNER_ONLY_FILE, "the keyring passphrase file")
    with open(path, "r", encoding="utf-8") as handle:
        value = handle.read().strip()
    logutil.register_secret(value)
    return value


def dbus_arguments(socket_path):
    """The argument vector of the private session bus."""
    return [
        "dbus-daemon",
        "--session",
        "--nofork",
        "--print-address=1",
        "--address=unix:path=%s" % socket_path,
    ]


def dbus_environment(socket_path, runtime_dir, data_home):
    """The environment of every component that talks to the session bus."""
    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s" % socket_path
    env.pop("DBUS_SESSION_BUS_PID", None)
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["XDG_DATA_HOME"] = data_home
    env["XDG_CACHE_HOME"] = os.path.join(data_home, "cache")
    env["XDG_CONFIG_HOME"] = os.path.join(data_home, "config")
    return env


def keyring_arguments(replace=False):
    """The argument vector of the keyring daemon.

    ``--unlock`` unlocks the login keyring, reading the passphrase from
    standard input, and creates the login keyring when it does not exist
    yet. The passphrase is therefore never visible in the process table.

    ``--replace`` makes the daemon displace a daemon that already serves the
    Secret Service. gnome-keyring installs a D-Bus service that activates a
    second, *locked* daemon the moment the first client calls the Secret
    Service, and that activated daemon wins the
    ``org.freedesktop.secrets`` name. It never received the unlock
    passphrase, so it has no login collection and every store fails with
    "Object does not exist at path .../collection/login". ``--replace``
    displaces the activated daemon instead.

    ``--replace`` only works when the daemon it displaces can be found
    through the control socket, so it is used when a competing daemon was
    actually seen. Without a competing daemon it is left out, because a
    replacement that finds nothing to replace is not what is wanted.

    ``--foreground`` keeps the daemon as a child of the supervisor, so the
    supervisor owns its lifecycle.
    """
    arguments = ["gnome-keyring-daemon", "--foreground", "--unlock"]
    if replace:
        arguments.append("--replace")
    arguments.append("--components=secrets")
    return arguments


def secret_service_owner_pid(env, timeout=10):
    """The PID of the process that owns ``org.freedesktop.secrets``.

    The bus is asked directly, because the name owner is the only reliable
    answer: a daemon that is alive but lost the name race still satisfies
    every other check.
    """
    if shutil.which("dbus-send") is None:
        return None

    def ask(*arguments):
        result = subprocess.run(
            [
                "dbus-send",
                "--session",
                "--dest=org.freedesktop.DBus",
                "--print-reply=literal",
                "/org/freedesktop/DBus",
            ]
            + list(arguments),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return result.stdout.decode("utf-8", "replace").strip()

    owner = ask("org.freedesktop.DBus.GetNameOwner", "string:org.freedesktop.secrets")
    if not owner or owner.startswith("Error"):
        return None
    reply = ask(
        "org.freedesktop.DBus.GetConnectionUnixProcessID", "string:%s" % owner
    )
    match = None
    for token in reply.replace("(", " ").replace(")", " ").split():
        if token.isdigit():
            match = int(token)
    return match


def wait_for_secret_service(
    env, daemon_pid, keyring_file, timeout=30.0, interval=0.25, should_stop=None
):
    """Wait until the unlocking daemon serves the Secret Service.

    Three things have to be true before the first client call: the daemon
    must still be alive, it must own the ``org.freedesktop.secrets`` name,
    and the login keyring must exist. The order matters: the wait happens
    before any client call, so the D-Bus service never activates the
    competing daemon.

    ``should_stop`` is called at the top of every round. It may raise, and
    that interrupts the wait, so a stop request is honoured while the
    keyring is coming up.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_stop is not None:
            should_stop()
        if daemon_pid is not None and not _pid_alive(daemon_pid):
            raise UnsupportedError(
                "the keyring daemon exited before it served the secret service"
            )
        if os.path.exists(keyring_file):
            # The bus is asked with the time that is left, so that the
            # wait as a whole is bounded by its timeout.
            remaining = max(1.0, deadline - time.monotonic())
            owner = secret_service_owner_pid(env, timeout=remaining)
            if owner == daemon_pid:
                return True
        time.sleep(interval)
    raise UnsupportedError(
        "the keyring daemon did not take ownership of the secret service within "
        "%ds" % int(timeout)
    )


def read_reported_control(process, timeout=15.0):
    """Read the control socket the daemon reports on its output.

    The daemon prints ``GNOME_KEYRING_CONTROL=<path>`` when it starts. The
    path is read from there, because the daemon decides where its socket
    lives.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.stdout is None:
            return None
        try:
            line = process.stdout.readline()
        except (OSError, ValueError):
            return None
        if not line:
            return None
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("GNOME_KEYRING_CONTROL="):
            return text.split("=", 1)[1].strip()
    return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        import errno

        if exc.errno == errno.ESRCH:
            return False
    return True


def secret_tool_available():
    return shutil.which("secret-tool") is not None


def verify_secret_service(env, timeout=25, should_stop=None):
    """Store, read back and delete a secret.

    This is the decisive check that the secret service genuinely works. The
    daemon registers itself on the session bus a moment after it is started,
    so the check retries until the service answers or the time runs out.

    It returns a dictionary describing the outcome; the secret itself is
    random and is never logged.

    ``should_stop`` is called before every step. It may raise, and that
    interrupts the check, so a stop request is honoured while the secret
    service is being verified.
    """
    if not secret_tool_available():
        raise UnsupportedError(
            "secret-tool is not installed; the secret service cannot be verified"
        )

    import base64
    import os as _os
    import time

    probe = base64.b32encode(_os.urandom(16)).decode("ascii").rstrip("=").lower()

    # secret-tool identifies an item by attribute/value PAIRS. The secret
    # itself is read from standard input, so it never appears in the process
    # table, and it carries no trailing newline, because a newline would
    # become part of the stored value.
    pairs = ["application", "cloudchrome"]

    deadline = time.monotonic() + timeout

    def run(args, stdin=None):
        # The call is given the time that is left, so that the check as a
        # whole is bounded by its timeout and not by the timeout of every
        # single call.
        remaining = max(1.0, deadline - time.monotonic())
        return subprocess.run(
            args,
            input=stdin.encode("utf-8") if stdin is not None else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=remaining,
            check=False,
        )

    store = None
    last_error = ""
    while True:
        if should_stop is not None:
            should_stop()
        store = run(
            ["secret-tool", "store", "--label=Cloud-Chrome probe"] + pairs,
            stdin=probe,
        )
        if store.returncode == 0:
            break
        last_error = store.stderr.decode("utf-8", "replace").strip()
        if time.monotonic() >= deadline:
            raise UnsupportedError(
                "the secret service did not accept a stored secret "
                "(secret-tool exited %d%s)"
                % (store.returncode, ": " + last_error if last_error else "")
            )
        time.sleep(0.5)

    if should_stop is not None:
        should_stop()
    lookup = run(["secret-tool", "lookup"] + pairs)
    if lookup.returncode != 0:
        raise UnsupportedError(
            "the secret service did not return the stored secret (secret-tool exited %d)"
            % lookup.returncode
        )
    returned = lookup.stdout.decode("utf-8", "replace").strip()
    if returned != probe:
        raise UnsupportedError(
            "the secret service returned a different value than the one stored"
        )

    run(["secret-tool", "clear"] + pairs)

    return {
        "stored": True,
        "retrieved": True,
        "matched": True,
        "pairs": pairs,
    }
