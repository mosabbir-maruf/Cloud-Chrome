"""Configuration resolution.

Precedence, highest first:

1. an explicit command-line option
2. an environment variable
3. ``/etc/cloudchrome/cloudchrome.env`` (root-owned, non-secret)
4. the built-in default

The configuration file is parsed as ``KEY=VALUE`` lines. It is never sourced as
a shell script and it never holds a secret: the CI workflow asserts that.
"""

import os
from dataclasses import dataclass, field

CONFIG_FILE = "/etc/cloudchrome/cloudchrome.env"

# Built-in defaults. Every value here can be overridden by the environment
# variable of the same name.
DEFAULTS = {
    "CLOUDCHROME_DISPLAY": ":99",
    "CLOUDCHROME_GEOMETRY": "1440x900",
    "CLOUDCHROME_DEPTH": "24",
    "CLOUDCHROME_HTTP_PORT": "6080",
    "CLOUDCHROME_VNC_PORT": "5900",
    "CLOUDCHROME_HTTP_BIND": "0.0.0.0",
    "CLOUDCHROME_DESKTOP": "xfce",
    "CLOUDCHROME_STOP_TIMEOUT": "20",
    "CLOUDCHROME_LOG_MAX_BYTES": "5242880",
    "CLOUDCHROME_LOG_KEEP": "3",
    "CLOUDCHROME_PERSIST_DIR": "/workspaces/Cloud-Chrome/.cloudchrome",
    "CLOUDCHROME_RUNTIME_DIR": "/workspaces/Cloud-Chrome/.cloudchrome/run",
    "CLOUDCHROME_NOVNC_ROOT": "/usr/local/share/novnc",
    "CLOUDCHROME_CHROME_BINARY": "google-chrome",
    "CLOUDCHROME_KEYRING_DIR": "keyring",
    "CLOUDCHROME_BACKUP_DIR": "backups",
    "CLOUDCHROME_LOG_DIR": "logs",
}

# Keys that must be integers.
INTEGER_KEYS = (
    "CLOUDCHROME_DEPTH",
    "CLOUDCHROME_HTTP_PORT",
    "CLOUDCHROME_VNC_PORT",
    "CLOUDCHROME_STOP_TIMEOUT",
    "CLOUDCHROME_LOG_MAX_BYTES",
    "CLOUDCHROME_LOG_KEEP",
)


def parse_config_file(path=CONFIG_FILE):
    """Read a ``KEY=VALUE`` configuration file.

    Comments, blank lines and unknown keys are ignored. A value may be quoted.
    """
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (OSError, UnicodeDecodeError):
        return values

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


@dataclass
class Config:
    """The resolved configuration for one CLI invocation."""

    values: dict = field(default_factory=dict)

    def get(self, key):
        return self.values.get(key, DEFAULTS.get(key, ""))

    def get_int(self, key):
        raw = self.get(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(DEFAULTS.get(key, "0"))

    @property
    def display(self):
        return self.get("CLOUDCHROME_DISPLAY")

    @property
    def geometry(self):
        return self.get("CLOUDCHROME_GEOMETRY")

    @property
    def depth(self):
        return self.get_int("CLOUDCHROME_DEPTH")

    @property
    def http_port(self):
        return self.get_int("CLOUDCHROME_HTTP_PORT")

    @property
    def http_bind(self):
        return self.get("CLOUDCHROME_HTTP_BIND")

    @property
    def vnc_port(self):
        return self.get_int("CLOUDCHROME_VNC_PORT")

    @property
    def desktop(self):
        return self.get("CLOUDCHROME_DESKTOP")

    @property
    def stop_timeout(self):
        return self.get_int("CLOUDCHROME_STOP_TIMEOUT")

    @property
    def log_max_bytes(self):
        return self.get_int("CLOUDCHROME_LOG_MAX_BYTES")

    @property
    def log_keep(self):
        return self.get_int("CLOUDCHROME_LOG_KEEP")

    @property
    def persist_dir(self):
        return self.get("CLOUDCHROME_PERSIST_DIR")

    @property
    def runtime_dir(self):
        return self.get("CLOUDCHROME_RUNTIME_DIR")

    @property
    def novnc_root(self):
        return self.get("CLOUDCHROME_NOVNC_ROOT")

    @property
    def chrome_binary(self):
        return self.get("CLOUDCHROME_CHROME_BINARY")

    @property
    def keyring_dir_name(self):
        return self.get("CLOUDCHROME_KEYRING_DIR")

    @property
    def backup_dir_name(self):
        return self.get("CLOUDCHROME_BACKUP_DIR")

    @property
    def log_dir_name(self):
        return self.get("CLOUDCHROME_LOG_DIR")

    def as_dict(self):
        return dict(self.values)


def repository_root():
    """The root of the Cloud-Chrome checkout, when the CLI runs from one.

    ``bin/cloudchrome`` exports ``CLOUDCHROME_HOME`` with the directory that
    holds it. When that directory is part of a checkout, the checkout root is
    used, so the persistent state follows the source tree whatever the
    workspace folder is called. When the CLI runs from the installed copy in
    the image there is no checkout, and the home directory is used instead.
    """
    candidates = []
    home = os.environ.get("CLOUDCHROME_HOME")
    if home:
        candidates.append(os.path.abspath(home))
    candidates.append(
        os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir)
        )
    )
    for candidate in candidates:
        for marker in (".devcontainer", ".git"):
            if os.path.exists(os.path.join(candidate, marker)):
                return candidate
    return os.path.expanduser("~")


def load_config(overrides=None, environ=None):
    """Resolve the configuration.

    ``overrides`` maps configuration keys to values and wins over everything
    else; it carries the command-line options that have a configuration
    counterpart.
    """
    environ = os.environ if environ is None else environ
    values = dict(DEFAULTS)
    values.update(parse_config_file())
    for key in DEFAULTS:
        if key in environ:
            values[key] = environ[key]
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                values[key] = str(value)

    # The persistent state follows the checkout unless it is set explicitly.
    root = repository_root()
    if not values.get("CLOUDCHROME_PERSIST_DIR"):
        values["CLOUDCHROME_PERSIST_DIR"] = os.path.join(root, ".cloudchrome")
    if not values.get("CLOUDCHROME_RUNTIME_DIR"):
        values["CLOUDCHROME_RUNTIME_DIR"] = os.path.join(
            values["CLOUDCHROME_PERSIST_DIR"], "run"
        )
    return Config(values)
