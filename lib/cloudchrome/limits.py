"""The resource limits that an untrusted archive is held to.

A backup archive is untrusted input. Without limits, a hostile archive
could exhaust the persistent workspace storage, or fill the memory of the
machine, long before its members are found to be unacceptable.

Every limit has a default that is generous enough for a real profile and
small enough to fail early. A limit can be changed through the configuration
file or the environment, but only to a value that is itself validated: a
negative number, a zero or a value beyond the hard ceiling is refused, so a
misconfiguration cannot remove the protection altogether.
"""

from .errors import InvalidStateError

# The ceiling of the encrypted archive itself.
DEFAULT_MAX_ARCHIVE_BYTES = 2 * 1024 ** 3
# The total size of everything the archive expands to.
DEFAULT_MAX_TOTAL_BYTES = 8 * 1024 ** 3
# The largest single member.
DEFAULT_MAX_MEMBER_BYTES = 2 * 1024 ** 3
# The number of members.
DEFAULT_MAX_MEMBERS = 200000
# The manifest itself.
DEFAULT_MAX_MANIFEST_BYTES = 32 * 1024 ** 2

# The absolute ceilings. A configuration may lower a limit but never raise
# it beyond these, so the protection cannot be configured away.
HARD_MAX_ARCHIVE_BYTES = 64 * 1024 ** 3
HARD_MAX_TOTAL_BYTES = 256 * 1024 ** 3
HARD_MAX_MEMBER_BYTES = 64 * 1024 ** 3
HARD_MAX_MEMBERS = 1000000
HARD_MAX_MANIFEST_BYTES = 256 * 1024 ** 2

# The configuration keys and their defaults and ceilings.
LIMITS = {
    "CLOUDCHROME_MAX_ARCHIVE_BYTES": (
        DEFAULT_MAX_ARCHIVE_BYTES,
        HARD_MAX_ARCHIVE_BYTES,
    ),
    "CLOUDCHROME_MAX_TOTAL_BYTES": (
        DEFAULT_MAX_TOTAL_BYTES,
        HARD_MAX_TOTAL_BYTES,
    ),
    "CLOUDCHROME_MAX_MEMBER_BYTES": (
        DEFAULT_MAX_MEMBER_BYTES,
        HARD_MAX_MEMBER_BYTES,
    ),
    "CLOUDCHROME_MAX_MEMBERS": (
        DEFAULT_MAX_MEMBERS,
        HARD_MAX_MEMBERS,
    ),
    "CLOUDCHROME_MAX_MANIFEST_BYTES": (
        DEFAULT_MAX_MANIFEST_BYTES,
        HARD_MAX_MANIFEST_BYTES,
    ),
}


def _resolve(key, value, default, ceiling):
    """Resolve one limit, refusing an unsafe value."""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidStateError(
            "%s must be a whole number, not %r" % (key, value)
        ) from None
    if number <= 0:
        raise InvalidStateError("%s must be greater than zero, not %d" % (key, number))
    if number > ceiling:
        raise InvalidStateError(
            "%s must not be larger than %d, not %d" % (key, ceiling, number)
        )
    return number


def resolve_limits(values):
    """Resolve every limit from a mapping of configuration values.

    The mapping is the merged configuration, so a limit can come from the
    configuration file or from the environment. A value that is not safe is
    refused rather than silently ignored.
    """
    resolved = {}
    for key, (default, ceiling) in LIMITS.items():
        resolved[key] = _resolve(key, values.get(key), default, ceiling)
    return resolved


class Limits:
    """The limits that one operation is held to."""

    def __init__(self, values=None):
        resolved = resolve_limits(values or {})
        self.max_archive_bytes = resolved["CLOUDCHROME_MAX_ARCHIVE_BYTES"]
        self.max_total_bytes = resolved["CLOUDCHROME_MAX_TOTAL_BYTES"]
        self.max_member_bytes = resolved["CLOUDCHROME_MAX_MEMBER_BYTES"]
        self.max_members = resolved["CLOUDCHROME_MAX_MEMBERS"]
        self.max_manifest_bytes = resolved["CLOUDCHROME_MAX_MANIFEST_BYTES"]

    def describe(self):
        return {
            "archive_bytes": self.max_archive_bytes,
            "total_bytes": self.max_total_bytes,
            "member_bytes": self.max_member_bytes,
            "members": self.max_members,
            "manifest_bytes": self.max_manifest_bytes,
        }
