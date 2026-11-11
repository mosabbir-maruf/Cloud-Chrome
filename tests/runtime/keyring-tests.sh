#!/usr/bin/env bash
# Cloud-Chrome — the keyring startup test.
#
# The keyring is the component that fails in the most obscure ways, so its
# startup is measured on its own: the daemon is started exactly as the
# supervisor starts it, and the secret service is verified.
#
# Usage: bash tests/runtime/keyring-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-keyring.XXXXXX)
export CLOUDCHROME_PERSIST_DIR="$WORK/state/.cloudchrome"
export CLOUDCHROME_RUNTIME_DIR="$CLOUDCHROME_PERSIST_DIR/run"
export HOME="$WORK/home"
mkdir -p "$HOME"

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
    [ -n "${DAEMON_PID:-}" ] && kill "$DAEMON_PID" 2>/dev/null
    [ -n "${BUS_PID:-}" ] && kill "$BUS_PID" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

section "1. The state"
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
print("xdg_data=%s" % layout.xdg_data_home)
print("keyring_store=%s" % layout.keyring_store_dir)
print("xdg_runtime=%s" % layout.xdg_runtime_dir)
PY
sed 's/^/      /' "$WORK/layout.txt"

# ---------------------------------------------------------------------------
section "2. The session bus"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/bus.txt" 2>&1
import os, subprocess, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)

env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home)
bus = subprocess.Popen(
    keyring.dbus_arguments(layout.dbus_socket),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
for _ in range(80):
    if os.path.exists(layout.dbus_socket):
        break
    time.sleep(0.25)
print("socket=%s" % os.path.exists(layout.dbus_socket))
print("address=%s" % env["DBUS_SESSION_BUS_ADDRESS"])
with open("/tmp/cloudchrome-keyring-bus.pid", "w") as handle:
    handle.write(str(bus.pid))
PY
sed 's/^/      /' "$WORK/bus.txt"
BUS_PID=$(cat /tmp/cloudchrome-keyring-bus.pid 2>/dev/null || echo "")

if grep -q "socket=True" "$WORK/bus.txt"; then
    pass "the session bus is listening on its socket"
else
    fail "the session bus is listening on its socket" "$(cat "$WORK/bus.txt")"
fi

# ---------------------------------------------------------------------------
section "3. The keyring daemon"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/daemon.txt" 2>&1
import os, subprocess, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
keyring.create_passphrase_file(layout.keyring_pass_file)
passphrase = keyring.read_passphrase(layout.keyring_pass_file)

env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home)
env["GNOME_KEYRING_CONTROL"] = os.path.join(layout.xdg_runtime_dir, "keyring")

log = "/tmp/cloudchrome-keyring-daemon.log"
daemon = subprocess.Popen(
    keyring.keyring_arguments(),
    stdin=subprocess.PIPE,
    stdout=open(log, "wb"),
    stderr=subprocess.STDOUT,
    env=env)
daemon.stdin.write((passphrase + "\n").encode())
daemon.stdin.flush()
daemon.stdin.close()

with open("/tmp/cloudchrome-keyring-daemon.pid", "w") as handle:
    handle.write(str(daemon.pid))

# The daemon reports the control socket it created on its output.
reported = ""
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("GNOME_KEYRING_CONTROL="):
                    reported = line.strip().split("=", 1)[1]
    except OSError:
        pass
    if reported:
        break
    time.sleep(0.25)
print("reported_control=%s" % (reported or "<none>"))
print("daemon_alive=%s" % (daemon.poll() is None))
PY
sed 's/^/      /' "$WORK/daemon.txt"
DAEMON_PID=$(cat /tmp/cloudchrome-keyring-daemon.pid 2>/dev/null || echo "")

printf -- '\n--- what the daemon reported ---\n'
cat /tmp/cloudchrome-keyring-daemon.log 2>/dev/null | sed 's/^/      /'

# The control socket must exist.
control="$CLOUDCHROME_PERSIST_DIR/run/xdg/keyring"
if [ -S "$control" ]; then
    pass "the keyring created its control socket" "$control"
else
    fail "the keyring created its control socket" "$control is missing"
fi

# The keyring data must land in the persistent directory.
store=$(find "$CLOUDCHROME_PERSIST_DIR/xdg" -name '*.keyring' 2>/dev/null | head -1)
if [ -n "$store" ]; then
    pass "the keyring data is in the persistent directory" "$store"
else
    fail "the keyring data is in the persistent directory" "no keyring file"
fi

# The daemon must own the secret service, and it must be the daemon that
# was started here: gnome-keyring installs a D-Bus service that activates a
# second, locked daemon on the first client call, and that one wins the name
# unless it is displaced.
python3 - <<'PY' > "$WORK/owner.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home)
env["GNOME_KEYRING_CONTROL"] = os.path.join(layout.xdg_runtime_dir, "keyring")
daemon_pid = int(open("/tmp/cloudchrome-keyring-daemon.pid").read().strip())
print("daemon_pid=%d" % daemon_pid)
print("owner_pid=%s" % keyring.secret_service_owner_pid(env))
print("login_keyring=%s" % os.path.exists(layout.keyring_store_file))
PY
sed 's/^/      /' "$WORK/owner.txt"

owner=$(sed -n 's/^owner_pid=//p' "$WORK/owner.txt")
daemon=$(sed -n 's/^daemon_pid=//p' "$WORK/owner.txt")
if [ -n "$owner" ] && [ "$owner" = "$daemon" ]; then
    pass "the daemon that was started owns the secret service" "pid=$owner"
else
    fail "the daemon that was started owns the secret service" "owner=$owner daemon=$daemon"
fi

# ---------------------------------------------------------------------------
section "4. The secret service"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/secret.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home)
env["GNOME_KEYRING_CONTROL"] = os.path.join(layout.xdg_runtime_dir, "keyring")
try:
    result = keyring.verify_secret_service(env)
    print("RESULT=works")
    print("stored=%s retrieved=%s matched=%s"
          % (result["stored"], result["retrieved"], result["matched"]))
except Exception as exc:
    print("RESULT=failed")
    print("REASON=%s" % exc)
PY
sed 's/^/      /' "$WORK/secret.txt"

if grep -q "RESULT=works" "$WORK/secret.txt"; then
    pass "the secret service stores and returns a secret"
else
    fail "the secret service stores and returns a secret" "$(grep REASON "$WORK/secret.txt" | head -1)"
fi

# ---------------------------------------------------------------------------
section "5. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "the keyring expectations hold"
    exit 0
fi
echo "the keyring expectations failed"
exit 1
