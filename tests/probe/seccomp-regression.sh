#!/usr/bin/env bash
# Cloud-Chrome — seccomp regression tests.
#
# Runs INSIDE the container and reports, as a table, what the active seccomp
# filter permits and denies. CI runs it with the Cloud-Chrome profile and with
# the Docker daemon's own default profile, so the difference between the two is
# recorded rather than assumed.
#
# Checks:
#   1. socket(AF_ALG) is denied on the direct path.
#   2. socket(AF_VSOCK) is denied.
#   3. socketpair(2) is restricted to AF_UNIX (upstream's mitigation for the
#      socketcall ABIs).
#   4. the i386 socketcall(2) compatibility path (int 0x80) cannot create an
#      AF_ALG socket.
#   5. Chromium's required namespace operations are permitted.
#   6. unrelated namespace operations, and namespace flags combined with
#      unrelated clone flags, are denied.
#   7. Chrome starts and its renderers are sandboxed.
#   8. CAP_SYS_ADMIN is absent from the container's capability bounding set.
#   9. --no-sandbox is absent from Chrome's argv.
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
# 1-3: direct socket families, socketpair and the socketcall ABI
# ---------------------------------------------------------------------------
echo
echo "=== 1-3: socket families and the socketcall ABI ==="
cat > "$WORK/socketprobe.py" <<'PY'
import ctypes, errno, mmap, os, platform, socket, struct, sys

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

# --- the i386 socketcall(2) compatibility path -------------------------------
# An x86_64 process can enter the 32-bit syscall ABI with "int 0x80". The
# arguments of socketcall(2) sit behind a pointer, so the socket domain cannot be
# filtered by seccomp on that path. The kernel applies the profile's
# SCMP_ARCH_X86 sub-architecture filter to it.
machine = platform.machine()

# Optional: disable the 32-bit compatibility entry point.
# personality(0xFFFFFFFF) includes ADDR_NO_32BIT and is one of the personality
# values the upstream profile permits.
if len(sys.argv) > 1 and sys.argv[1] == "no32":
    # ADDR_NO_32BIT on its own: 0xFFFFFFFF is what upstream permits, but the
    # kernel rejects it, so the profile permits the exact value instead.
    ADDR_NO_32BIT = 0x0040000
    ctypes.set_errno(0)
    old_persona = libc.personality(ADDR_NO_32BIT)
    applied = old_persona >= 0
    err = ctypes.get_errno()
    print("personality(ADDR_NO_32BIT)|%s|previous=%d|errno=%s" % (
        "APPLIED" if applied else "FAILED", old_persona,
        errno.errorcode.get(err, "0") if not applied else "0"))

if machine not in ("x86_64", "i686"):
    print(f"socketcall_compat|NOT_APPLICABLE|0|{machine}")
else:
    SYS_SOCKETCALL = 102          # i386
    SYS_SOCKET = 1                # i386 socketcall sub-call
    MAP_32BIT = 0x40

    # The argument array must live below 4 GiB: the compat ABI uses a 32-bit
    # pointer.
    args_page = mmap.mmap(-1, 0x1000, prot=mmap.PROT_READ | mmap.PROT_WRITE,
                           flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS | MAP_32BIT)
    args_ref = ctypes.c_char.from_buffer(args_page)
    args_addr = ctypes.addressof(args_ref)

    stub_page = mmap.mmap(-1, 0x1000,
                           prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC,
                           flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    stub_ref = ctypes.c_char.from_buffer(stub_page)
    stub_addr = ctypes.addressof(stub_ref)

    code = (b"\xb8" + struct.pack("<I", SYS_SOCKETCALL) +
            b"\xbb" + struct.pack("<I", SYS_SOCKET) +
            b"\xb9" + struct.pack("<I", args_addr & 0xFFFFFFFF) +
            b"\xcd\x80" +
            b"\xc3")
    ctypes.memmove(stub_addr, code, len(code))
    fn = ctypes.CFUNCTYPE(ctypes.c_int)(stub_addr)

    def compat_socket(family, socktype):
        ctypes.memmove(args_addr, struct.pack("<IIII", family, socktype, 0, 0), 16)
        ret = fn()
        if ret >= 0:
            try:
                os.close(ret)
            except OSError:
                pass
            return "ALLOWED", 0
        err = -ret
        return "DENIED", err

    # The control is a valid socket call. If it succeeds, the 32-bit
    # compatibility entry point is reachable and the profile did not block it;
    # the refusals of the other families are then the kernel's own.
    for label, family, socktype in (
            ("socketcall_compat(AF_INET=2,SOCK_STREAM)", 2, 1),
            ("socketcall_compat(AF_ALG=38,SOCK_RAW)", 38, 3),
            ("socketcall_compat(AF_VSOCK=40,SOCK_RAW)", 40, 3)):
        state, err = compat_socket(family, socktype)
        print(f"{label}|{state}|{err}|{errno.errorcode.get(err, '0')}")

    # Keep the buffers alive until here.
    del args_ref, stub_ref
PY

socket_probe=$(python3 "$WORK/socketprobe.py")
echo "$socket_probe" | sed 's/^/      /'

# The same probe under ADDR_NO_32BIT: the kernel then refuses the 32-bit
# compatibility entry point, which is the mitigation for a container whose
# seccomp filter does not cover the compat syscall architecture.
compat_no32=$(python3 "$WORK/socketprobe.py" no32 2>&1 || true)
echo "--- the same probe with ADDR_NO_32BIT (personality(0xFFFFFFFF)) ---"
echo "$compat_no32" | sed 's/^/      /'

get() { echo "$socket_probe" | awk -F'|' -v k="$1" '$1==k{print $2":"$4}'; }

if [ "$(get 'AF_INET (control)')" = "ALLOWED:0" ]; then
  pass "socket(AF_INET) control call works" "environment is sane"
else
  fail "socket(AF_INET) control call works" "environment is broken; other results are not meaningful"
fi
case "$(get 'AF_ALG (38)')" in
  DENIED*) pass "socket(AF_ALG=38) is denied on the direct path" "$(get 'AF_ALG (38)')" ;;
  ALLOWED*) fail "socket(AF_ALG=38) is denied on the direct path" "$(get 'AF_ALG (38)')" ;;
esac
case "$(get 'AF_VSOCK (40)')" in
  DENIED*) pass "socket(AF_VSOCK=40) is denied" "$(get 'AF_VSOCK (40)')" ;;
  ALLOWED*) fail "socket(AF_VSOCK=40) is denied" "$(get 'AF_VSOCK (40)')" ;;
esac
case "$(get 'socketpair(AF_UNIX=1)')" in
  ALLOWED*) pass "socketpair(AF_UNIX) works" "upstream mitigation for socketcall ABIs" ;;
  *) fail "socketpair(AF_UNIX) works" "upstream mitigation for socketcall ABIs is broken" ;;
esac
case "$(get 'socketpair(AF_NETLINK=16)')" in
  DENIED*) pass "socketpair(AF_NETLINK) is denied" "upstream mitigation for socketcall ABIs" ;;
  ALLOWED*) fail "socketpair(AF_NETLINK) is denied" "upstream mitigation for socketcall ABIs is broken" ;;
esac

# The compatibility path. The control decides whether the 32-bit entry point is
# reachable at all:
#   * control EPERM  -> the profile blocks the compatibility path (seccomp).
#   * control ALLOWED-> the compatibility path is reachable, so any refusal of
#                        AF_ALG below comes from the kernel, not from the filter.
# Either way AF_ALG must not be creatable.
compat_control=$(get 'socketcall_compat(AF_INET=2,SOCK_STREAM)')
compat_alg=$(get 'socketcall_compat(AF_ALG=38,SOCK_RAW)')
compat_vsock=$(get 'socketcall_compat(AF_VSOCK=40,SOCK_RAW)')
case "$compat_control" in
  *EPERM*) pass "the 32-bit socketcall path is blocked by seccomp" "$compat_control" ;;
  ALLOWED*) note "the 32-bit socketcall path is reachable" "$compat_control (the kernel refuses the families below)" ;;
  *) note "the 32-bit socketcall path is reachable" "$compat_control" ;;
esac
case "$compat_alg" in
  ALLOWED*)
    fail "AF_ALG is not creatable via socketcall(2) (int 0x80)" "$compat_alg (the compatibility path reaches the kernel)" ;;
  *EPERM*) pass "AF_ALG is not creatable via socketcall(2) (int 0x80)" "$compat_alg (seccomp)" ;;
  DENIED*) pass "AF_ALG is not creatable via socketcall(2) (int 0x80)" "$compat_alg (the kernel does not provide this family)" ;;
  *) note "AF_ALG via socketcall(2) (int 0x80)" "$compat_alg" ;;
esac
case "$compat_vsock" in
  ALLOWED*) fail "AF_VSOCK is not creatable via socketcall(2) (int 0x80)" "$compat_vsock" ;;
  DENIED*) pass "AF_VSOCK is not creatable via socketcall(2) (int 0x80)" "$compat_vsock" ;;
  *) note "AF_VSOCK via socketcall(2) (int 0x80)" "$compat_vsock" ;;
esac

# Under ADDR_NO_32BIT the compatibility entry point must be unusable.
no32_persona=$(printf '%s' "$compat_no32" | awk -F'|' '$1 ~ /personality\(ADDR_NO_32BIT\)/{print $2}' | head -1)
no32_control=$(printf '%s' "$compat_no32" | awk -F'|' '$1 ~ /socketcall_compat\(AF_INET/{print $2":"$4}' | head -1)
no32_alg=$(printf '%s' "$compat_no32" | awk -F'|' '$1 ~ /socketcall_compat\(AF_ALG/{print $2":"$4}' | head -1)
# personality(ADDR_NO_32BIT) prevents executing 32-bit binaries. It does not
# prevent the int 0x80 instruction path, which the measurement shows is not
# covered by the container's seccomp filter. Both facts are reported.
case "$no32_persona" in
  APPLIED*) pass "personality(ADDR_NO_32BIT) can be applied" "blocks 32-bit binaries" ;;
  *) fail "personality(ADDR_NO_32BIT) can be applied" "$no32_persona" ;;
esac
case "$no32_control" in
  DENIED*) pass "ADDR_NO_32BIT also closes the int 0x80 compatibility path" "$no32_control" ;;
  *) note "ADDR_NO_32BIT does not close the int 0x80 path" "$no32_control (the compat syscall is not covered by the seccomp filter)" ;;
esac
note "AF_ALG via socketcall(2) with ADDR_NO_32BIT" "$no32_alg"

# ---------------------------------------------------------------------------
# 5-6: namespace operations
# ---------------------------------------------------------------------------
echo
echo "=== 5-6: namespace operations ==="
cat > "$WORK/nsprobe.py" <<'PY'
import ctypes, errno, mmap, os, signal, struct, sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)
SYS_clone = 56  # x86_64

CLONE_VM      = 0x00000100
CLONE_FS      = 0x00000200
CLONE_FILES   = 0x00000400
CLONE_SIGHAND = 0x00000800
CLONE_PIDFD   = 0x00001000
CLONE_PTRACE  = 0x00002000
CLONE_VFORK   = 0x00004000
CLONE_PARENT  = 0x00008000
CLONE_NEWNS   = 0x00020000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID  = 0x20000000
CLONE_NEWNET  = 0x40000000

SIGCHLD = signal.SIGCHLD


def child_result(child_mode):
    """Runs in the forked child; the errno is written to fd 3."""
    w = os.fdopen(3, "w")
    kind, value = child_mode.split(":", 1)
    flags = int(value, 0)
    if kind == "unshare":
        rc = libc.unshare(flags)
        e = ctypes.get_errno() if rc != 0 else 0
    else:
        # A dedicated stack for the child, so the child never shares the
        # parent's stack.
        stack = mmap.mmap(-1, 1 << 20, prot=mmap.PROT_READ | mmap.PROT_WRITE,
                           flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS | mmap.MAP_STACK)
        stack_ref = ctypes.c_char.from_buffer(stack)
        pid = libc.syscall(SYS_clone, flags | SIGCHLD,
                           ctypes.addressof(stack_ref), 0, 0, 0)
        if pid < 0:
            e = ctypes.get_errno()
        elif pid == 0:
            # This is the child: leave immediately.
            os._exit(0)
        else:
            e = 0
            # Reap the child, and never leave one running.
            for _ in range(200):
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                time.sleep(0.01)
            else:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
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
    print("\t".join([label, "ALLOWED" if e == 0 else "DENIED", str(e),
                      errno.errorcode.get(e, str(e))]))


import time

# Chromium's operations.
run("unshare(CLONE_NEWUSER)", "unshare:0x10000000")
run("clone(CLONE_NEWUSER|SIGCHLD)", "clone:0x10000011")
run("clone(CLONE_NEWPID|SIGCHLD)", "clone:0x20000011")
# Namespace flags combined with unrelated clone flags must be denied.
run("clone(CLONE_NEWUSER|CLONE_VM|SIGCHLD)", "clone:0x10000111")
run("clone(CLONE_NEWUSER|CLONE_FS|SIGCHLD)", "clone:0x10000211")
run("clone(CLONE_NEWUSER|CLONE_FILES|SIGCHLD)", "clone:0x10000411")
run("clone(CLONE_NEWUSER|CLONE_SIGHAND|SIGCHLD)", "clone:0x10000811")
run("clone(CLONE_NEWUSER|CLONE_PIDFD|SIGCHLD)", "clone:0x10001011")
run("clone(CLONE_NEWUSER|CLONE_PTRACE|SIGCHLD)", "clone:0x10002011")
run("clone(CLONE_NEWUSER|CLONE_VFORK|SIGCHLD)", "clone:0x10004011")
run("clone(CLONE_NEWUSER|CLONE_PARENT|SIGCHLD)", "clone:0x10008011")
# Unrelated flags without a namespace flag stay permitted (thread creation).
run("clone(CLONE_VM|CLONE_FS|SIGCHLD)", "clone:0x00000311")
# Unrelated namespace operations stay denied.
run("unshare(CLONE_NEWUSER|CLONE_NEWNET)", "unshare:0x50000000")
run("unshare(CLONE_NEWNET)", "unshare:0x40000000")
run("unshare(CLONE_NEWNS)", "unshare:0x20000")
run("unshare(CLONE_NEWUSER|CLONE_NEWIPC|CLONE_NEWUTS)", "unshare:0x1c000000")
run("clone(CLONE_NEWNET|SIGCHLD)", "clone:0x40000011")
run("clone(CLONE_NEWNS|SIGCHLD)", "clone:0x00020011")
PY
ns_probe=$(python3 "$WORK/nsprobe.py")
echo "$ns_probe" | sed 's/^/      /'

ns() { echo "$ns_probe" | awk -F'\t' -v k="$1" '$1==k{print $2":"$4}'; }

# Expected: permitted
for allowed in 'unshare(CLONE_NEWUSER)' 'clone(CLONE_NEWUSER|SIGCHLD)'; do
  case "$(ns "$allowed")" in
    ALLOWED:*) pass "$allowed permitted" "$(ns "$allowed")" ;;
    *) fail "$allowed permitted" "$(ns "$allowed")" ;;
  esac
done
# Expected: denied, and denied by seccomp (EPERM) rather than by the kernel
for denied in \
  'unshare(CLONE_NEWUSER|CLONE_NEWNET)' \
  'unshare(CLONE_NEWNET)' \
  'unshare(CLONE_NEWNS)' \
  'unshare(CLONE_NEWUSER|CLONE_NEWIPC|CLONE_NEWUTS)' \
  'clone(CLONE_NEWUSER|CLONE_VM|SIGCHLD)' \
  'clone(CLONE_NEWUSER|CLONE_FS|SIGCHLD)' \
  'clone(CLONE_NEWUSER|CLONE_FILES|SIGCHLD)' \
  'clone(CLONE_NEWUSER|CLONE_SIGHAND|SIGCHLD)' \
  'clone(CLONE_NEWUSER|CLONE_PIDFD|SIGCHLD)' \
  'clone(CLONE_NEWUSER|CLONE_PTRACE|SIGCHLD)' \
  'clone(CLONE_NEWUSER|CLONE_VFORK|SIGCHLD)' \
  'clone(CLONE_NEWUSER|CLONE_PARENT|SIGCHLD)' \
  'clone(CLONE_NEWNET|SIGCHLD)' \
  'clone(CLONE_NEWNS|SIGCHLD)'; do
  outcome=$(ns "$denied")
  case "$outcome" in
    DENIED:EPERM) pass "$denied denied by seccomp" "$outcome" ;;
    DENIED:*) fail "$denied denied by seccomp" "$outcome (not EPERM: the kernel refused it first)" ;;
    ALLOWED:*) fail "$denied denied by seccomp" "$outcome (allowed)" ;;
    *) fail "$denied denied by seccomp" "$outcome" ;;
  esac
done
# Unrelated flags without a namespace flag must stay permitted.
case "$(ns 'clone(CLONE_VM|CLONE_FS|SIGCHLD)')" in
  ALLOWED:*) pass "clone without a namespace flag stays permitted" "$(ns 'clone(CLONE_VM|CLONE_FS|SIGCHLD)')" ;;
  *) fail "clone without a namespace flag stays permitted" "$(ns 'clone(CLONE_VM|CLONE_FS|SIGCHLD)')" ;;
esac
note "clone(CLONE_NEWPID|SIGCHLD) outside a user namespace" \
  "$(ns 'clone(CLONE_NEWPID|SIGCHLD)') (the kernel requires CAP_SYS_ADMIN; the filter permits the flag)"

# ---------------------------------------------------------------------------
# 7-9: Chrome and its sandbox
# ---------------------------------------------------------------------------
echo
echo "=== 7-9: Chrome sandbox ==="
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
    fail "CAP_SYS_ADMIN is absent from the container's capability bounding set" "present"
  else
    pass "CAP_SYS_ADMIN is absent from the container's capability bounding set" "CapBnd=$capbnd"
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
