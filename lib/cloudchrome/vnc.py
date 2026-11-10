"""The TigerVNC password file and the VNC server.

TigerVNC stores the password as an **8-byte obfuscated key**, not as encrypted
storage: the key is a DES key derived from the password, and anyone who can
read the file can mount an offline attack on it. Cloud-Chrome never describes
this file as encrypted.

The file is written by ``tigervncpasswd`` itself so that the tool owns the
mode, it is replaced atomically, and it is mode 0600 inside the persistent
state directory.
"""

import os
import shlex
import shutil
import subprocess

from . import logutil
from .errors import InvalidStateError, UnsupportedError
from .paths import OWNER_ONLY_FILE, assert_safe_file, write_private_file

# TigerVNC's password file is exactly 8 bytes.
VNC_PASSWORD_FILE_SIZE = 8

# The file name TigerVNC expects for a VncAuth password file.
VNC_PASSWORD_FILENAME = "vnc.pass"


def _generate_password(length=16):
    """Return a random, human-typeable password."""
    import base64
    import os as _os

    raw = _os.urandom(length)
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def write_password_file(path, password):
    """Write the TigerVNC password file for *password*.

    ``tigervncpasswd`` is used so that the key is derived by the same tool
    that reads it. The password is never passed on a command line and never
    held in a shell variable: it goes to the tool through a pipe.

    Two mechanisms are used, in this order:

    1. the documented filter mode (``tigervncpasswd -f``), which reads the
       plain-text password from standard input and writes the obfuscated key
       to standard output;
    2. a pseudo-terminal, for the case where the filter mode is unavailable.
       ``tigervncpasswd`` then prompts for the password twice, and the
       pseudo-terminal supplies it.

    Either way the result is the same 8-byte obfuscated key that
    ``Xtigervnc -rfbauth`` expects.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)

    raw_path = path + ".raw"
    made = False

    # 1. Filter mode: the password goes in on stdin, the key comes out on
    #    stdout. The key is binary, so it is written straight to a file and
    #    never held in a shell variable. A single line is sent, so the output
    #    is the normal password key alone.
    try:
        with open(raw_path, "wb") as handle:
            subprocess.run(
                ["tigervncpasswd", "-f"],
                input=(password + "\n").encode("utf-8"),
                stdout=handle,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
    except (OSError, subprocess.SubprocessError):
        made = False

    if os.path.exists(raw_path):
        with open(raw_path, "rb") as source:
            key = source.read(VNC_PASSWORD_FILE_SIZE)
        os.unlink(raw_path)
        if len(key) == VNC_PASSWORD_FILE_SIZE:
            with open(path, "wb") as target:
                target.write(key)
            os.chmod(path, OWNER_ONLY_FILE)
            made = True

    # 2. Fallback: tigervncpasswd prompts on the terminal, so a
    #    pseudo-terminal is provided. Its output echoes the password back and
    #    is therefore discarded.
    if not made and shutil.which("script"):
        try:
            with open(os.devnull, "wb") as devnull:
                subprocess.run(
                    [
                        "script",
                        "-qec",
                        "tigervncpasswd %s" % shlex.quote(path),
                        "/dev/null",
                    ],
                    input=("%s\n%s\n" % (password, password)).encode("utf-8"),
                    stdout=devnull,
                    stderr=devnull,
                    check=False,
                    timeout=30,
                )
            made = os.path.exists(path) and os.path.getsize(path) == VNC_PASSWORD_FILE_SIZE
        except (OSError, subprocess.SubprocessError):
            made = False

    if not made or not os.path.exists(path):
        try:
            os.unlink(raw_path)
        except OSError:
            pass
        raise UnsupportedError(
            "tigervncpasswd could not create a valid VNC password file"
        )

    os.chmod(path, OWNER_ONLY_FILE)
    if os.path.getsize(path) != VNC_PASSWORD_FILE_SIZE:
        raise InvalidStateError(
            "the VNC password file is %d bytes, expected %d"
            % (os.path.getsize(path), VNC_PASSWORD_FILE_SIZE)
        )
    return path


def create_password_file(path, password=None):
    """Create the VNC password file if it does not exist yet."""
    if os.path.exists(path):
        assert_password_file(path)
        return path
    return write_password_file(path, password or _generate_password())


def assert_password_file(path):
    """Verify that the VNC password file is usable.

    The file must exist, be a regular file owned by the runtime user, have
    mode 0600 and hold exactly the 8-byte obfuscated key that TigerVNC
    expects. A broken file is refused, never repaired: replacing it would
    silently change the password the user knows.
    """
    assert_safe_file(path, OWNER_ONLY_FILE, "the VNC password file")
    size = os.path.getsize(path)
    if size != VNC_PASSWORD_FILE_SIZE:
        raise InvalidStateError(
            "the VNC password file is %d bytes, expected %d; run "
            "'cloudchrome passwd' to replace it" % (size, VNC_PASSWORD_FILE_SIZE)
        )
    return path


def rotate_password_file(path, password=None):
    """Replace the VNC password file atomically.

    The replacement is atomic, so the server never reads a half-written file
    and the sessions that are already authenticated stay authenticated: the
    server consults the file when a new connection arrives.

    The password is returned so that the caller can show it once. It is never
    written to a log and never passed on a command line.
    """
    password = password or _generate_password()
    directory = os.path.dirname(os.path.abspath(path))
    staged = os.path.join(directory, ".%s.new" % os.path.basename(path))
    try:
        write_password_file(staged, password)
        os.replace(staged, path)
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
    os.chmod(path, OWNER_ONLY_FILE)
    return path, password


def password_file_status(path):
    """Describe the VNC password file without revealing anything."""
    if not os.path.exists(path):
        return {"present": False}
    info = os.stat(path)
    return {
        "present": True,
        "size": info.st_size,
        "mode": oct(info.st_mode & 0o777),
        "owner_uid": info.st_uid,
        "expected_size": VNC_PASSWORD_FILE_SIZE,
        "valid": info.st_size == VNC_PASSWORD_FILE_SIZE,
    }


def vnc_arguments(display, geometry, depth, port, password_file):
    """The argument vector of the X/VNC server.

    ``-localhost`` restricts the RFB port to the loopback interface, so the
    raw VNC protocol is never reachable from outside the container. The
    forwarded port is the noVNC/WebSocket one.
    """
    return [
        "Xtigervnc",
        display,
        "-geometry",
        geometry,
        "-depth",
        str(depth),
        "-rfbport",
        str(port),
        "-localhost",
        "-SecurityTypes",
        "VncAuth",
        "-rfbauth",
        password_file,
        "-AlwaysShared",
    ]


def write_password_to_stdout_free(fileobj, password):
    """Write *password* to *fileobj* without any shell involvement."""
    fileobj.write(("%s\n%s\n" % (password, password)).encode("utf-8"))
    fileobj.flush()
