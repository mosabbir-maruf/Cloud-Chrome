#!/usr/bin/env bash
# Cloud-Chrome — sandbox and TigerVNC compatibility probe.
#
# Runs INSIDE a real GitHub Codespaces dev container and records raw evidence
# about the container's namespace, seccomp, LSM and capability layers, about
# Google Chrome's own sandbox activation, and about the packaged TigerVNC's
# password-file behaviour.
#
# It invokes the installed binaries directly. It is a measurement harness, not
# part of the runtime architecture, and it must not be mistaken for one.
#
# Usage: bash sandbox-probe.sh
# Output: human-readable evidence on stdout, and to the file named by
# CLOUDCHROME_PROBE_OUTPUT (default: a temporary file removed on exit).

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-probe.XXXXXX)
EVIDENCE_FILE="${CLOUDCHROME_PROBE_OUTPUT:-$WORK/evidence.txt}"
mkdir -p "$(dirname "$EVIDENCE_FILE")"
exec > >(tee -a "$EVIDENCE_FILE") 2>&1

# The workspace root is derived from this script's own location: Codespaces
# does not export GITHUB_WORKSPACE inside the container.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKSPACE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

section() { printf '\n########## %s ##########\n' "$1"; }
kv() { printf '%-46s %s\n' "$1" "${2:-<empty>}"; }

cleanup() {
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  [ -n "${XVNC_PID:-}" ] && kill "$XVNC_PID" 2>/dev/null
  [ -n "${XVNC2_PID:-}" ] && kill "$XVNC2_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
section "1. Environment"
# ---------------------------------------------------------------------------
kv "date (UTC)"        "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kv "uname -a"          "$(uname -a)"
kv "os-release"        "$(. /etc/os-release && echo "$PRETTY_NAME")"
kv "id"                "$(id)"
kv "container user"    "$(id -un)"
kv "workspace root"    "$WORKSPACE_ROOT"

# ---------------------------------------------------------------------------
section "2. Kernel and LSM layer"
# ---------------------------------------------------------------------------
for f in /proc/sys/user/max_user_namespaces \
         /proc/sys/kernel/unprivileged_userns_clone \
         /proc/sys/kernel/apparmor_restrict_unprivileged_userns \
         /proc/sys/user/max_pid_namespaces; do
  if [ -r "$f" ]; then kv "$f" "$(cat "$f")"; else kv "$f" "<not present>"; fi
done
printf '\n--- /proc/self/attr/current (AppArmor profile) ---\n'
if [ -r /proc/self/attr/current ]; then cat /proc/self/attr/current; else echo "<unavailable>"; fi
printf '\n--- /sys/kernel/security/apparmor ---\n'
if [ -d /sys/kernel/security/apparmor ]; then ls -1 /sys/kernel/security/apparmor | head -20; else echo "<apparmor securityfs not mounted>"; fi

# ---------------------------------------------------------------------------
section "3. Container seccomp and capability layer"
# ---------------------------------------------------------------------------
printf -- '--- /proc/self/status (selected) ---\n'
grep -E '^(Seccomp|Seccomp_filters|CapInh|CapPrm|CapEff|CapBnd|CapAmb|NoNewPrivs):' /proc/self/status
printf -- '\n--- decoded capability sets ---\n'
if command -v capsh >/dev/null 2>&1; then capsh --print 2>/dev/null | head -20; else echo "<capsh not installed>"; fi
printf -- '\n--- CAP_SYS_ADMIN present in bounding set? ---\n'
cap_bnd_hex=$(awk '/^CapBnd:/{print $2}' /proc/self/status)
if [ -n "$cap_bnd_hex" ]; then
  if python3 -c "import sys; sys.exit(0 if (int('$cap_bnd_hex',16) >> 21) & 1 else 1)" 2>/dev/null; then
    kv "CAP_SYS_ADMIN in bounding set" "YES"
  else
    kv "CAP_SYS_ADMIN in bounding set" "no (setuid sandbox path unavailable)"
  fi
fi

# ---------------------------------------------------------------------------
section "4. Namespace creation probes (raw outcomes)"
# ---------------------------------------------------------------------------
probe() {
  desc="$1"; shift
  out=$("$@" 2>&1); rc=$?
  printf '%-50s rc=%-3s %s\n' "$desc" "$rc" "$(printf '%s' "$out" | head -1)"
}
probe "unshare -U"                       unshare -U true
probe "unshare -U -r"                    unshare -U -r true
probe "unshare -Ur --map-root-user"      unshare -Ur true
probe "unshare -U -m"                    unshare -U -m true
probe "unshare -U -n"                    unshare -U -n true
probe "unshare -U -p --fork"             unshare -U -p --fork true
probe "unshare -U --map-root-user id"    unshare -Ur id

printf '\n--- exact errno for unshare(CLONE_NEWUSER) ---\n'
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY'
import ctypes, errno, os
libc = ctypes.CDLL("libc.so.6", use_errno=True)
CLONE_NEWUSER = 0x10000000
pid = os.fork()
if pid == 0:
    rc = libc.unshare(CLONE_NEWUSER)
    e = ctypes.get_errno()
    os._exit(0 if rc == 0 else (e if e else 1))
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
if code == 0:
    print("unshare(CLONE_NEWUSER) = 0  (user namespace created)")
else:
    print(f"unshare(CLONE_NEWUSER) failed: errno={code} ({errno.errorcode.get(code,'?')})")
PY
else
  echo "<python3 not available; exact errno not captured>"
fi

# ---------------------------------------------------------------------------
section "5. Google Chrome sandbox activation (the decisive test)"
# ---------------------------------------------------------------------------
printf -- '--- chrome version ---\n'
google-chrome --version 2>&1 | head -2

export HOME="$WORK/home"; mkdir -p "$HOME"
PROFILE="$WORK/profile"; mkdir -p "$PROFILE"

# A temporary display. Xtigervnc is used because it is already installed and
# creates the X server and the VNC server in one process; the RFB port is
# disabled so the probe never opens a listening socket.
start_display() {
  Xtigervnc :99 -geometry 1280x800 -depth 24 -SecurityTypes None -rfbport -1 \
      >"$WORK/xvnc.log" 2>&1 &
  XVNC_PID=$!
  for _ in $(seq 1 40); do
    [ -e /tmp/.X11-unix/X99 ] && return 0
    sleep 0.25
  done
  return 1
}
if command -v Xtigervnc >/dev/null 2>&1; then
  if start_display; then
    kv "temporary display" ":99 (Xvnc, rfb port disabled)"
  else
    kv "temporary display" "FAILED to start Xvnc"
    sed -n '1,20p' "$WORK/xvnc.log" 2>/dev/null
  fi
  export DISPLAY=:99
fi

CHROME_OUT="$WORK/chrome.stdout"; CHROME_ERR="$WORK/chrome.stderr"
google-chrome \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  --enable-logging=stderr --v=1 \
  about:blank >"$CHROME_OUT" 2>"$CHROME_ERR" &
CHROME_PID=$!
sleep 12

if kill -0 "$CHROME_PID" 2>/dev/null; then
  kv "chrome browser process" "alive (pid $CHROME_PID)"
else
  kv "chrome browser process" "EXITED"
fi

printf '\n--- chrome process tree ---\n'
ps -eo pid,ppid,user,stat,args 2>/dev/null | grep -E "[c]hrome|[c]hromium" | head -40

printf '\n--- per-process sandbox state ---\n'
for pid in $(pgrep -f -- "--user-data-dir=$PROFILE" 2>/dev/null); do
  [ -r "/proc/$pid/status" ] || continue
  ptype=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -o -- '--type=[a-z-]*' | head -1)
  printf '\n[pid %s] %s\n' "$pid" "${ptype:-browser}"
  grep -E '^(Name|Seccomp|Seccomp_filters|NoNewPrivs|CapEff|CapBnd):' "/proc/$pid/status" 2>/dev/null | sed 's/^/    /'
  printf '    ns/user: '; readlink "/proc/$pid/ns/user" 2>/dev/null || echo "<unreadable>"
  printf '    ns/pid:  '; readlink "/proc/$pid/ns/pid" 2>/dev/null || echo "<unreadable>"
done

printf '\n--- sandbox-related chrome log output ---\n'
grep -iE 'sandbox|seccomp|namespace|zygote|no usable' "$CHROME_ERR" 2>/dev/null | head -30 || true
printf '\n--- chrome stderr (first 30 lines) ---\n'
sed -n '1,30p' "$CHROME_ERR" 2>/dev/null

kill "$CHROME_PID" 2>/dev/null
wait "$CHROME_PID" 2>/dev/null

# ---------------------------------------------------------------------------
section "6. TigerVNC package and command-line compatibility"
# ---------------------------------------------------------------------------
kv "dpkg tigervnc-standalone-server" "$(dpkg-query -W -f='${Version}' tigervnc-standalone-server 2>/dev/null)"
kv "dpkg tigervnc-common"            "$(dpkg-query -W -f='${Version}' tigervnc-common 2>/dev/null)"
kv "dpkg tigervnc-tools"             "$(dpkg-query -W -f='${Version}' tigervnc-tools 2>/dev/null)"
printf '\n--- Xtigervnc -version ---\n'
Xtigervnc -version 2>&1 | head -5
printf '\n--- packaged man page locations ---\n'
dpkg -L tigervnc-standalone-server 2>/dev/null | grep -i 'man' | head -10 || echo "<none listed>"
printf '\n--- packaged -rfbauth documentation ---\n'
manpage=$(dpkg -L tigervnc-standalone-server 2>/dev/null | grep -E 'man1/Xtigervnc\.1\.gz$' | head -1)
if [ -n "$manpage" ] && [ -r "$manpage" ]; then
  echo "reading $manpage"
  gzip -dc "$manpage" 2>/dev/null | grep -B2 -A8 -i 'rfbauth' | head -30
else
  echo "<packaged man page not readable>"
fi
printf '\n--- tigervncpasswd -help ---\n'
tigervncpasswd -help 2>&1 | head -20

# ---------------------------------------------------------------------------
section "7. TigerVNC password file creation, permissions and live replacement"
# ---------------------------------------------------------------------------
PASSFILE="$WORK/passwd"

# The password file must be written by tigervncpasswd itself so that it owns
# the file and applies mode 0600. It reads the password from the terminal, so
# a pty is provided when stdin is not one.
make_password_file() { # $1 = password, $2 = target file
  # TigerVNC stores an 8-byte obfuscated key. tigervncpasswd reads the password
  # from the terminal, so a pty is used when one is available. The binary key is
  # never captured in a shell variable: it is written straight to a file.
  local pw="$1" out="$2" raw
  rm -f "$out"
  if command -v script >/dev/null 2>&1; then
    ( umask 077
      printf '%s\n%s\n' "$pw" "$pw" | script -qec "tigervncpasswd '$out'" /dev/null >/dev/null 2>&1
    )
  fi
  if [ ! -s "$out" ]; then
    # Fallback: tigervncpasswd -f writes the obfuscated key to stdout. The output
    # is binary, so it goes to a file and is never held in a shell variable.
    raw="$out.raw"
    rm -f "$raw"
    ( umask 077
      printf '%s\n%s\n' "$pw" "$pw" | tigervncpasswd -f > "$raw" 2>/dev/null
    )
    if [ -s "$raw" ]; then
      head -c 8 "$raw" > "$out"
    fi
    rm -f "$raw"
  fi
  [ -f "$out" ] || return 1
  [ "$(wc -c < "$out")" -eq 8 ] || return 2
  chmod 0600 "$out"
  return 0
}

make_password_file "probepw1" "$PASSFILE"; rc=$?
case "$rc" in
  0) kv "password file created" "yes (8-byte obfuscated key)" ;;
  2) kv "password file created" "created but NOT 8 bytes: $(wc -c < "$PASSFILE")" ;;
  *) kv "password file created" "FAILED" ;;
esac
if [ -f "$PASSFILE" ]; then
  stat -c 'password file mode=%a owner=%U group=%G size=%s' "$PASSFILE"
fi

RFB_SOCK="$WORK/vnc.sock"
Xtigervnc :98 -geometry 640x480 -depth 24 -SecurityTypes VncAuth -rfbauth "$PASSFILE" \
    -rfbport -1 -rfbunixpath "$RFB_SOCK" >"$WORK/xvnc2.log" 2>&1 &
XVNC2_PID=$!
for _ in $(seq 1 40); do [ -S "$RFB_SOCK" ] && break; sleep 0.25; done
kv "unix RFB socket" "$([ -S "$RFB_SOCK" ] && echo created || echo "NOT created")"

if [[ -S "$RFB_SOCK" ]] && command -v python3 >/dev/null 2>&1; then
  printf '\n--- RFB authentication tests (python3, DES via openssl) ---\n'
  python3 - "$RFB_SOCK" "$PASSFILE" <<'PYEOF'
import os, shutil, socket, struct, subprocess, sys

sock_path, passfile = sys.argv[1], sys.argv[2]


def read_exact(sock, count):
    """Read exactly count bytes. A single recv() may return fewer."""
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise EOFError("connection closed after %d of %d bytes" % (len(buf), count))
        buf += chunk
    return buf


def des_ecb(key8, data):
    last = b""
    for extra in ([], ["-provider", "legacy", "-provider", "default"]):
        cmd = ["openssl", "enc", "-des-ecb", "-nopad", *extra, "-K", key8.hex()]
        p = subprocess.run(cmd, input=data, capture_output=True)
        last = p.stderr
        if p.returncode == 0 and len(p.stdout) == len(data):
            return p.stdout
    raise RuntimeError("openssl DES unavailable: " + last.decode("replace"))


def reverse_bits(b):
    return int("{:08b}".format(b)[::-1], 2)


def response_for(password, challenge):
    pw = password.encode("latin-1")[:8].ljust(8, b"\0")
    key = bytes(reverse_bits(x) for x in pw)
    return des_ecb(key, challenge)


def open_authenticated(password):
    """Authenticate and RETURN THE OPEN SOCKET so the caller can keep using it."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sock_path)
    read_exact(s, 12)                       # server protocol version
    s.sendall(b"RFB 003.008\n")
    count = read_exact(s, 1)[0]
    types = read_exact(s, count)
    if 2 not in types:
        s.close()
        return None, "VNC auth not offered (types=%r)" % (list(types),)
    s.sendall(bytes([2]))                   # security type 2 = VNC auth
    challenge = read_exact(s, 16)
    s.sendall(response_for(password, challenge))
    result = struct.unpack(">I", read_exact(s, 4))[0]
    if result != 0:
        s.close()
        return None, "rejected (SecurityResult=%d)" % result
    return s, "authenticated (connection left open)"


def write_password_file(path, password):
    """Create the replacement with tigervncpasswd and install it atomically."""
    tmp = path + ".new"
    if os.path.exists(tmp):
        os.unlink(tmp)
    made = False
    if shutil.which("script"):
        # tigervncpasswd writes the file itself, so it owns the mode.
        subprocess.run(
            ["script", "-qec", "tigervncpasswd '%s'" % tmp, "/dev/null"],
            input=(password + "\n" + password + "\n").encode(),
            capture_output=True)
        made = os.path.exists(tmp) and os.path.getsize(tmp) == 8
    if not made:
        # Fallback: write the binary key straight to a file, never through a
        # shell variable, and refuse anything that is not exactly 8 bytes.
        raw = tmp + ".raw"
        with open(raw, "wb") as fh:
            subprocess.run(
                ["tigervncpasswd", "-f"],
                input=(password + "\n" + password + "\n").encode(),
                stdout=fh, stderr=subprocess.DEVNULL)
        if os.path.exists(raw) and os.path.getsize(raw) >= 8:
            with open(raw, "rb") as src, open(tmp, "wb") as dst:
                dst.write(src.read(8))
            os.chmod(tmp, 0o600)
            made = True
        if os.path.exists(raw):
            os.unlink(raw)
    if not made:
        return False, "tigervncpasswd did not produce a valid 8-byte password file"
    os.replace(tmp, path)                    # atomic replacement
    return True, "replaced atomically, mode=%s" % oct(os.stat(path).st_mode & 0o777)


def request_update(sock):
    """Ask for a framebuffer update and read the reply header."""
    sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, 16, 16))  # FramebufferUpdateRequest
    header = read_exact(sock, 4)
    return struct.unpack(">BBH", header[:4])


# 1. authenticate with the original password and KEEP the connection open
sock, status = open_authenticated("probepw1")
print("auth with the ORIGINAL password :", status)

if sock is None:
    print("cannot continue: the original password was not accepted")
else:
    # 2. complete the client initialisation and prove the session is live
    sock.sendall(b"\x01")                   # ClientInit, shared session
    server_init = read_exact(sock, 24)
    width, height = struct.unpack(">HH", server_init[:4])
    # ServerInit ends with a variable-length desktop name; it must be consumed
    # or the following reads are misaligned.
    name_len = struct.unpack(">I", server_init[20:24])[0]
    name = read_exact(sock, name_len).decode("latin-1", "replace") if name_len else ""
    print("client initialisation complete : ServerInit %dx%d name=%r" % (width, height, name))

    # 3. atomically replace the password file while the session stays open
    ok, detail = write_password_file(passfile, "probepw2")
    print("password file replaced          :", ok, "-", detail)

    # 4. the already-authenticated connection must still work afterwards
    try:
        msg_type, _pad, rects = request_update(sock)
        print("existing connection after replace: still responsive "
              "(FramebufferUpdate type=%d rects=%d)" % (msg_type, rects))
    except Exception as exc:                 # noqa: BLE001
        print("existing connection after replace: FAILED (%s)" % exc)
    sock.close()

    # 5. a NEW connection must accept the new password
    s2, st2 = open_authenticated("probepw2")
    print("auth with the NEW password      :", st2)
    if s2 is not None:
        s2.close()

    # 6. a NEW connection must reject the old password
    s3, st3 = open_authenticated("probepw1")
    print("auth with the OLD password      :", st3)
    if s3 is not None:
        s3.close()
PYEOF
else
  printf '\n<python3 or the RFB socket unavailable: live replacement not functionally tested>\n'
fi

printf '\n--- probe workdir (the password file is removed on exit) ---\n'
ls -la "$WORK" | sed -n '1,20p'

# ---------------------------------------------------------------------------
section "8. Publishing the evidence"
# ---------------------------------------------------------------------------
# The Codespaces GITHUB_TOKEN is a REST API credential, not a git credential,
# so the evidence is published through the repository API. A git push is kept as
# a secondary path for environments where the workspace remote can push.
publish_via_issue() {
  local token="$1" body_file=/tmp/probe-issue.md json_file=/tmp/probe-issue.json
  {
    echo "## Cloud-Chrome sandbox probe evidence"
    echo
    echo '```'
    # Keep the body comfortably below the GitHub issue size limit.
    head -c 50000 "$EVIDENCE_FILE"
    echo '```'
  } > "$body_file"
  python3 -c 'import json,sys;print(json.dumps({"title":"Cloud-Chrome sandbox probe evidence","body":open(sys.argv[1],encoding="utf-8",errors="replace").read()}))' "$body_file" > "$json_file"
  status=$(curl -sS -o /tmp/probe-issue-resp.json -w '%{http_code}' -X POST \
      -H "Authorization: Bearer ${token}" \
      -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" \
      --data @"$json_file" \
      "https://api.github.com/repos/${GITHUB_REPOSITORY}/issues" 2>/tmp/probe-issue-err.txt)
  echo "issue publication HTTP status: ${status}"
  if [ "${status}" = "201" ]; then
    echo "published as an issue: $(jq -r '.html_url' /tmp/probe-issue-resp.json 2>/dev/null)"
  else
    echo "issue publication failed: $(head -c 300 /tmp/probe-issue-resp.json 2>/dev/null) $(head -c 200 /tmp/probe-issue-err.txt 2>/dev/null)"
  fi
  rm -f "$body_file" "$json_file" /tmp/probe-issue-resp.json /tmp/probe-issue-err.txt
  [ "${status}" = "201" ]
}

publish_via_git() {
  local branch="$1" token="$2"
  (
    cd "$WORKSPACE_ROOT" || exit 1
    git config user.email "codespace@users.noreply.github.com"
    git config user.name "Cloud-Chrome probe"
    git checkout -B "$branch" >/dev/null 2>&1
    git add probe-evidence.txt >/dev/null 2>&1
    git commit -q -m "chore(probe): record sandbox probe evidence" >/dev/null 2>&1
    git push -q origin "HEAD:refs/heads/${branch}" && exit 0
    git push -q "https://x-access-token:${token}@github.com/${GITHUB_REPOSITORY}.git" \
      "HEAD:refs/heads/${branch}" && exit 0
    exit 1
  )
}

if [ "${CLOUDCHROME_PROBE_PUBLISH:-0}" = "1" ] \
   && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  branch="${CLOUDCHROME_PROBE_BRANCH:-probe/evidence}"
  git_token="${PROBE_GIT_TOKEN:-$GITHUB_TOKEN}"
  echo "publish target: branch=$branch workspace=$WORKSPACE_ROOT"
  published=false
  if publish_via_issue "$git_token"; then
    published=true
  fi
  if [ "$published" != "true" ] && [ -d "$WORKSPACE_ROOT/.git" ]; then
    cp "$EVIDENCE_FILE" "$WORKSPACE_ROOT/probe-evidence.txt"
    if publish_via_git "$branch" "$git_token"; then
      echo "published via git"
      published=true
    fi
  fi
  echo "published=$published"
else
  echo "publishing not requested"
fi

section "END OF EVIDENCE"
