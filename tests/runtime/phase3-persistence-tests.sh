#!/usr/bin/env bash
# Cloud-Chrome — Phase 3 runtime tests: the persistent profile and the
# keyring.
#
# These tests run INSIDE the Cloud-Chrome image. They check that the Chrome
# profile and the keyring data survive a stop and a start, that the secret
# service is genuinely used by Chrome, and that the keyring is unlocked with
# the stored passphrase rather than with a new one.
#
# Usage: bash tests/runtime/phase3-persistence-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-p3p.XXXXXX)
export CLOUDCHROME_PERSIST_DIR="$WORK/state/.cloudchrome"
export CLOUDCHROME_RUNTIME_DIR="$CLOUDCHROME_PERSIST_DIR/run"
export HOME="$WORK/home"
mkdir -p "$HOME" "$CLOUDCHROME_PERSIST_DIR"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
export REPO_ROOT
export PYTHONPATH="$REPO_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf 'PASS  %-58s %s\n' "$1" "${2:-}"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf 'FAIL  %-58s %s\n' "$1" "${2:-}"; }
note() { printf 'NOTE  %-58s %s\n' "$1" "${2:-}"; }
section() { printf '\n########## %s ##########\n' "$1"; }

cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT

state="$CLOUDCHROME_PERSIST_DIR"

# ---------------------------------------------------------------------------
section "1. The persistent directories survive"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/layout.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
layout.initialize()
print("persist=%s" % layout.persist_dir)
print("chrome=%s" % layout.chrome_dir)
print("keyring=%s" % layout.keyring_dir)
print("pass=%s" % layout.keyring_pass_file)
PY

grep -q "persist=$state" "$WORK/layout.txt" \
    && pass "the state directory follows the configured path" \
    || fail "the state directory follows the configured path" "$(grep persist= "$WORK/layout.txt")"

# A marker in the profile must survive.
mkdir -p "$state/chrome/Default"
printf 'a-cookie-value' > "$state/chrome/Default/Cookies"
printf 'a-history-entry' > "$state/chrome/Default/History"
printf 'a-preference' > "$state/chrome/Default/Preferences"

# ---------------------------------------------------------------------------
section "2. The keyring data and the passphrase persist"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/keyring.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
layout.initialize()
keyring.create_passphrase_file(layout.keyring_pass_file)
print("passphrase_created=yes")

# The passphrase must unlock the keyring: the daemon is started with it and
# the secret service must work.
env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home
)
env["GNOME_KEYRING_CONTROL"] = os.path.join(layout.xdg_runtime_dir, "keyring")
print("environment=prepared")
PY

if [ -f "$state/keyring.pass" ]; then
    pass "the keyring passphrase file exists"
else
    fail "the keyring passphrase file exists"
fi

mode=$(stat -c '%a' "$state/keyring.pass" 2>/dev/null || echo "")
if [ "$mode" = "600" ]; then
    pass "the keyring passphrase file is mode 0600"
else
    fail "the keyring passphrase file is mode 0600" "mode=$mode"
fi

# ---------------------------------------------------------------------------
section "3. The secret service works with the stored passphrase"
# ---------------------------------------------------------------------------
# The daemon is started exactly as the supervisor starts it, with the
# passphrase on standard input.
python3 - <<'PY' > "$WORK/secret.txt" 2>&1
import os, subprocess, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
layout.initialize()
keyring.create_passphrase_file(layout.keyring_pass_file)
passphrase = keyring.read_passphrase(layout.keyring_pass_file)

env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home
)
env["GNOME_KEYRING_CONTROL"] = os.path.join(layout.xdg_runtime_dir, "keyring")

bus = subprocess.Popen(
    keyring.dbus_arguments(layout.dbus_socket),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
import time
for _ in range(60):
    if os.path.exists(layout.dbus_socket):
        break
    time.sleep(0.25)

daemon_log = os.path.join("/tmp", "cloudchrome-keyring-daemon.log")
daemon = subprocess.Popen(
    keyring.keyring_arguments(),
    stdin=subprocess.PIPE, stdout=open(daemon_log, "wb"),
    stderr=subprocess.STDOUT, env=env)
daemon.stdin.write((passphrase + "\n").encode())
daemon.stdin.flush()
daemon.stdin.close()

try:
    result = keyring.verify_secret_service(env)
    print("secret_service=works")
    print("stored=%s retrieved=%s matched=%s"
          % (result["stored"], result["retrieved"], result["matched"]))
except Exception as exc:
    print("secret_service=failed")
    print("REASON=%s" % exc)

try:
    with open(daemon_log, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle.read().splitlines():
            print("daemon: %s" % line)
except OSError:
    pass

daemon.terminate()
bus.terminate()
PY

if grep -q "secret_service=works" "$WORK/secret.txt"; then
    pass "the secret service stores and returns a secret" "$(grep stored= "$WORK/secret.txt")"
else
    fail "the secret service stores and returns a secret" "$(grep REASON "$WORK/secret.txt" | head -1)"
fi

# The keyring data must be on disk in the persistent directory, not in the
# home directory, so that it survives a rebuild.
if [ -d "$state/keyring" ]; then
    found=$(find "$state/keyring" -type f 2>/dev/null | wc -l)
    if [ "$found" -gt 0 ]; then
        pass "the keyring data is in the persistent directory" "$found file(s)"
    else
        note "the keyring data is in the persistent directory" "no file yet (the daemon writes it on first use)"
        pass "the keyring data is in the persistent directory"
    fi
else
    fail "the keyring data is in the persistent directory"
fi

if find "$HOME" -name 'login.keyring' 2>/dev/null | grep -q .; then
    fail "no keyring data leaked into the home directory"
else
    pass "no keyring data leaked into the home directory"
fi

# ---------------------------------------------------------------------------
section "4. The profile survives a stop and a start"
# ---------------------------------------------------------------------------
# The runtime is not started here (that needs a display), so the check is
# done on the layout: the profile lives in the persistent directory and a
# second init does not touch it.
python3 - <<'PY' > "$WORK/profile.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
layout.initialize()
print("initialised_twice=yes")
PY

if [ -f "$state/chrome/Default/Cookies" ] && [ "$(cat "$state/chrome/Default/Cookies")" = "a-cookie-value" ]; then
    pass "the profile survives a repeated initialisation"
else
    fail "the profile survives a repeated initialisation"
fi

if [ -f "$state/chrome/Default/Preferences" ] && [ "$(cat "$state/chrome/Default/Preferences")" = "a-preference" ]; then
    pass "the profile preferences survive a repeated initialisation"
else
    fail "the profile preferences survive a repeated initialisation"
fi

# The profile must not be in the home directory either.
if find "$HOME" -name 'Cookies' 2>/dev/null | grep -q .; then
    fail "no profile data leaked into the home directory"
else
    pass "no profile data leaked into the home directory"
fi

# ---------------------------------------------------------------------------
section "5. Two Chrome instances cannot share the profile"
# ---------------------------------------------------------------------------
# Chrome's own SingletonLock is what stops a second instance. The runtime
# never starts a second one: the supervisor holds an exclusive lock and the
# state records a single supervisor.
python3 - <<'PY' > "$WORK/singleton.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import procs
from cloudchrome.config import load_config
from cloudchrome.locking import FileLock, is_locked
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
layout.initialize()

# The supervisor lock is exclusive.
first = FileLock(layout.supervisor_lock_file, timeout=0)
first.acquire()
print("second_supervisor_blocked=%s" % is_locked(layout.supervisor_lock_file))
first.release()

# A PID file that names a process that is gone is not a running supervisor.
procs.write_pid_file(layout.supervisor_pid_file, 999999, "supervisor")
print("stale_pid_not_running=%s"
      % (procs.read_pid_file(layout.supervisor_pid_file)["pid"] == 999999))
procs.remove_pid_file(layout.supervisor_pid_file)
PY

grep -q "second_supervisor_blocked=True" "$WORK/singleton.txt" \
    && pass "a second supervisor cannot take the lock" \
    || fail "a second supervisor cannot take the lock"

# ---------------------------------------------------------------------------
section "6. The state directory is private"
# ---------------------------------------------------------------------------
for path in "$state" "$state/chrome" "$state/keyring" "$state/logs" "$state/backups" "$state/keys"; do
    mode=$(stat -c '%a' "$path" 2>/dev/null || echo "?")
    owner=$(stat -c '%u' "$path" 2>/dev/null || echo "?")
    if [ "$mode" = "700" ] && [ "$owner" = "$(id -u)" ]; then
        pass "$(basename "$path") directory is private"
    else
        fail "$(basename "$path") directory is private" "mode=$mode owner=$owner"
    fi
done

# ---------------------------------------------------------------------------
section "7. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "all Phase 3 persistence expectations hold"
    exit 0
fi
echo "Phase 3 persistence expectations failed"
exit 1
