#!/usr/bin/env bash
# Render the Cloud-Chrome seccomp profile from the pinned upstream baseline, or
# verify that the committed profile still matches it.
#
# The profile is the upstream Docker/Moby default profile plus a small, declared
# set of namespace rules. This script is the single source of truth for that
# derivation: CI runs it in verify mode, so an upstream restriction that was
# silently dropped cannot reach the committed file.
#
# Usage:
#   render-seccomp-profile.sh --write    # (re)generate .devcontainer/seccomp/chrome.json
#   render-seccomp-profile.sh --verify   # verify the committed file (default)
#
# Requires: curl, python3. The upstream baseline is pinned by commit and
# verified by SHA-256 before it is used.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# shellcheck disable=SC1091
. config/versions.env

TARGET=".devcontainer/seccomp/chrome.json"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mode="${1:---verify}"

echo "==> fetching the pinned upstream baseline"
curl -fsSL --retry 3 -o "$WORK/baseline.json" "$SECCOMP_BASE_URL"
echo "${SECCOMP_BASE_SHA256}  $WORK/baseline.json" | sha256sum -c -

echo "==> rendering the profile"
python3 - "$WORK/baseline.json" "$WORK/rendered.json" <<'PY'
import json, sys

baseline_path, out_path = sys.argv[1], sys.argv[2]
with open(baseline_path, encoding="utf-8") as fh:
    profile = json.load(fh)

# Linux namespace flags (uapi/linux/sched.h).
CLONE_NEWNS   = 0x00020000
CLONE_NEWUTS  = 0x04000000
CLONE_NEWIPC  = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID  = 0x20000000
CLONE_NEWNET  = 0x40000000
CLONE_NEWCGROUP = 0x02000000

# The low 16 bits of clone(2)/unshare(2) carry the termination signal.
SIGNAL_MASK = 0x0000FFFF

# Chromium's namespace sandbox (sandbox/linux/services/namespace_sandbox.cc):
#   kSupportedTypes[] = {CLONE_NEWUSER, CLONE_NEWPID, CLONE_NEWNET}
# and it never calls setns().
CLONE_ALLOWED = CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET
UNSHARE_ALLOWED = CLONE_NEWUSER

# The upstream baseline expresses "these bits must be zero" with
# SCMP_CMP_MASKED_EQ and ValueTwo == 0, i.e. the rule matches when
# (arg & mask) == 0. To permit a set of flags, the mask is the complement of
# that set.
def complement(allowed):
    return (1 << 64) - 1 - allowed

mask_clone = complement(CLONE_ALLOWED | SIGNAL_MASK)
mask_unshare = complement(UNSHARE_ALLOWED)

profile["syscalls"].extend([
    {
        "names": ["unshare"],
        "action": "SCMP_ACT_ALLOW",
        "args": [{"index": 0, "value": mask_unshare, "op": "SCMP_CMP_MASKED_EQ"}],
        "excludes": {"caps": ["CAP_SYS_ADMIN"]},
        "comment": (
            "Cloud-Chrome: permits unshare(CLONE_NEWUSER) only, which is the single "
            "namespace operation Chromium's user-namespace sandbox performs "
            "(sandbox/linux/services/credentials.cc). Every other flag combination "
            "stays denied by the container seccomp filter."
        ),
    },
    {
        "names": ["clone"],
        "action": "SCMP_ACT_ALLOW",
        "args": [{"index": 0, "value": mask_clone, "op": "SCMP_CMP_MASKED_EQ"}],
        "excludes": {"caps": ["CAP_SYS_ADMIN"]},
        "comment": (
            "Cloud-Chrome: permits clone(2) only with Chromium's namespace flags "
            "(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET) plus a termination signal, "
            "which is what the namespace sandbox uses "
            "(sandbox/linux/services/namespace_sandbox.cc, credentials.cc). "
            "clone3 is still denied with ENOSYS so Chromium falls back to clone(2)."
        ),
    },
])

with open(out_path, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(profile, indent=2) + "\n")

print(f"    clone mask   = {mask_clone} (allowed flags 0x{CLONE_ALLOWED | SIGNAL_MASK:016x})")
print(f"    unshare mask = {mask_unshare} (allowed flags 0x{UNSHARE_ALLOWED:016x})")
PY

case "$mode" in
  --write)
    mkdir -p "$(dirname "$TARGET")"
    cp "$WORK/rendered.json" "$TARGET"
    echo "==> wrote $TARGET"
    ;;
  --verify)
    if ! diff -u "$TARGET" "$WORK/rendered.json"; then
      echo "ERROR: $TARGET does not match the pinned upstream baseline plus the declared rules." >&2
      exit 1
    fi
    echo "==> $TARGET matches the pinned upstream baseline plus the declared namespace rules"
    ;;
  *)
    echo "usage: $0 [--write|--verify]" >&2
    exit 2
    ;;
esac
