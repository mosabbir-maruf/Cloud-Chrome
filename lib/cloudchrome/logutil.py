"""Logging, rotation and secret redaction.

Two rules govern every log line in Cloud-Chrome:

* a secret never reaches a log file, and
* a log file never grows without bound.

The redaction list holds the values that are known to be secret in this
process (currently the keyring unlock passphrase). Anything else that might
look like a secret is never written to a log in the first place: the
supervisor logs identifiers, exit codes and file paths, not payloads.
"""

import os
import time

REDACTED = "[redacted]"

# Values that must never appear in a log line.
_secrets = set()


def register_secret(value):
    """Register *value* so that it is scrubbed from every log line."""
    if value and isinstance(value, str) and len(value) >= 4:
        _secrets.add(value)


def clear_secrets():
    _secrets.clear()


def redact(text):
    """Replace every registered secret in *text* with a placeholder."""
    if not text:
        return text
    for secret in _secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _rotate(path, max_bytes, keep):
    """Keep at most *keep* previous generations of *path*.

    The current file becomes ``.1``, the previous ``.1`` becomes ``.2`` and so
    on; anything older than ``.keep`` is removed.
    """
    if keep < 1:
        try:
            os.unlink(path)
        except OSError:
            pass
        return

    # The oldest generation falls off the end.
    try:
        os.unlink("%s.%d" % (path, keep))
    except OSError:
        pass
    # Shift the remaining generations up by one, newest first.
    for index in range(keep - 1, 0, -1):
        source = "%s.%d" % (path, index)
        target = "%s.%d" % (path, index + 1)
        try:
            if os.path.exists(source):
                os.replace(source, target)
        except OSError:
            break
    try:
        os.replace(path, "%s.1" % path)
    except OSError:
        pass


def append(path, message, max_bytes=5242880, keep=3):
    """Append one timestamped line to *path*, rotating it when it grows."""
    line = "%s %s\n" % (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        redact(str(message)).rstrip("\n"),
    )
    try:
        if max_bytes > 0 and os.path.exists(path):
            if os.path.getsize(path) + len(line) > max_bytes:
                _rotate(path, max_bytes, keep)
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        # Logging must never take the runtime down.
        pass


def read_tail(path, lines=200):
    """Return the last *lines* lines of *path*, with secrets redacted."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return []
    tail = content.splitlines()[-max(lines, 0):]
    return [redact(line) for line in tail]
