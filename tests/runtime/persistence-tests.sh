#!/usr/bin/env bash
# Cloud-Chrome — the persistent state integration test.
#
# This test runs INSIDE the Cloud-Chrome image and starts the COMPLETE
# desktop stack: the X/VNC server, the D-Bus session bus, the GNOME
# Keyring, the XFCE session and window manager, the noVNC bridge and
# Chrome.
#
# It writes a synthetic profile marker and stores a synthetic item in the
# Secret Service, stops the stack, starts it again with the same
# persistent state directory, and verifies that both survived. It is the
# proof that the state is persistent; a repeated initialisation is not.
#
# Nothing of a real browser is used. The profile marker is a file this
# test writes, and the secret is a random value this test generates.
#
# Usage: bash tests/runtime/persistence-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-persist.XXXXXX)
export CLOUDCHROME_PERSIST_DIR="$WORK/state/.cloudchrome"
export CLOUDCHROME_RUNTIME_DIR="$CLOUDCHROME_PERSIST_DIR/run"
export HOME="$WORK/home"
export CLOUDCHROME_PERSIST_SECRET_FILE="$WORK/secret.txt"
mkdir -p "$HOME"

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

# The stack is stopped before the state is removed, on every way out.
cleanup() {
    bash "$CLI" stop >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

state="$CLOUDCHROME_PERSIST_DIR"
MARKER="$state/chrome/Default/CloudChromePersistenceMarker"

# Wait until the desktop reports itself healthy, using the runtime's own
# health check.
wait_until_healthy() {
    python3 - <<'PY'
import os, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.lifecycle import Lifecycle
from cloudchrome.paths import Layout

config = load_config()
lifecycle = Lifecycle(config, Layout(config))
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    try:
        if lifecycle.health().get("healthy"):
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.2)
sys.exit(1)
PY
}

# ---------------------------------------------------------------------------
section "1. The state is created"
# ---------------------------------------------------------------------------
"$CLI" init >/dev/null 2>&1
if [ $? -eq 0 ]; then
    pass "init succeeds"
else
    fail "init succeeds"
fi

# ---------------------------------------------------------------------------
section "2. The desktop stack starts"
# ---------------------------------------------------------------------------
"$CLI" start > "$WORK/start.txt" 2>&1
start_rc=$?
if [ "$start_rc" -eq 0 ] && wait_until_healthy; then
    pass "the stack starts and is healthy" "$(head -1 "$WORK/start.txt")"
else
    fail "the stack starts and is healthy" "$(head -3 "$WORK/start.txt")"
    printf -- '\n--- the component logs ---\n'
    for log in "$state"/logs/*.log; do
        [ -e "$log" ] || continue
        printf '\n=== %s ===\n' "$(basename "$log")"
        tail -15 "$log" | sed 's/^/      /'
    done
    echo "the desktop did not come up: the state cannot be measured" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
section "3. The state is written"
# ---------------------------------------------------------------------------
# A marker in the profile. It is a file this test writes, and Chrome does
# not manage it.
mkdir -p "$(dirname "$MARKER")"
printf 'a-synthetic-marker' > "$MARKER"
if [ -f "$MARKER" ]; then
    pass "the synthetic profile marker is written"
else
    fail "the synthetic profile marker is written"
fi

# A synthetic item in the Secret Service. The secret is a random value
# that is generated here and that is kept in the work directory, which is
# removed on exit.
python3 - <<'PY' > "$WORK/store.txt" 2>&1
import json, os, secrets, subprocess, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)

state = {}
try:
    with open(layout.supervisor_state_file, encoding="utf-8") as handle:
        state = json.load(handle)
except Exception as exc:
    print("REASON=the supervisor state is unreadable: %s" % exc)
    raise SystemExit(1)

env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home
)
control = state.get("keyring_control")
if control:
    env["GNOME_KEYRING_CONTROL"] = control

pairs = ["application", "cloudchrome-persistence-test"]
value = secrets.token_hex(16)

store = subprocess.run(
    ["secret-tool", "store", "--label=Cloud-Chrome persistence test"] + pairs,
    input=value.encode("utf-8"),
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=60,
    check=False,
)
print("stored=%s" % (store.returncode == 0))
if store.returncode != 0:
    print("REASON=%s" % store.stderr.decode("utf-8", "replace").strip())
    raise SystemExit(1)

with open(os.environ["CLOUDCHROME_PERSIST_SECRET_FILE"], "w", encoding="utf-8") as handle:
    handle.write(value)
os.chmod(os.environ["CLOUDCHROME_PERSIST_SECRET_FILE"], 0o600)
print("value_written=yes")
print("store=%s" % layout.keyring_store_dir)
PY

if grep -q "stored=True" "$WORK/store.txt"; then
    pass "the secret service stores the synthetic item"
else
    fail "the secret service stores the synthetic item" \
        "$(grep REASON "$WORK/store.txt" | head -1)"
fi

# The item is read back straight away, so that a failure after the restart
# is about the persistence and not about the storage.
python3 - <<'PY' > "$WORK/lookup-now.txt" 2>&1
import json, os, subprocess, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

layout = Layout(load_config())
state = {}
with open(layout.supervisor_state_file, encoding="utf-8") as handle:
    state = json.load(handle)

env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home
)
control = state.get("keyring_control")
if control:
    env["GNOME_KEYRING_CONTROL"] = control

with open(os.environ["CLOUDCHROME_PERSIST_SECRET_FILE"], encoding="utf-8") as handle:
    expected = handle.read()

pairs = ["application", "cloudchrome-persistence-test"]
lookup = subprocess.run(
    ["secret-tool", "lookup"] + pairs,
    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
)
returned = lookup.stdout.decode("utf-8", "replace").strip()
print("retrieved=%s" % (returned == expected))
PY

if grep -q "retrieved=True" "$WORK/lookup-now.txt"; then
    pass "the secret service returns the synthetic item"
else
    fail "the secret service returns the synthetic item" \
        "$(cat "$WORK/lookup-now.txt" | tr '\n' ' ')"
fi

# ---------------------------------------------------------------------------
section "4. The desktop stack stops"
# ---------------------------------------------------------------------------
"$CLI" stop > "$WORK/stop.txt" 2>&1
stop_rc=$?
if [ "$stop_rc" -eq 0 ]; then
    pass "the stack stops"
else
    fail "the stack stops" "$(head -3 "$WORK/stop.txt")"
fi

if bash "$CLI" status 2>/dev/null | grep -qE "^stack: *running"; then
    fail "the stack is stopped"
else
    pass "the stack is stopped"
fi

# ---------------------------------------------------------------------------
section "5. The desktop stack starts again"
# ---------------------------------------------------------------------------
# The same persistent state directory: nothing is moved and nothing is
# copied. The state has to be there on its own.
"$CLI" start > "$WORK/restart.txt" 2>&1
restart_rc=$?
if [ "$restart_rc" -eq 0 ] && wait_until_healthy; then
    pass "the stack starts again and is healthy" "$(head -1 "$WORK/restart.txt")"
else
    fail "the stack starts again and is healthy" "$(head -3 "$WORK/restart.txt")"
    printf -- '\n--- the component logs ---\n'
    for log in "$state"/logs/*.log; do
        [ -e "$log" ] || continue
        printf '\n=== %s ===\n' "$(basename "$log")"
        tail -15 "$log" | sed 's/^/      /'
    done
fi

# ---------------------------------------------------------------------------
section "6. The state survived the stop and the start"
# ---------------------------------------------------------------------------
# The profile marker.
if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "a-synthetic-marker" ]; then
    pass "the profile marker survived the stop and the start"
else
    fail "the profile marker survived the stop and the start" \
        "$(cat "$MARKER" 2>/dev/null)"
fi

# The Secret Service item.
python3 - <<'PY' > "$WORK/lookup.txt" 2>&1
import json, os, subprocess, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)

state = {}
try:
    with open(layout.supervisor_state_file, encoding="utf-8") as handle:
        state = json.load(handle)
except Exception as exc:
    print("REASON=the supervisor state is unreadable: %s" % exc)
    raise SystemExit(1)

env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home
)
control = state.get("keyring_control")
if control:
    env["GNOME_KEYRING_CONTROL"] = control

with open(os.environ["CLOUDCHROME_PERSIST_SECRET_FILE"], encoding="utf-8") as handle:
    expected = handle.read()

pairs = ["application", "cloudchrome-persistence-test"]
lookup = subprocess.run(
    ["secret-tool", "lookup"] + pairs,
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=60,
    check=False,
)
returned = lookup.stdout.decode("utf-8", "replace").strip()
print("retrieved=%s" % (returned == expected))
if returned != expected:
    print("REASON=%s" % lookup.stderr.decode("utf-8", "replace").strip()[:160])
print("store=%s" % layout.keyring_store_dir)
PY

if grep -q "retrieved=True" "$WORK/lookup.txt"; then
    pass "the secret service returns the same item after the restart"
else
    fail "the secret service returns the same item after the restart" \
        "$(grep REASON "$WORK/lookup.txt" | head -1)"
fi

# The keyring store has to be the directory the daemon uses.
python3 - <<'PY' > "$WORK/store-location.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

layout = Layout(load_config())
print("store=%s" % layout.keyring_store_dir)
print("store_exists=%s" % os.path.isdir(layout.keyring_store_dir))
print("login_keyring=%s" % os.path.isfile(
    os.path.join(layout.keyring_store_dir, "login.keyring")))
print("legacy_store_holds_nothing=%s" % (
    not os.path.isdir(layout.keyring_dir)
    or not os.listdir(layout.keyring_dir)))
PY

store=$(sed -n 's/^store=//p' "$WORK/store-location.txt")
if grep -q "store_exists=True" "$WORK/store-location.txt"; then
    pass "the keyring store is under the state directory" "$store"
else
    fail "the keyring store is under the state directory" "$store"
fi

if grep -q "login_keyring=True" "$WORK/store-location.txt"; then
    pass "the login keyring is in the keyring store"
else
    fail "the login keyring is in the keyring store" "$store"
fi

if grep -q "legacy_store_holds_nothing=True" "$WORK/store-location.txt"; then
    pass "the legacy keyring directory holds nothing"
else
    fail "the legacy keyring directory holds nothing"
fi

# The item must not have leaked into the home directory.
if find "$HOME" -name 'login.keyring' 2>/dev/null | grep -q .; then
    fail "no keyring data leaked into the home directory"
else
    pass "no keyring data leaked into the home directory"
fi

# ---------------------------------------------------------------------------
section "7. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "the persistent state survived a stop and a start"
    exit 0
fi
echo "the persistent state did not survive a stop and a start"
exit 1
