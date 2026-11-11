"""The XFCE desktop session.

XFCE is started as a session, not as a set of unrelated programs: the session
manager owns the panel, the window manager and the applications, and it exits
when the last client goes away. That is what keeps a single desktop stack
per container.
"""

import os


def session_arguments(display):
    """The argument vector of the XFCE session.

    ``startxfce4`` is the documented entry point; ``xfce4-session`` on its
    own is not, and it accepts only ``--disable-tcp``, ``--help`` and
    ``--version``. ``startxfce4`` also runs the X initialisation script,
    which sets up the environment the failsafe session needs.

    The session starts the window manager itself, so the window manager is
    not started separately.
    """
    return ["startxfce4"]


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


def desktop_environment(display, dbus_address, layout):
    """The environment of the desktop components."""
    env = dict(os.environ)
    env["DISPLAY"] = display
    if dbus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    env["XDG_RUNTIME_DIR"] = layout.xdg_runtime_dir
    env["XDG_DATA_HOME"] = layout.xdg_data_home
    env["XDG_CACHE_HOME"] = layout.xdg_cache_home
    env["XDG_CONFIG_HOME"] = layout.xdg_config_home
    env["XDG_CONFIG_DIRS"] = "/etc/xdg"
    # The failsafe session can only be determined when the desktop is named.
    env["XDG_CURRENT_DESKTOP"] = "XFCE"
    env["DESKTOP_SESSION"] = "cloudchrome"
    env["HOME"] = os.path.expanduser("~")
    env["USER"] = "vscode"
    env["LOGNAME"] = "vscode"
    env["SHELL"] = "/bin/bash"
    return env
