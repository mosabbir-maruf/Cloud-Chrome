# Cloud-Chrome — Backup and Restore

How the persistent state is backed up, how it is restored, and what happens
when something goes wrong.

- [What is in a backup](#what-is-in-a-backup)
- [Creating a backup](#creating-a-backup)
- [Restoring](#restoring)
- [The manifest](#the-manifest)
- [Pruning](#pruning)
- [Security properties](#security-properties)

---

## What is in a backup

| Member | What it is |
|---|---|
| `manifest.json` | the description of the backup: format version, creation time, application version, and the size and SHA-256 digest of every member |
| `keyring.pass` | the keyring unlock passphrase |
| `chrome/...` | the persistent Chrome profile |
| `keyring/...` | the GNOME Keyring data |

**The VNC password file is never included.** It is an 8-byte obfuscated key;
anyone who could read it would have the password for the desktop. It is
rotated with `cloudchrome passwd` instead of being restored.

The `keyring/...` members are the files of the **GNOME Keyring store**,
the directory the keyring daemon is told to use. The restore installs them
there, and the verification after the installation checks that directory —
not the legacy `.cloudchrome/keyring` directory, which holds nothing and
would therefore prove nothing about the live store.

The age private key is **not** in the backup either. It lives in
`.cloudchrome/keys/backup-identity.txt` with mode 0600 and it is the only
thing that can decrypt the archive. Export it and store it securely outside
the Codespace if you want to restore a backup after deleting that Codespace.

**Keep the identity outside the Codespace.** The identity and the backups
live in the same persistent volume, so deleting the Codespace takes both
of them. A backup without its identity is a file that nobody can read:
the age format is authenticated, and the wrong identity is refused. Before
you delete a Codespace, export and securely store the identity outside it,
and copy the `.cloudchrome/backups/` directory or any separately saved backup
archives somewhere else too. Do not promise recovery after deletion unless
you have both the matching identity and an archive outside the Codespace.

---

## Creating a backup

```
cloudchrome backup
cloudchrome backup --label "before the upgrade"
cloudchrome backup --output /tmp/my-backup.tar.gz.age
```

The output directory must already exist. It is never created and never
changed, so a shared directory such as `/tmp` keeps its own mode; the backup
file itself is written with mode 0600. The archive is written to a unique
temporary file in the same directory and moved into place, so the target is
never a partly written file and an existing backup is replaced atomically. A
target that is a symbolic link is refused, so the runtime cannot be made to
write through a link to somewhere else.

The command prints the path, the number of members and the SHA-256 digest of
the resulting file.

**The desktop stack is stopped for the snapshot and started again
afterwards.** A profile that Chrome is still writing to is not a consistent
snapshot: Chrome updates SQLite databases, their journals and LevelDB files
while it runs, so hashing the files and streaming them afterwards can record
a digest of content that is no longer there. Stopping the stack first makes
the profile a consistent snapshot. A failure of the restart is never
swallowed: the command reports it and exits non-zero.

The archive is written as a tar stream that is piped straight into `age`, so
**no plaintext archive is ever written to disk**.

The backups live in `.cloudchrome/backups/` and are named
`cloudchrome-backup-<timestamp>.tar.gz.age`.

---

## Restoring

```
cloudchrome restore --source .cloudchrome/backups/cloudchrome-backup-20260101T120000Z.tar.gz.age
cloudchrome restore --yes      # the newest backup, without the question
```

The restore is **one transaction**, from the first byte of the archive to
the health check of the restored desktop:

1. the archive is streamed, every member is validated, and the extracted
   files are checked against the manifest — nothing of the live state is
   touched;
2. the desktop stack is stopped, so Chrome never writes to a profile that is
   being replaced;
3. the current profile, keyring and passphrase are moved aside;
4. the staged state is installed;
5. the ownership and the permissions are verified;
6. the restored desktop is started;
7. the restored Chrome and the Secret Service are checked;
8. only then is the restore finalised and the rollback state disposed of.

If any step fails, the recovery runs in a fixed order, and the command
exits non-zero. A failed restore never reports success and never leaves a
partly installed state.

## The recovery of a failed restore

The recovery does not guess which stack is running. It records what the
transaction itself did — whether the original stack was running, whether
the transaction stopped it, and whether it tried to start the restored
stack — and it acts on that record:

1. **the restored stack is stopped**, and it is confirmed to be stopped,
   before anything of the live state is touched. A restored desktop that is
   still up may be writing to the profile that is about to be replaced. A
   start that failed half way is cleaned up the same way: the stack it
   left behind is stopped.
2. **the previous profile, keyring and passphrase are put back** from the
   retained rollback copy.
3. **the previous desktop is started again, and its health is checked**,
   when the transaction stopped it — that is, when it was running before
   the restore. The health is verified, not assumed.
4. **a stack that was stopped before the restore is left stopped.**

A stack that the transaction did not start is never stopped. When the
failure happens before the original stack was stopped, that stack is left
exactly as it is: it is still running with the data it had.

If the recovery itself fails — the restored stack cannot be stopped, the
rollback does not complete, or the previous desktop cannot be started or
is not usable — the command reports that, exits non-zero, and **keeps the
rollback copy**, so the previous data stays recoverable. Nothing is
discarded and no success is reported.

**The rollback state is kept after a failure**, and its location is written
to the supervisor's log, so the previous profile, keyring and passphrase stay
recoverable whatever went wrong — including the case where the previous
desktop could not be started either, which is reported with the path. The
rollback state is only disposed of once the restore has succeeded and the
restored desktop has passed its health checks.

The rollback state keeps its **own copy** of the previous data: the
previous state is put back by copying it into place, not by moving it, so
the recoverable copy survives even when the state that was put back turns
out to be damaged. A failure in the middle of the installation is rolled
back as well — the previous state is put back even when the installation
failed after the live profile had already been moved aside.

The rollback state is never deleted while a restore is in progress, so an
interrupted restore leaves the previous data on disk rather than losing it.

**The restore starts the restored desktop.** The transaction is verified
against a running desktop, because "the profile was restored" is not the
same as "the profile is usable". A restore therefore leaves the desktop
running, whether it was running before or not.

The files that Chrome owns are rewritten by Chrome when the restored
desktop starts, so the content of the profile after a restore is the
restored profile *as Chrome has used it*, not a byte-for-byte copy of the
backup.

The restore asks for confirmation when it is attached to a terminal. Use
`--yes` for a non-interactive restore.

---

## The manifest

The manifest is written into the archive and is validated before anything is
restored. It records:

* the format version, so an archive from a newer release is refused rather
  than misread;
* the creation time in UTC;
* the application and its version;
* every included path, with its size and its SHA-256 digest.

A restore is refused when:

* the manifest is not valid JSON, or is not a Cloud-Chrome manifest;
* the manifest uses a format version this release does not understand;
* the manifest lists no members;
* a member is listed twice, or has no valid size or digest;
* a member the manifest describes is missing from the archive;
* a member the manifest does not describe is present in the archive;
* a member does not match its recorded size or its recorded SHA-256 digest.

---

## Pruning

```
cloudchrome prune --keep 5            # asks before it deletes
cloudchrome prune --keep 5 --yes      # non-interactive
cloudchrome prune --keep 5 --dry-run  # only shows what would go
```

`--keep` is required: there is no default retention policy.

The pruning only ever considers the files in the managed backup directory
that carry the Cloud-Chrome backup name. It never follows a symbolic link and
it never touches a file outside that directory.

The **newest backup is never pruned**, whatever the retention policy says, so
there is always at least one valid backup.

The command shows what it is about to delete.

---

## The limits

An archive is untrusted input, so it is held to limits. They can be lowered
through the configuration file or the environment, but never raised beyond
the hard ceiling, so the protection cannot be configured away.

| Limit | Default | What it bounds |
|---|---|---|
| `CLOUDCHROME_MAX_ARCHIVE_BYTES` | 2 GiB | the encrypted archive itself |
| `CLOUDCHROME_MAX_TOTAL_BYTES` | 8 GiB | everything the archive expands to |
| `CLOUDCHROME_MAX_MEMBER_BYTES` | 2 GiB | the largest single member |
| `CLOUDCHROME_MAX_MEMBERS` | 200 000 | the number of members |
| `CLOUDCHROME_MAX_MANIFEST_BYTES` | 32 MiB | the manifest itself |

The archive is checked against the size limit before it is decrypted, and the
member count, the member size and the total size are checked while it is
read, so an archive that expands beyond the limits is refused before it can
fill the disk.

## Security properties

The archive is treated as untrusted input. Before anything is extracted,
every member is checked:

| Check | What it refuses |
|---|---|
| the name is relative | `/etc/passwd` |
| the name has no `..` component | `../../etc/passwd` |
| the name has no backslash | `chrome\file` |
| the member is a regular file or a directory | symbolic links, hard links, device nodes, FIFOs, sockets |
| the member is in the approved set | anything outside `chrome/`, `keyring/` and the two top-level files |
| the member appears once | a duplicate path |
| the staging directory is empty | an extraction into a reused directory |

The extraction writes every file with the runtime's own permissions and
never follows a symbolic link, so a link in the archive cannot be used to
write outside the staging directory.

## The temporary plaintext

The restore reads the archive back through a pipe: the plaintext is streamed
from `age --decrypt` into the reader and **never lands on the filesystem**.
There is no plaintext archive file to clean up.

The fallback path, which writes the plaintext into a private directory
first, removes it on every way out of the restore: on success, on failure,
on a timeout and on an interruption. The file is overwritten before it is
unlinked.

**What that does and does not guarantee.** Overwriting and unlinking removes
the plaintext from the filesystem's view, inside the private directory it was
written to. It is **not** a guarantee that the plaintext is gone from the
device: on storage with copy-on-write, wear levelling or snapshots, the
previous content can survive in the free blocks, and the operating system's
own wear levelling can copy the old blocks before they are reused. Cloud-
Chrome does not claim secure erasure, and it does not claim that the
plaintext cannot be recovered from the raw device. The guarantee it makes is
that the plaintext is not left in a file, in a directory another user can
read, or in the backup directory.

The encryption uses an age key pair, not a passphrase. No passphrase is ever
placed in a command-line argument, an environment variable, a log or a shell
history.
