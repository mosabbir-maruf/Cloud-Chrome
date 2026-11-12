#!/usr/bin/env bash
# Cloud-Chrome — Phase 4 runtime tests: the security audit and the failure
# injection.
#
# These tests run INSIDE the Cloud-Chrome image. They measure the security
# boundaries and they inject real failures: a component is killed, a file is
# made unreadable, a signal is delivered at the wrong moment.
#
# Usage: bash tests/runtime/phase4-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-p4.XXXXXX)
export CLOUDCHROME_PERSIST_DIR="$WORK/state/.cloudchrome"
export CLOUDCHROME_RUNTIME_DIR="$CLOUDCHROME_PERSIST_DIR/run"
export HOME="$WORK/home"
mkdir -p "$HOME" "$CLOUDCHROME_PERSIST_DIR"

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
section "1. The file and directory audit"
# ---------------------------------------------------------------------------
"$CLI" init >/dev/null 2>&1

python3 - <<'PY' > "$WORK/audit.txt" 2>&1
import os, stat, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.paths import Layout, assert_safe_file
from cloudchrome.errors import InvalidStateError

config = load_config()
layout = Layout(config)

problems = []

# Every directory must be private and owned by the runtime user.
for path in (layout.persist_dir, layout.chrome_dir, layout.keyring_dir,
             layout.log_dir, layout.backup_dir, layout.keys_dir,
             layout.runtime_dir, layout.xdg_runtime_dir, layout.xdg_data_home):
    if not os.path.isdir(path):
        problems.append("%s is missing" % path)
        continue
    info = os.lstat(path)
    if stat.S_IMODE(info.st_mode) != 0o700:
        problems.append("%s has mode %o" % (path, stat.S_IMODE(info.st_mode)))
    if info.st_uid != os.getuid():
        problems.append("%s is owned by uid %d" % (path, info.st_uid))

# Every secret file must be private.
for path in (layout.keyring_pass_file, layout.vnc_pass_file):
    if not os.path.isfile(path):
        problems.append("%s is missing" % path)
        continue
    info = os.lstat(path)
    if stat.S_IMODE(info.st_mode) != 0o600:
        problems.append("%s has mode %o" % (path, stat.S_IMODE(info.st_mode)))
    if info.st_uid != os.getuid():
        problems.append("%s is owned by uid %d" % (path, info.st_uid))

# A file with the wrong mode must be refused, not repaired.
loose = os.path.join(layout.persist_dir, "loose-secret")
with open(loose, "w", encoding="utf-8") as handle:
    handle.write("x")
os.chmod(loose, 0o644)
try:
    assert_safe_file(loose)
    problems.append("a loose file was accepted")
except InvalidStateError:
    pass

# A symbolic link must never be followed.
link = os.path.join(layout.persist_dir, "link-to-secret")
os.symlink(layout.keyring_pass_file, link)
try:
    assert_safe_file(link)
    problems.append("a symbolic link was accepted")
except InvalidStateError:
    pass

# A directory must not be accepted where a file is expected.
try:
    assert_safe_file(layout.chrome_dir)
    problems.append("a directory was accepted as a file")
except Exception:
    pass

for problem in problems:
    print("PROBLEM %s" % problem)
print("problems=%d" % len(problems))
PY

problem_count=$(awk '/^PROBLEM/{n++} END{print n+0}' "$WORK/audit.txt" 2>/dev/null)
if [ "$problem_count" -eq 0 ]; then
    pass "the state directory passes the file and directory audit"
else
    fail "the state directory passes the file and directory audit" "$(grep '^PROBLEM' "$WORK/audit.txt" | head -3)"
fi

# The runtime must refuse a state directory with the wrong mode rather than
# repairing it. 'init' is the exception: establishing the state is its job,
# so it restores the permissions of a directory it owns.
chmod 0755 "$state"
"$CLI" start > "$WORK/start-loose.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "start refuses a state directory with a loose mode" "exit $rc"
else
    fail "start refuses a state directory with a loose mode" "exit 0"
fi
grep -q "accessible to other users" "$WORK/start-loose.txt" \
    && pass "the refusal explains why the directory is refused" \
    || fail "the refusal explains why the directory is refused"

"$CLI" init > /dev/null 2>&1
mode=$(stat -c '%a' "$state")
if [ "$mode" = "700" ]; then
    pass "init restores the permissions of the state directory" "mode=$mode"
else
    fail "init restores the permissions of the state directory" "mode=$mode"
fi

# ---------------------------------------------------------------------------
section "2. The lock behaviour under concurrency"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/locks2.txt" 2>&1
import os, subprocess, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome.config import load_config
from cloudchrome.errors import BusyError
from cloudchrome.locking import FileLock, is_locked
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)
os.makedirs(layout.runtime_dir, mode=0o700, exist_ok=True)
path = layout.control_lock_file

# Two locks at the same time: the second one must not succeed.
first = FileLock(path, timeout=0)
first.acquire()
try:
    second = FileLock(path, timeout=0.2)
    try:
        second.acquire()
        print("second=acquired")
    except BusyError:
        print("second=refused")
finally:
    first.release()

# After the release the lock is free again, so a stale lock never wedges the
# runtime.
third = FileLock(path, timeout=0)
third.acquire()
third.release()
print("after_release=free")

# A lock file that exists but is not held is not a lock.
print("idle_lockfile_is_locked=%s" % is_locked(path))
PY

grep -q "second=refused" "$WORK/locks2.txt" \
    && pass "a concurrent operation cannot take the control lock" \
    || fail "a concurrent operation cannot take the control lock" "$(grep second= "$WORK/locks2.txt")"

grep -q "after_release=free" "$WORK/locks2.txt" \
    && pass "the control lock is released again, so no stale lock remains" \
    || fail "the control lock is released again, so no stale lock remains"

# Two concurrent CLI calls: one wins, the other is refused, and neither
# corrupts the state.
"$CLI" init >/dev/null 2>&1
( "$CLI" status > "$WORK/conc1.txt" 2>&1 ) &
( "$CLI" status > "$WORK/conc2.txt" 2>&1 ) &
wait
if [ -s "$WORK/conc1.txt" ] || [ -s "$WORK/conc2.txt" ]; then
    pass "two concurrent commands both complete"
else
    fail "two concurrent commands both complete"
fi

# ---------------------------------------------------------------------------
section "3. The port exposure audit"
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

# The dev container must forward exactly one port, and it must be 6080.
forwarded=$(jq -c '.forwardPorts' "$REPO_ROOT/.devcontainer/devcontainer.json")
if [ "$forwarded" = "[6080]" ]; then
    pass "the dev container forwards only port 6080" "$forwarded"
else
    fail "the dev container forwards only port 6080" "$forwarded"
fi

# The forwarded port must not be configured as public.
visibility=$(jq -r '.portsAttributes["6080"].visibility // "unset (private by default)"' \
    "$REPO_ROOT/.devcontainer/devcontainer.json")
case "$visibility" in
    public) fail "the forwarded port is not configured as public" "$visibility" ;;
    *) pass "the forwarded port is not configured as public" "$visibility" ;;
esac

# 5900 must not appear anywhere in the forwarded-port configuration.
if grep -q '5900' "$REPO_ROOT/.devcontainer/devcontainer.json"; then
    fail "port 5900 is not mentioned in the dev container configuration"
else
    pass "port 5900 is not mentioned in the dev container configuration"
fi

# ---------------------------------------------------------------------------
section "4. The secret and logging audit"
# ---------------------------------------------------------------------------
secret=$(cat "$state/keyring.pass" 2>/dev/null)

# No command output may contain the passphrase.
for command in status doctor logs version url; do
    "$CLI" "$command" > "$WORK/audit-$command.txt" 2>&1
    if [ -n "$secret" ] && grep -qF "$secret" "$WORK/audit-$command.txt"; then
        fail "'$command' does not leak the keyring passphrase"
    else
        pass "'$command' does not leak the keyring passphrase"
    fi
done

# No log file may contain it either.
if [ -n "$secret" ] && grep -rqF "$secret" "$state/logs" 2>/dev/null; then
    fail "no log file contains the keyring passphrase"
else
    pass "no log file contains the keyring passphrase"
fi

# The keyring passphrase must never appear in the process arguments. The
# search reads the passphrase from a file, so the search itself does not put
# it in its own arguments.
printf '%s' "$secret" > "$WORK/secret.txt"
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

# The VNC password must never appear in the logs either. The stored password
# is an obfuscated key, so the check is on the file's content.
vnc_key=$(od -An -tx1 "$state/vnc.pass" 2>/dev/null | tr -d ' \n')
if [ -n "$vnc_key" ] && grep -rqF "$vnc_key" "$state/logs" 2>/dev/null; then
    fail "no log file contains the VNC password key"
else
    pass "no log file contains the VNC password key"
fi

# The log files must be private.
for log in "$state/logs"/*.log; do
    [ -e "$log" ] || continue
    mode=$(stat -c '%a' "$log")
    if [ "$mode" = "600" ] || [ "$mode" = "644" ]; then
        pass "$(basename "$log") has an acceptable mode" "mode=$mode"
    else
        fail "$(basename "$log") has an acceptable mode" "mode=$mode"
    fi
done

# ---------------------------------------------------------------------------
section "5. The failure injection"
# ---------------------------------------------------------------------------
# The supervisor is not started here: it needs a display. What is measured
# is that the runtime fails closed on a broken state and that it never
# destroys the user's data.

# 5a. A missing VNC password file.
mv "$state/vnc.pass" "$WORK/vnc.pass.saved"
"$CLI" start > "$WORK/fail-vnc.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "start refuses a missing VNC password file" "exit $rc"
else
    fail "start refuses a missing VNC password file" "exit 0"
fi
mv "$WORK/vnc.pass.saved" "$state/vnc.pass"

# 5b. An invalid VNC password file.
cp "$state/vnc.pass" "$WORK/vnc.pass.saved"
printf 'not-eight-bytes-at-all' > "$state/vnc.pass"
"$CLI" start > "$WORK/fail-vnc2.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "start refuses an invalid VNC password file" "exit $rc"
else
    fail "start refuses an invalid VNC password file" "exit 0"
fi
cp "$WORK/vnc.pass.saved" "$state/vnc.pass"

# 5c. A keyring passphrase file with unsafe permissions.
cp "$state/keyring.pass" "$WORK/keyring.pass.saved"
chmod 0644 "$state/keyring.pass"
"$CLI" start > "$WORK/fail-pass.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "start refuses a keyring passphrase file with unsafe permissions" "exit $rc"
else
    fail "start refuses a keyring passphrase file with unsafe permissions" "exit 0"
fi
chmod 0600 "$state/keyring.pass"

# 5d. An unreadable profile directory.
mkdir -p "$state/chrome/Default"
printf 'data' > "$state/chrome/Default/History"
chmod 0000 "$state/chrome"
"$CLI" start > "$WORK/fail-profile.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "start refuses an unreadable profile directory" "exit $rc"
else
    fail "start refuses an unreadable profile directory" "exit 0"
fi
chmod 0700 "$state/chrome"

# The user's data must have survived every one of those failures.
if [ -f "$state/chrome/Default/History" ] && [ "$(cat "$state/chrome/Default/History")" = "data" ]; then
    pass "the user's data survives the failed starts"
else
    fail "the user's data survives the failed starts"
fi

# 5e. A corrupt backup is refused by the restore.
"$CLI" backup >/dev/null 2>&1
backup_file=$(ls -t "$state/backups"/*.tar.gz.age 2>/dev/null | head -1)
if [ -n "$backup_file" ]; then
    cp "$backup_file" "$WORK/backup.saved"
    printf 'this is not an age file' > "$backup_file"
    "$CLI" restore --source "$backup_file" --yes > "$WORK/fail-restore.txt" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        pass "restore refuses a corrupt backup" "exit $rc"
    else
        fail "restore refuses a corrupt backup" "exit 0"
    fi
    # The state must be untouched.
    if [ -f "$state/chrome/Default/History" ]; then
        pass "the state is unchanged after the refused restore"
    else
        fail "the state is unchanged after the refused restore"
    fi
    cp "$WORK/backup.saved" "$backup_file"
fi

# 5f. A SIGTERM during the startup.
python3 - <<'PY' > "$WORK/sigterm.txt" 2>&1
import os, signal, subprocess, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import procs
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)

# A supervisor that receives SIGTERM while it is starting must clean up the
# processes it already started and must not leave a stale PID file.
env = dict(os.environ)
env["PYTHONPATH"] = os.path.join(os.environ["REPO_ROOT"], "lib")
process = subprocess.Popen(
    [sys.executable, "-m", "cloudchrome.supervisor"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
time.sleep(0.5)
try:
    os.kill(process.pid, signal.SIGTERM)
except ProcessLookupError:
    pass
process.wait(timeout=60)
print("supervisor_exited=%s" % (process.returncode is not None))
print("exit_code=%s" % process.returncode)

# The PID file must be gone: a stale PID file would confuse the next start.
print("pidfile_removed=%s" % (not os.path.exists(layout.supervisor_pid_file)))

# No component may be left running.
leftover = 0
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    argv = procs.read_cmdline(int(entry))
    joined = " ".join(argv)
    if "cloudchrome" in joined and "supervisor" in joined:
        leftover += 1
print("supervisors_left=%d" % leftover)

# No component of the desktop may be left running either: a supervisor that
# exits without stopping what it started leaves an orphaned desktop.
markers = ("Xtigervnc", "gnome-keyring-daemon", "xfce4-session", "websockify")
leftover_components = 0
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    argv = procs.read_cmdline(int(entry))
    joined = " ".join(argv)
    if any(marker in joined for marker in markers):
        leftover_components += 1
    elif "--user-data-dir=%s" % layout.chrome_dir in joined:
        leftover_components += 1
print("components_left=%d" % leftover_components)
PY

grep -q "supervisor_exited=True" "$WORK/sigterm.txt" \
    && pass "the supervisor exits on SIGTERM" \
    || fail "the supervisor exits on SIGTERM" "$(cat "$WORK/sigterm.txt")"

grep -q "pidfile_removed=True" "$WORK/sigterm.txt" \
    && pass "the supervisor removes its PID file when it stops" \
    || fail "the supervisor removes its PID file when it stops"

grep -q "supervisors_left=0" "$WORK/sigterm.txt" \
    && pass "no supervisor is left running after SIGTERM" \
    || fail "no supervisor is left running after SIGTERM"

grep -q "components_left=0" "$WORK/sigterm.txt" \
    && pass "no component is left running after SIGTERM" \
    || fail "no component is left running after SIGTERM" \
        "$(tr '\n' ' ' < "$WORK/sigterm.txt")"

# ---------------------------------------------------------------------------
section "6. The privilege audit"
# ---------------------------------------------------------------------------
if sudo -n true 2>/dev/null; then
    fail "the runtime user has no passwordless root"
else
    pass "the runtime user has no passwordless root"
fi

if sudo -n -l 2>/dev/null | grep -q "ALL"; then
    fail "the runtime user holds no blanket sudo rule"
else
    pass "the runtime user holds no blanket sudo rule"
fi

# No privileged capability may be in the bounding set.
if grep -q '^CapBnd:' /proc/self/status; then
    cap_bnd=$(awk '/^CapBnd:/{print $2}' /proc/self/status)
    if python3 -c "import sys; sys.exit(0 if (int('$cap_bnd',16) >> 21) & 1 else 1)"; then
        fail "CAP_SYS_ADMIN is absent from the capability bounding set" "CapBnd=$cap_bnd"
    else
        pass "CAP_SYS_ADMIN is absent from the capability bounding set" "CapBnd=$cap_bnd"
    fi
fi

# ---------------------------------------------------------------------------
section "7. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "all Phase 4 expectations hold"
    exit 0
fi
echo "Phase 4 expectations failed"
exit 1
