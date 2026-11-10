"""The XFCE desktop session.

XFCE is started as a session, not as a set of unrelated programs: the session
manager owns the panel, the window manager and the applications, and it exits
when the last client goes away. That is what keeps a single desktop stack
per container.
"""

import os


def session_arguments(display):
    """The argument vector of the XFCE session manager."""
    return [
        "xfce4-session",
        "--session=cloudchrome-%s" % display.lstrip(":"),
    ]


def window_manager_arguments(display):
    """The argument vector of the XFCE window manager.

    The window manager is started explicitly rather than being left to the
    session, because the desktop has to be usable as soon as the supervisor
    reports the stack as ready.
    """
    return [
        "xfwm4",
        "--display=%s" % display,
    ]


def desktop_environment(display, dbus_address, persist_dir):
    """The environment of the desktop components."""
    env = dict(os.environ)
    env["DISPLAY"] = display
    if dbus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    env["XDG_RUNTIME_DIR"] = os.path.join(persist_dir, "run", "xdg")
    env["XDG_DATA_HOME"] = os.path.join(persist_dir, "xdg")
    env["XDG_CACHE_HOME"] = os.path.join(persist_dir, "cache")
    env["XDG_CONFIG_HOME"] = os.path.join(persist_dir, "config")
    env["XDG_CONFIG_DIRS"] = "/etc/xdg"
    env["HOME"] = os.path.expanduser("~")
    env["USER"] = "vscode"
    env["LOGNAME"] = "vscode"
    env["SHELL"] = "/bin/bash"
    return env
