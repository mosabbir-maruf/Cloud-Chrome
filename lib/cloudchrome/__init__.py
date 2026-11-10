"""Cloud-Chrome runtime.

The package is the single implementation of the Cloud-Chrome desktop runtime.
`bin/cloudchrome` is the only entry point and dispatches into
:mod:`cloudchrome.cli`.

Module map:

* :mod:`cloudchrome.errors`      -- exit codes and the shared error type
* :mod:`cloudchrome.config`      -- configuration resolution and precedence
* :mod:`cloudchrome.paths`       -- the on-disk layout and path validation
* :mod:`cloudchrome.logutil`     -- logging, rotation and secret redaction
* :mod:`cloudchrome.locking`     -- advisory file locks
* :mod:`cloudchrome.procs`       -- process tracking, PID files and reaping
* :mod:`cloudchrome.vnc`         -- TigerVNC password files
* :mod:`cloudchrome.keyring`     -- D-Bus and the GNOME Keyring secret service
* :mod:`cloudchrome.desktop`     -- the XFCE session
* :mod:`cloudchrome.chrome`      -- the Chrome launch policy
* :mod:`cloudchrome.web`         -- websockify and the noVNC assets
* :mod:`cloudchrome.supervisor`  -- the service lifecycle supervisor
* :mod:`cloudchrome.archive`     -- untrusted-archive member validation
* :mod:`cloudchrome.manifest`    -- the backup manifest
* :mod:`cloudchrome.backup`      -- encrypted backup, restore and pruning
"""

from .version import __version__

__all__ = ["__version__"]
