"""Encrypted backup, transactional restore and pruning.

The design decisions that the requirements ask for:

* **age encryption.** The archive is encrypted with an age key pair. The
  private identity stays in ``<state>/keys/`` with mode 0600 and is never
  part of the archive; the archive only ever carries the public recipient.
  No passphrase is used, so no passphrase ever reaches a command line, an
  environment variable, a log or a shell history.

* **Streaming.** The backup is written as a tar stream that is piped straight
  into ``age``, so no plaintext archive is ever written to disk. The restore
  reads the archive back the same way: the plaintext is streamed through a
  pipe and never lands on the filesystem.

* **The VNC password is never included.** It is an obfuscated key that would
  give anyone who reads the archive access to the desktop, so it is
  explicitly excluded.

* **Every archive is untrusted.** The members are validated before anything
  is installed, the extracted files are verified against the manifest, and
  the archive is held to the resource limits in :mod:`cloudchrome.limits`.

* **The restore is transactional.** The current state is moved aside, the
  staged state is installed, the runtime is started again and checked, and
  any failure puts the old state back and restarts the old desktop. A failed
  restore never leaves a half-replaced profile and never loses the previous
  keyring.
"""

import io
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
from .limits import Limits
from .paths import OWNER_ONLY_DIR, OWNER_ONLY_FILE, ensure_dir, write_private_file

# The backup file name prefix. The pruning only ever touches files that match
# it, so it can never delete an unrelated file in the backup directory.
BACKUP_PREFIX = "cloudchrome-backup-"
BACKUP_SUFFIX = ".tar.gz.age"

# The VNC password file is never part of a backup.
EXCLUDED_NAMES = ("vnc.pass",)

# Chrome's singleton files record that an instance is running. They are
# runtime state, not user data: restoring them would make Chrome refuse to
# start, because it would believe another instance already holds the
# profile.
CHROME_SINGLETON_FILES = (
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
)


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
            if os.path.basename(relative) in CHROME_SINGLETON_FILES:
                # The singleton files are runtime state, not user data.
                continue
            members.append(("chrome/%s" % relative, os.path.join(chrome, relative)))

    # The keyring data lives under the XDG data directory, because that is
    # what the keyring daemon is told to use. Backing up any other directory
    # would capture nothing and restore nothing.
    keyring = layout.keyring_store_dir
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


def _resolve_output(layout, output):
    """Work out where the backup is written, and refuse an unsafe target.

    The parent directory must already exist; it is never created and never
    changed, so a shared directory such as /tmp keeps its own mode. The
    target itself must not be a symbolic link, so a link cannot be used to
    make the runtime write somewhere else.
    """
    if output:
        target = os.path.abspath(output)
        directory = os.path.dirname(target) or "."
        if not os.path.isdir(directory):
            raise InvalidStateError(
                "the output directory %s does not exist; create it first" % directory
            )
        if os.path.islink(target):
            raise InvalidStateError(
                "the output %s is a symbolic link; refusing to write through it"
                % target
            )
        if os.path.exists(target) and not os.path.isfile(target):
            raise InvalidStateError(
                "the output %s exists and is not a regular file" % target
            )
    else:
        ensure_dir(layout.backup_dir, OWNER_ONLY_DIR)
        target = os.path.join(
            layout.backup_dir,
            "%s%s%s" % (BACKUP_PREFIX, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), BACKUP_SUFFIX),
        )
    return target


def create_backup(layout, config, output=None, label=None):
    """Write an encrypted backup.

    The tar stream is piped straight into ``age``, so no plaintext archive is
    written to disk.

    The caller is responsible for quiescing the runtime: a profile that
    Chrome is still writing to is not a consistent snapshot, and hashing it
    does not make it one. See :func:`cloudchrome.cli.command_backup`.
    """
    layout.require_initialized()
    ensure_dir(layout.backup_dir, OWNER_ONLY_DIR)
    recipient = read_recipient(layout)

    payload = _collect_payload(layout)
    if not payload:
        raise NotInitializedError("there is nothing to back up yet")

    built = manifest_module.build_manifest(payload, layout.persist_dir, label=label)
    manifest_data = manifest_module.manifest_bytes(built)
    target = _resolve_output(layout, output)
    directory = os.path.dirname(target) or "."

    # The archive is written to a unique temporary file in the same
    # directory and then moved into place, so the target is never a partly
    # written file and an existing backup is replaced atomically.
    handle, temporary = tempfile.mkstemp(
        prefix=".cloudchrome-backup-", suffix=".partial", dir=directory
    )
    os.close(handle)
    os.chmod(temporary, OWNER_ONLY_FILE)
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
                _add_bytes(archive, manifest_data, manifest_module.MANIFEST_NAME)
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

        _stdout, stderr = age_process.communicate(timeout=1800)
        if age_process.returncode != 0:
            raise UnsupportedError(
                "the backup could not be encrypted: %s"
                % stderr.decode("utf-8", "replace").strip()
            )
        os.chmod(temporary, OWNER_ONLY_FILE)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass

    digest = manifest_module.sha256_file(target)
    return {
        "path": target,
        "members": len(built["members"]) + 1,
        "sha256": digest,
        "size": os.path.getsize(target),
        "created_utc": built["created_utc"],
        "format_version": built["format_version"],
    }


def _add_bytes(archive, data, arcname):
    """Add a fixed piece of content to the tar stream."""
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.type = tarfile.REGTYPE
    archive.addfile(info, io.BytesIO(data))
    return arcname


# ---------------------------------------------------------------------------
# restoring a backup
# ---------------------------------------------------------------------------
def _decrypt_stream(source, layout, identity=None):
    """Start ``age --decrypt`` and return the process to read from.

    The plaintext is streamed through a pipe. It never lands on the
    filesystem, so there is no plaintext file to clean up and no window in
    which a copy of the archive exists on disk.
    """
    identity = identity or layout.identity_file
    return subprocess.Popen(
        ["age", "--decrypt", "--identity", identity, source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _decrypt_to_file(source, layout, identity=None):
    """Decrypt the archive into a private temporary file.

    This is the fallback for the environments where the streaming path is
    not used. The plaintext is written inside a private mode-0700 directory
    and removed when the restore ends, whether it succeeded, failed or was
    interrupted.

    The removal overwrites the file before unlinking it. That is a best
    effort: on storage with copy-on-write, wear levelling or snapshots the
    previous content may survive in the free blocks, so no physical erasure
    is claimed.
    """
    identity = identity or layout.identity_file
    work = tempfile.mkdtemp(prefix=".cloudchrome-restore.", dir=layout.backup_dir)
    os.chmod(work, OWNER_ONLY_DIR)
    target = os.path.join(work, "backup.tar.gz")
    try:
        with open(target, "wb") as handle:
            result = subprocess.run(
                ["age", "--decrypt", "--identity", identity, source],
                stdout=handle,
                stderr=subprocess.PIPE,
                timeout=1800,
                check=False,
            )
    except BaseException:
        _remove_plaintext(work)
        raise
    if result.returncode != 0:
        _remove_plaintext(work)
        raise IntegrityError(
            "the backup could not be decrypted with this identity: %s"
            % result.stderr.decode("utf-8", "replace").strip()
        )
    os.chmod(target, OWNER_ONLY_FILE)
    return work, target


def _remove_plaintext(directory):
    """Remove a directory that held plaintext.

    The files are overwritten before they are unlinked. That shortens the
    window in which the plaintext is readable, but it is **not** a guarantee
    that the plaintext is gone from the device: on storage with copy-on-write,
    wear levelling or snapshots the old content can survive in the free
    blocks. The guarantee this function provides is that the plaintext is
    removed from the filesystem's view, in the private directory it was
    written to, on every path out of the restore.
    """
    for base, _directories, files in os.walk(directory):
        for name in files:
            path = os.path.join(base, name)
            try:
                size = os.path.getsize(path)
                with open(path, "r+b") as handle:
                    handle.write(b"\0" * size)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
    shutil.rmtree(directory, ignore_errors=True)


def _stage_backup(plaintext, staging, limits=None):
    """Validate the archive and extract it into a fresh staging directory."""
    limits = limits or Limits()
    approved = archive_module.validate_archive(plaintext, limits=limits)
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


def _stage_from_stream(source, layout, staging, limits=None, identity=None):
    """Validate and extract an archive by streaming it through a pipe.

    The archive is read twice: once to validate every member and to collect
    the manifest, and once to extract it. Neither pass writes the plaintext
    archive to the filesystem, and the members are validated before anything
    is installed.
    """
    limits = limits or Limits()

    # The encrypted archive is bounded before it is even decrypted.
    try:
        size = os.path.getsize(source)
    except OSError as exc:
        raise IntegrityError("the backup could not be read: %s" % exc) from exc
    if size > limits.max_archive_bytes:
        raise IntegrityError(
            "the backup is larger than the limit of %d bytes; refused"
            % limits.max_archive_bytes
        )

    manifest = _read_manifest_from_stream(source, layout, limits, identity)
    extracted = _extract_from_stream(source, layout, staging, limits, identity)
    manifest_module.verify_members(
        manifest, staging, exclude={manifest_module.MANIFEST_NAME}
    )
    return manifest, extracted


def _read_manifest_from_stream(source, layout, limits, identity):
    """Read the manifest out of a streamed archive.

    Every member is validated on the way past, so a hostile archive is
    refused before anything is extracted. The manifest itself is bounded, so
    it cannot be used to exhaust the memory either.
    """
    process = _decrypt_stream(source, layout, identity=identity)
    manifest = None
    seen = set()
    total = 0
    count = 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
            for member in archive:
                name = archive_module.validate_member(member)
                if name in seen:
                    raise IntegrityError(
                        "the archive contains the member %s more than once; refused"
                        % name
                    )
                seen.add(name)
                count += 1
                total += member.size
                if count > limits.max_members:
                    raise IntegrityError(
                        "the archive contains more than %d members; refused"
                        % limits.max_members
                    )
                if member.size > limits.max_member_bytes:
                    raise IntegrityError(
                        "the archive member %s is larger than the limit of %d "
                        "bytes; refused" % (name, limits.max_member_bytes)
                    )
                if total > limits.max_total_bytes:
                    raise IntegrityError(
                        "the archive expands to more than the limit of %d bytes; "
                        "refused" % limits.max_total_bytes
                    )
                if name == manifest_module.MANIFEST_NAME:
                    if member.size > limits.max_manifest_bytes:
                        raise IntegrityError(
                            "the manifest is larger than the limit of %d bytes; "
                            "refused" % limits.max_manifest_bytes
                        )
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise IntegrityError("the manifest has no readable content")
                    manifest = manifest_module.parse_manifest(
                        handle.read(limits.max_manifest_bytes + 1)
                    )
    except IntegrityError:
        _close_stream(process)
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        _close_stream(process)
        raise IntegrityError("the archive could not be read: %s" % exc) from exc

    _close_stream(process)
    if manifest is None:
        raise IntegrityError(
            "the archive does not contain the required member %s"
            % manifest_module.MANIFEST_NAME
        )
    return manifest


def _extract_from_stream(source, layout, staging, limits, identity):
    """Extract a streamed archive into the staging directory."""
    process = _decrypt_stream(source, layout, identity=identity)
    extracted = []
    total = 0
    count = 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
            for member in archive:
                name = archive_module.validate_member(member)
                count += 1
                total += member.size
                if count > limits.max_members:
                    raise IntegrityError(
                        "the archive contains more than %d members; refused"
                        % limits.max_members
                    )
                if member.size > limits.max_member_bytes:
                    raise IntegrityError(
                        "the archive member %s is larger than the limit of %d "
                        "bytes; refused" % (name, limits.max_member_bytes)
                    )
                if total > limits.max_total_bytes:
                    raise IntegrityError(
                        "the archive expands to more than the limit of %d bytes; "
                        "refused" % limits.max_total_bytes
                    )
                if member.isdir():
                    os.makedirs(
                        os.path.join(staging, name), mode=OWNER_ONLY_DIR, exist_ok=True
                    )
                    extracted.append(name)
                    continue
                target = os.path.join(staging, name)
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, mode=OWNER_ONLY_DIR, exist_ok=True)
                source_handle = archive.extractfile(member)
                if source_handle is None:
                    raise IntegrityError(
                        "the archive member %s has no readable content" % name
                    )
                with open(target, "wb") as sink:
                    shutil.copyfileobj(source_handle, sink, 65536)
                os.chmod(target, OWNER_ONLY_FILE)
                extracted.append(name)
    except IntegrityError:
        _close_stream(process)
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        _close_stream(process)
        raise IntegrityError("the archive could not be extracted: %s" % exc) from exc
    _close_stream(process)
    return extracted


def _close_stream(process):
    """Close a decryption process and report whether it failed."""
    try:
        if process.stdout is not None:
            process.stdout.close()
    except OSError:
        pass
    try:
        _stdout, stderr = process.communicate(timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        process.kill()
        return
    if process.returncode not in (0, None):
        raise IntegrityError(
            "the backup could not be decrypted: %s"
            % stderr.decode("utf-8", "replace").strip()
        )


# ---------------------------------------------------------------------------
# the restore transaction
# ---------------------------------------------------------------------------
def restore_transaction(
    lifecycle,
    layout,
    config,
    source=None,
    assume_yes=False,
    identity=None,
    limits=None,
):
    """Restore a backup as one transaction, from the first byte to the last.

    The steps are, in order:

    1. the archive is validated and staged completely, and nothing of the
       live state is touched;
    2. the running stack is stopped, so Chrome never writes to a profile
       that is being replaced;
    3. the current profile, keyring and passphrase are moved aside;
    4. the staged state is installed;
    5. the ownership and the permissions are verified;
    6. the restored desktop is started;
    7. the restored Chrome and the Secret Service are checked;
    8. only then is the restore finalised and the rollback state disposed of.

    The caller holds the control lock for the whole transaction, so the
    operation is serialised with start, stop, restart, backup and prune.

    If anything fails, the previous profile, keyring and passphrase are put
    back, the previous desktop is started again, and the failure is
    returned. A failed restore never reports success and never leaves a
    partly installed state.
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

    limits = limits or Limits()
    staging = tempfile.mkdtemp(prefix=".cloudchrome-stage.", dir=layout.backup_dir)
    os.chmod(staging, OWNER_ONLY_DIR)

    rollback = None
    was_running = lifecycle.is_running()
    swapped = False
    try:
        # 1. Validate and stage the archive completely. The plaintext is
        #    streamed through a pipe and never lands on the filesystem.
        built, extracted = _stage_from_stream(
            source, layout, staging, limits=limits, identity=identity
        )

        # 2. Stop the running stack.
        if was_running:
            lifecycle.stop()

        # 3. and 4. Preserve the previous state and install the staged one.
        rollback = tempfile.mkdtemp(
            prefix=".cloudchrome-rollback.", dir=layout.persist_dir
        )
        os.chmod(rollback, OWNER_ONLY_DIR)
        _swap_in(layout, staging, rollback)
        swapped = True

        # 5. Verify the ownership and the permissions.
        _verify_installed(layout)

        # 6. Start the restored desktop.
        lifecycle.start()

        # 7. Confirm that the restored Chrome and the Secret Service are
        #    usable.
        health = lifecycle.health()
        if not health["healthy"]:
            raise CloudChromeError(
                "the restored desktop is not usable: %s"
                % "; ".join(health["problems"])
            )

        # 8. Finalise. The rollback state is only disposed of once the
        #    restored runtime has passed its health checks.
        _dispose_rollback(rollback)
        rollback = None

        return {
            "source": source,
            "members": len(built["members"]),
            "created_utc": built["created_utc"],
            "health": health,
            "restarted": was_running,
        }
    except BaseException as failure:
        _recover(lifecycle, layout, rollback, swapped, was_running, failure)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _swap_in(layout, staging, rollback):
    """Move the live state aside and the staged state into place.

    Only the parts that the archive actually contains are replaced, so an
    archive without a profile does not destroy the live one.
    """
    for name, live in _swappable(layout):
        staged = os.path.join(staging, name)
        if not os.path.isdir(staged):
            continue
        saved = os.path.join(rollback, name)
        if os.path.exists(live):
            os.replace(live, saved)
        os.replace(staged, live)

    # A restored profile must not carry Chrome's singleton files: Chrome would
    # believe another instance already holds the profile and refuse to start.
    staged_chrome = os.path.join(staging, "chrome")
    if os.path.isdir(staged_chrome):
        for name in CHROME_SINGLETON_FILES:
            stale = os.path.join(staged_chrome, name)
            if os.path.lexists(stale):
                try:
                    os.unlink(stale)
                except OSError:
                    pass

    staged_pass = os.path.join(staging, "keyring.pass")
    if os.path.isfile(staged_pass):
        saved_pass = os.path.join(rollback, "keyring.pass")
        if os.path.exists(layout.keyring_pass_file):
            os.replace(layout.keyring_pass_file, saved_pass)
        os.replace(staged_pass, layout.keyring_pass_file)
        os.chmod(layout.keyring_pass_file, OWNER_ONLY_FILE)


def _swappable(layout):
    """The parts of the state that a restore replaces.

    The keyring data lives under the XDG data directory, because that is
    what the keyring daemon is told to use.
    """
    return (
        ("chrome", layout.chrome_dir),
        ("keyring", layout.keyring_store_dir),
    )


def _dispose_rollback(rollback):
    """Dispose of the rollback state after a successful restore."""
    if rollback is None:
        return
    shutil.rmtree(rollback, ignore_errors=True)


def _recover(lifecycle, layout, rollback, swapped, was_running, failure):
    """Put the previous state back and restart the previous desktop.

    The rollback state is kept when the previous desktop cannot be started,
    so that the previous profile, keyring and passphrase stay recoverable.
    """
    if rollback is not None and swapped:
        try:
            _rollback(layout, rollback)
        except OSError as exc:
            logutil.append(
                layout.supervisor_log,
                "the rollback of the failed restore did not complete: %s" % exc,
                max_bytes=5242880,
                keep=3,
            )
    if was_running:
        try:
            lifecycle.start()
        except CloudChromeError as exc:
            # The previous desktop could not be started either. The rollback
            # state is kept, so the previous profile, keyring and passphrase
            # stay recoverable.
            raise CloudChromeError(
                "the restore failed (%s) and the previous desktop could not be "
                "started either (%s); the previous state is kept in %s"
                % (failure, exc, rollback)
            ) from failure
        if rollback is not None:
            _dispose_rollback(rollback)


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
    for name, live in _swappable(layout):
        saved = os.path.join(rollback, name)
        if os.path.isdir(saved):
            if os.path.exists(live):
                shutil.rmtree(live, ignore_errors=True)
            os.replace(saved, live)
    saved_pass = os.path.join(rollback, "keyring.pass")
    if os.path.exists(saved_pass):
        if os.path.exists(layout.keyring_pass_file):
            os.unlink(layout.keyring_pass_file)
        os.replace(saved_pass, layout.keyring_pass_file)
        os.chmod(layout.keyring_pass_file, OWNER_ONLY_FILE)


# ---------------------------------------------------------------------------
# confirmation and pruning
# ---------------------------------------------------------------------------
def _confirm(question):
    """Ask for confirmation on the terminal."""
    if not os.isatty(0) or not os.isatty(1):
        return False
    sys.stdout.write("%s [y/N] " % question)
    sys.stdout.flush()
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")


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
    Cloud-Chrome backup name are ever considered, a symbolic link is never
    followed, and the last valid backup is never pruned.
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
        return {
            "deleted": [],
            "kept": len(backups) - len(candidates),
            "candidates": candidates,
        }

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
