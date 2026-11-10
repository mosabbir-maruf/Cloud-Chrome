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

The age private key is **not** in the backup either. It lives in
`.cloudchrome/keys/backup-identity.txt` with mode 0600 and it is the only
thing that can decrypt the archive. Copy it somewhere safe if you want to be
able to restore the backup on another machine.

---

## Creating a backup

```
cloudchrome backup
cloudchrome backup --label "before the upgrade"
cloudchrome backup --output /tmp/my-backup.tar.gz.age
```

The command prints the path, the number of members and the SHA-256 digest of
the resulting file.

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

The restore is **transactional**:

1. the archive is decrypted into a private temporary directory;
2. every member is validated, and the extracted files are checked against
   the manifest;
3. the desktop stack is stopped, so Chrome never writes to a profile that is
   being replaced;
4. the current profile and keyring are moved aside;
5. the staged state is installed;
6. the ownership and the permissions are verified;
7. the desktop stack is started again.

If any step fails, the previous state is put back. A failed restore never
leaves a partly replaced profile and never loses the previous keyring, and
the exact failure is reported.

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

The temporary plaintext archive is overwritten and removed as soon as the
restore is done, and it never leaves a private mode-0700 directory.

The encryption uses an age key pair, not a passphrase. No passphrase is ever
placed in a command-line argument, an environment variable, a log or a shell
history.
