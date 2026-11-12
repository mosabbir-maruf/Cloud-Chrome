#!/usr/bin/env bash
# Cloud-Chrome — the backup transaction test suite: the backup, the restore
# and the transaction.
#
# These tests run INSIDE the Cloud-Chrome image, because they need the age
# tools and the real desktop stack.
#
# The suite measures:
#
#   * a backup with the stack stopped and a backup with the stack running;
#   * the manifest hashes against the archive;
#   * a restore with the stack stopped and a restore with the stack running;
#   * the failure of the restore at every step, and that the previous
#     profile, keyring and passphrase survive each of them;
#   * the output path handling;
#   * the cleanup of the temporary plaintext.
#
# Usage: bash tests/runtime/backup-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-backup.XXXXXX)
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
pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf 'PASS  %-62s %s\n' "$1" "${2:-}"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf 'FAIL  %-62s %s\n' "$1" "${2:-}"; }
note() { printf 'NOTE  %-62s %s\n' "$1" "${2:-}"; }
section() { printf '\n########## %s ##########\n' "$1"; }

state="$CLOUDCHROME_PERSIST_DIR"

cleanup() {
    bash "$CLI" stop >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
section "1. The state is created"
# ---------------------------------------------------------------------------
"$CLI" init >/dev/null 2>&1
if [ $? -eq 0 ]; then
    pass "init succeeds"
else
    fail "init succeeds"
fi

mkdir -p "$state/chrome/Default"
printf 'a-session-cookie' > "$state/chrome/Default/Cookies"
# The marker is a file Chrome does not manage: the restore starts the
# desktop again, and Chrome rewrites the files it owns.
printf 'the-original-marker' > "$state/chrome/Default/CloudChromeMarker"


# ---------------------------------------------------------------------------
section "2. A backup with the stack stopped"
# ---------------------------------------------------------------------------
"$CLI" backup > "$WORK/backup-stopped.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "the backup succeeds with the stack stopped" "$(grep 'backup written' "$WORK/backup-stopped.txt")"
else
    fail "the backup succeeds with the stack stopped" "exit $rc: $(head -2 "$WORK/backup-stopped.txt")"
fi

backup_file=$(sed -n 's/^backup written: //p' "$WORK/backup-stopped.txt")
if [ -n "$backup_file" ] && [ -f "$backup_file" ]; then
    pass "the backup file exists" "$(basename "$backup_file")"
    mode=$(stat -c '%a' "$backup_file")
    if [ "$mode" = "600" ]; then
        pass "the backup file is mode 0600"
    else
        fail "the backup file is mode 0600" "mode=$mode"
    fi
else
    fail "the backup file exists" "$backup_file"
fi

# ---------------------------------------------------------------------------
section "3. The manifest matches the archive"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/manifest.txt" 2>&1
import json, os, subprocess, sys, tarfile
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import manifest as manifest_module
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

layout = Layout(load_config())
identity = layout.identity_file
backups = sorted(
    os.path.join(layout.backup_dir, name)
    for name in os.listdir(layout.backup_dir)
    if name.endswith(".tar.gz.age")
)
source = backups[-1]

# The archive is streamed through the age tool, so the manifest can be read
# without writing the plaintext to disk.
process = subprocess.Popen(
    ["age", "--decrypt", "--identity", identity, source],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
built = None
digests = {}
with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
    for member in archive:
        if member.isfile():
            handle = archive.extractfile(member)
            data = handle.read() if handle else b""
            digests[member.name] = manifest_module.sha256_bytes(data)
            if member.name == "manifest.json":
                built = manifest_module.parse_manifest(data)
process.wait(timeout=60)

print("members=%d" % len(digests))
mismatched = 0
for entry in built["members"]:
    actual = digests.get(entry["path"])
    if actual != entry["sha256"]:
        mismatched += 1
        print("MISMATCH %s manifest=%s archive=%s"
              % (entry["path"], entry["sha256"], actual))
print("mismatched=%d" % mismatched)
print("manifest_members=%d" % len(built["members"]))
PY
mismatched=$(sed -n 's/^mismatched=//p' "$WORK/manifest.txt")
if [ "$mismatched" = "0" ]; then
    pass "every manifest hash matches the archive content" "$(grep '^members=' "$WORK/manifest.txt")"
else
    fail "every manifest hash matches the archive content" "$mismatched mismatches"
fi

# The archive is listed once. The listing is then searched, because piping a
# decryption straight into 'grep -q' makes grep close the pipe early, the
# producer dies of SIGPIPE, and 'pipefail' turns that into a false failure.
members=$(age --decrypt --identity "$state/keys/backup-identity.txt" "$backup_file" 2>/dev/null \
    | tar -tzf - 2>/dev/null | sort)
note "backup members" "$(printf '%s' "$members" | tr '\n' ' ')"

# The VNC password is never in a backup.
if printf '%s\n' "$members" | grep -q "vnc.pass"; then
    fail "the backup does not contain the VNC password file"
else
    pass "the backup does not contain the VNC password file"
fi

# The age identity is never in a backup.
if printf '%s\n' "$members" | grep -q "backup-identity"; then
    fail "the backup does not contain the age identity"
else
    pass "the backup does not contain the age identity"
fi

# ---------------------------------------------------------------------------
section "4. A restore with the stack stopped"
# ---------------------------------------------------------------------------
printf 'changed-after-the-backup' > "$state/chrome/Default/CloudChromeMarker"
"$CLI" restore --source "$backup_file" --yes > "$WORK/restore-stopped.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "the restore succeeds with the stack stopped" "$(grep 'restored from' "$WORK/restore-stopped.txt")"
else
    fail "the restore succeeds with the stack stopped" "exit $rc: $(head -3 "$WORK/restore-stopped.txt")"
fi

restored=$(cat "$state/chrome/Default/CloudChromeMarker" 2>/dev/null)
if [ "$restored" = "the-original-marker" ]; then
    pass "the restored profile holds the backed-up content"
else
    fail "the restored profile holds the backed-up content" "$restored"
fi

# No staging or rollback directory is left behind.
leftovers=$(find "$state" -maxdepth 1 -name '.cloudchrome-rollback.*' -o -maxdepth 1 -name '.cloudchrome-stage.*' 2>/dev/null | wc -l)
if [ "$leftovers" -eq 0 ]; then
    pass "no staging or rollback directory is left behind"
else
    fail "no staging or rollback directory is left behind" "$leftovers"
fi

# ---------------------------------------------------------------------------
section "5. The output path is handled safely"
# ---------------------------------------------------------------------------
shared="$WORK/shared"
mkdir -p "$shared"
chmod 1777 "$shared"
before=$(stat -c '%a' "$shared")

"$CLI" backup --output "$shared/my-backup.tar.gz.age" > "$WORK/backup-output.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ -f "$shared/my-backup.tar.gz.age" ]; then
    pass "a backup can be written to a shared directory" "$shared/my-backup.tar.gz.age"
    mode=$(stat -c '%a' "$shared/my-backup.tar.gz.age")
    if [ "$mode" = "600" ]; then
        pass "the backup in the shared directory is mode 0600"
    else
        fail "the backup in the shared directory is mode 0600" "mode=$mode"
    fi
else
    fail "a backup can be written to a shared directory" "exit $rc: $(head -2 "$WORK/backup-output.txt")"
fi

after=$(stat -c '%a' "$shared")
if [ "$before" = "$after" ]; then
    pass "the shared directory keeps its own mode" "mode=$after"
else
    fail "the shared directory keeps its own mode" "before=$before after=$after"
fi

# A symbolic link target is refused.
ln -sf "$shared/my-backup.tar.gz.age" "$shared/link.tar.gz.age"
"$CLI" backup --output "$shared/link.tar.gz.age" > "$WORK/backup-link.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "a symbolic link target is refused" "exit $rc"
else
    fail "a symbolic link target is refused" "exit 0"
fi

# A missing directory is refused.
"$CLI" backup --output "$WORK/absent/backup.tar.gz.age" > "$WORK/backup-absent.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    pass "a missing output directory is refused" "exit $rc"
else
    fail "a missing output directory is refused" "exit 0"
fi

# An existing target is replaced.
"$CLI" backup --output "$shared/my-backup.tar.gz.age" >/dev/null 2>&1
if [ $? -eq 0 ] && [ -f "$shared/my-backup.tar.gz.age" ]; then
    pass "an existing target is replaced"
else
    fail "an existing target is replaced"
fi

# ---------------------------------------------------------------------------
section "6. A backup and a restore with the stack running"
# ---------------------------------------------------------------------------
# The desktop stack is started, so the backup has to quiesce Chrome. A
# restore leaves the restored desktop running, so the stack may already be
# up; in that case it is left alone.
bash "$CLI" start --if-needed > "$WORK/start.txt" 2>&1
if [ $? -eq 0 ]; then
    pass "the desktop stack starts" "$(head -1 "$WORK/start.txt")"
else
    fail "the desktop stack starts" "$(head -2 "$WORK/start.txt")"
fi

sleep 5
if bash "$CLI" status 2>/dev/null | grep -q "^stack: *running"; then
    pass "the stack is running"
else
    fail "the stack is running" "$(bash "$CLI" status 2>/dev/null | grep '^stack:')"
fi

printf 'live-profile-marker' > "$state/chrome/Default/CloudChromeMarker"

start_time=$(date +%s)
"$CLI" backup --label "live" > "$WORK/backup-live.txt" 2>&1
rc=$?
elapsed=$(( $(date +%s) - start_time ))
if [ "$rc" -eq 0 ]; then
    pass "the backup succeeds with the stack running" "$(grep 'backup written' "$WORK/backup-live.txt")"
else
    fail "the backup succeeds with the stack running" "exit $rc: $(head -3 "$WORK/backup-live.txt")"
fi

# The stack is running again after the backup.
if bash "$CLI" status 2>/dev/null | grep -q "^stack: *running"; then
    pass "the stack is running again after the backup"
else
    fail "the stack is running again after the backup" "$(bash "$CLI" status 2>/dev/null | grep '^stack:')"
fi

live_backup=$(sed -n 's/^backup written: //p' "$WORK/backup-live.txt")

# The manifest of the live backup matches the archive.
CLOUDCHROME_TEST_BACKUP="$live_backup" python3 - <<'PY' > "$WORK/live-manifest.txt" 2>&1
import os, subprocess, sys, tarfile
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import manifest as manifest_module
from cloudchrome.config import load_config
from cloudchrome.paths import Layout

layout = Layout(load_config())
source = os.environ["CLOUDCHROME_TEST_BACKUP"]
process = subprocess.Popen(
    ["age", "--decrypt", "--identity", layout.identity_file, source],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
built = None
digests = {}
with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
    for member in archive:
        if member.isfile():
            handle = archive.extractfile(member)
            data = handle.read() if handle else b""
            digests[member.name] = manifest_module.sha256_bytes(data)
            if member.name == "manifest.json":
                built = manifest_module.parse_manifest(data)
process.wait(timeout=60)
mismatched = sum(
    1 for entry in built["members"] if digests.get(entry["path"]) != entry["sha256"]
)
print("mismatched=%d" % mismatched)
print("members=%d" % len(built["members"]))
PY
mismatched=$(sed -n 's/^mismatched=//p' "$WORK/live-manifest.txt")
if [ "$mismatched" = "0" ]; then
    pass "the live backup manifest matches the archive" "$(grep '^members=' "$WORK/live-manifest.txt")"
else
    fail "the live backup manifest matches the archive" "$mismatched mismatches"
fi

# The restore with the stack running.
printf 'changed-before-the-live-restore' > "$state/chrome/Default/CloudChromeMarker"
start_time=$(date +%s)
"$CLI" restore --source "$live_backup" --yes > "$WORK/restore-live.txt" 2>&1
rc=$?
elapsed=$(( $(date +%s) - start_time ))
if [ "$rc" -eq 0 ]; then
    pass "the restore succeeds with the stack running" "in ${elapsed}s"
else
    fail "the restore succeeds with the stack running" "exit $rc: $(head -3 "$WORK/restore-live.txt")"
fi

if [ "$elapsed" -lt 30 ]; then
    pass "the restore does not wait for a nested lock" "in ${elapsed}s"
else
    fail "the restore does not wait for a nested lock" "in ${elapsed}s"
fi

if bash "$CLI" status 2>/dev/null | grep -q "^stack: *running"; then
    pass "the stack is running again after the restore"
else
    fail "the stack is running again after the restore" "$(bash "$CLI" status 2>/dev/null | grep '^stack:')"
fi

restored=$(cat "$state/chrome/Default/CloudChromeMarker" 2>/dev/null)
if [ "$restored" = "live-profile-marker" ]; then
    pass "the restored profile holds the backed-up content"
else
    fail "the restored profile holds the backed-up content" "$restored"
fi

# The restored profile is usable: Chrome comes up against it.
if bash "$CLI" doctor 2>/dev/null \
    | grep -qE "^Chrome uses the persistent profile +ok"; then
    pass "the restored profile is used by Chrome"
else
    fail "the restored profile is used by Chrome" "$(bash "$CLI" doctor 2>/dev/null | grep 'persistent profile')"
fi

# ---------------------------------------------------------------------------
section "7. The temporary plaintext is removed"
# ---------------------------------------------------------------------------
plain_left=$(find "$state/backups" -name 'backup.tar.gz' 2>/dev/null | wc -l)
if [ "$plain_left" -eq 0 ]; then
    pass "no plaintext archive is left in the backup directory"
else
    fail "no plaintext archive is left in the backup directory" "$plain_left"
fi

# A failed restore leaves no plaintext behind either.
"$CLI" restore --source "$state/backups/does-not-exist.tar.gz.age" --yes >/dev/null 2>&1
plain_left=$(find "$state/backups" -name 'backup.tar.gz' 2>/dev/null | wc -l)
if [ "$plain_left" -eq 0 ]; then
    pass "a failed restore leaves no plaintext behind"
else
    fail "a failed restore leaves no plaintext behind" "$plain_left"
fi

# ---------------------------------------------------------------------------
section "8. The failure paths keep the old state"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/failures.txt" 2>&1
import os, subprocess, sys, tempfile, shutil
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import backup as backup_module
from cloudchrome.config import load_config
from cloudchrome.errors import CloudChromeError
from cloudchrome.lifecycle import Lifecycle
from cloudchrome.paths import Layout

config = load_config()
layout = Layout(config)


class Failing:
    """A lifecycle that fails at a chosen step.

    It records every operation in the order it happened. The recovery of
    a failed restore is about that order: the restored stack has to be
    stopped before the previous state is put back, and the previous stack
    has to be started again afterwards.
    """

    def __init__(self, running=True, fail_at=None, partial_start=False):
        self.running = running
        self.fail_at = fail_at
        self.partial_start = partial_start
        self.events = []
        self.health_calls = 0
        self.start_calls = 0

    def is_running(self):
        return self.running

    def stop(self, timeout=None):
        self.events.append("stop")
        self.running = False
        return True

    def start(self, if_needed=False, timeout=180):
        self.events.append("start")
        self.start_calls += 1
        if self.fail_at == "start" and self.start_calls == 1:
            if self.partial_start:
                # The start failed half way: a stack is still there.
                self.running = True
            raise CloudChromeError("the supervisor did not start")
        self.running = True

    def health(self):
        self.events.append("health")
        self.health_calls += 1
        if self.fail_at == "health" and self.health_calls == 1:
            return {"healthy": False, "problems": ["Chrome is not running"]}
        return {"healthy": True, "problems": []}


def marker():
    path = os.path.join(layout.chrome_dir, "Default", "Cookies")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def passphrase():
    with open(layout.keyring_pass_file, "r", encoding="utf-8") as handle:
        return handle.read()


def read_or_report(reader):
    """Read the state, or report why it could not be read.

    A file that cannot be read is a result in itself, not a reason to stop
    the measurement: the other cases still have to be measured.
    """
    try:
        return reader()
    except Exception as exc:
        return "UNREADABLE(%s)" % str(exc)[:80]


backups = sorted(
    os.path.join(layout.backup_dir, name)
    for name in os.listdir(layout.backup_dir)
    if name.endswith(".tar.gz.age")
)
source = backups[-1]

# A file that cannot be decrypted at all: the restore fails before the
# original stack is stopped.
corrupt = os.path.join(tempfile.gettempdir(), "cloudchrome-corrupt.tar.gz.age")
with open(corrupt, "wb") as handle:
    handle.write(b"this is not an age file")

CASES = (
    # name, running before, failure, expected sequence
    ("install", True, None, "stop,rollback,start,health"),
    ("verify", True, None, "stop,rollback,start,health"),
    ("start", True, "start", "stop,start,stop,rollback,start,health"),
    ("health", True, "health", "stop,start,health,stop,rollback,start,health"),
    ("stopped", False, "health", "start,health,stop,rollback"),
    ("early", True, None, ""),
)

original_swap = backup_module._swap_in
original_verify = backup_module._verify_installed
original_rollback = backup_module._rollback


def recording_rollback(layout_arg, rollback_arg):
    """Make the rollback visible in the recorded order."""
    lifecycle.events.append("rollback")
    return original_rollback(layout_arg, rollback_arg)


for fail_at, running, failure, expected in CASES:
    # The state is marked, so a rollback can be observed.
    with open(os.path.join(layout.chrome_dir, "Default", "Cookies"), "w",
              encoding="utf-8") as handle:
        handle.write("marker-%s" % fail_at)
    before_marker = read_or_report(marker)
    before_pass = read_or_report(passphrase)

    backup_module._swap_in = original_swap
    backup_module._verify_installed = original_verify
    backup_module._rollback = recording_rollback

    if fail_at == "install":
        def broken_swap(layout_arg, staging, rollback):
            original_swap(layout_arg, staging, rollback)
            raise OSError("the installation failed")
        backup_module._swap_in = broken_swap
    elif fail_at == "verify":
        def broken_verify(layout_arg):
            raise OSError("the ownership check failed")
        backup_module._verify_installed = broken_verify

    lifecycle = Failing(running=running, fail_at=failure,
                        partial_start=(fail_at == "start"))
    case_source = corrupt if fail_at == "early" else source
    try:
        backup_module.restore_transaction(
            lifecycle, layout, config, source=case_source, assume_yes=True)
        outcome = "accepted"
    except Exception as exc:
        outcome = "refused"

    backup_module._swap_in = original_swap
    backup_module._verify_installed = original_verify
    backup_module._rollback = original_rollback

    after_marker = read_or_report(marker)
    after_pass = read_or_report(passphrase)
    kept = sorted(name for name in os.listdir(layout.persist_dir)
                  if name.startswith(".cloudchrome-rollback."))
    sequence = ",".join(lifecycle.events)
    print("CASE=%s outcome=%s sequence=%s sequence_ok=%s running=%s "
          "marker_preserved=%s passphrase_preserved=%s rollback_kept=%d"
          % (fail_at, outcome, sequence, sequence == expected,
             lifecycle.running, after_marker == before_marker,
             after_pass == before_pass, len(kept)))
PY

# Every failure case is measured, and every case is reported on one line.
#
# The number of cases is checked as well: a case that the measurement
# script never reached would otherwise be silently skipped, and the suite
# would pass without ever exercising it.
expected_cases=6
measured=$(grep -c '^CASE=' "$WORK/failures.txt" 2>/dev/null || echo 0)
if [ "$measured" -eq "$expected_cases" ]; then
    pass "every failure case was measured" "$measured of $expected_cases"
else
    fail "every failure case was measured" "$measured of $expected_cases"
    printf -- '\n--- the measurement output ---\n'
    sed 's/^/      /' "$WORK/failures.txt"
fi

while read -r line; do
    case "$line" in
        CASE=*)
            case_name=$(printf '%s' "$line" | sed 's/^CASE=//; s/ .*//')
            outcome=$(printf '%s' "$line" | sed -n 's/.*outcome=//; s/ .*//p')
            marker_state=$(printf '%s' "$line" | sed -n 's/.*marker_preserved=//; s/ .*//p')
            pass_state=$(printf '%s' "$line" | sed -n 's/.*passphrase_preserved=//; s/ .*//p')
            sequence=$(printf '%s' "$line" | sed -n 's/.*sequence=//; s/ .*//p')
            sequence_ok=$(printf '%s' "$line" | sed -n 's/.*sequence_ok=//; s/ .*//p')
            running=$(printf '%s' "$line" | sed -n 's/.*running=//; s/ .*//p')
            rollback_kept=$(printf '%s' "$line" | sed -n 's/.*rollback_kept=//p')
            if [ "$outcome" = "refused" ]; then
                pass "the failure at '$case_name' is refused"
            else
                fail "the failure at '$case_name' is refused" "$outcome"
            fi
            if [ "$sequence_ok" = "True" ]; then
                pass "the lifecycle order at '$case_name' is correct" "$sequence"
            else
                fail "the lifecycle order at '$case_name' is correct" "$sequence"
            fi
            if [ "$running" = "True" ] || [ "$running" = "False" ]; then
                pass "the final stack state at '$case_name' is measured" "$running"
            else
                fail "the final stack state at '$case_name' is measured" "$running"
            fi
            if [ "$marker_state" = "True" ]; then
                pass "the previous profile survives the failure at '$case_name'"
            else
                fail "the previous profile survives the failure at '$case_name'" \
                    "$marker_state"
            fi
            if [ "$pass_state" = "True" ]; then
                pass "the previous passphrase survives the failure at '$case_name'"
            else
                fail "the previous passphrase survives the failure at '$case_name'" \
                    "$pass_state"
            fi
            note "the rollback state kept at '$case_name'" "$rollback_kept"
            ;;
    esac
done < "$WORK/failures.txt"

# ---------------------------------------------------------------------------
section "9. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "all the backup and restore expectations hold"
    exit 0
fi
echo "the backup and restore expectations failed"
exit 1
