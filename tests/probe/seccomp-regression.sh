#!/usr/bin/env bash
# Cloud-Chrome — seccomp regression tests.
#
# Runs INSIDE the container and reports, as a table, what the active seccomp
# filter permits and denies. It is executed twice by CI: once with the
# Cloud-Chrome profile and once with the Docker daemon's own default profile,
# so the difference between the two is recorded rather than assumed.
#
# Checks:
#   1. socket(AF_ALG) is denied.
#   2. socket(AF_VSOCK) is denied.
#   3. socketcall is not usable on this architecture.
#   4. Chromium's required namespace operations are permitted.
#   5. Unrelated namespace operations are denied.
#   6. Chrome starts and its renderers are sandboxed.
#   7. CAP_SYS_ADMIN is absent.
#   8. --no-sandbox is absent from Chrome's argv.
#
# Exit status: 0 when every expectation for the active profile holds.

set -uo pipefail

WORK=$(mktemp -d /tmp/seccomp-regression.XXXXXX)
cleanup() {
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  [ -n "${XVNC_PID:-}" ] && kill "$XVNC_PID" 2>/dev/null
  sleep 1
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

fail=0
pass() { printf 'PASS  %-52s %s\n' "$1" "${2:-}"; }
fail() { printf 'FAIL  %-52s %s\n' "$1" "${2:-}"; fail=1; }
note() { printf '      %-52s %s\n' "$1" "${2:-}"; }

echo "=== seccomp regression: container state ==="
printf 'profile     : %s\n' "${CLOUDCHROME_PROFILE_LABEL:-<unspecified>}"
printf 'Seccomp     : %s (filters=%s)\n' \
  "$(awk '/^Seccomp:/{print $2}' /proc/self/status)" \
  "$(awk '/^Seccomp_filters:/{print $2}' /proc/self/status)"
printf 'CapBnd      : %s\n' "$(awk '/^CapBnd:/{print $2}' /proc/self/status)"

# ---------------------------------------------------------------------------
# 1-3: socket families and socketcall
# ---------------------------------------------------------------------------
echo
echo "=== 1-3: socket families ==="
socket_probe=$(python3 - <<'PY'
import ctypes, errno, socket, os
libc = ctypes.CDLL("libc.so.6", use_errno=True)
results = {}

def attempt(label, family, socktype, proto):
    try:
        fd = socket.socket(family, socktype, proto)
        fd.close()
        results[label] = ("ALLOWED", 0)
    except OSError as e:
        results[label] = ("DENIED", e.errno)

# Control: a family every profile allows. If this fails the environment is
# broken and the other results would be meaningless.
attempt("AF_INET (control)", socket.AF_INET, socket.SOCK_STREAM, 0)
# AF_ALG = 38: direct access to the kernel crypto API.
attempt("AF_ALG (38)", 38, socket.SOCK_RAW, 0)
# AF_VSOCK = 40: host/guest communication.
attempt("AF_VSOCK (40)", 40, socket.SOCK_RAW, 0)

for label, (state, err) in results.items():
    print(f"{label}|{state}|{err}|{errno.errorcode.get(err, '0')}")

# socketcall(2) is the 32-bit x86 multiplexer. It is NOT part of the x86_64
# syscall table (where 102 is getuid), so it cannot be invoked here at all.
# Upstream keeps socketcall in the allow-list because on socketcall ABIs the
# socket arguments sit behind a pointer and cannot be filtered; the mitigation
# is to restrict socketpair(2) instead, which is checked below.
import platform
machine = platform.machine()
print(f"socketcall|{'NOT_APPLICABLE' if machine in ('x86_64', 'aarch64') else 'CHECK'}|0|{machine}")

# socketpair(2) is the call upstream restricts, because socketcall(2) cannot
# be filtered. AF_UNIX (1) must work, other families must not.
def socketpair_probe(family):
    try:
        s1, s2 = socket.socketpair(family, socket.SOCK_STREAM)
        s1.close()
        s2.close()
        return "ALLOWED", 0
    except OSError as e:
        return "DENIED", e.errno

for label, family in (("socketpair(AF_UNIX=1)", socket.AF_UNIX), ("socketpair(AF_NETLINK=16)", 16)):
    state, err = socketpair_probe(family)
    print(f"{label}|{state}|{err}|{errno.errorcode.get(err, '0')}")
PY
)
echo "$socket_probe" | sed 's/^/      /'

control=$(echo "$socket_probe" | awk -F'|' '$1 ~ /AF_INET \(control\)/{print $2}')
alg=$(echo "$socket_probe" | awk -F'|' '$1 ~ /AF_ALG/{print $2":"$4}')
vsock=$(echo "$socket_probe" | awk -F'|' '$1 ~ /AF_VSOCK/{print $2":"$4}')
sockcall=$(echo "$socket_probe" | awk -F'|' '$1 ~ /socketcall/{print $2":"$4}')

if [ "$control" = "ALLOWED" ]; then
  pass "socket(AF_INET) control call works" "environment is sane"
else
  fail "socket(AF_INET) control call works" "environment is broken; other results are not meaningful"
fi
case "$alg" in
  DENIED*) pass "socket(AF_ALG=38) is denied" "$alg" ;;
  ALLOWED*) fail "socket(AF_ALG=38) is denied" "$alg" ;;
esac
case "$vsock" in
  DENIED*) pass "socket(AF_VSOCK=40) is denied" "$vsock" ;;
  ALLOWED*) fail "socket(AF_VSOCK=40) is denied" "$vsock" ;;
esac
case "$sockcall" in
  *NOT_APPLICABLE*) pass "socketcall does not exist on this ABI" "$sockcall" ;;
  *) note "socketcall" "$sockcall" ;;
esac
case "$(echo "$socket_probe" | awk -F'|' '$1 ~ /socketpair\(AF_UNIX/{print $2}')" in
  ALLOWED) pass "socketpair(AF_UNIX) works" "upstream mitigation for socketcall ABIs" ;;
  *) fail "socketpair(AF_UNIX) works" "upstream mitigation for socketcall ABIs is broken" ;;
esac
case "$(echo "$socket_probe" | awk -F'|' '$1 ~ /socketpair\(AF_NETLINK/{print $2}')" in
  DENIED*) pass "socketpair(AF_NETLINK) is denied" "upstream mitigation for socketcall ABIs" ;;
  ALLOWED*) fail "socketpair(AF_NETLINK) is denied" "upstream mitigation for socketcall ABIs is broken" ;;
esac

# ---------------------------------------------------------------------------
# 4-5: namespace operations
# ---------------------------------------------------------------------------
echo
echo "=== 4-5: namespace operations ==="
# Every namespace check runs in its own forked child, so no check can change
# the namespace of the checks that follow it.
cat > "$WORK/nsprobe.py" <<'PY'
import ctypes, errno, os, signal, sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)
SYS_clone = 56

CLONE_NEWNS     = 0x00020000
CLONE_NEWUTS    = 0x04000000
CLONE_NEWIPC    = 0x08000000
CLONE_NEWUSER   = 0x10000000
CLONE_NEWPID    = 0x20000000
CLONE_NEWNET    = 0x40000000
CLONE_NEWCGROUP = 0x02000000

def child_result(child_mode):
    """Run in a forked child; the result is written to fd 3 as an errno."""
    w = os.fdopen(3, "w")
    if child_mode.startswith("unshare"):
        rc = libc.unshare(int(child_mode.split(":", 1)[1], 0))
        e = ctypes.get_errno() if rc != 0 else 0
    else:
        pid = libc.syscall(SYS_clone, int(child_mode.split(":", 1)[1], 0) | signal.SIGCHLD, 0, 0, 0, 0)
        if pid < 0:
            e = ctypes.get_errno()
        else:
            e = 0
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
    w.write(str(e))
    w.close()
    os._exit(0)

def run(label, mode):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.dup2(w, 3)
        os.close(w)
        child_result(mode)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 64)
        if not chunk:
            break
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    e = int(data.decode() or "1")
    print(f"{label}|{'ALLOWED' if e == 0 else 'DENIED'}|{e}|{errno.errorcode.get(e, str(e))}")

run("unshare(CLONE_NEWUSER)", "unshare:0x10000000")
run("unshare(CLONE_NEWUSER|CLONE_NEWNET)", "unshare:0x50000000")
run("unshare(CLONE_NEWNET)", "unshare:0x40000000")
run("unshare(CLONE_NEWNS)", "unshare:0x20000")
run("unshare(CLONE_NEWUSER|CLONE_NEWIPC|CLONE_NEWUTS)", "unshare:0x1c000000")
run("clone(CLONE_NEWUSER|SIGCHLD)", "clone:0x10000000")
run("clone(CLONE_NEWNET|SIGCHLD)", "clone:0x40000000")
run("clone(CLONE_NEWNS|SIGCHLD)", "clone:0x20000")
run("clone(CLONE_NEWPID|SIGCHLD) outside a userns", "clone:0x20000000")
PY
ns_probe=$(python3 "$WORK/nsprobe.py")
echo "$ns_probe" | sed 's/^/      /'

ns() { echo "$ns_probe" | awk -F'|' -v k="$1" '$1==k{print $2":"$4}'; }

case "$(ns 'unshare(CLONE_NEWUSER)')" in
  ALLOWED:*) pass "unshare(CLONE_NEWUSER) permitted" "$(ns 'unshare(CLONE_NEWUSER)')" ;;
  *) fail "unshare(CLONE_NEWUSER) permitted" "$(ns 'unshare(CLONE_NEWUSER)')" ;;
esac
for restricted in \
  'unshare(CLONE_NEWUSER|CLONE_NEWNET)' \
  'unshare(CLONE_NEWNET)' \
  'unshare(CLONE_NEWNS)' \
  'unshare(CLONE_NEWUSER|CLONE_NEWIPC|CLONE_NEWUTS)' \
  'clone(CLONE_NEWNET|SIGCHLD)' \
  'clone(CLONE_NEWNS|SIGCHLD)'; do
  case "$(ns "$restricted")" in
    DENIED:*) pass "$restricted denied" "$(ns "$restricted")" ;;
    ALLOWED:*) fail "$restricted denied" "$(ns "$restricted")" ;;
  esac
done
case "$(ns 'clone(CLONE_NEWUSER|SIGCHLD)')" in
  ALLOWED:*) pass "clone(CLONE_NEWUSER|SIGCHLD) permitted" "$(ns 'clone(CLONE_NEWUSER|SIGCHLD)')" ;;
  *) fail "clone(CLONE_NEWUSER|SIGCHLD) permitted" "$(ns 'clone(CLONE_NEWUSER|SIGCHLD)')" ;;
esac
note "clone(CLONE_NEWPID|SIGCHLD) outside a userns" \
  "$(ns 'clone(CLONE_NEWPID|SIGCHLD) outside a userns') (kernel requires CAP_SYS_ADMIN; the seccomp filter permits the flag)"

# ---------------------------------------------------------------------------
# 6-8: Chrome and its sandbox
# ---------------------------------------------------------------------------
echo
echo "=== 6-8: Chrome sandbox ==="
if ! command -v google-chrome >/dev/null 2>&1; then
  fail "google-chrome is installed" "missing"
else
  export HOME="$WORK/home"; mkdir -p "$HOME"
  PROFILE="$WORK/profile"; mkdir -p "$PROFILE"
  if command -v Xtigervnc >/dev/null 2>&1; then
    Xtigervnc :99 -geometry 1280x800 -depth 24 -SecurityTypes None -rfbport -1 \
        >"$WORK/xvnc.log" 2>&1 &
    XVNC_PID=$!
    for _ in $(seq 1 40); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.25; done
    export DISPLAY=:99
  fi
  google-chrome --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
      --enable-logging=stderr about:blank >"$WORK/chrome.out" 2>"$WORK/chrome.err" &
  CHROME_PID=$!
  sleep 12

  if kill -0 "$CHROME_PID" 2>/dev/null; then
    pass "chrome browser process is alive" "pid $CHROME_PID"
  else
    fail "chrome browser process is alive" "$(grep -iE 'fatal|no usable sandbox' "$WORK/chrome.err" | head -1)"
  fi

  renderers=$(pgrep -f -- "--type=renderer" 2>/dev/null | wc -l)
  if [ "$renderers" -gt 0 ]; then
    pass "chrome spawned renderer processes" "$renderers"
  else
    fail "chrome spawned renderer processes" "none"
  fi

  bad=0
  for pid in $(pgrep -f -- "--type=renderer" 2>/dev/null); do
    nnp=$(awk '/^NoNewPrivs:/{print $2}' "/proc/$pid/status" 2>/dev/null)
    sec=$(awk '/^Seccomp:/{print $2}' "/proc/$pid/status" 2>/dev/null)
    [ "$nnp" = "1" ] || bad=1
    [ "$sec" = "2" ] || bad=1
  done
  if [ "$renderers" -gt 0 ] && [ "$bad" -eq 0 ]; then
    pass "every renderer has NoNewPrivs=1 and Seccomp=2" "checked $renderers renderers"
  else
    fail "every renderer has NoNewPrivs=1 and Seccomp=2" "renderer count=$renderers"
  fi

  if pgrep -f -- "--no-sandbox" >/dev/null 2>&1; then
    fail "no chrome process uses --no-sandbox" "found"
  else
    pass "no chrome process uses --no-sandbox" "none found"
  fi

  capbnd=$(awk '/^CapBnd:/{print $2}' /proc/self/status)
  if python3 -c "import sys; sys.exit(0 if (int('$capbnd',16) >> 21) & 1 else 1)"; then
    fail "CAP_SYS_ADMIN is absent from the capability bounding set" "present"
  else
    pass "CAP_SYS_ADMIN is absent from the capability bounding set" "CapBnd=$capbnd"
  fi
  kill "$CHROME_PID" 2>/dev/null
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "RESULT: all expectations hold for this profile"
else
  echo "RESULT: at least one expectation failed for this profile"
fi
exit "$fail"
