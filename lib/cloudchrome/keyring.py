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


def keyring_arguments():
    """The argument vector of the keyring daemon.

    ``--unlock`` unlocks the login keyring, reading the passphrase from
    standard input. The passphrase is therefore never visible in the process
    table. ``--foreground`` keeps the daemon as a child of the supervisor, so
    the supervisor owns its lifecycle.
    """
    return [
        "gnome-keyring-daemon",
        "--foreground",
        "--unlock",
        "--components=secrets",
    ]


def secret_tool_available():
    return shutil.which("secret-tool") is not None


def verify_secret_service(env, timeout=25):
    """Store, read back and delete a secret.

    This is the decisive check that the secret service genuinely works. It
    returns a dictionary describing the outcome; the secret itself is random
    and is never logged.
    """
    if not secret_tool_available():
        raise UnsupportedError(
            "secret-tool is not installed; the secret service cannot be verified"
        )

    import base64
    import os as _os

    probe = base64.b32encode(_os.urandom(16)).decode("ascii").rstrip("=").lower()
    attribute = "/cloudchrome/secret-service-probe"

    def run(args, stdin=None):
        return subprocess.run(
            args,
            input=stdin.encode("utf-8") if stdin is not None else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

    store = run(
        ["secret-tool", "store", "--label=Cloud-Chrome probe", attribute],
        stdin=probe,
    )
    if store.returncode != 0:
        raise UnsupportedError(
            "the secret service did not accept a stored secret (secret-tool exited %d)"
            % store.returncode
        )

    lookup = run(["secret-tool", "lookup", attribute])
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

    run(["secret-tool", "clear", attribute])

    return {
        "stored": True,
        "retrieved": True,
        "matched": True,
        "attribute": attribute,
    }
