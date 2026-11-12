# Cloud-Chrome — Usage

How to run a Chrome desktop in a GitHub Codespace, and what every command
does.

The whole runtime is one program: `bin/cloudchrome`. It is installed in the
image as `/usr/local/bin/cloudchrome`, so it is on `PATH` in the Codespace
terminal.

- [Quick start](#quick-start)
- [The commands](#the-commands)
- [Exit codes](#exit-codes)
- [Where the state lives](#where-the-state-lives)
- [Accessing the desktop](#accessing-the-desktop)
- [The VNC password](#the-vnc-password)
- [Troubleshooting](#troubleshooting)

---

## Quick start

Open the repository as a GitHub Codespace. The dev container does the rest:
the post-create hook initialises the state silently, and the post-start hook
starts the desktop when the container starts.

Then, in the Codespace terminal:

```
cloudchrome status
```

The desktop is reachable through the forwarded port 6080, which the Codespaces
port panel opens in the browser. The browser asks for the VNC password.

To see the password:

```
cloudchrome passwd
```

That rotates the password and prints the new one. It refuses to print when
standard output is not a terminal, so the password never ends up in a pipe, a
log or a shell history by accident.

---

## The commands

| Command | What it does |
|---|---|
| `cloudchrome init` | Creates the persistent state. Silent and non-interactive. Idempotent: running it again never destroys anything. |
| `cloudchrome start` | Starts the desktop stack. |
| `cloudchrome start --if-needed` | Starts the desktop stack only when it is not already running. |
| `cloudchrome stop` | Stops the desktop stack gracefully. |
| `cloudchrome restart` | Stops the stack and starts it again. |
| `cloudchrome status` | Shows the supervisor, the components and the ports. |
| `cloudchrome logs [--component NAME] [--lines N]` | Shows the runtime logs. |
| `cloudchrome doctor` | Runs the diagnostics, including the known host-level limitation. |
| `cloudchrome passwd` | Rotates the VNC password and prints it once. |
| `cloudchrome url` | Prints the noVNC URL. |
| `cloudchrome version` | Prints the version. |
| `cloudchrome backup` | Writes an encrypted backup. |
| `cloudchrome restore` | Restores an encrypted backup. |
| `cloudchrome prune --keep N` | Deletes old encrypted backups. |

### `init`

Creates the persistent state directory and everything in it: the Chrome
profile directory, the keyring directory, the keyring passphrase and the VNC
password file.

It prints nothing. It never prompts. It is safe to run again: an existing
keyring passphrase and an existing VNC password are kept, so a rebuild does
not lock the user out of their own keyring.

### `start`

Starts the supervisor, which starts the whole desktop stack in dependency
order: the X/VNC server, the D-Bus session bus, the GNOME Keyring, the XFCE
session and window manager, the noVNC bridge and finally Chrome.

The command waits until the stack reports itself ready, and prints
`the desktop stack is running`. If a component cannot start, the command says
which one and why, and nothing is left running.

`--if-needed` makes the command a no-op when the stack is already running,
which is what the post-start hook uses.

### `stop`

Stops the stack in the reverse order. Chrome is asked to exit first, so the
profile is written cleanly. The command is safe to run when nothing is
running.

### `status`

Shows the supervisor's PID, the state of every component and the state of the
two ports. It never prints a secret.

### `logs`

Shows the tail of the runtime logs. The component logs are one file each:
`supervisor.log`, `xvnc.log`, `dbus.log`, `keyring.log`, `desktop.log`,
`novnc.log` and `chrome.log`.

```
cloudchrome logs --component chrome --lines 50
```

Secrets are redacted from the output.

### `doctor`

Runs the diagnostics and prints one line per check. It covers the identity
and the privileges of the runtime user, the permissions of the state
directory, the secrets, the running stack, the two ports, the Chrome argument
vector and the known host-level limitation.

It never prints a secret.

### `passwd`

Rotates the VNC password. The new password file is written by
`tigervncpasswd` and installed atomically, so the sessions that are already
authenticated stay authenticated: the server reads the file when a new
connection arrives.

The password is printed only when standard output is an interactive terminal.
Without a terminal the rotation still happens, but nothing is printed and the
command says so.

The stored password is an **8-byte obfuscated key**, not encrypted storage.
Anyone who can read the file can mount an offline attack on the password.

### `url`

Prints the URL of the noVNC client. In a Codespace this is the forwarded
port; elsewhere it is the local address.

### `backup`, `restore` and `prune`

These are documented in [BACKUP.md](BACKUP.md).

The backup is encrypted with age and streamed, so no plaintext archive is
ever written to disk. The restore is transactional: the previous profile and
keyring are put back if anything fails.

`prune` requires an explicit retention policy (`--keep N`), shows what it is
about to delete and never removes the newest backup.

---

## Exit codes

Every command returns one of these codes, so scripts can branch on them.

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | A generic runtime failure. The message says what went wrong. |
| 2 | The arguments were invalid. The usage message was printed. |
| 3 | The state has not been created yet; run `cloudchrome init`. |
| 4 | The state exists but cannot be trusted: wrong owner, wrong mode, corrupt or unsafe. |
| 5 | The operation needs a running desktop stack and there is none. |
| 6 | The environment cannot support the operation (missing dependency, missing secret service, unsupported platform). |
| 7 | An archive, a manifest or a checksum failed validation. Nothing was changed. |
| 8 | Another operation holds the lock. The command is safe to repeat later. |
| 130 | The command was interrupted. |

---

## Where the state lives

Everything the runtime writes is under one directory in the workspace:

```
.cloudchrome/
  chrome/            the persistent Chrome profile
  xdg/keyrings/      the GNOME Keyring data
  xdg/               the XDG data directory
  cache/             the XDG cache directory
  config/            the XDG configuration directory
  keyring.pass       the keyring unlock passphrase        (mode 0600)
  vnc.pass           the TigerVNC password file           (mode 0600)
  logs/              the runtime logs
  run/               pid files, locks, sockets
  run/xdg/           the XDG runtime directory
  backups/           the encrypted backups
  keys/              the age identity and recipient
```

**The GNOME Keyring data is in `xdg/keyrings/`.** That is the directory
the keyring daemon is told to use, and it is where the daemon writes the
login keyring and the user keystore. The legacy `keyring/` directory
beside it holds nothing; it is not where the keyring lives, and the
runtime does not treat it as proof that the live store is safe.

The workspace is the Codespaces persistent volume, so the profile, the
keyring and the backups are there when the container is replaced. Whether a
**rebuild** of the image keeps them depends on the Codespaces volume
surviving that rebuild; that has not been verified end to end, so it is
listed as a pending acceptance item in `docs/ACCEPTANCE.md`.

The directory is listed in `.gitignore` and excluded from the Docker build
context, so no personal data ever reaches Git or an image.

Every directory is mode `0700` and every file that holds a secret is mode
`0600`. The runtime refuses to use a path whose owner or mode is wrong
rather than repairing it silently.

---

## Accessing the desktop

Only one port is reachable from outside the container:

| Port | What | Reachability |
|---|---|---|
| 6080 | noVNC over WebSocket | the only forwarded port |
| 5900 | the raw RFB protocol | loopback only, never forwarded |

The VNC server is started with `-localhost`, so the raw protocol is not
reachable even from inside the container's network namespace. The forwarded
port stays private: it is never configured as a public port.

The noVNC interface asks for the VNC password. There is no unauthenticated
path to the desktop.

The browser reaches the desktop through the forwarded port 6080. In a
Codespace the port panel opens it; `cloudchrome url` prints the same URL.

**Keep the forwarded port private.** A forwarded Codespaces port is
reachable by the account that owns the Codespace and by nobody else, and
it is never published to the internet. The noVNC client asks for the VNC
password in any case, so a forwarded port that is made public is a
password on the public internet.

The raw RFB port stays on the loopback interface. It is not forwarded, and
`Xvnc` is started with `-localhost`, so it is not reachable from the
container's network namespace either.

---

## The Chrome profile

The browser profile lives in `.cloudchrome/chrome/`, which is inside the
Codespaces persistent volume. It therefore survives:

* a stop and a start,
* a restart of the container.

It is on the persistent volume, so a rebuild of the image keeps it as
well. That last one is the design, not a measurement: no rebuild has been
performed end to end, and it is a pending item in the acceptance
checklist.

Cookies, the browsing history, the preferences, the extensions and the
signed-in website sessions are all part of it, so a session that was signed
in on one day is still signed in the next.

Only one Chrome may use the profile at a time. Chrome's own profile lock
takes care of that, and the supervisor only ever starts one browser.

**The browser-level Google account is turned off.** The managed policy
sets `BrowserSignin: 0` and `SyncDisabled: true`, so Chrome does not ask
to sign in to a Google account and does not sync the profile.

That is a **browser-level** control, and it is worth being precise about
what it is not: it does not block a sign-in to a *website*. Signing in to
a Google website from the desktop is a separate matter, and it has not
been verified. Both are listed in `docs/ACCEPTANCE.md`.

A profile that is in use when the container is stopped uncleanly is not
deleted: Chrome writes its state when it exits, and the next start opens the
same profile. Nothing in the runtime resets the profile.

---

## The GNOME Keyring

The runtime starts its own D-Bus session bus and its own GNOME Keyring, and
it unlocks the keyring with a passphrase that is generated once and kept in
`.cloudchrome/keyring.pass` with mode `0600`.

Chrome is started with `--password-store=gnome-libsecret`, so the passwords
it stores go into the keyring and not into a file in the clear.

The passphrase is never printed, never in a log, never in the process table
and never in an environment variable. It is written to the keyring daemon's
standard input and to nothing else.

The keyring data lives in `.cloudchrome/xdg/keyrings/`, which is where the
keyring daemon writes the login keyring and the user keystore. It is on
the persistent volume, so the secrets survive a restart of the container
and, by the design of the persistent volume, a rebuild of the image as
well. The rebuild itself has not been performed end to end; it is a
pending item in the acceptance checklist.

**The runtime fails closed.** Before Chrome is started, the secret service is
verified by storing a secret and reading it back. If that does not work,
Chrome is not started at all: it is never started with a plaintext password
store as a fallback.

`cloudchrome doctor` reports the state of the keyring and the secret
service.

---

## Troubleshooting

**The desktop does not come up.**

```
cloudchrome status
cloudchrome logs --component supervisor
```

The supervisor logs which component failed and why, and every component has
its own log in `.cloudchrome/logs/`.

**Chrome does not start.**

The most common cause is that the Secret Service is not usable. Chrome is
never started with a plaintext password store, so the runtime fails closed
instead. `cloudchrome logs --component keyring` shows what the keyring
reported.

**The browser shows a black screen.**

The desktop is still starting. `cloudchrome status` shows the state of the
XFCE session; the desktop is ready when `desktop` reports `running`.

**The VNC password is rejected.**

The password was rotated. Run `cloudchrome passwd` in an interactive terminal
and use the new one.

**A second `cloudchrome start` refuses.**

The stack is already running. Use `cloudchrome restart` to replace it.

**`cloudchrome init` fails with "the state directory exists but is not a
directory" or an ownership error.**

The state directory was changed outside the runtime. Check it:

```
ls -la .cloudchrome
```

It must be owned by the runtime user and have mode `0700`.

---

## The known host-level limitation

The container's seccomp filter does not cover the 32-bit compatibility
syscall architecture (`int 0x80`), so that entry point stays reachable. The
AF_ALG restriction is therefore enforced on the native syscall path only.

The only control over the compatibility entry point is the host-level
`/proc/sys/abi/ia32_emulation` sysctl, which is not exposed inside the
container.

`cloudchrome doctor` states this on every run. Cloud-Chrome does not claim
complete syscall isolation on a host where this remains unresolved, and it
does not try to close the path with privilege escalation or by weakening the
sandbox.
