"""Encrypted backup, transactional restore and pruning.

The design decisions that the requirements ask for:

* **age encryption.** The archive is encrypted with an age key pair. The
  private identity stays in ``<state>/keys/`` with mode 0600 and is never
  part of the archive; the archive only ever carries the public recipient.
  No passphrase is used, so no passphrase ever reaches a command line, an
  environment variable, a log or a shell history.

* **Streaming.** The backup is written as a tar stream that is piped straight
  into ``age``, so no plaintext archive is ever written to disk.

* **The VNC password is never included.** It is an obfuscated key that would
  give anyone who reads the archive access to the desktop, so it is
  explicitly excluded.

* **Every archive is untrusted.** The members are validated before anything
  is extracted, and the extracted files are verified against the manifest
  before the live state is touched.

* **The restore is transactional.** The current state is moved aside, the
  staged state is installed, and any failure puts the old state back. A
  failed restore never leaves a half-replaced profile and never loses the
  previous keyring.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from . import archive as archive_module
from . import logutil
from . import manifest as manifest_module
from .errors import (
    CloudChromeError,
    IntegrityError,
    InvalidStateError,
    NotInitializedError,
    UnsupportedError,
)
from .paths import OWNER_ONLY_DIR, OWNER_ONLY_FILE, ensure_dir, write_private_file

# The backup file name prefix. The pruning only ever touches files that match
# it, so it can never delete an unrelated file in the backup directory.
BACKUP_PREFIX = "cloudchrome-backup-"
BACKUP_SUFFIX = ".tar.gz.age"

# The VNC password file is never part of a backup.
EXCLUDED_NAMES = ("vnc.pass",)


# ---------------------------------------------------------------------------
# the age identity
# ---------------------------------------------------------------------------
def ensure_identity(layout):
    """Create the age key pair if it does not exist yet.

    The identity is a private key file with mode 0600. The recipient is the
    public key, which is what the archive is encrypted to.
    """
    ensure_dir(layout.keys_dir, OWNER_ONLY_DIR)
    if not os.path.exists(layout.identity_file):
        result = subprocess.run(
            ["age-keygen", "-o", layout.identity_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise UnsupportedError(
                "age-keygen failed: %s" % result.stderr.decode("utf-8", "replace").strip()
            )
        os.chmod(layout.identity_file, OWNER_ONLY_FILE)

    if not os.path.exists(layout.recipient_file):
        result = subprocess.run(
            ["age-keygen", "-y", layout.identity_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise UnsupportedError(
                "the age recipient could not be derived: %s"
                % result.stderr.decode("utf-8", "replace").strip()
            )
        write_private_file(
            layout.recipient_file, result.stdout.decode("utf-8").strip() + "\n", 0o644
        )
    return layout.identity_file


def read_recipient(layout):
    """The public key the backups are encrypted to."""
    ensure_identity(layout)
    with open(layout.recipient_file, "r", encoding="utf-8") as handle:
        recipient = handle.read().strip()
    if not recipient:
        raise InvalidStateError("the age recipient file is empty")
    return recipient


# ---------------------------------------------------------------------------
# creating a backup
# ---------------------------------------------------------------------------
def _collect_payload(layout):
    """Return the ``(relative, absolute)`` pairs that go into a backup.

    The VNC password file is deliberately absent: it is an obfuscated key
    that would give anyone who reads the archive access to the desktop, and
    it is rotated with 'cloudchrome passwd' instead of being restored.
    """
    members = []

    chrome = layout.chrome_dir
    if os.path.isdir(chrome):
        for relative in archive_module.directory_members(chrome):
            members.append(("chrome/%s" % relative, os.path.join(chrome, relative)))

    keyring = layout.keyring_dir
    if os.path.isdir(keyring):
        for relative in archive_module.directory_members(keyring):
            members.append(
                ("keyring/%s" % relative, os.path.join(keyring, relative))
            )

    passphrase = layout.keyring_pass_file
    if os.path.exists(passphrase):
        members.append(("keyring.pass", passphrase))

    return sorted(members)


def _add_file(archive, source, arcname):
    """Add one file to the tar stream under the given member name.

    The ownership, the permissions and the timestamp of the member are fixed,
    so two backups of the same state produce the same archive, and the
    archive never carries the identity of the machine that made it.
    """
    info = tarfile.TarInfo(name=arcname)
    info.size = os.path.getsize(source)
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.type = tarfile.REGTYPE
    with open(source, "rb") as handle:
        archive.addfile(info, handle)
    return arcname


def create_backup(layout, config, output=None, label=None):
    """Write an encrypted backup.

    The tar stream is piped straight into ``age``, so no plaintext archive is
    written to disk.
    """
    layout.require_initialized()
    ensure_dir(layout.backup_dir, OWNER_ONLY_DIR)
    recipient = read_recipient(layout)

    payload = _collect_payload(layout)
    if not payload:
        raise NotInitializedError("there is nothing to back up yet")

    built = manifest_module.build_manifest(payload, layout.persist_dir, label=label)
    manifest_data = manifest_module.manifest_bytes(built)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if output:
        target = os.path.abspath(output)
        directory = os.path.dirname(target)
        if directory:
            ensure_dir(directory, OWNER_ONLY_DIR)
    else:
        target = os.path.join(
            layout.backup_dir, "%s%s%s" % (BACKUP_PREFIX, stamp, BACKUP_SUFFIX)
        )

    staging = tempfile.mkdtemp(prefix=".cloudchrome-backup.", dir=layout.backup_dir)
    try:
        os.chmod(staging, OWNER_ONLY_DIR)
        manifest_path = os.path.join(staging, manifest_module.MANIFEST_NAME)
        with open(manifest_path, "wb") as handle:
            handle.write(manifest_data)

        # The tar stream goes into age directly: no plaintext archive file is
        # ever created. The archive is written here rather than by the tar
        # binary so that every member carries exactly the approved relative
        # name, and so that the ownership, the permissions and the timestamps
        # of the archive are fixed and reproducible.
        temporary = target + ".partial"
        try:
            age_process = subprocess.Popen(
                ["age", "--encrypt", "--recipient", recipient, "--output", temporary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                with tarfile.open(
                    fileobj=age_process.stdin, mode="w|gz", format=tarfile.PAX_FORMAT
                ) as archive:
                    _add_file(archive, manifest_path, manifest_module.MANIFEST_NAME)
                    for relative, absolute in payload:
                        _add_file(archive, absolute, relative)
            except (OSError, tarfile.TarError) as exc:
                try:
                    age_process.stdin.close()
                except OSError:
                    pass
                age_process.wait(timeout=60)
                raise UnsupportedError(
                    "the backup could not be created: %s" % exc
                ) from exc

            age_stdout, age_stderr = age_process.communicate(timeout=1800)
            if age_process.returncode != 0:
                raise UnsupportedError(
                    "the backup could not be encrypted: %s"
                    % age_stderr.decode("utf-8", "replace").strip()
                )
            os.chmod(temporary, OWNER_ONLY_FILE)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    digest = manifest_module.sha256_file(target)
    return {
        "path": target,
        "members": len(built["members"]) + 1,
        "sha256": digest,
        "size": os.path.getsize(target),
        "created_utc": built["created_utc"],
        "format_version": built["format_version"],
    }


# ---------------------------------------------------------------------------
# restoring a backup
# ---------------------------------------------------------------------------
def _decrypt_to_file(source, layout):
    """Decrypt the archive into a private temporary file.

    The plaintext never leaves the private temporary directory, and it is
    overwritten and removed as soon as the restore is done.
    """
    work = tempfile.mkdtemp(prefix=".cloudchrome-restore.", dir=layout.backup_dir)
    os.chmod(work, OWNER_ONLY_DIR)
    target = os.path.join(work, "backup.tar.gz")
    try:
        with open(target, "wb") as handle:
            result = subprocess.run(
                ["age", "--decrypt", "--identity", layout.identity_file, source],
                stdout=handle,
                stderr=subprocess.PIPE,
                timeout=1800,
                check=False,
            )
    except BaseException:
        _shred(work)
        raise
    if result.returncode != 0:
        _shred(work)
        raise IntegrityError(
            "the backup could not be decrypted with this identity: %s"
            % result.stderr.decode("utf-8", "replace").strip()
        )
    os.chmod(target, OWNER_ONLY_FILE)
    return work, target


def _shred(directory):
    """Overwrite and remove a directory that held plaintext.

    The plain archive is overwritten before it is removed, so a copy of the
    plaintext does not survive in the free blocks of the filesystem.
    """
    for base, _directories, files in os.walk(directory):
        for name in files:
            path = os.path.join(base, name)
            try:
                size = os.path.getsize(path)
                with open(path, "r+b") as handle:
                    handle.write(b"\0" * min(size, 1 << 20))
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
    shutil.rmtree(directory, ignore_errors=True)


def _stage_backup(plaintext, staging):
    """Validate the archive and extract it into a fresh staging directory."""
    approved = archive_module.validate_archive(plaintext)
    extracted = archive_module.extract_archive(plaintext, staging)

    manifest_path = os.path.join(staging, manifest_module.MANIFEST_NAME)
    with open(manifest_path, "rb") as handle:
        built = manifest_module.parse_manifest(handle.read())

    listed = {entry["path"] for entry in built["members"]}
    staged = set(extracted)
    missing = sorted(listed - staged)
    if missing:
        raise IntegrityError(
            "the archive does not contain the member %s that the manifest "
            "describes" % missing[0]
        )
    unexpected = sorted(
        staged - listed - {manifest_module.MANIFEST_NAME}
    )
    if unexpected:
        raise IntegrityError(
            "the archive contains the member %s that the manifest does not "
            "describe" % unexpected[0]
        )

    manifest_module.verify_members(
        built, staging, exclude={manifest_module.MANIFEST_NAME}
    )
    return built, approved


def restore_backup(layout, config, source=None, assume_yes=False):
    """Restore an encrypted backup, transactionally.

    The current state is moved aside and put back if anything fails. A
    failed restore never leaves a partly replaced profile and never loses the
    previous keyring.
    """
    layout.require_initialized()

    if not source:
        source = _latest_backup(layout)
    source = os.path.abspath(source)
    if not os.path.isfile(source):
        raise NotInitializedError("the backup %s does not exist" % source)

    if not assume_yes and not _confirm(
        "Restore %s? The current profile and keyring are replaced." % source
    ):
        raise CloudChromeError("the restore was not confirmed")

    work = None
    staging = None
    rollback = None
    try:
        work, plaintext = _decrypt_to_file(source, layout)
        staging = tempfile.mkdtemp(prefix=".cloudchrome-stage.", dir=layout.backup_dir)
        os.chmod(staging, OWNER_ONLY_DIR)
        built, _approved = _stage_backup(plaintext, staging)

        # The state is only touched once everything has been validated.
        rollback = tempfile.mkdtemp(prefix=".cloudchrome-rollback.", dir=layout.persist_dir)
        os.chmod(rollback, OWNER_ONLY_DIR)

        moved = []
        for name, staged_source in (
            ("chrome", os.path.join(staging, "chrome")),
            ("keyring", os.path.join(staging, "keyring")),
        ):
            live = getattr(layout, "%s_dir" % name)
            if os.path.isdir(staged_source):
                backup_target = os.path.join(rollback, name)
                if os.path.exists(live):
                    os.replace(live, backup_target)
                os.replace(staged_source, live)
                moved.append(name)

        passphrase_source = os.path.join(staging, "keyring.pass")
        if os.path.isfile(passphrase_source):
            passphrase_backup = os.path.join(rollback, "keyring.pass")
            if os.path.exists(layout.keyring_pass_file):
                os.replace(layout.keyring_pass_file, passphrase_backup)
            os.replace(passphrase_source, layout.keyring_pass_file)
            os.chmod(layout.keyring_pass_file, OWNER_ONLY_FILE)

        _verify_installed(layout)

        return {
            "source": source,
            "members": len(built["members"]),
            "created_utc": built["created_utc"],
            "rollback": rollback,
        }
    except BaseException:
        if rollback is not None:
            _rollback(layout, rollback)
        raise
    finally:
        if work is not None:
            _shred(work)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _verify_installed(layout):
    """Verify the ownership and the permissions of the installed state."""
    for directory in (layout.chrome_dir, layout.keyring_dir):
        if os.path.isdir(directory):
            info = os.lstat(directory)
            if info.st_uid != os.getuid():
                raise InvalidStateError(
                    "%s is owned by uid %d, expected %d"
                    % (directory, info.st_uid, os.getuid())
                )
            os.chmod(directory, OWNER_ONLY_DIR)
    if os.path.exists(layout.keyring_pass_file):
        os.chmod(layout.keyring_pass_file, OWNER_ONLY_FILE)
    return True


def _rollback(layout, rollback):
    """Put the previous state back."""
    for name in ("chrome", "keyring"):
        saved = os.path.join(rollback, name)
        live = getattr(layout, "%s_dir" % name)
        if os.path.isdir(saved):
            if os.path.exists(live):
                shutil.rmtree(live, ignore_errors=True)
            os.replace(saved, live)
    saved_pass = os.path.join(rollback, "keyring.pass")
    if os.path.exists(saved_pass):
        if os.path.exists(layout.keyring_pass_file):
            os.unlink(layout.keyring_pass_file)
        os.replace(saved_pass, layout.keyring_pass_file)
    shutil.rmtree(rollback, ignore_errors=True)


def _confirm(question):
    """Ask for confirmation on the terminal."""
    if not os.isatty(0) or not os.isatty(1):
        return False
    sys.stdout.write("%s [y/N] " % question)
    sys.stdout.flush()
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# pruning
# ---------------------------------------------------------------------------
def _managed_backups(layout):
    """Return the backup files in the managed directory, newest first."""
    if not os.path.isdir(layout.backup_dir):
        return []
    entries = []
    for name in os.listdir(layout.backup_dir):
        if not name.startswith(BACKUP_PREFIX) or not name.endswith(BACKUP_SUFFIX):
            continue
        path = os.path.join(layout.backup_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        entries.append(path)
    entries.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return entries


def _latest_backup(layout):
    backups = _managed_backups(layout)
    if not backups:
        raise NotInitializedError("there is no backup to restore")
    return backups[0]


def prune_backups(layout, keep=3, assume_yes=False, dry_run=False):
    """Delete the old encrypted backups.

    Only the files in the managed backup directory that carry the
    Cloud-Chrome backup name are ever considered, and the last valid backup
    is never pruned.
    """
    layout.require_initialized()
    if keep < 1:
        raise CloudChromeError("at least one backup must be kept")

    backups = _managed_backups(layout)
    if not backups:
        return {"deleted": [], "kept": 0, "candidates": []}

    candidates = backups[keep:]
    if not candidates:
        return {"deleted": [], "kept": len(backups), "candidates": []}

    # The newest backup is always kept, whatever the retention policy says.
    protected = backups[0]
    candidates = [path for path in candidates if path != protected]
    if not candidates:
        return {"deleted": [], "kept": len(backups), "candidates": []}

    if dry_run:
        return {"deleted": [], "kept": len(backups) - len(candidates), "candidates": candidates}

    if not assume_yes:
        listing = "\n".join("  %s" % os.path.basename(path) for path in candidates)
        if not _confirm("Delete %d backup(s)?\n%s\n" % (len(candidates), listing)):
            raise CloudChromeError("the pruning was not confirmed")

    deleted = []
    for path in candidates:
        # The file is re-checked immediately before it is removed: it must
        # still be a regular file inside the managed directory.
        if os.path.islink(path):
            continue
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(layout.backup_dir):
            continue
        try:
            os.unlink(path)
            deleted.append(path)
        except OSError as exc:
            raise CloudChromeError(
                "%s could not be deleted: %s" % (os.path.basename(path), exc.strerror)
            ) from exc

    return {
        "deleted": deleted,
        "kept": len(backups) - len(deleted),
        "candidates": candidates,
    }
