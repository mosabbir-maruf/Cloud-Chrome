#!/usr/bin/env bash
# Cloud-Chrome — Phase 1 sandbox and TigerVNC compatibility probe.
#
# Runs INSIDE a real GitHub Codespace dev container and records raw evidence
# about the container's namespace, seccomp, LSM and capability layers, about
# Google Chrome's own sandbox activation, and about the packaged TigerVNC's
# password-file behaviour.
#
# It invokes the installed binaries directly. It is a measurement harness, not
# part of the runtime architecture, and it must not be mistaken for one.
#
# Usage: bash sandbox-probe.sh
# Output: human-readable evidence on stdout (no secrets are produced).

set -uo pipefail

section() { printf '\n########## %s ##########\n' "$1"; }
kv() { printf '%-46s %s\n' "$1" "${2:-<empty>}"; }

WORK=$(mktemp -d /tmp/cloudchrome-probe.XXXXXX)
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
printf '\n--- /proc/filesystems userns support ---\n'
grep -iE 'userfs|userns' /proc/filesystems || echo "<no userns entry>"

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
  # CAP_SYS_ADMIN is bit 21.
  if python3 -c "import sys; sys.exit(0 if (int('$cap_bnd_hex',16) >> 21) & 1 else 1)" 2>/dev/null; then
    kv "CAP_SYS_ADMIN in bounding set" "YES (setuid sandbox path would be available)"
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

# A temporary display. Xtigervnc is used because it is already installed and
# creates the X server and the VNC server in one process; the RFB port is
# disabled so the probe never opens a listening socket.
export HOME="$WORK/home"; mkdir -p "$HOME"
PROFILE="$WORK/profile"; mkdir -p "$PROFILE"
Xvnc_pid=""

start_display() {
  Xtigervnc :99 -geometry 1280x800 -depth 24 -SecurityTypes None -rfbport -1 \
      >"$WORK/xvnc.log" 2>&1 &
  XVNC_PID=$!
  for _ in $(seq 1 40); do
    if xdpyinfo -display :99 >/dev/null 2>&1 || [ -e /tmp/.X11-unix/X99 ]; then return 0; fi
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

printf '\n--- chrome process tree (type=renderer highlighted) ---\n'
ps -eo pid,ppid,user,stat,args 2>/dev/null | grep -E "[c]hrome|[c]hromium" | head -40

printf '\n--- per-process sandbox state ---\n'
for pid in $(pgrep -f -- "--user-data-dir=$PROFILE" 2>/dev/null); do
  [ -r "/proc/$pid/status" ] || continue
  ptype=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -o -- '--type=[a-z-]*' | head -1)
  printf '\n[pid %s] %s\n' "$pid" "${ptype:-browser}"
  grep -E '^(Name|Seccomp|Seccomp_filters|NoNewPrivs|CapEff|CapBnd):' "/proc/$pid/status" 2>/dev/null | sed 's/^/    /'
  printf '    NSpid: '; grep -E '^NSpid:' "/proc/$pid/status" 2>/dev/null | cut -f2-
  printf '    ns/user: '; readlink "/proc/$pid/ns/user" 2>/dev/null || echo "<unreadable>"
  printf '    ns/pid:  '; readlink "/proc/$pid/ns/pid" 2>/dev/null || echo "<unreadable>"
done

printf '\n--- sandbox-related chrome log output ---\n'
grep -iE 'sandbox|seccomp|namespace|zygote|no usable' "$CHROME_ERR" 2>/dev/null | head -30 || true
printf '\n--- chrome stderr (first 40 lines) ---\n'
sed -n '1,40p' "$CHROME_ERR" 2>/dev/null

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
printf '\n--- Xtigervnc documented -rfbauth option (packaged man page) ---\n'
if [ -r /usr/share/man/man1/Xtigervnc.1.gz ]; then
  zcat /usr/share/man/man1/Xtigervnc.1.gz 2>/dev/null | grep -A6 -i 'rfbauth' | head -20
else
  echo "<packaged man page not found>"
fi
printf '\n--- tigervncpasswd -help (first lines) ---\n'
tigervncpasswd -help 2>&1 | head -20

# ---------------------------------------------------------------------------
section "7. TigerVNC password file creation, permissions and live replacement"
# ---------------------------------------------------------------------------
PASSFILE="$WORK/passwd"
make_password_file() { # $1 = password, $2 = target file
  local pw="$1" out="$2"
  rm -f "$out"
  # tigervncpasswd -f writes the obfuscated password to stdout; it reads the
  # password from the terminal, so a pty is provided when stdin is not one.
  if printf '%s\n%s\n' "$pw" "$pw" | tigervncpasswd -f > "$out" 2>/dev/null && [ -s "$out" ]; then
    return 0
  fi
  rm -f "$out"
  if command -v script >/dev/null 2>&1; then
    printf '%s\n%s\n' "$pw" "$pw" | script -qec "tigervncpasswd -f > '$out'" /dev/null >/dev/null 2>&1
    [ -s "$out" ] && return 0
  fi
  return 1
}

if make_password_file "probepw1" "$PASSFILE"; then
  kv "password file created" "yes"
  kv "password file size"   "$(wc -c < "$PASSFILE") bytes (TigerVNC obfuscated form is 8 bytes)"
  stat -c 'password file mode=%a owner=%U group=%G' "$PASSFILE"
else
  kv "password file created" "FAILED"
fi

RFB_SOCK="$WORK/vnc.sock"
Xtigervnc :98 -geometry 640x480 -depth 24 -SecurityTypes VncAuth -rfbauth "$PASSFILE" \
    -rfbport -1 -rfbunixpath "$RFB_SOCK" >"$WORK/xvnc2.log" 2>&1 &
XVNC2_PID=$!
for _ in $(seq 1 40); do [ -S "$RFB_SOCK" ] && break; sleep 0.25; done
kv "unix RFB socket" "$([ -S "$RFB_SOCK" ] && echo created || echo "NOT created")"

if [ -S "$RFB_SOCK" ] && command -v python3 >/dev/null 2>&1; then
  printf '\n--- RFB authentication tests (python3, DES via openssl) ---\n'
  python3 - "$RFB_SOCK" "$PASSFILE" "$WORK" <<'PY'
import os, socket, struct, subprocess, sys

sock_path, passfile, work = sys.argv[1], sys.argv[2], sys.argv[3]

def des_ecb(key8, data):
    for extra in ([], ["-provider", "legacy", "-provider", "default"]):
        cmd = ["openssl", "enc", "-des-ecb", "-nopad", *extra, "-K", key8.hex()]
        p = subprocess.run(cmd, input=data, capture_output=True)
        if p.returncode == 0 and len(p.stdout) == len(data):
            return p.stdout
    raise RuntimeError("openssl DES unavailable: " + p.stderr.decode("replace"))

def reverse_bits(b):
    return int("{:08b}".format(b)[::-1], 2)

def response_for(password, challenge):
    pw = password.encode("latin-1")[:8].ljust(8, b"\0")
    key = bytes(reverse_bits(x) for x in pw)
    return des_ecb(key, challenge)

def try_auth(password):
    """Returns (result_code, description). 0 = success."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sock_path)
    s.recv(12)
    s.sendall(b"RFB 003.008\n")
    n = s.recv(1)[0]
    types = s.recv(n)
    if 2 not in types:
        s.close()
        return None, f"VNC auth not offered (types={list(types)})"
    s.sendall(bytes([2]))
    challenge = s.recv(16)
    s.sendall(response_for(password, challenge))
    result = struct.unpack(">I", s.recv(4))[0]
    if result == 0:
        # Keep the session open: this is the "existing connection" case.
        s.sendall(b"\x01")                       # ClientInit (shared)
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, 16, 16))  # FramebufferUpdateRequest
        s.settimeout(5)
        try:
            alive = len(s.recv(64)) > 0
        except socket.timeout:
            alive = False
        s.close()
        return 0, f"authenticated; session still responsive after replacement={alive}"
    s.close()
    return result, f"authentication rejected (SecurityResult={result})"

def write_password_file(path, password):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        p = subprocess.run(["script", "-qec", f"tigervncpasswd -f > {path}.tmp", "/dev/null"],
                           input=(password + "\n" + password + "\n").encode(),
                           capture_output=True)
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        return False
    os.replace(tmp, path)          # atomic replacement
    return True

print("auth with the ORIGINAL password :", try_auth("probepw1"))

ok = write_password_file(passfile, "probepw2")
print("password file replaced atomically:", ok)
print("replacement file mode          :", oct(os.stat(passfile).st_mode & 0o777))
print("auth with the NEW password      :", try_auth("probepw2"))
print("auth with the OLD password      :", try_auth("probepw1"))
PY
else
  printf '\n<python3 or the RFB socket unavailable: live replacement not functionally tested>\n'
fi

printf '\n--- probe workdir contents (password files are removed on exit) ---\n'
ls -la "$WORK" | sed -n '1,20p'

section "END OF EVIDENCE"
