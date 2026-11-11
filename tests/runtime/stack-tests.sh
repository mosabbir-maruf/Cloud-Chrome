#!/usr/bin/env bash
# Cloud-Chrome — the full desktop stack test.
#
# This test runs INSIDE the Cloud-Chrome image and starts the COMPLETE
# desktop stack: the X/VNC server, the D-Bus session bus, the GNOME
# Keyring, the XFCE session and window manager, the noVNC bridge and Chrome.
#
# It is the fast feedback loop for the lifecycle: everything the runtime
# starts must come up, stay up, and go down again in order.
#
# Usage: bash tests/runtime/stack-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-stack.XXXXXX)
export CLOUDCHROME_PERSIST_DIR="$WORK/state/.cloudchrome"
export CLOUDCHROME_RUNTIME_DIR="$CLOUDCHROME_PERSIST_DIR/run"
export HOME="$WORK/home"
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

cleanup() {
    bash "$CLI" stop >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

state="$CLOUDCHROME_PERSIST_DIR"

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
section "2. The whole stack comes up"
# ---------------------------------------------------------------------------
"$CLI" start > "$WORK/start.txt" 2>&1
start_rc=$?
if [ "$start_rc" -eq 0 ]; then
    pass "the stack starts" "$(head -1 "$WORK/start.txt")"
else
    fail "the stack starts" "exit $start_rc: $(head -2 "$WORK/start.txt")"
fi

# Give every component a moment to settle.
sleep 8
"$CLI" status > "$WORK/status.txt" 2>&1
sed -n '1,30p' "$WORK/status.txt" | sed 's/^/      /'

stack_state=$(sed -n 's/^stack: *//p' "$WORK/status.txt")
if [ "$stack_state" = "running" ]; then
    pass "the stack reports itself as running"
else
    fail "the stack reports itself as running" "state=$stack_state"
fi

# Every component must be running. The window manager is started by the
# session itself, so it is checked as a process rather than as a component.
for component in xvnc dbus keyring desktop novnc chrome; do
    if grep -qE "^  ${component} +running" "$WORK/status.txt"; then
        pass "the component '$component' is running"
    else
        fail "the component '$component' is running"
    fi
done

if pgrep -x xfwm4 > /dev/null 2>&1; then
    pass "the XFCE window manager is running"
else
    fail "the XFCE window manager is running"
fi

# A component that died is reported with the reason.
if grep -q "^error:" "$WORK/status.txt"; then
    fail "no component reported an error" "$(grep '^error:' "$WORK/status.txt")"
else
    pass "no component reported an error"
fi

# The component logs are shown when something failed, because that is where
# the reason is.
if [ "$stack_state" != "running" ]; then
    printf -- '\n--- the component logs ---\n'
    for log in "$state/logs"/*.log; do
        [ -e "$log" ] || continue
        printf -- '\n=== %s ===\n' "$(basename "$log")"
        tail -20 "$log" | sed 's/^/      /'
    done
fi

# ---------------------------------------------------------------------------
section "3. The network model"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/ports.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import web

rfb = web.check_rfb_is_loopback_only(5900)
print("rfb_listening=%s" % rfb["listening"])
print("rfb_non_loopback=%s" % rfb.get("non_loopback", 0))
print("novnc_bound=%s" % web.http_port_is_bound(6080))
PY

grep -q "rfb_non_loopback=0" "$WORK/ports.txt" \
    && pass "the RFB port has no non-loopback listener" \
    || fail "the RFB port has no non-loopback listener" "$(grep rfb_ "$WORK/ports.txt")"

grep -q "novnc_bound=True" "$WORK/ports.txt" \
    && pass "the noVNC port is bound" \
    || fail "the noVNC port is bound" "$(grep novnc "$WORK/ports.txt")"

# ---------------------------------------------------------------------------
section "4. The Chrome launch policy"
# ---------------------------------------------------------------------------
chrome_pid=$(python3 -c "
import os, sys
sys.path.insert(0, os.environ['REPO_ROOT'] + '/lib')
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
from cloudchrome import procs

layout = Layout(load_config())
marker = '--user-data-dir=%s' % layout.chrome_dir
for entry in sorted(os.listdir('/proc')):
    if not entry.isdigit():
        continue
    argv = procs.read_cmdline(int(entry))
    if not argv:
        continue
    line = ' '.join(argv)
    if marker in line and '--type=' not in line:
        print(entry)
        break
" 2>/dev/null)
if [ -n "$chrome_pid" ]; then
    # Chrome rewrites its argument vector into a single string, so the checks
    # are made against the joined command line.
    command_line=$(tr '\0' ' ' < "/proc/$chrome_pid/cmdline" 2>/dev/null)
    if printf '%s' "$command_line" | grep -q -- '--password-store=gnome-libsecret'; then
        pass "Chrome uses the libsecret password store"
    else
        fail "Chrome uses the libsecret password store" "$(printf '%s' "$command_line" | cut -c1-100)"
    fi
    if printf '%s' "$command_line" | grep -qE -- '--no-sandbox|--remote-debugging|--disable-setuid-sandbox'; then
        fail "Chrome runs without a forbidden flag" "$(printf '%s' "$command_line" | cut -c1-100)"
    else
        pass "Chrome runs without a forbidden flag"
    fi
    if printf '%s' "$command_line" | grep -q -- "--user-data-dir=$state/chrome"; then
        pass "Chrome uses the persistent profile"
    else
        fail "Chrome uses the persistent profile" "$(printf '%s' "$command_line" | cut -c1-100)"
    fi
else
    fail "Chrome is running" "no Chrome process found"
fi

# The sandbox must be in effect for the renderers.
renderers=$(pgrep -f "type=renderer" 2>/dev/null | wc -l)
if [ "$renderers" -gt 0 ]; then
    bad=0
    for pid in $(pgrep -f "type=renderer" 2>/dev/null); do
        nnp=$(awk '/^NoNewPrivs:/{print $2}' "/proc/$pid/status" 2>/dev/null)
        sec=$(awk '/^Seccomp:/{print $2}' "/proc/$pid/status" 2>/dev/null)
        [ "$nnp" = "1" ] && [ "$sec" = "2" ] || bad=$((bad + 1))
    done
    if [ "$bad" -eq 0 ]; then
        pass "every renderer has NoNewPrivs=1 and Seccomp=2" "$renderers renderers"
    else
        fail "every renderer has NoNewPrivs=1 and Seccomp=2" "$bad of $renderers"
    fi
else
    note "no renderer was started" "the browser may not have opened a window yet"
fi

# ---------------------------------------------------------------------------
section "4b. The diagnostics"
# ---------------------------------------------------------------------------
"$CLI" doctor > "$WORK/doctor.txt" 2>&1
doctor_rc=$?
sed -n '1,40p' "$WORK/doctor.txt" | sed 's/^/      /'
if [ "$doctor_rc" -eq 0 ]; then
    pass "the diagnostics pass"
else
    fail "the diagnostics pass" "$(grep -E '^FAIL' "$WORK/doctor.txt" | head -3 | tr '\n' ' ')"
fi

# ---------------------------------------------------------------------------
section "5. The stack goes down in order"
# ---------------------------------------------------------------------------
"$CLI" stop > "$WORK/stop.txt" 2>&1
if [ $? -eq 0 ]; then
    pass "the stack stops"
else
    fail "the stack stops" "$(head -2 "$WORK/stop.txt")"
fi

sleep 3
if pgrep -f "Xtigervnc :99" >/dev/null 2>&1; then
    fail "the X/VNC server was stopped"
else
    pass "the X/VNC server was stopped"
fi
if pgrep -f "websockify" >/dev/null 2>&1; then
    fail "the noVNC bridge was stopped"
else
    pass "the noVNC bridge was stopped"
fi

# ---------------------------------------------------------------------------
section "6. The stack starts again"
# ---------------------------------------------------------------------------
"$CLI" start > "$WORK/start2.txt" 2>&1
if [ $? -eq 0 ]; then
    pass "the stack starts again"
else
    fail "the stack starts again" "$(head -2 "$WORK/start2.txt")"
fi

sleep 8
"$CLI" status > "$WORK/status2.txt" 2>&1
stack_state=$(sed -n 's/^stack: *//p' "$WORK/status2.txt")
if [ "$stack_state" = "running" ]; then
    pass "the stack is running again"
else
    fail "the stack is running again" "state=$stack_state"
fi

# ---------------------------------------------------------------------------
section "7. There is no duplicate stack"
# ---------------------------------------------------------------------------
supervisors=$(pgrep -fc "cloudchrome.supervisor" 2>/dev/null || echo 0)
xvnc=$(pgrep -fc "Xtigervnc" 2>/dev/null || echo 0)
websockify=$(pgrep -fc "websockify" 2>/dev/null || echo 0)
chrome=$(pgrep -fc "google-chrome" 2>/dev/null || echo 0)
note "the process counts" "supervisor=$supervisors xvnc=$xvnc websockify=$websockify chrome=$chrome"
if [ "$supervisors" -le 1 ] && [ "$xvnc" -le 1 ] && [ "$websockify" -le 1 ]; then
    pass "there is exactly one of each component"
else
    fail "there is exactly one of each component"
fi

# A second start must be refused.
"$CLI" start > "$WORK/start3.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "a second start is refused" "exit $rc"
else
    fail "a second start is refused" "exit 0"
fi

# ---------------------------------------------------------------------------
section "8. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "all the desktop stack expectations hold"
    exit 0
fi
echo "the desktop stack expectations failed"
exit 1
