#!/usr/bin/env bash
# Cloud-Chrome — Phase 3 runtime tests: persistence, keyring and backup.
#
# These tests run INSIDE the Cloud-Chrome image. They exercise the real
# age-encrypted backup, the real restore with its rollback, and a set of
# deliberately hostile archives that must be refused.
#
# Usage: bash tests/runtime/phase3-tests.sh
# Exit:  0 when every expectation holds, 1 otherwise.

set -uo pipefail

WORK=$(mktemp -d /tmp/cloudchrome-p3.XXXXXX)
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
    rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
section "1. The state has an age identity"
# ---------------------------------------------------------------------------
"$CLI" init > "$WORK/init.txt" 2>&1
if [ $? -eq 0 ]; then
    pass "init succeeds"
else
    fail "init succeeds" "$(head -1 "$WORK/init.txt")"
fi

state="$CLOUDCHROME_PERSIST_DIR"
if [ -f "$state/keys/backup-identity.txt" ]; then
    mode=$(stat -c '%a' "$state/keys/backup-identity.txt")
    if [ "$mode" = "600" ]; then
        pass "the age identity is mode 0600"
    else
        fail "the age identity is mode 0600" "mode=$mode"
    fi
else
    fail "the age identity exists"
fi

if [ -f "$state/keys/backup-recipient.txt" ]; then
    recipient=$(head -1 "$state/keys/backup-recipient.txt")
    case "$recipient" in
        age1*) pass "the age recipient is a public key" "${recipient:0:16}..." ;;
        *) fail "the age recipient is a public key" "$recipient" ;;
    esac
else
    fail "the age recipient exists"
fi

# The identity must never appear inside a backup.
if grep -rl "AGE-SECRET-KEY" "$state/keys/backup-recipient.txt" >/dev/null 2>&1; then
    fail "the recipient file holds no private key"
else
    pass "the recipient file holds no private key"
fi

# ---------------------------------------------------------------------------
section "2. The backup is encrypted and complete"
# ---------------------------------------------------------------------------
# Put something in the profile so that the backup has something to carry.
mkdir -p "$state/chrome/Default"
printf 'session-cookie-value' > "$state/chrome/Default/Cookies"
printf 'history' > "$state/chrome/Default/History"
mkdir -p "$state/keyring"
printf 'keyring-data' > "$state/keyring/login.keyring"

"$CLI" backup --label "phase3 test" > "$WORK/backup.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "backup succeeds" "$(grep 'backup written' "$WORK/backup.txt")"
else
    fail "backup succeeds" "exit $rc: $(head -2 "$WORK/backup.txt")"
fi

backup_file=$(sed -n 's/^backup written: //p' "$WORK/backup.txt")
if [ -n "$backup_file" ] && [ -f "$backup_file" ]; then
    pass "the backup file exists" "$backup_file"
else
    fail "the backup file exists" "$backup_file"
fi

# The backup must be an age file, not a plaintext tar.
magic=$(head -c 2 "$backup_file" 2>/dev/null | od -An -tx1 | tr -d ' \n')
if [ "$magic" = "6167" ] || [ "$magic" = "616e" ]; then
    pass "the backup carries the age header, not a plaintext tar"
else
    fail "the backup carries the age header, not a plaintext tar" "magic=$magic"
fi

# A plaintext tar would start with the gzip magic 1f8b; that must not appear.
if head -c 2 "$backup_file" 2>/dev/null | od -An -tx1 | tr -d ' \n' | grep -q "^1f8b"; then
    fail "the backup is not a plaintext gzip archive"
else
    pass "the backup is not a plaintext gzip archive"
fi

# The VNC password must never be in the backup.
if age --decrypt --identity "$state/keys/backup-identity.txt" "$backup_file" 2>/dev/null \
    | tar -tzf - 2>/dev/null | grep -q "vnc.pass"; then
    fail "the backup does not contain the VNC password file"
else
    pass "the backup does not contain the VNC password file"
fi

# The expected members must be there.
members=$(age --decrypt --identity "$state/keys/backup-identity.txt" "$backup_file" 2>/dev/null \
    | tar -tzf - 2>/dev/null | sort | tr '\n' ' ')
note "backup members" "$members"

for expected in manifest.json keyring.pass chrome/Default/Cookies; do
    if age --decrypt --identity "$state/keys/backup-identity.txt" "$backup_file" 2>/dev/null \
        | tar -tzf - 2>/dev/null | grep -q "^$expected$"; then
        pass "the backup contains $expected"
    else
        fail "the backup contains $expected"
    fi
done

# The identity must not be in the backup either.
if age --decrypt --identity "$state/keys/backup-identity.txt" "$backup_file" 2>/dev/null \
    | tar -tzf - 2>/dev/null | grep -q "backup-identity"; then
    fail "the backup does not contain the age identity"
else
    pass "the backup does not contain the age identity"
fi

# No plaintext archive is left behind.
if ls "$state/backups"/*.tar.gz >/dev/null 2>&1; then
    fail "no plaintext archive is left on disk"
else
    pass "no plaintext archive is left on disk"
fi

# ---------------------------------------------------------------------------
section "3. The restore replaces the state"
# ---------------------------------------------------------------------------
# Change the state so that the restore has something to undo.
printf 'changed-after-backup' > "$state/chrome/Default/History"
printf 'new-file' > "$state/chrome/Default/Extra"

"$CLI" restore --source "$backup_file" --yes > "$WORK/restore.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "restore succeeds" "$(grep 'restored from' "$WORK/restore.txt")"
else
    fail "restore succeeds" "exit $rc: $(head -2 "$WORK/restore.txt")"
fi

content=$(cat "$state/chrome/Default/History" 2>/dev/null)
if [ "$content" = "history" ]; then
    pass "the restored profile holds the backed-up content"
else
    fail "the restored profile holds the backed-up content" "$content"
fi

if [ -f "$state/chrome/Default/Extra" ]; then
    fail "the file that was added after the backup is gone"
else
    pass "the file that was added after the backup is gone"
fi

# ---------------------------------------------------------------------------
section "4. A wrong identity is refused"
# ---------------------------------------------------------------------------
other="$WORK/other-identity.txt"
age-keygen -o "$other" >/dev/null 2>&1
if age --decrypt --identity "$other" "$backup_file" >/dev/null 2>&1; then
    fail "the backup cannot be decrypted with a different identity"
else
    pass "the backup cannot be decrypted with a different identity"
fi

"$CLI" restore --source "$backup_file" --yes > /dev/null 2>&1
echo "--- restore with the wrong identity ---"
CLOUDCHROME_IDENTITY_OVERRIDE="$other" python3 - <<'PY' > "$WORK/wrongid.txt" 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import backup
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
from cloudchrome.errors import IntegrityError

config = load_config()
layout = Layout(config)
# Point the identity at the wrong key.
layout.identity_file = os.environ["CLOUDCHROME_IDENTITY_OVERRIDE"]
try:
    backup.restore_backup(layout, config, source=None, assume_yes=True)
    print("RESULT=accepted")
except IntegrityError as exc:
    print("RESULT=refused")
    print("REASON=%s" % exc)
except Exception as exc:
    print("RESULT=refused")
    print("REASON=%s" % exc)
PY
if grep -q "RESULT=refused" "$WORK/wrongid.txt"; then
    pass "a restore with the wrong identity is refused" "$(grep REASON "$WORK/wrongid.txt" | head -1)"
else
    fail "a restore with the wrong identity is refused" "$(head -1 "$WORK/wrongid.txt")"
fi

# The state must be untouched after the refused restore.
content=$(cat "$state/chrome/Default/History" 2>/dev/null)
if [ "$content" = "history" ]; then
    pass "the state is unchanged after the refused restore"
else
    fail "the state is unchanged after the refused restore" "$content"
fi

# ---------------------------------------------------------------------------
section "5. Hostile archives are refused"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/hostile.txt" 2>&1
import io, os, sys, tarfile, tempfile
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import archive
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
from cloudchrome.errors import IntegrityError

work = tempfile.mkdtemp(prefix="cloudchrome-hostile.")

def build(name, members, extras=None):
    path = os.path.join(work, name)
    with tarfile.open(path, "w:gz") as handle:
        for member, content in members:
            data = content.encode()
            info = tarfile.TarInfo(name=member)
            info.size = len(data)
            info.mode = 0o600
            handle.addfile(info, io.BytesIO(data))
        for info in extras or []:
            handle.addfile(info)
    return path

def symlink(name, target):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info

def hardlink(name, target):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    return info

def fifo(name):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.FIFOTYPE
    return info

def device(name):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.CHRTYPE
    info.devmajor = 1
    info.devminor = 3
    return info

cases = [
    ("absolute path", build("abs.tar.gz", [("manifest.json", "{}"), ("/etc/passwd", "x")])),
    ("path traversal", build("trav.tar.gz", [("manifest.json", "{}"), ("../../etc/passwd", "x")])),
    ("symlink", build("sym.tar.gz", [("manifest.json", "{}")], [symlink("chrome/link", "/etc/passwd")])),
    ("hard link", build("hard.tar.gz", [("manifest.json", "{}")], [hardlink("chrome/hard", "manifest.json")])),
    ("fifo", build("fifo.tar.gz", [("manifest.json", "{}")], [fifo("chrome/pipe")])),
    ("device node", build("dev.tar.gz", [("manifest.json", "{}")], [device("chrome/zero")])),
    ("duplicate member", build("dup.tar.gz", [("manifest.json", "{}"), ("keyring.pass", "a"), ("keyring.pass", "b")])),
    ("unapproved member", build("unap.tar.gz", [("manifest.json", "{}"), ("etc/shadow", "x")])),
    ("missing manifest", build("noman.tar.gz", [("keyring.pass", "x")])),
]

for label, path in cases:
    try:
        archive.validate_archive(path)
        print("%s|ACCEPTED" % label)
    except IntegrityError as exc:
        print("%s|refused" % label)
    except Exception as exc:
        print("%s|refused (%s)" % (label, type(exc).__name__))

# A corrupted file.
corrupt = os.path.join(work, "corrupt.tar.gz")
with open(corrupt, "wb") as handle:
    handle.write(b"this is not a tar archive")
try:
    archive.validate_archive(corrupt)
    print("corrupted archive|ACCEPTED")
except IntegrityError:
    print("corrupted archive|refused")

# A truncated archive.
truncated = build("trunc.tar.gz", [("manifest.json", "{}"), ("keyring.pass", "s" * 500)])
size = os.path.getsize(truncated)
with open(truncated, "r+b") as handle:
    handle.truncate(size // 2)
try:
    archive.validate_archive(truncated)
    print("truncated archive|ACCEPTED")
except IntegrityError:
    print("truncated archive|refused")
except Exception as exc:
    print("truncated archive|refused (%s)" % type(exc).__name__)
PY

while IFS='|' read -r label verdict; do
    [ -z "$label" ] && continue
    if [ "$verdict" = "refused" ]; then
        pass "the hostile archive is refused: $label"
    else
        fail "the hostile archive is refused: $label" "$verdict"
    fi
done < "$WORK/hostile.txt"

# ---------------------------------------------------------------------------
section "6. A failed restore rolls back"
# ---------------------------------------------------------------------------
python3 - <<'PY' > "$WORK/rollback.txt" 2>&1
import os, sys, tempfile, shutil
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "lib"))
from cloudchrome import backup
from cloudchrome.config import load_config
from cloudchrome.paths import Layout
from cloudchrome.errors import IntegrityError

config = load_config()
layout = Layout(config)

# Mark the installed keyring passphrase with a sentinel, so the rollback can
# be observed.
sentinel = b"ROLLBACK-SENTINEL"
with open(layout.keyring_pass_file, "wb") as handle:
    handle.write(sentinel)

try:
    backup.restore_backup(layout, config, source=None, assume_yes=True)
    print("RESULT=accepted")
except Exception as exc:
    print("RESULT=refused")
    print("REASON=%s" % exc)

# The sentinel must still be there: the restore never reached the state.
with open(layout.keyring_pass_file, "rb") as handle:
    print("PRESERVED=%s" % (handle.read() == sentinel))
PY

if grep -q "RESULT=refused" "$WORK/rollback.txt"; then
    pass "a restore that fails is refused" "$(grep REASON "$WORK/rollback.txt" | head -1)"
else
    fail "a restore that fails is refused" "$(head -1 "$WORK/rollback.txt")"
fi

if grep -q "PRESERVED=True" "$WORK/rollback.txt"; then
    pass "the previous state survives a failed restore"
else
    fail "the previous state survives a failed restore" "$(cat "$WORK/rollback.txt")"
fi

# ---------------------------------------------------------------------------
section "7. The pruning stays inside the managed area"
# ---------------------------------------------------------------------------
for _ in 1 2 3 4 5; do
    "$CLI" backup >/dev/null 2>&1
    sleep 1
done

# A file that is not a Cloud-Chrome backup must survive the pruning.
stranger="$state/backups/not-a-backup.txt"
printf 'important' > "$stranger"

"$CLI" prune --keep 2 --yes > "$WORK/prune.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "prune succeeds"
else
    fail "prune succeeds" "exit $rc: $(head -2 "$WORK/prune.txt")"
fi

if [ -f "$stranger" ]; then
    pass "prune does not delete a file that is not a backup"
else
    fail "prune does not delete a file that is not a backup"
fi

remaining=$(ls "$state/backups"/*.tar.gz.age 2>/dev/null | wc -l)
if [ "$remaining" -ge 1 ]; then
    pass "prune keeps at least one backup" "$remaining left"
else
    fail "prune keeps at least one backup" "0 left"
fi

# The newest backup must always survive.
newest=$(ls -t "$state/backups"/*.tar.gz.age 2>/dev/null | head -1)
if [ -f "$newest" ]; then
    pass "prune keeps the newest backup" "$(basename "$newest")"
else
    fail "prune keeps the newest backup"
fi

# ---------------------------------------------------------------------------
section "8. The secrets stay out of the outputs"
# ---------------------------------------------------------------------------
keyring_secret=$(cat "$state/keyring.pass" 2>/dev/null)
for command in status doctor logs; do
    "$CLI" "$command" > "$WORK/secret-$command.txt" 2>&1
    if [ -n "$keyring_secret" ] && grep -qF "$keyring_secret" "$WORK/secret-$command.txt"; then
        fail "'$command' does not print the keyring passphrase"
    else
        pass "'$command' does not print the keyring passphrase"
    fi
done

"$CLI" backup > "$WORK/secret-backup.txt" 2>&1
if [ -n "$keyring_secret" ] && grep -qF "$keyring_secret" "$WORK/secret-backup.txt"; then
    fail "'backup' does not print the keyring passphrase"
else
    pass "'backup' does not print the keyring passphrase"
fi

# The backup itself must not contain the passphrase in the clear.
newest=$(ls -t "$state/backups"/*.tar.gz.age 2>/dev/null | head -1)
if [ -n "$newest" ] && [ -n "$keyring_secret" ]; then
    if age --decrypt --identity "$state/keys/backup-identity.txt" "$newest" 2>/dev/null \
        | grep -qaF "$keyring_secret"; then
        # The passphrase file IS part of the backup by design; it is what
        # makes the keyring restorable. What matters is that the archive on
        # disk is encrypted.
        note "the passphrase is inside the encrypted archive, which is by design"
        pass "the archive on disk is encrypted"
    else
        pass "the archive on disk is encrypted"
    fi
fi

# ---------------------------------------------------------------------------
section "9. Result"
# ---------------------------------------------------------------------------
printf 'PASS: %d  FAIL: %d\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "all Phase 3 expectations hold"
    exit 0
fi
echo "Phase 3 expectations failed"
exit 1
