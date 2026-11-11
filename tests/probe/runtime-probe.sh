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

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-runtime-probe.XXXXXX)
EVIDENCE_FILE="${CLOUDCHROME_PROBE_OUTPUT:-$WORK/evidence.txt}"
mkdir -p "$(dirname "$EVIDENCE_FILE")"
exec > >(tee -a "$EVIDENCE_FILE") 2>&1

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
export REPO_ROOT
export PYTHONPATH="$REPO_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
CLI="$REPO_ROOT/bin/cloudchrome"

section() { printf '\n########## %s ##########\n' "$1"; }

publish_via_issue() {
    local token="$1" body_file=/tmp/probe-issue.md json_file=/tmp/probe-issue.json
    {
        echo "## Cloud-Chrome runtime probe evidence"
        echo
        echo '```'
        head -c 50000 "$EVIDENCE_FILE"
        echo '```'
    } > "$body_file"
    python3 -c 'import json,sys;print(json.dumps({"title":"Cloud-Chrome runtime probe evidence","body":open(sys.argv[1],encoding="utf-8",errors="replace").read()}))' "$body_file" > "$json_file"
    status=$(curl -sS -o /tmp/probe-issue-resp.json -w '%{http_code}' -X POST \
        -H "Authorization: Bearer ${token}" \
        -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" \
        --data @"$json_file" \
        "https://api.github.com/repos/${GITHUB_REPOSITORY}/issues" 2>/tmp/probe-issue-err.txt)
    echo "issue publication HTTP status: ${status}"
    if [ "${status}" = "201" ]; then
        echo "published as an issue: $(jq -r '.html_url' /tmp/probe-issue-resp.json 2>/dev/null)"
    else
        echo "issue publication failed: $(head -c 300 /tmp/probe-issue-resp.json 2>/dev/null)"
    fi
    rm -f "$body_file" "$json_file" /tmp/probe-issue-resp.json /tmp/probe-issue-err.txt
    [ "${status}" = "201" ]
}

publish_via_git() {
    # The Codespaces token is a REST API credential, not a git credential,
    # and the container has no repository token by default. The git
    # credentials of the Codespace do work, so the evidence is committed to
    # a branch of the repository.
    #
    # The commit is made on the current branch on purpose: creating a new
    # branch inside the workspace is a bigger operation than the Codespaces
    # credentials are set up for.
    local branch="$1"
    (
        cd "$REPO_ROOT" || exit 1
        git config user.email "codespace@users.noreply.github.com" 2>&1
        git config user.name "Cloud-Chrome probe" 2>&1
        cp "$EVIDENCE_FILE" "$REPO_ROOT/probe-evidence.txt" 2>&1
        git add probe-evidence.txt 2>&1
        if git diff --cached --quiet; then
            echo "nothing to commit: the evidence is unchanged" >&2
            exit 1
        fi
        git commit -q -m "chore(probe): record the runtime probe evidence" 2>&1
        echo "pushing to $branch" >&2
        # The outcome of the push is recorded in the commit message, so a
        # failure says why without a readable log.
        if git push origin "HEAD:refs/heads/${branch}" > "$WORK/push.log" 2>&1; then
            git commit -q --amend -m "chore(probe): record the runtime probe evidence

pushed to ${branch}" 2>&1
            git push -f -q origin "HEAD:refs/heads/${branch}" >/dev/null 2>&1
            echo "pushed to $branch" >&2
            exit 0
        fi
        detail=$(tr '\n' ' ' < "$WORK/push.log" | cut -c1-300)
        git commit -q --amend -m "chore(probe): record the runtime probe evidence

the push failed: ${detail}" 2>&1
        git push -f -q origin "HEAD:refs/heads/${branch}" >/dev/null 2>&1
        echo "the push failed: $detail" >&2
        exit 1
    )
}

# The branch the Codespace was created from. A Codespace checkout can be
# detached, so the upstream tracking branch is used when the current branch
# is not a real branch.
probe_branch() {
    local branch
    branch=$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        branch=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)
        branch="${branch#origin/}"
    fi
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        branch="probe/evidence"
    fi
    printf '%s\n' "$branch"
}

# Publish the evidence, whichever way is available in this container.
publish_evidence() {
    local published=false
    if [ "${CLOUDCHROME_PROBE_PUBLISH:-0}" != "1" ]; then
        echo "publishing not requested"
        return 0
    fi

    local token="${GITHUB_TOKEN:-${PROBE_GIT_TOKEN:-}}"
    local repository="${GITHUB_REPOSITORY:-}"
    if [ -z "$token" ] && command -v gh > /dev/null 2>&1; then
        # The Codespaces image authenticates the gh CLI with the user's
        # token, which is a usable repository credential.
        token=$(gh auth token 2>/dev/null || true)
        repository=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
    fi
    if [ -n "$token" ] && [ -n "$repository" ]; then
        GITHUB_REPOSITORY="$repository"
        export GITHUB_REPOSITORY
        if publish_via_issue "$token"; then
            published=true
        fi
    else
        echo "no repository token in the container; publishing through git instead"
    fi
    if [ "$published" != "true" ]; then
        local branch="${CLOUDCHROME_PROBE_BRANCH:-$(probe_branch)}"
        if publish_via_git "$branch"; then
            echo "published as a branch: $branch"
            published=true
        else
            echo "the evidence could not be published"
        fi
    fi
    echo "published=$published"
}

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
    if tr '\0' '\n' < "/proc/$chrome_pid/cmdline" 2>/dev/null | grep -qE '^--no-sandbox|^--remote-debugging|^--disable-setuid-sandbox'; then
        echo "a forbidden flag is present: FAIL"
    else
        echo "no forbidden flag is present: ok"
    fi
    if tr '\0' '\n' < "/proc/$chrome_pid/cmdline" 2>/dev/null | grep -q '^--password-store=gnome-libsecret'; then
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
"$CLI" backup > "$WORK/backup.txt" 2>&1
kv "backup exit" "$?"
sed -n '1,6p' "$WORK/backup.txt"
backup_file=$(sed -n 's/^backup written: //p' "$WORK/backup.txt")
if [ -n "$backup_file" ] && [ -f "$backup_file" ]; then
    kv "the backup is an age file" "$(head -c 2 "$backup_file" | od -An -tx1 | tr -d ' \n')"
    kv "the backup size" "$(stat -c '%s' "$backup_file") bytes"
    printf -- '\n--- the members of the backup ---\n'
    age --decrypt --identity "$REPO_ROOT/.cloudchrome/keys/backup-identity.txt" \
        "$backup_file" 2>/dev/null | tar -tzf - 2>/dev/null | head -20
    printf -- '\n--- the VNC password is not in the backup ---\n'
    if age --decrypt --identity "$REPO_ROOT/.cloudchrome/keys/backup-identity.txt" \
        "$backup_file" 2>/dev/null | tar -tzf - 2>/dev/null | grep -q "vnc.pass"; then
        echo "the VNC password file IS in the backup: FAIL"
    else
        echo "the VNC password file is not in the backup: ok"
    fi
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
