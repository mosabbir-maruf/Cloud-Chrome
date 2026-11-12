#!/usr/bin/env bash
# Cloud-Chrome — the runtime test suite: the CLI surface, the launch policy,
# the network model, the supervisor's logic, the locks and the secret
# service.
#
# These tests run INSIDE the Cloud-Chrome image. They exercise the CLI, the
# supervisor, the password handling, the Chrome launch policy and the keyring
# integration without needing a desktop to be visible.
#
# Usage: bash tests/runtime/runtime-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-runtime.XXXXXX)
export CLOUDCHROME_PERSIST_DIR="$WORK/state/.cloudchrome"
export CLOUDCHROME_RUNTIME_DIR="$WORK/state/.cloudchrome/run"
export CLOUDCHROME_LOG_DIR="$WORK/state/.cloudchrome/logs"
export CLOUDCHROME_BACKUP_DIR="$WORK/state/.cloudchrome/backups"
export HOME="$WORK/home"
mkdir -p "$HOME" "$CLOUDCHROME_PERSIST_DIR"

# The workspace root is derived from this script's own location.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
export REPO_ROOT
export PYTHONPATH="$REPO_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
CLI="$REPO_ROOT/bin/cloudchrome"

PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf 'PASS  %-58s %s\n' "$1" "${2:-}"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf 'FAIL  %-58s %s\n' "$1" "${2:-}"; }
note() { printf 'NOTE  %-58s %s\n' "$1" "${2:-}"; }
section() { printf '\n########## %s ##########\n' "$1"; }

cleanup() {
    # Never leave a desktop stack behind.
    bash "$CLI" stop >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
section "1. The CLI surface"
# ---------------------------------------------------------------------------
"$CLI" --help > "$WORK/help.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ] && grep -q "cloudchrome" "$WORK/help.txt"; then
    pass "the CLI prints a usage message" "exit 0"
else
    fail "the CLI prints a usage message" "exit $rc"
fi

for command in init start stop restart status logs doctor passwd url version backup restore prune; do
    if "$CLI" --help 2>&1 | grep -qE "^\s+${command}\b"; then
        pass "the CLI documents '$command'"
    else
        fail "the CLI documents '$command'"
    fi
done

"$CLI" version > "$WORK/version.txt" 2>&1
if grep -qE "cloudchrome [0-9]+\.[0-9]+\.[0-9]+" "$WORK/version.txt"; then
    pass "the CLI prints a version" "$(cat "$WORK/version.txt")"
else
    fail "the CLI prints a version" "$(head -1 "$WORK/version.txt")"
fi

"$CLI" > "$WORK/noargs.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "the CLI without a command exits non-zero" "exit $rc"
else
    fail "the CLI without a command exits non-zero" "exit 0"
fi

# ---------------------------------------------------------------------------
section "2. init is silent, idempotent and creates safe state"
# ---------------------------------------------------------------------------
"$CLI" init > "$WORK/init1.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "init succeeds" "exit 0"
else
    fail "init succeeds" "exit $rc: $(head -1 "$WORK/init1.txt")"
fi

if [ ! -s "$WORK/init1.txt" ]; then
    pass "init prints nothing"
else
    fail "init prints nothing" "$(head -1 "$WORK/init1.txt")"
fi

state="$CLOUDCHROME_PERSIST_DIR"
for check_path in "$state" "$state/chrome" "$state/keyring" "$state/logs" "$state/backups"; do
    if [ -d "$check_path" ]; then
        mode=$(stat -c '%a' "$check_path")
        if [ "$mode" = "700" ]; then
            pass "$(basename "$check_path") directory is mode 0700"
        else
            fail "$(basename "$check_path") directory is mode 0700" "mode=$mode"
        fi
    else
        fail "$(basename "$check_path") directory exists"
    fi
done

for secret_file in "$state/keyring.pass" "$state/vnc.pass"; do
    if [ -f "$secret_file" ]; then
        mode=$(stat -c '%a' "$secret_file")
        size=$(stat -c '%s' "$secret_file")
        if [ "$mode" = "600" ]; then
            pass "$(basename "$secret_file") is mode 0600" "size=$size"
        else
            fail "$(basename "$secret_file") is mode 0600" "mode=$mode"
        fi
    else
        fail "$(basename "$secret_file") exists"
    fi
done

vnc_size=$(stat -c '%s' "$state/vnc.pass" 2>/dev/null || echo 0)
if [ "$vnc_size" -eq 8 ]; then
    pass "the VNC password file is an 8-byte obfuscated key" "8 bytes"
else
    fail "the VNC password file is an 8-byte obfuscated key" "$vnc_size bytes"
fi

# Idempotency: a second init must not destroy the existing state.
keyring_before=$(sha256sum "$state/keyring.pass" | cut -d' ' -f1)
vnc_before=$(sha256sum "$state/vnc.pass" | cut -d' ' -f1)
"$CLI" init > "$WORK/init2.txt" 2>&1
keyring_after=$(sha256sum "$state/keyring.pass" | cut -d' ' -f1)
vnc_after=$(sha256sum "$state/vnc.pass" | cut -d' ' -f1)
if [ "$keyring_before" = "$keyring_after" ]; then
    pass "a second init keeps the keyring passphrase"
else
    fail "a second init keeps the keyring passphrase"
fi
if [ "$vnc_before" = "$vnc_after" ]; then
    pass "a second init keeps the VNC password"
else
    fail "a second init keeps the VNC password"
fi

# ---------------------------------------------------------------------------
section "3. Secrets never leak"
# ---------------------------------------------------------------------------
keyring_secret=$(cat "$state/keyring.pass")
if grep -rqF "$keyring_secret" "$state/logs" 2>/dev/null; then
    fail "the keyring passphrase is absent from the logs"
else
    pass "the keyring passphrase is absent from the logs"
fi

# The passphrase must not be in any process arguments either. The search
# reads the passphrase from a file, so the search itself does not put it in
# its own arguments.
printf '%s' "$keyring_secret" > "$WORK/secret.txt"
chmod 0600 "$WORK/secret.txt"
if CLOUDCHROME_TEST_SECRET="$WORK/secret.txt" python3 - <<'PY'
import os, sys

secret = open(os.environ["CLOUDCHROME_TEST_SECRET"], "r", encoding="utf-8").read().strip()
mine = {os.getpid(), os.getppid()}
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    pid = int(entry)
    if pid in mine:
        continue
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            raw = handle.read()
    except OSError:
        continue
    if secret.encode("utf-8") in raw:
        print("the passphrase is in the arguments of pid %d" % pid, file=sys.stderr)
        sys.exit(1)
sys.exit(0)
PY
then
    pass "the keyring passphrase is absent from the process table"
else
    fail "the keyring passphrase is absent from the process table"
fi
rm -f "$WORK/secret.txt"

# status / doctor / logs must not print it.
for command in status doctor logs; do
    "$CLI" "$command" > "$WORK/$command.txt" 2>&1
    if grep -qF "$keyring_secret" "$WORK/$command.txt"; then
        fail "'$command' does not print the keyring passphrase"
    else
        pass "'$command' does not print the keyring passphrase"
    fi
done

# ---------------------------------------------------------------------------
section "4. passwd rotates the VNC password safely"
# ---------------------------------------------------------------------------
vnc_before=$(sha256sum "$state/vnc.pass" | cut -d' ' -f1)
"$CLI" passwd > "$WORK/passwd.txt" 2>&1
rc=$?
vnc_after=$(sha256sum "$state/vnc.pass" | cut -d' ' -f1)
if [ "$rc" -ne 0 ] && [ "$vnc_before" = "$vnc_after" ]; then
    pass "passwd refuses to print without a terminal" "exit $rc, file unchanged"
elif [ "$rc" -eq 0 ] && [ "$vnc_before" != "$vnc_after" ]; then
    pass "passwd rotates the password file" "the file changed"
else
    fail "passwd behaves correctly" "exit $rc, changed=$([ "$vnc_before" = "$vnc_after" ] && echo no || echo yes)"
fi

if [ -s "$WORK/passwd.txt" ]; then
    new_size=$(stat -c '%s' "$state/vnc.pass" 2>/dev/null || echo 0)
    if [ "$new_size" -eq 8 ] 2>/dev/null; then
        pass "the rotated VNC password file is still 8 bytes" "8 bytes"
    else
        fail "the rotated VNC password file is still 8 bytes" "$new_size bytes"
    fi
else
    pass "passwd printed nothing without a terminal"
fi

# ---------------------------------------------------------------------------
section "5. The Chrome launch policy"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/chrome-policy.txt" 2>&1
import os
import sys

sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import chrome as policy
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
arguments = policy.build_arguments(config, layout, config.display)

print("arguments=%s" % " ".join(arguments))
policy.assert_safe_arguments(arguments)
print("assert_safe_arguments=ok")

# A vector that contains a forbidden flag must be refused.
forbidden = [
    ["--no-sandbox"],
    ["--remote-debugging-port=9222"],
    ["--disable-setuid-sandbox"],
    ["--password-store=basic"],
    ["--use-mock-keychain"],
    ["--disable-web-security"],
]
refused = 0
for extra in forbidden:
    try:
        policy.assert_safe_arguments(arguments + extra)
    except Exception:
        refused += 1
print("refused=%d/%d" % (refused, len(forbidden)))

# A vector with the wrong password store must be refused too.
try:
    policy.assert_safe_arguments(["--password-store=basic"])
    print("wrong_store_refused=no")
except Exception:
    print("wrong_store_refused=yes")

# The real vector must carry the persistent profile and libsecret.
print("has_user_data_dir=%s" % any(
    a.startswith("--user-data-dir=") for a in arguments))
print("has_libsecret=%s" % any(
    a == "--password-store=gnome-libsecret" for a in arguments))
print("has_no_sandbox=%s" % any(
    a.split("=")[0] == "--no-sandbox" for a in arguments))
print("has_remote_debugging=%s" % any(
    "remote-debugging" in a for a in arguments))
PY
grep -q "assert_safe_arguments=ok" "$WORK/chrome-policy.txt" \
    && pass "the real Chrome vector passes the policy check" \
    || fail "the real Chrome vector passes the policy check" "$(head -3 "$WORK/chrome-policy.txt")"

refused=$(grep -o "refused=[0-9]*/[0-9]*" "$WORK/chrome-policy.txt" | cut -d= -f2)
if [ "$refused" = "6/6" ]; then
    pass "every forbidden flag is refused" "$refused"
else
    fail "every forbidden flag is refused" "$refused"
fi

grep -q "wrong_store_refused=yes" "$WORK/chrome-policy.txt" \
    && pass "a plaintext password store is refused" \
    || fail "a plaintext password store is refused"

grep -q "has_libsecret=True" "$WORK/chrome-policy.txt" \
    && pass "Chrome uses the libsecret password store" \
    || fail "Chrome uses the libsecret password store"

grep -q "has_no_sandbox=False" "$WORK/chrome-policy.txt" \
    && pass "the Chrome vector has no --no-sandbox" \
    || fail "the Chrome vector has no --no-sandbox"

grep -q "has_remote_debugging=False" "$WORK/chrome-policy.txt" \
    && pass "the Chrome vector has no remote-debugging flag" \
    || fail "the Chrome vector has no remote-debugging flag"

grep -q "has_user_data_dir=True" "$WORK/chrome-policy.txt" \
    && pass "the Chrome vector uses the persistent profile" \
    || fail "the Chrome vector uses the persistent profile"

# ---------------------------------------------------------------------------
section "6. The network model"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/network.txt" 2>&1
import os
import sys

sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import web

# The RFB port must be loopback only, and the noVNC port is the only one
# that is forwarded.
print("rfb=%s" % web.check_rfb_is_loopback_only(5900))
print("novnc_bound=%s" % web.http_port_is_bound(6080))
print("novnc_client=%s" % web.novnc_index_present("/usr/local/share/novnc"))

argv = web.websockify_arguments(6080, 5900, "/usr/local/share/novnc", "0.0.0.0")
print("websockify=%s" % " ".join(argv))
PY

grep -q "novnc_client=True" "$WORK/network.txt" \
    && pass "the noVNC client is installed in the image" \
    || fail "the noVNC client is installed in the image"

grep -q "websockify=websockify --web=/usr/local/share/novnc 0.0.0.0:6080 127.0.0.1:5900" "$WORK/network.txt" \
    && pass "websockify bridges the noVNC port to the loopback RFB port" \
    || fail "websockify bridges the noVNC port to the loopback RFB port" "$(grep websockify= "$WORK/network.txt")"

# The forwarded port must be the only one the dev container declares.
forwarded=$(jq -r '.forwardPorts | @json' "$REPO_ROOT/.devcontainer/devcontainer.json")
if [ "$forwarded" = "[6080]" ]; then
    pass "the dev container forwards only port 6080" "$forwarded"
else
    fail "the dev container forwards only port 6080" "$forwarded"
fi

# ---------------------------------------------------------------------------
section "7. The supervisor's lifecycle logic"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/supervisor.txt" 2>&1
import os
import sys

sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import procs
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
from cloudchrome.supervisor import COMPONENT_ORDER, Supervisor

config = load_config()
layout = Layout(config)

# The shutdown order must be the reverse of the start order.
supervisor = Supervisor(config, layout)
print("order=%s" % ",".join(COMPONENT_ORDER))

# A PID file must not be trusted on the PID alone.
record = procs.read_pid_file(layout.supervisor_pid_file)
print("pidfile_missing=%s" % (record is None))

# A live PID with a wrong start time must not match.
print("mismatch=%s" % procs.pid_matches(os.getpid(), 1))
print("self_match=%s" % procs.pid_matches(os.getpid(), procs.proc_start_time(os.getpid())))

# A PID file written for this process must match afterwards.
procs.write_pid_file(layout.supervisor_pid_file, os.getpid(), "test")
written = procs.read_pid_file(layout.supervisor_pid_file)
print("written=%s" % (written is not None and written["pid"] == os.getpid()))
print("readback_matches=%s" % procs.pid_matches(written["pid"], written["start_time"]))
procs.remove_pid_file(layout.supervisor_pid_file)
PY

grep -q "mismatch=False" "$WORK/supervisor.txt" \
    && pass "a recycled PID does not match a stale PID file" \
    || fail "a recycled PID does not match a stale PID file"

grep -q "self_match=True" "$WORK/supervisor.txt" \
    && pass "a live process matches its own PID file" \
    || fail "a live process matches its own PID file"

grep -q "readback_matches=True" "$WORK/supervisor.txt" \
    && pass "a PID file survives a write/read round trip" \
    || fail "a PID file survives a write/read round trip"

grep -q "pidfile_missing=True" "$WORK/supervisor.txt" \
    && pass "a missing PID file is reported, not guessed" \
    || fail "a missing PID file is reported, not guessed"

# ---------------------------------------------------------------------------
section "8. Concurrency: the locks"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/locks.txt" 2>&1
import os
import sys

sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.locking import FileLock, is_locked
from cloudchrome.paths import Layout
from cloudchrome.config import load_config

config = load_config()
layout = Layout(config)
os.makedirs(layout.runtime_dir, mode=0o700, exist_ok=True)
path = layout.control_lock_file

print("locked_before=%s" % is_locked(path))
lock = FileLock(path, timeout=0)
lock.acquire()
print("locked_while=%s" % is_locked(path))

# A second acquisition must fail rather than block forever.
second = FileLock(path, timeout=0.2)
try:
    second.acquire()
    print("second_acquired=yes")
except Exception as exc:
    print("second_acquired=no (%s)" % type(exc).__name__)
lock.release()
print("locked_after=%s" % is_locked(path))
PY

grep -q "locked_before=False" "$WORK/locks.txt" \
    && pass "the control lock is free before use" \
    || fail "the control lock is free before use"

grep -q "locked_while=True" "$WORK/locks.txt" \
    && pass "the control lock is held while it is in use" \
    || fail "the control lock is held while it is in use"

grep -q "second_acquired=no" "$WORK/locks.txt" \
    && pass "a concurrent operation cannot take the lock" \
    || fail "a concurrent operation cannot take the lock"

grep -q "locked_after=False" "$WORK/locks.txt" \
    && pass "the control lock is released again" \
    || fail "the control lock is released again"

# ---------------------------------------------------------------------------
section "9. The secret service"
# ---------------------------------------------------------------------------
if command -v secret-tool > /dev/null 2>&1; then
    note "secret-tool is present" "$(command -v secret-tool)"
else
    fail "secret-tool is present"
fi

# ---------------------------------------------------------------------------
section "10. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "all the runtime expectations hold"
    exit 0
fi
echo "the runtime expectations failed"
exit 1
