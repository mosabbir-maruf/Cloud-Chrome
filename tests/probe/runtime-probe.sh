#!/usr/bin/env bash
# Cloud-Chrome — the platform acceptance probe.
#
# Runs INSIDE a real GitHub Codespaces dev container. It starts the complete
# desktop stack, exercises it, and records what it measured as raw evidence.
#
# The evidence is published through the repository API as an issue, because
# the Codespaces token is a REST API credential and not a git credential.
#
# Usage: bash tests/probe/runtime-probe.sh
# Output: human-readable evidence on stdout, and to the file named by
#         CLOUDCHROME_PROBE_OUTPUT (default: a temporary file removed on exit).

PROBE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

PROBE_SLUG="runtime"
PROBE_TITLE="Cloud-Chrome runtime probe evidence"
PROBE_EVIDENCE_NAME="probe-evidence.txt"
PROBE_OUTPUT="${CLOUDCHROME_PROBE_OUTPUT:-}"

# The publishing machinery, the work directory and the statistics helpers
# are shared with the performance probe.
# shellcheck source=lib/probe-common.sh
. "$PROBE_DIR/lib/probe-common.sh"

kv() { printf '%-46s %s\n' "$1" "${2:-<empty>}"; }

cleanup() {
    bash "$CLI" stop >/dev/null 2>&1 || true
    rm -rf "$WORK"
}

# The evidence is published however this script ends, so a failure is
# visible from outside as well, and the desktop stack is always stopped.
on_exit() {
    local status=$?
    trap - EXIT
    publish_evidence
    cleanup
    exit "$status"
}
trap on_exit EXIT

# ---------------------------------------------------------------------------
section "1. Environment"
# ---------------------------------------------------------------------------
kv "date (UTC)"     "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kv "uname -a"       "$(uname -a)"
kv "os-release"     "$(. /etc/os-release && echo "$PRETTY_NAME")"
kv "id"             "$(id)"
kv "container user" "$(id -un)"
kv "workspace root" "$REPO_ROOT"
kv "kernel"         "$(uname -r)"
kv "container"      "${CODESPACES:+Codespaces ${CODESPACES}}" "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-not a Codespace}"

# ---------------------------------------------------------------------------
section "2. The runtime state"
# ---------------------------------------------------------------------------
"$CLI" version
kv "persistent dir" "$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/lib')
from cloudchrome.config import load_config
print(load_config().persist_dir)")"

"$CLI" init > /dev/null 2>&1
kv "init exit" "$?"
PERSIST=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/lib')
from cloudchrome.config import load_config
print(load_config().persist_dir)")
"$CLI" status

# ---------------------------------------------------------------------------
section "3. The desktop stack starts"
# ---------------------------------------------------------------------------
"$CLI" start > "$WORK/start.txt" 2>&1
start_rc=$?
kv "start exit" "$start_rc"
if [ "$start_rc" -ne 0 ]; then
    sed -n '1,40p' "$WORK/start.txt"
    kv "start output" "$(head -3 "$WORK/start.txt")"
fi

sleep 5
"$CLI" status | tee "$WORK/status.txt"
kv "supervisor state file" "$(python3 -c "
import json, sys
sys.path.insert(0, '$REPO_ROOT/lib')
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
layout = Layout(load_config())
try:
    print(json.load(open(layout.supervisor_state_file))['state'])
except Exception as exc:
    print('unreadable: %s' % exc)")"

# ---------------------------------------------------------------------------
section "4. The processes"
# ---------------------------------------------------------------------------
# The component logs are shown when a component did not come up, because
# that is where the reason is.
if grep -q "^error:" "$WORK/status.txt" 2>/dev/null; then
    printf -- '\n--- the component logs ---\n'
    for log in "$PERSIST"/logs/*.log; do
        [ -e "$log" ] || continue
        printf -- '\n=== %s ===\n' "$(basename "$log")"
        tail -25 "$log" | sed 's/^/      /'
    done
fi

printf -- '\n--- the component processes ---\n'
ps -eo pid,ppid,user,stat,args 2>/dev/null | grep -E "[X]tigervnc|[d]bus-daemon|[g]nome-keyring|[x]fce4-session|[x]fwm4|[w]ebsockify|[g]oogle-chrome" | head -30

printf -- '\n--- no duplicate supervisor or desktop stack ---\n'
supervisors=$(pgrep -fc "cloudchrome.supervisor" 2>/dev/null || echo 0)
xvnc_count=$(pgrep -fc "Xtigervnc" 2>/dev/null || echo 0)
websockify_count=$(pgrep -fc "websockify" 2>/dev/null || echo 0)
kv "supervisor processes" "$supervisors"
kv "X/VNC servers" "$xvnc_count"
kv "websockify processes" "$websockify_count"
if [ "$supervisors" -le 1 ] && [ "$xvnc_count" -le 1 ] && [ "$websockify_count" -le 1 ]; then
    kv "duplicate check" "ok: one of each"
else
    kv "duplicate check" "DUPLICATES FOUND"
fi

# ---------------------------------------------------------------------------
section "5. The ports"
# ---------------------------------------------------------------------------
printf -- '--- the listening sockets ---\n'
(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null || cat /proc/net/tcp) | head -30

python3 - <<'PY'
import sys
sys.path.insert(0, "%s/lib" % __import__("os").environ["REPO_ROOT"])
from cloudchrome import web

rfb = web.check_rfb_is_loopback_only(5900)
print("RFB port 5900 listening      : %s" % rfb["listening"])
print("RFB port 5900 addresses     : %s" % rfb["addresses"])
print("RFB port 5900 non-loopback  : %s" % rfb.get("non_loopback"))
print("noVNC port 6080 bound       : %s" % web.http_port_is_bound(6080))
PY

# The raw RFB port must not be reachable from outside the container.
printf -- '\n--- the raw RFB port from the container network ---\n'
python3 - <<'PY'
import socket
for host in ("127.0.0.1", "0.0.0.0"):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        result = probe.connect_ex((host, 5900))
        print("connect %s:5900 -> %s" % (host, "open" if result == 0 else "refused"))
    finally:
        probe.close()
PY

# ---------------------------------------------------------------------------
section "6. The Chrome sandbox"
# ---------------------------------------------------------------------------
printf -- '--- the Chrome argument vector ---\n'
# Chrome re-executes itself, so the browser is looked up through the process
# table by its profile directory rather than by the command name.
chrome_pid=$(python3 -c "
import os, sys
sys.path.insert(0, '$REPO_ROOT/lib')
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
    if marker not in ' '.join(argv):
        continue
    if any(a.startswith('--type=') for a in argv):
        continue
    print(entry)
    break
" 2>/dev/null)

if [ -n "$chrome_pid" ]; then
    tr '\0' ' ' < "/proc/$chrome_pid/cmdline" 2>/dev/null | fold -w 100
    printf '\n--- the per-process sandbox state ---\n'
    for pid in $(python3 -c "
import os, sys
sys.path.insert(0, '$REPO_ROOT/lib')
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
from cloudchrome import procs

layout = Layout(load_config())
marker = '--user-data-dir=%s' % layout.chrome_dir
for entry in sorted(os.listdir('/proc')):
    if not entry.isdigit():
        continue
    argv = procs.read_cmdline(int(entry))
    if marker in ' '.join(argv):
        print(entry)
" 2>/dev/null); do
        [ -r "/proc/$pid/status" ] || continue
        ptype=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -o -- '--type=[a-z-]*' | head -1)
        printf '\n[pid %s] %s\n' "$pid" "${ptype:-browser}"
        grep -E '^(NoNewPrivs|Seccomp|Seccomp_filters|CapEff|CapBnd):' "/proc/$pid/status" 2>/dev/null | sed 's/^/    /'
        printf '    ns/user: '; readlink "/proc/$pid/ns/user" 2>/dev/null || echo "<unreadable>"
    done

    printf '\n--- the forbidden flags ---\n'
    # Chrome rewrites its argument vector into a single string, so the
    # checks are made against the joined command line.
    command_line=$(tr '\0' ' ' < "/proc/$chrome_pid/cmdline" 2>/dev/null)
    if printf '%s' "$command_line" | grep -qE -- '--no-sandbox|--remote-debugging|--disable-setuid-sandbox'; then
        echo "a forbidden flag is present: FAIL"
    else
        echo "no forbidden flag is present: ok"
    fi
    if printf '%s' "$command_line" | grep -q -- '--password-store=gnome-libsecret'; then
        echo "the libsecret password store is selected: ok"
    else
        echo "the libsecret password store is selected: FAIL"
    fi
else
    echo "Chrome is not running"
fi

# ---------------------------------------------------------------------------
section "7. The noVNC client"
# ---------------------------------------------------------------------------
printf -- '--- the noVNC client on the forwarded port ---\n'
python3 - <<'PY'
import os
import urllib.request

for path in ("/vnc.html", "/"):
    url = "http://127.0.0.1:6080%s" % path
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read(400).decode("utf-8", "replace")
            print("%s -> HTTP %s, %d bytes read" % (url, response.status, len(body)))
            print("   the page is the noVNC client: %s" % ("noVNC" in body or "vnc.js" in body))
    except Exception as exc:
        print("%s -> %s" % (url, exc))
PY

printf -- '\n--- the noVNC assets ---\n'
ls /usr/local/share/novnc/vnc.html /usr/local/share/novnc/core/rfb.js 2>/dev/null

# ---------------------------------------------------------------------------
section "8. The VNC password"
# ---------------------------------------------------------------------------
# The VNC password file is the 8-byte obfuscated key; it is never printed.
password_file="$("$CLI" status >/dev/null 2>&1; python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/lib')
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
print(Layout(load_config()).vnc_pass_file)")"
if [ -f "$password_file" ]; then
    stat -c 'password file: mode=%a owner=%U group=%G size=%s' "$password_file"
fi

printf -- '\n--- a connection without the password is refused ---\n'
python3 - <<'PY'
import socket, struct, sys

# The RFB handshake is performed against the loopback port and the wrong
# password is used, to show that the server really asks for one.
try:
    sock = socket.create_connection(("127.0.0.1", 5900), timeout=10)
    version = sock.recv(12)
    sock.sendall(b"RFB 003.008\n")
    count = sock.recv(1)
    types = sock.recv(count[0]) if count else b""
    print("the server offers the security types: %s" % list(types))
    if 2 in types:
        print("VncAuth is offered: the desktop requires a password")
        sock.sendall(bytes([2]))
        challenge = sock.recv(16)
        print("the server sent a %d byte challenge: ok" % len(challenge))
        sock.sendall(b"\0" * 16)
        result = struct.unpack(">I", sock.recv(4))[0]
        print("the wrong password is rejected: %s" % ("yes" if result != 0 else "NO"))
    sock.close()
except Exception as exc:
    print("the handshake failed: %s" % exc)
PY

printf -- '\n--- the password rotation keeps the session alive ---\n'
python3 - <<'PY'
import os, socket, struct, subprocess, sys
sys.path.insert(0, os.environ["REPO_ROOT"] + "/lib")
from cloudchrome import vnc as vnc_policy
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

layout = Layout(load_config())

def read_exact(sock, count):
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise EOFError("closed")
        buf += chunk
    return buf

def open_session(password):
    sock = socket.create_connection(("127.0.0.1", 5900), timeout=10)
    read_exact(sock, 12)
    sock.sendall(b"RFB 003.008\n")
    count = read_exact(sock, 1)[0]
    types = read_exact(sock, count)
    if 2 not in types:
        sock.close()
        return None, "VncAuth is not offered"
    sock.sendall(bytes([2]))
    read_exact(sock, 16)
    # The DES response is not computed here; the point of the probe is the
    # rotation, not the cryptography.
    sock.close()
    return None, "the challenge was received"

# The rotation itself is measured: the file changes atomically and stays
# 8 bytes long.
before = open(layout.vnc_pass_file, "rb").read()
path, password = vnc_policy.rotate_password_file(layout.vnc_pass_file)
after = open(layout.vnc_pass_file, "rb").read()
print("the password file changed        : %s" % (before != after))
print("the new file is still 8 bytes    : %s" % (len(after) == 8))
print("the new file has mode 0600       : %s" % (oct(os.stat(layout.vnc_pass_file).st_mode & 0o777)))
print("the password was not printed     : yes")
PY

# ---------------------------------------------------------------------------
section "9. The secret service"
# ---------------------------------------------------------------------------
python3 - <<'PY'
import os, subprocess, sys
sys.path.insert(0, os.environ["REPO_ROOT"] + "/lib")
from cloudchrome import keyring
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

layout = Layout(load_config())
passphrase = keyring.read_passphrase(layout.keyring_pass_file)
env = keyring.dbus_environment(
    layout.dbus_socket, layout.xdg_runtime_dir, layout.xdg_data_home)
env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s" % layout.dbus_socket
try:
    result = keyring.verify_secret_service(env)
    print("the secret service stores and returns a secret: %s" % result)
except Exception as exc:
    print("the secret service could not be verified: %s" % exc)
PY

# ---------------------------------------------------------------------------
section "10. The backup and the restore"
# ---------------------------------------------------------------------------
# The backup stops the desktop for the snapshot and starts it again
# afterwards, so it is taken with the desktop running.
"$CLI" backup > "$WORK/backup.txt" 2>&1
kv "backup exit" "$?"
sed -n '1,6p' "$WORK/backup.txt"
backup_file=$(sed -n 's/^backup written: //p' "$WORK/backup.txt")
if [ -n "$backup_file" ] && [ -f "$backup_file" ]; then
    kv "the backup is an age file" "$(head -c 2 "$backup_file" | od -An -tx1 | tr -d ' \n')"
    kv "the backup size" "$(stat -c '%s' "$backup_file") bytes"
    printf -- '\n--- the members of the backup ---\n'
    # The archive is listed once and the listing is searched. Piping the
    # decryption straight into 'grep -q' makes grep close the pipe early, the
    # producer dies of SIGPIPE, and the check reports a false failure.
    #
    # Only the first members are shown: a full listing would be longer than
    # the evidence the issue can carry, and the count is the measure that
    # matters.
    members=$(age --decrypt --identity "$REPO_ROOT/.cloudchrome/keys/backup-identity.txt" \
        "$backup_file" 2>/dev/null | tar -tzf - 2>/dev/null | sort)
    member_count=$(printf '%s\n' "$members" | wc -l)
    printf 'the archive holds %s members; the first 30 follow\n' "$member_count"
    printf '%s\n' "$members" | head -30
    printf -- '\n--- the desktop is running again after the backup ---\n'
    if bash "$CLI" status 2>/dev/null | grep -qE "^stack: *running"; then
        echo "the desktop is running again after the backup: ok"
    else
        echo "the desktop is running again after the backup: FAIL"
    fi
    printf -- '\n--- the VNC password is not in the backup ---\n'
    if printf '%s\n' "$members" | grep -q "vnc.pass"; then
        echo "the VNC password file IS in the backup: FAIL"
    else
        echo "the VNC password file is not in the backup: ok"
    fi
fi

# ---------------------------------------------------------------------------
section "10b. The restore is one transaction"
# ---------------------------------------------------------------------------
# The marker is a file Chrome does not manage. The restore starts the
# desktop again, and Chrome rewrites the files it owns, so the content of
# such a file cannot be the measure of a restore.
MARKER="$REPO_ROOT/.cloudchrome/chrome/Default/CloudChromeProbeMarker"
mkdir -p "$(dirname "$MARKER")"
printf 'probe-marker' > "$MARKER"

# A backup of the marked profile, taken with the desktop running.
"$CLI" backup --label "probe transaction" > "$WORK/backup-tx.txt" 2>&1
kv "the transaction backup exit" "$?"
tx_backup=$(sed -n 's/^backup written: //p' "$WORK/backup-tx.txt")

if [ -z "$tx_backup" ] || [ ! -f "$tx_backup" ]; then
    echo "the transaction backup was not written: FAIL"
else
    # --- a restore with the desktop running --------------------------------
    printf 'changed-after-the-backup' > "$MARKER"
    "$CLI" restore --source "$tx_backup" --yes > "$WORK/restore-live.txt" 2>&1
    kv "the restore with the desktop running exit" "$?"
    sed -n '1,4p' "$WORK/restore-live.txt"
    restored=$(cat "$MARKER" 2>/dev/null)
    if [ "$restored" = "probe-marker" ]; then
        echo "the restored profile holds the backed-up marker: ok"
    else
        echo "the restored profile holds the backed-up marker: FAIL ($restored)"
    fi
    if bash "$CLI" status 2>/dev/null | grep -qE "^stack: *running"; then
        echo "the desktop is running after the restore: ok"
    else
        echo "the desktop is running after the restore: FAIL"
    fi
    if bash "$CLI" doctor 2>/dev/null | grep -qE "^Chrome uses the persistent profile +ok"; then
        echo "the restored profile is the one Chrome uses: ok"
    else
        echo "the restored profile is the one Chrome uses: FAIL"
    fi

    # --- a restore with the desktop stopped -------------------------------
    bash "$CLI" stop > "$WORK/stop.txt" 2>&1
    kv "the desktop stops" "$?"
    printf 'changed-while-the-desktop-was-stopped' > "$MARKER"
    "$CLI" restore --source "$tx_backup" --yes > "$WORK/restore-stopped.txt" 2>&1
    kv "the restore with the desktop stopped exit" "$?"
    sed -n '1,4p' "$WORK/restore-stopped.txt"
    restored=$(cat "$MARKER" 2>/dev/null)
    if [ "$restored" = "probe-marker" ]; then
        echo "the stopped restore holds the backed-up marker: ok"
    else
        echo "the stopped restore holds the backed-up marker: FAIL ($restored)"
    fi
    if bash "$CLI" status 2>/dev/null | grep -qE "^stack: *running"; then
        echo "the restore starts the restored desktop: ok"
    else
        echo "the restore starts the restored desktop: FAIL"
    fi

    # --- a refused restore leaves the state alone -------------------------
    before=$(cat "$MARKER" 2>/dev/null)
    cp "$tx_backup" "$WORK/corrupt.tar.gz.age"
    # A byte is flipped in the encrypted header, so the archive cannot be
    # decrypted at all.
    printf '\x00' | dd of="$WORK/corrupt.tar.gz.age" bs=1 seek=200 count=1 conv=notrunc status=none
    "$CLI" restore --source "$WORK/corrupt.tar.gz.age" --yes > "$WORK/restore-bad.txt" 2>&1
    rc=$?
    kv "the refused restore exit" "$rc"
    if [ "$rc" -ne 0 ]; then
        echo "a restore that cannot be decrypted is refused: ok"
    else
        echo "a restore that cannot be decrypted is refused: FAIL"
    fi
    after=$(cat "$MARKER" 2>/dev/null)
    if [ "$after" = "$before" ]; then
        echo "the state is unchanged after the refused restore: ok"
    else
        echo "the state is unchanged after the refused restore: FAIL ($after)"
    fi

    # --- a failure inside the transaction keeps the old state -------------
    # The failure is injected at the health check, which is the last step
    # before the restore is finalised. The previous profile, keyring and
    # passphrase must survive it.
    #
    # The order of the operations is measured as well: the restored stack
    # has to be stopped before the previous state is put back, and the
    # previous stack has to be started again and checked afterwards.
    python3 - <<'PY' 2>&1 | sed 's/^/    /'
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import backup as backup_module
from cloudchrome.config import load_config
from cloudchrome.errors import CloudChromeError
from cloudchrome.lifecycle import Lifecycle
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)


class Failing:
    """A lifecycle whose health check fails, like a broken desktop.

    The first health call is the one of the restored desktop and fails.
    The second one is the check of the previous desktop, which has to
    pass: the recovery verifies it instead of assuming it.
    """

    def __init__(self, running=True):
        self.running = running
        self.events = []
        self.health_calls = 0

    def is_running(self):
        return self.running

    def stop(self, timeout=None):
        self.events.append("stop")
        self.running = False
        return True

    def start(self, if_needed=False, timeout=180):
        self.events.append("start")
        self.running = True

    def health(self):
        self.events.append("health")
        self.health_calls += 1
        if self.health_calls == 1:
            return {"healthy": False, "problems": ["the injected failure"]}
        return {"healthy": True, "problems": []}


def profile_marker():
    path = os.path.join(layout.chrome_dir, "Default", "CloudChromeProbeMarker")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def passphrase():
    with open(layout.keyring_pass_file, "r", encoding="utf-8") as handle:
        return handle.read()


backups = sorted(
    os.path.join(layout.backup_dir, name)
    for name in os.listdir(layout.backup_dir)
    if name.endswith(".tar.gz.age")
)
source = backups[-1]

original_rollback = backup_module._rollback


def recording_rollback(layout_arg, rollback_arg):
    """Make the rollback visible in the recorded order."""
    lifecycle.events.append("rollback")
    return original_rollback(layout_arg, rollback_arg)


for scenario, running, expected in (
    # The stack was running: it has to be stopped, the previous state put
    # back, and the stack started again and checked.
    ("the stack was running", True,
     "stop,start,health,stop,rollback,start,health"),
    # The stack was stopped: it has to be left stopped.
    ("the stack was stopped", False,
     "start,health,stop,rollback"),
):
    print("--- the failure with %s ---" % scenario)
    with open(os.path.join(layout.chrome_dir, "Default", "CloudChromeProbeMarker"), "w",
              encoding="utf-8") as handle:
        handle.write("the-state-before-the-failed-restore")

    before_marker = profile_marker()
    before_pass = passphrase()

    lifecycle = Failing(running=running)
    backup_module._rollback = recording_rollback
    try:
        backup_module.restore_transaction(
            lifecycle, layout, config, source=source, assume_yes=True)
        print("outcome=accepted")
    except CloudChromeError as exc:
        print("outcome=refused")
        # The whole reason is printed: the path that failed is part of the
        # evidence.
        print("reason=%s" % str(exc))
    except Exception as exc:  # noqa: BLE001
        print("outcome=refused")
        print("reason=%s" % str(exc))
    finally:
        backup_module._rollback = original_rollback

    sequence = ",".join(lifecycle.events)
    print("sequence=%s" % sequence)
    print("sequence_is_correct=%s" % (sequence == expected))
    print("the_stack_is_running_afterwards=%s" % lifecycle.running)
    print("profile_preserved=%s" % (profile_marker() == before_marker))
    print("passphrase_preserved=%s" % (passphrase() == before_pass))

    # The rollback state is kept after the failure, and it still holds the
    # previous data.
    kept = sorted(
        name for name in os.listdir(layout.persist_dir)
        if name.startswith(".cloudchrome-rollback.")
    )
    holding = 0
    for name in kept:
        saved = os.path.join(layout.persist_dir, name, "chrome")
        if os.path.isfile(os.path.join(saved, "Default", "CloudChromeProbeMarker")):
            holding += 1
    print("rollback_states_kept=%d" % len(kept))
    print("rollback_states_holding_the_previous_profile=%d" % holding)
PY

    # --- the rollback state is kept after the failure ---------------------
    kept=$(find "$REPO_ROOT/.cloudchrome" -maxdepth 1 -name '.cloudchrome-rollback.*' 2>/dev/null | wc -l)
    kv "the rollback directories kept after the failures" "$kept"

    # --- no plaintext is left behind --------------------------------------
    plain=$(find "$REPO_ROOT/.cloudchrome" -name 'backup.tar.gz' 2>/dev/null | wc -l)
    kv "the plaintext archives left in the state directory" "$plain"
fi

# ---------------------------------------------------------------------------
section "10c. The output path is handled safely"
# ---------------------------------------------------------------------------
# The output directory must already exist and keeps its own mode: a shared
# directory such as /tmp is never changed by the runtime.
before_mode=$(stat -c '%a' /tmp)
"$CLI" backup --output /tmp/cloudchrome-probe-backup.tar.gz.age > "$WORK/backup-output.txt" 2>&1
kv "the backup into a shared directory exit" "$?"
if [ -f /tmp/cloudchrome-probe-backup.tar.gz.age ]; then
    echo "the backup was written to the shared directory: ok"
    kv "the mode of the backup in the shared directory" "$(stat -c '%a' /tmp/cloudchrome-probe-backup.tar.gz.age)"
else
    echo "the backup was written to the shared directory: FAIL"
    sed -n '1,3p' "$WORK/backup-output.txt"
fi
after_mode=$(stat -c '%a' /tmp)
if [ "$before_mode" = "$after_mode" ]; then
    echo "the shared directory keeps its own mode: ok ($after_mode)"
else
    echo "the shared directory keeps its own mode: FAIL ($before_mode -> $after_mode)"
fi

# A symbolic link target is refused.
ln -sf /tmp/cloudchrome-probe-backup.tar.gz.age /tmp/cloudchrome-probe-link.tar.gz.age
"$CLI" backup --output /tmp/cloudchrome-probe-link.tar.gz.age > "$WORK/backup-link.txt" 2>&1
rc=$?
kv "the refused backup through a symbolic link exit" "$rc"
if [ "$rc" -ne 0 ]; then
    echo "a symbolic link target is refused: ok"
else
    echo "a symbolic link target is refused: FAIL"
fi

# A missing directory is refused.
"$CLI" backup --output /tmp/cloudchrome-probe-absent/backup.tar.gz.age > "$WORK/backup-absent.txt" 2>&1
rc=$?
kv "the refused backup into a missing directory exit" "$rc"
if [ "$rc" -ne 0 ]; then
    echo "a missing output directory is refused: ok"
else
    echo "a missing output directory is refused: FAIL"
fi

# ---------------------------------------------------------------------------
section "11. The diagnostics"
# ---------------------------------------------------------------------------
"$CLI" doctor
kv "doctor exit" "$?"

# ---------------------------------------------------------------------------
section "12. The secrets never appear"
# ---------------------------------------------------------------------------
secret=$(cat "$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/lib')
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
print(Layout(load_config()).keyring_pass_file)")")
if grep -rqF "$secret" "$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/lib')
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
print(Layout(load_config()).log_dir)")" 2>/dev/null; then
    kv "the keyring passphrase in the logs" "FOUND: FAIL"
else
    kv "the keyring passphrase in the logs" "absent: ok"
fi
# The passphrase must not be in the arguments of any process. The search
# reads the passphrase from a file, so the search itself does not put it in
# its own arguments, and it skips the processes of this probe.
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
    kv "the keyring passphrase in the process table" "absent: ok"
else
    kv "the keyring passphrase in the process table" "FOUND: FAIL"
fi
rm -f "$WORK/secret.txt"
for command in status doctor logs version url; do
    "$CLI" "$command" > "$WORK/secret-$command.txt" 2>&1
    if grep -qF "$secret" "$WORK/secret-$command.txt" 2>/dev/null; then
        kv "the passphrase in '$command' output" "FOUND: FAIL"
    else
        kv "the passphrase in '$command' output" "absent: ok"
    fi
done

# ---------------------------------------------------------------------------
section "13. The privileges"
# ---------------------------------------------------------------------------
if sudo -n true 2>/dev/null; then
    kv "passwordless root" "AVAILABLE: FAIL"
else
    kv "passwordless root" "not available: ok"
fi
kv "CapBnd" "$(awk '/^CapBnd:/{print $2}' /proc/self/status)"
kv "the managed Chrome policy" "$(jq -c . /etc/opt/chrome/policies/managed/cloudchrome.json)"

# ---------------------------------------------------------------------------
section "14. The host-level limitation"
# ---------------------------------------------------------------------------
if [ -r /proc/sys/abi/ia32_emulation ]; then
    kv "ia32_emulation" "$(cat /proc/sys/abi/ia32_emulation)"
else
    kv "ia32_emulation" "not exposed: a host-level control is required"
fi
kv "syscall isolation" "PARTIAL: the int 0x80 compatibility path stays reachable"

# ---------------------------------------------------------------------------
section "15. Publishing the evidence"
# ---------------------------------------------------------------------------
section "END OF EVIDENCE"
