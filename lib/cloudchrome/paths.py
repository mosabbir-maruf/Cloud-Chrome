"""The on-disk layout of the persistent state, and path validation.

Everything the runtime writes lives under one persistent directory so that a
Codespaces rebuild keeps it:

    <persist>/
      chrome/          the Chrome user-data directory (persistent profile)
      keyring/         the GNOME Keyring data directory
      keyring.pass     the keyring unlock passphrase            (mode 0600)
      vnc.pass         the TigerVNC password file               (mode 0600)
      logs/            supervisor and component logs
      run/             pid files, locks and the supervisor state
      backups/         age-encrypted backups
      keys/            the age identity and recipient (never archived)

The runtime directory holds volatile state. The functions here create the
directories with restrictive modes and refuse to continue when a path exists
with the wrong type, owner or mode: a path is never trusted because of its
name alone.
"""

import errno
import os
import stat

from .errors import InvalidStateError, NotInitializedError

OWNER_ONLY_DIR = 0o700
OWNER_ONLY_FILE = 0o600


def _stat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise


def is_symlink(path):
    """True when *path* is a symbolic link (a link is never followed here)."""
    info = _stat(path)
    return info is not None and stat.S_ISLNK(info.st_mode)


def ensure_dir(path, mode=OWNER_ONLY_DIR, uid=None, tighten=False):
    """Create *path* if needed and verify that it is a safe private directory.

    A directory that exists with the wrong owner is always refused: the
    runtime does not repair that silently, because repairing it could mean
    overwriting somebody else's directory.

    A directory that exists with the right owner but with group or world
    permissions is refused as well, unless ``tighten`` is set. ``init`` uses
    ``tighten``, because establishing the state is exactly its job; the
    runtime commands do not, because a directory that lost its permissions
    after the setup is a sign that something is wrong.

    A symbolic link is never followed and never accepted.
    """
    uid = os.getuid() if uid is None else uid
    info = _stat(path)
    if info is None:
        try:
            os.makedirs(path, mode=mode, exist_ok=True)
        except OSError as exc:
            raise InvalidStateError(
                "cannot create the directory %s: %s" % (path, exc.strerror)
            ) from exc
        os.chmod(path, mode)
        info = _stat(path)

    if stat.S_ISLNK(info.st_mode):
        raise InvalidStateError("%s is a symbolic link; refusing to use it" % path)
    if not stat.S_ISDIR(info.st_mode):
        raise InvalidStateError("%s exists but is not a directory" % path)
    if info.st_uid != uid:
        raise InvalidStateError(
            "%s is owned by uid %d, expected %d" % (path, info.st_uid, uid)
        )
    if info.st_mode & 0o077:
        if tighten:
            # The directory belongs to the runtime user, so the runtime may
            # restore the permissions it is supposed to have.
            os.chmod(path, mode)
            info = _stat(path)
        else:
            raise InvalidStateError(
                "%s is accessible to other users (mode %o); refusing to use it"
                % (path, stat.S_IMODE(info.st_mode))
            )
    return path


def assert_safe_file(path, expect_mode=None, label=None):
    """Verify that *path* is a regular file owned by the current user.

    ``expect_mode`` additionally pins the permission bits. The check never
    follows a symbolic link.
    """
    label = label or path
    info = _stat(path)
    if info is None:
        raise NotInitializedError("%s does not exist" % label)
    if stat.S_ISLNK(info.st_mode):
        raise InvalidStateError("%s is a symbolic link; refusing to use it" % label)
    if not stat.S_ISREG(info.st_mode):
        raise InvalidStateError("%s is not a regular file" % label)
    if info.st_uid != os.getuid():
        raise InvalidStateError(
            "%s is owned by uid %d, expected %d" % (label, info.st_uid, os.getuid())
        )
    if info.st_mode & 0o077:
        raise InvalidStateError(
            "%s is accessible to other users (mode %o)"
            % (label, stat.S_IMODE(info.st_mode))
        )
    if expect_mode is not None and stat.S_IMODE(info.st_mode) != expect_mode:
        raise InvalidStateError(
            "%s has mode %o, expected %o"
            % (label, stat.S_IMODE(info.st_mode), expect_mode)
        )
    return path


def write_private_file(path, data, mode=OWNER_ONLY_FILE):
    """Write *data* to *path* atomically with restrictive permissions.

    The content is written to a temporary file in the same directory, given
    the final mode, and then renamed over the target, so a reader never sees a
    partially written file and the mode is never briefly too permissive.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory, OWNER_ONLY_DIR)
    tmp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise InvalidStateError("cannot write %s: %s" % (path, exc.strerror)) from exc
    return path


class Layout:
    """The resolved set of paths for one runtime."""

    def __init__(self, config):
        self.config = config
        self.persist_dir = os.path.abspath(config.persist_dir)
        self.runtime_dir = os.path.abspath(config.runtime_dir)

    # --- persistent state ---------------------------------------------------
    @property
    def chrome_dir(self):
        return os.path.join(self.persist_dir, "chrome")

    @property
    def keyring_dir(self):
        return os.path.join(self.persist_dir, self.config.keyring_dir_name)

    @property
    def keyring_pass_file(self):
        return os.path.join(self.persist_dir, "keyring.pass")

    @property
    def vnc_pass_file(self):
        return os.path.join(self.persist_dir, "vnc.pass")

    @property
    def log_dir(self):
        return os.path.join(self.persist_dir, self.config.log_dir_name)

    @property
    def backup_dir(self):
        return os.path.join(self.persist_dir, self.config.backup_dir_name)

    @property
    def keys_dir(self):
        return os.path.join(self.persist_dir, "keys")

    @property
    def identity_file(self):
        return os.path.join(self.keys_dir, "backup-identity.txt")

    @property
    def recipient_file(self):
        return os.path.join(self.keys_dir, "backup-recipient.txt")

    # --- volatile runtime state --------------------------------------------
    @property
    def supervisor_pid_file(self):
        return os.path.join(self.runtime_dir, "supervisor.pid")

    @property
    def supervisor_state_file(self):
        return os.path.join(self.runtime_dir, "supervisor.json")

    @property
    def supervisor_lock_file(self):
        return os.path.join(self.runtime_dir, "supervisor.lock")

    @property
    def control_lock_file(self):
        return os.path.join(self.runtime_dir, "control.lock")

    @property
    def dbus_socket(self):
        return os.path.join(self.runtime_dir, "dbus.sock")

    @property
    def xdg_runtime_dir(self):
        return os.path.join(self.runtime_dir, "xdg")

    @property
    def xdg_data_home(self):
        return os.path.join(self.persist_dir, "xdg")

    @property
    def xdg_cache_home(self):
        return os.path.join(self.persist_dir, "cache")

    @property
    def xdg_config_home(self):
        return os.path.join(self.persist_dir, "config")

    @property
    def keyring_store_dir(self):
        """Where the GNOME Keyring keeps its keyring files."""
        return os.path.join(self.xdg_data_home, "keyrings")

    @property
    def chrome_log(self):
        return os.path.join(self.log_dir, "chrome.log")

    @property
    def supervisor_log(self):
        return os.path.join(self.log_dir, "supervisor.log")

    # --- helpers ------------------------------------------------------------
    def initialize(self):
        """Create the persistent directory tree. Idempotent.

        This is the one place where the permissions of an existing
        directory may be restored: establishing the state is exactly what
        ``init`` is for. Every other command refuses a directory that does
        not have the right permissions.
        """
        ensure_dir(self.persist_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.chrome_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.keyring_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.log_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.backup_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.keys_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.runtime_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.xdg_runtime_dir, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.xdg_data_home, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.xdg_cache_home, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.xdg_config_home, OWNER_ONLY_DIR, tighten=True)
        ensure_dir(self.keyring_store_dir, OWNER_ONLY_DIR, tighten=True)
        return self

    def require_initialized(self):
        """Fail with a clear message when the state does not exist yet.

        The permissions are checked too: a state directory that became
        accessible to other users is not trusted.
        """
        info = _stat(self.persist_dir)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise NotInitializedError(
                "%s does not exist; run 'cloudchrome init' first" % self.persist_dir
            )
        ensure_dir(self.persist_dir, OWNER_ONLY_DIR)
        return self

    def validate_for_start(self):
        """Check that the state can actually be started from.

        The checks run before the supervisor is launched, so a broken state
        is reported immediately with an actionable message instead of after
        the stack has already been brought half up.
        """
        from . import vnc as vnc_policy

        vnc_policy.assert_password_file(self.vnc_pass_file)

        passphrase = self.keyring_pass_file
        if not os.path.exists(passphrase):
            raise NotInitializedError(
                "the keyring passphrase file %s does not exist; run "
                "'cloudchrome init' to create it" % passphrase
            )
        assert_safe_file(passphrase, OWNER_ONLY_FILE, "the keyring passphrase file")

        chrome = self.chrome_dir
        if not os.path.isdir(chrome):
            raise NotInitializedError(
                "the Chrome profile directory %s does not exist; run "
                "'cloudchrome init' to create it" % chrome
            )
        info = _stat(chrome)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise InvalidStateError("%s is not a directory" % chrome)
        if info.st_uid != os.getuid():
            raise InvalidStateError(
                "%s is owned by uid %d, expected %d"
                % (chrome, info.st_uid, os.getuid())
            )
        if not os.access(chrome, os.R_OK | os.W_OK | os.X_OK):
            raise InvalidStateError(
                "%s is not readable and writable by the runtime user" % chrome
            )
        return True
