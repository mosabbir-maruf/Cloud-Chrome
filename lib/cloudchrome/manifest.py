"""The backup manifest.

The manifest is the description of what a backup contains. It is written
into the archive and it is validated before anything is restored.

It records:

* the format version, so an archive from a future release is refused rather
  than misread;
* the creation time in UTC;
* the application and its version;
* the list of included paths, with their sizes and their SHA-256 digests.

Every digest is verified before the state is replaced.
"""

import hashlib
import json
import os
import time

from .errors import IntegrityError
from .version import BACKUP_FORMAT_VERSION, __version__

MANIFEST_NAME = "manifest.json"


def sha256_file(path, chunk_size=65536):
    """Return the SHA-256 digest of the file at *path*."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def build_manifest(members, root, label=None):
    """Build the manifest for the given members.

    ``members`` is a list of ``(relative_path, absolute_path)`` pairs. Every
    path must be a regular file inside *root*.
    """
    entries = []
    for relative, absolute in sorted(members):
        if os.path.islink(absolute):
            raise IntegrityError(
                "%s is a symbolic link; a backup never follows a link" % relative
            )
        info = os.lstat(absolute)
        if not stat_is_regular(info):
            raise IntegrityError("%s is not a regular file" % relative)
        entries.append(
            {
                "path": relative,
                "size": info.st_size,
                "sha256": sha256_file(absolute),
            }
        )

    manifest = {
        "format": "cloudchrome-backup",
        "format_version": BACKUP_FORMAT_VERSION,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "application": "cloudchrome",
        "application_version": __version__,
        "label": label or "",
        "members": entries,
    }
    return manifest


def stat_is_regular(info):
    import stat as stat_module

    return stat_module.S_ISREG(info.st_mode)


def manifest_bytes(manifest):
    """Serialise the manifest deterministically."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def parse_manifest(data):
    """Parse and validate a manifest.

    Raises :class:`IntegrityError` when the manifest is malformed, has an
    unsupported format version or describes something impossible.
    """
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IntegrityError("the manifest is not valid JSON: %s" % exc) from exc

    if not isinstance(manifest, dict):
        raise IntegrityError("the manifest is not a JSON object")
    if manifest.get("format") != "cloudchrome-backup":
        raise IntegrityError(
            "the manifest is not a Cloud-Chrome backup (format=%r)"
            % manifest.get("format")
        )
    version = manifest.get("format_version")
    if not isinstance(version, int):
        raise IntegrityError("the manifest has no integer format version")
    if version > BACKUP_FORMAT_VERSION:
        raise IntegrityError(
            "the manifest uses format version %d, which this release does not "
            "understand (the newest supported version is %d)"
            % (version, BACKUP_FORMAT_VERSION)
        )
    if version < 1:
        raise IntegrityError(
            "the manifest uses format version %d, which is not valid" % version
        )
    if not manifest.get("created_utc"):
        raise IntegrityError("the manifest has no creation time")
    if not manifest.get("application_version"):
        raise IntegrityError("the manifest does not name the application version")

    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise IntegrityError("the manifest lists no members")

    seen = set()
    for entry in members:
        if not isinstance(entry, dict):
            raise IntegrityError("the manifest contains a malformed member entry")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise IntegrityError("the manifest contains a member with no path")
        if path in seen:
            raise IntegrityError(
                "the manifest lists the member %s more than once" % path
            )
        seen.add(path)
        size = entry.get("size")
        if not isinstance(size, int) or size < 0:
            raise IntegrityError(
                "the manifest member %s has no valid size" % path
            )
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise IntegrityError(
                "the manifest member %s has no valid SHA-256 digest" % path
            )
        try:
            int(digest, 16)
        except ValueError as exc:
            raise IntegrityError(
                "the manifest member %s has a malformed SHA-256 digest" % path
            ) from exc
    return manifest


def verify_members(manifest, root, exclude=None):
    """Verify that the extracted files match the manifest.

    Every listed member must exist, have the recorded size and have the
    recorded digest. A member that is present but not listed is refused
    too, so nothing unaccounted for is installed.

    ``exclude`` names the members that are expected but not listed, such as
    the manifest itself: a file cannot contain its own digest.
    """
    exclude = set(exclude or ())
    listed = {}
    for entry in manifest["members"]:
        listed[entry["path"]] = entry

    found = set()
    for base, _directories, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            relative = os.path.relpath(path, root)
            found.add(relative)

    missing = sorted(set(listed) - found)
    if missing:
        raise IntegrityError(
            "the archive does not contain the member %s that the manifest "
            "describes" % missing[0]
        )

    unexpected = sorted(found - set(listed) - exclude)
    if unexpected:
        raise IntegrityError(
            "the archive contains the member %s that the manifest does not "
            "describe" % unexpected[0]
        )

    for relative, entry in listed.items():
        path = os.path.join(root, relative)
        info = os.lstat(path)
        if info.st_size != entry["size"]:
            raise IntegrityError(
                "the member %s is %d bytes, the manifest says %d"
                % (relative, info.st_size, entry["size"])
            )
        digest = sha256_file(path)
        if digest != entry["sha256"]:
            raise IntegrityError(
                "the member %s does not match its SHA-256 digest in the manifest"
                % relative
            )
    return True
