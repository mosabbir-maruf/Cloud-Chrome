"""Treating an archive as untrusted input.

A backup archive is data that came from somewhere else. It is validated
member by member **before** anything is extracted, and only the members that
pass every check are ever written to disk.

The rules, in the order they are applied:

* the member name must be relative and must not contain a ``..`` component;
* the member must be a regular file or a directory;
* symbolic links, hard links, device nodes, FIFOs and sockets are refused;
* the member must be inside one of the approved prefixes;
* the same path must not appear twice;
* the member must not be a path that already exists in the staging directory.

Nothing here follows a symbolic link, and nothing is ever extracted over the
live profile: the extraction target is always a freshly created, empty
directory with mode 0700.
"""

import os
import stat
import tarfile

from .errors import IntegrityError

# The only directories an archive may contain. Everything else is refused.
APPROVED_PREFIXES = ("chrome/", "keyring/", "backups/meta/")

# The single files an archive may contain at the top level.
APPROVED_FILES = ("keyring.pass", "manifest.json")

# The members that must be present for the archive to be complete.
REQUIRED_MEMBERS = ("manifest.json",)


def _is_within(base, target):
    """True when *target* is inside *base*."""
    base = os.path.abspath(base)
    target = os.path.abspath(target)
    return target == base or target.startswith(base + os.sep)


def validate_member_name(name):
    """Validate the *name* of an archive member.

    Returns the normalised relative path, or raises :class:`IntegrityError`.
    """
    if not name or not name.strip():
        raise IntegrityError("the archive contains a member with an empty name")
    if name.startswith("/") or name.startswith("\\"):
        raise IntegrityError(
            "the archive contains an absolute path (%s); absolute paths are refused"
            % name
        )
    if "\\" in name:
        raise IntegrityError(
            "the archive contains a path with a backslash (%s); refused" % name
        )
    if "\x00" in name:
        raise IntegrityError("the archive contains a path with a null byte")

    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts:
        raise IntegrityError("the archive contains a member with an empty name")
    for part in parts:
        if part == "..":
            raise IntegrityError(
                "the archive contains a path traversal component (%s); refused" % name
            )
    return "/".join(parts)


def validate_member(member):
    """Validate one archive member.

    Returns the normalised relative path of the member, or raises
    :class:`IntegrityError`.
    """
    name = validate_member_name(member.name)

    if member.issym():
        raise IntegrityError(
            "the archive contains a symbolic link (%s); links are refused" % name
        )
    if member.islnk():
        raise IntegrityError(
            "the archive contains a hard link (%s); links are refused" % name
        )
    if member.ischr() or member.isblk():
        raise IntegrityError(
            "the archive contains a device node (%s); device nodes are refused" % name
        )
    if member.isfifo():
        raise IntegrityError(
            "the archive contains a FIFO (%s); FIFOs are refused" % name
        )
    if member.isdev():
        raise IntegrityError(
            "the archive contains a device file (%s); refused" % name
        )
    if not (member.isfile() or member.isdir()):
        raise IntegrityError(
            "the archive contains a member of an unexpected type (%s); refused" % name
        )

    approved = any(name.startswith(prefix) for prefix in APPROVED_PREFIXES)
    approved = approved or name in APPROVED_FILES
    if not approved:
        raise IntegrityError(
            "the archive contains a member outside the approved set (%s); refused"
            % name
        )
    return name


def validate_archive(archive):
    """Validate every member of *archive* before anything is extracted.

    Returns the list of approved members, in the order they appear. Raises
    :class:`IntegrityError` on the first problem.
    """
    seen = set()
    approved = []
    try:
        with tarfile.open(archive, "r:*") as handle:
            for member in handle:
                name = validate_member(member)
                if name in seen:
                    raise IntegrityError(
                        "the archive contains the member %s more than once; refused"
                        % name
                    )
                seen.add(name)
                approved.append(name)
    except IntegrityError:
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise IntegrityError("the archive could not be read: %s" % exc) from exc

    for required in REQUIRED_MEMBERS:
        if required not in seen:
            raise IntegrityError(
                "the archive does not contain the required member %s" % required
            )
    if not approved:
        raise IntegrityError("the archive is empty")
    return approved


def extract_archive(archive, staging_dir):
    """Extract the validated members of *archive* into *staging_dir*.

    The staging directory must be empty: it is created by the caller with
    mode 0700 and is never reused. Only regular files and directories are
    written, the ownership and the permissions of the archive are ignored,
    and a member that would land outside the staging directory is refused.
    """
    os.makedirs(staging_dir, mode=0o700, exist_ok=True)
    if os.listdir(staging_dir):
        raise IntegrityError(
            "the staging directory %s is not empty; refusing to extract into it"
            % staging_dir
        )

    extracted = []
    try:
        with tarfile.open(archive, "r:*") as handle:
            for member in handle:
                name = validate_member(member)
                target = os.path.join(staging_dir, name)
                if not _is_within(staging_dir, target):
                    raise IntegrityError(
                        "the archive member %s would be extracted outside the "
                        "staging directory; refused" % name
                    )
                if os.path.lexists(target):
                    raise IntegrityError(
                        "the archive member %s would overwrite an existing path; "
                        "refused" % name
                    )

                if member.isdir():
                    os.makedirs(target, mode=0o700, exist_ok=True)
                    extracted.append(name)
                    continue

                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, mode=0o700, exist_ok=True)

                source = handle.extractfile(member)
                if source is None:
                    raise IntegrityError(
                        "the archive member %s has no readable content" % name
                    )
                # The content is written with the runtime's own permissions:
                # the archive's ownership and mode are not trusted.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                fd = os.open(target, flags, 0o600)
                try:
                    with os.fdopen(fd, "wb") as sink:
                        while True:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            sink.write(chunk)
                except BaseException:
                    try:
                        os.unlink(target)
                    except OSError:
                        pass
                    raise
                extracted.append(name)
    except IntegrityError:
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise IntegrityError("the archive could not be extracted: %s" % exc) from exc
    return extracted


def directory_members(root):
    """Return the regular files under *root*, as sorted relative paths."""
    members = []
    for base, directories, files in os.walk(root):
        directories.sort()
        for name in sorted(files):
            path = os.path.join(base, name)
            if os.path.islink(path):
                continue
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            members.append(os.path.relpath(path, root))
    return sorted(members)
