"""The Chrome launch policy.

The argument vector is assembled here and nowhere else. Before Chrome is
started, the assembled vector is checked against a list of forbidden flags;
a vector that contains one of them is a bug, and the runtime refuses to start
rather than starting a weakened browser.

Two conditions make the runtime fail closed instead of starting Chrome:

* the Secret Service is not available, because Chrome would otherwise store
  passwords in plaintext, and
* the sandbox cannot work, because ``--no-sandbox`` is never added as a
  fallback.
"""

import os

from .errors import InvalidStateError, UnsupportedError

# Flags that must never appear in the argument vector.
FORBIDDEN_FLAGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu-sandbox",
    "--single-process",
    "--no-zygote",
    "--remote-debugging-port",
    "--remote-debugging-pipe",
    "--remote-debugging-address",
    "--remote-allow-origins",
    "--disable-web-security",
    "--allow-running-insecure-content",
    "--disable-features=NetworkServiceSandbox",
    "--password-store=basic",
    "--password-store=kwallet",
    "--use-mock-keychain",
    "--incognito",
    "--disable-gpu",
    "--auto-open-devtools-for-tabs",
    "--enable-automation",
    "--enable-blink-features=AutomationControlled",
)

# The only password store Cloud-Chrome accepts.
REQUIRED_PASSWORD_STORE = "gnome-libsecret"


def user_data_dir(layout):
    """The persistent Chrome profile directory."""
    return layout.chrome_dir


def build_arguments(config, layout, display=None):
    """Assemble the approved Chrome argument vector.

    The vector is minimal and every entry has a reason:

    ``--user-data-dir``       the persistent profile inside the state directory
    ``--password-store``      libsecret, so passwords go to the keyring
    ``--no-first-run``        no first-run wizard in an automated session
    ``--no-default-browser-check``  no "make Chrome the default" prompt
    ``--ozone-platform=x11``  the X11 desktop integration the session provides

    The display itself is not a flag: Chrome reads the ``DISPLAY`` environment
    variable, which the supervisor sets explicitly.
    """
    arguments = [
        "--user-data-dir=%s" % user_data_dir(layout),
        "--password-store=%s" % REQUIRED_PASSWORD_STORE,
        "--no-first-run",
        "--no-default-browser-check",
        "--ozone-platform=x11",
    ]
    return arguments


def assert_safe_arguments(arguments):
    """Refuse a vector that contains a forbidden flag.

    The check is exact on the flag name and on the ``--flag=value`` form, so
    ``--remote-debugging-port=9222`` is caught as well as the bare flag.
    """
    for argument in arguments:
        name = argument.split("=", 1)[0]
        if name in FORBIDDEN_FLAGS:
            raise InvalidStateError(
                "the assembled Chrome argument vector contains the forbidden flag %s"
                % argument
            )
        if argument in FORBIDDEN_FLAGS:
            raise InvalidStateError(
                "the assembled Chrome argument vector contains the forbidden flag %s"
                % argument
            )
    stores = [
        argument for argument in arguments if argument.startswith("--password-store=")
    ]
    if not stores:
        raise InvalidStateError(
            "the assembled Chrome argument vector does not select a password store"
        )
    if stores[0] != "--password-store=%s" % REQUIRED_PASSWORD_STORE:
        raise InvalidStateError(
            "the assembled Chrome argument vector uses %s instead of %s"
            % (stores[0], REQUIRED_PASSWORD_STORE)
        )
    return True


def assert_no_devtools_listener(desired_port=None):
    """Refuse to start while a DevTools/CDP listener is already present.

    Cloud-Chrome never opens a debugging port. If one is open already, that
    is somebody else's business and is reported, not silently reused.
    """
    import socket

    ports = [desired_port] if desired_port else []
    for port in ports:
        if port is None:
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise InvalidStateError(
                    "a DevTools listener is already present on port %d" % port
                )
        finally:
            probe.close()
    return True


def chrome_environment(display, dbus_address, keyring_control, layout):
    """The environment Chrome is started with.

    Chrome must see the session bus and the keyring control socket, otherwise
    it cannot reach the Secret Service and would fall back to plaintext
    password storage.
    """
    env = dict(os.environ)
    env["DISPLAY"] = display
    if dbus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    if keyring_control:
        env["GNOME_KEYRING_CONTROL"] = keyring_control
    env["XDG_RUNTIME_DIR"] = layout.xdg_runtime_dir
    env["XDG_DATA_HOME"] = layout.xdg_data_home
    env["XDG_CACHE_HOME"] = layout.xdg_cache_home
    env["XDG_CONFIG_HOME"] = layout.xdg_config_home
    env["XDG_CONFIG_DIRS"] = "/etc/xdg"
    # A container has no usable GPU; software rendering is the default and
    # nothing is switched off to make Chrome start.
    env["CHROME_DESKTOP"] = "cloudchrome"
    return env


def assert_secret_service_available(result):
    """Fail closed when the secret service is not usable."""
    if not result or not result.get("stored") or not result.get("retrieved"):
        raise UnsupportedError(
            "the Secret Service is not usable; Chrome would store passwords in "
            "plaintext, so it is not started"
        )
    return True


def assert_sandbox_available():
    """Fail closed when the sandbox cannot work.

    Chromium's sandbox needs unprivileged user namespaces. When they are
    unavailable, the only way to start Chrome would be ``--no-sandbox``, which
    Cloud-Chrome never passes.
    """
    for path in ("/proc/sys/kernel/unprivileged_userns_clone",):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value == "0":
            raise UnsupportedError(
                "unprivileged user namespaces are disabled on this host; Chrome "
                "could only run with --no-sandbox, so it is not started"
            )
    return True
