"""Exit codes and the shared error type.

Every command returns one of the codes below. The codes are part of the CLI
contract and are documented in docs/USAGE.md.
"""

EXIT_OK = 0
"""The command completed successfully."""

EXIT_FAILURE = 1
"""A generic runtime failure. The message says what went wrong."""

EXIT_USAGE = 2
"""The arguments were invalid. The usage message was printed."""

EXIT_NOT_INITIALIZED = 3
"""`cloudchrome init` has not been run, or the state directory is missing."""

EXIT_INVALID_STATE = 4
"""The persisted state is unusable: wrong owner, wrong mode, corrupt or
unsafe. The runtime refuses to continue rather than repairing it silently."""

EXIT_NOT_RUNNING = 5
"""The operation needs a running desktop stack and there is none."""

EXIT_UNSUPPORTED = 6
"""The environment cannot support the operation (missing dependency, missing
secret service, unsupported platform)."""

EXIT_INTEGRITY = 7
"""An archive, manifest or checksum failed validation. Nothing was changed."""

EXIT_BUSY = 8
"""Another operation holds the lock. The command is safe to repeat later."""

EXIT_INTERRUPTED = 130
"""The command was interrupted (SIGINT)."""


class CloudChromeError(Exception):
    """A failure with an exit code and, optionally, an actionable hint."""

    code = EXIT_FAILURE

    def __init__(self, message, code=None, hint=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.hint = hint

    def __str__(self):
        return self.message


class UsageError(CloudChromeError):
    """The arguments are invalid."""

    code = EXIT_USAGE


class NotInitializedError(CloudChromeError):
    """The persistent state has not been created yet."""

    code = EXIT_NOT_INITIALIZED


class InvalidStateError(CloudChromeError):
    """The persistent state exists but cannot be trusted or used."""

    code = EXIT_INVALID_STATE


class NotRunningError(CloudChromeError):
    """The desktop stack is not running."""

    code = EXIT_NOT_RUNNING


class UnsupportedError(CloudChromeError):
    """The environment does not support the requested operation."""

    code = EXIT_UNSUPPORTED


class IntegrityError(CloudChromeError):
    """Validation of an archive, a manifest or a checksum failed."""

    code = EXIT_INTEGRITY


class BusyError(CloudChromeError):
    """Another operation is in progress."""

    code = EXIT_BUSY


class Interrupted(CloudChromeError):
    """The operation was interrupted."""

    code = EXIT_INTERRUPTED
