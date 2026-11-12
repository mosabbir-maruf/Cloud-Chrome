# Cloud-Chrome

A Chrome desktop that runs inside a GitHub Codespace, and that you use
from the browser.

The container runs a real Linux desktop — Xvnc, XFCE, GNOME Keyring and
Google Chrome — behind a VNC server. The Codespaces port panel forwards the
noVNC port to a private URL, and that URL is the whole interface: there is
no other way in, and there is nothing to install on your machine.

```bash
cloudchrome status      # what is running
cloudchrome passwd      # rotate the VNC password and print it
cloudchrome url         # the noVNC URL of this Codespace
```

- [What it does](#what-it-does)
- [The architecture](#the-architecture)
- [The browser-only access model](#the-browser-only-access-model)
- [Quick start](#quick-start)
- [The commands](#the-commands)
- [Persistence and the Keyring](#persistence-and-the-keyring)
- [Encrypted backups](#encrypted-backups)
- [Security controls](#security-controls)
- [Known limitations](#known-limitations)
- [Performance](#performance)
- [The documentation](#the-documentation)

---

## What it does

- **A real desktop in the Codespace.** Chrome runs against a persistent
  profile on a real X display, with a window manager, a Secret Service and
  the fonts a page expects.
- **A browser is the only client.** The noVNC client is served over the
  forwarded port; the raw RFB port never leaves the container.
- **The state outlives the container.** The Chrome profile, the GNOME
  Keyring and the backups live on the Codespaces persistent volume, so
  they are there when the container is replaced. A rebuild of the image
  keeps them as well, because the volume is what holds them; that last
  one is the design, and no rebuild has been performed end to end yet.
- **The secrets stay secret.** The VNC password, the keyring passphrase
  and the encryption identity are private files, never printed into a log
  and never in a backup.
- **A backup is a transaction.** A restore is validated, staged, installed,
  verified and started as one operation under one lock. A failure puts the
  previous state back.

---

## The architecture

The runtime is one program, `cloudchrome`, and it supervises six
components in dependency order:

```
supervisor
├── xvnc      Xvnc on :99, bound to the loopback interface
├── dbus      a private session bus
├── keyring   gnome-keyring-daemon, unlocked, owning the Secret Service
├── desktop   xfce4-session, with its window manager
├── novnc     websockify, serving the noVNC client on 6080
└── chrome    Google Chrome, with its own sandbox, against the profile
```

The order matters. The Secret Service has to answer before Chrome starts,
because Chrome stores its passwords there and a locked keyring would make
every credential fail.

The components live in the dev container image
(`.devcontainer/Dockerfile`), which is built by GitHub Actions and pinned
by digest. The runtime never downloads anything and never runs anything
with more privilege than the user.

The code is in `lib/cloudchrome/`, and `bin/cloudchrome` is the only entry
point. `docs/SECURITY.md` describes the isolation in full.

---

## The browser-only access model

There is exactly one way in, and it is a browser:

| Port | What | Reachability |
|---|---|---|
| 6080 | the noVNC client, over a WebSocket | the only forwarded port, and it stays private |
| 5900 | the raw RFB protocol | loopback only, never forwarded |

The VNC server is started with `-localhost`, so the raw protocol is not
reachable even from inside the container's network namespace. The forwarded
port is never published: a Codespace's forwarded ports are private to the
account that owns the Codespace.

The noVNC client asks for the VNC password. There is no unauthenticated
path to the desktop, and there is no DevTools endpoint: Chrome is started
without `--remote-debugging-port`, so nothing can attach to it from
outside.

Keep the forwarded port private. If you need to reach the desktop from
another machine, use the Codespaces port forwarding, which keeps the
authentication and the privacy of the Codespace.

---

## Quick start

Open the repository as a GitHub Codespace. The dev container initialises
the state and starts the desktop for you.

Then, in the Codespace terminal:

```
cloudchrome status
```

Open the port panel and click the `6080` port. The noVNC client asks for
the VNC password:

```
cloudchrome passwd
```

That rotates the password and prints it. It refuses to print when standard
output is not a terminal, so the password never lands in a pipe, a log or
a shell history by accident.

---

## The commands

| Command | What it does |
|---|---|
| `cloudchrome init` | create the persistent state, silently |
| `cloudchrome start` | start the desktop stack and wait until it is healthy |
| `cloudchrome stop` | stop the desktop stack and every process it started |
| `cloudchrome restart` | stop and start again |
| `cloudchrome status` | what is running, and on which ports |
| `cloudchrome logs` | the logs of the runtime and of its components |
| `cloudchrome doctor` | the diagnostics: ownership, permissions, ports, sandbox |
| `cloudchrome passwd` | rotate the VNC password and print it |
| `cloudchrome url` | the noVNC URL of this Codespace |
| `cloudchrome version` | the version of the runtime |
| `cloudchrome backup` | write an encrypted backup of the profile and the keyring |
| `cloudchrome restore` | restore a backup as one transaction |
| `cloudchrome prune` | delete the old backups, keeping a number of them |

The commands that change the state — `start`, `stop`, `restart`, `backup`,
`restore` and `prune` — take the control lock, so they never run at the
same time as each other. `init`, `status`, `logs`, `doctor`, `passwd`,
`url` and `version` only read the state, and they do not wait for the
lock.

`docs/USAGE.md` describes every command, every exit code and the layout
of the state.

---

## Persistence and the Keyring

Everything the runtime writes lives under `.cloudchrome/` in the
workspace, which is the Codespaces persistent volume:

```
.cloudchrome/
  chrome/            the persistent Chrome profile
  xdg/keyrings/      the GNOME Keyring data
  keyring.pass       the keyring unlock passphrase        (mode 0600)
  vnc.pass           the TigerVNC password file           (mode 0600)
  logs/              the runtime logs
  run/               pid files, locks, sockets
  backups/           the encrypted backups
  keys/              the age identity and recipient
```

Every directory is mode `0700` and every file that holds a secret is mode
`0600`. The runtime refuses a path whose owner or mode is wrong rather
than repairing it silently.

**The Keyring is fail-closed.** The runtime waits until the daemon owns
the `org.freedesktop.secrets` name and the login keyring exists, and then
it stores a probe secret and reads it back. Chrome is only started when
that works. A keyring that cannot be unlocked is an error, not a
degraded mode: a locked keyring would make every stored credential fail
silently, which is worse than a desktop that refuses to start.

Chrome's own browser-level sign-in is turned off by the managed policy
(`SyncDisabled`, `BrowserSignin: 0`). Signing into a *website* is a
different thing and is not affected by that policy — see the acceptance
checklist.

---

## Encrypted backups

```bash
cloudchrome backup                                  # into the managed directory
cloudchrome backup --output /tmp/my-backup.tar.gz.age   # somewhere else
cloudchrome restore --source /tmp/my-backup.tar.gz.age --yes
```

The backup holds the Chrome profile, the GNOME Keyring data and the
keyring passphrase. It is a tar stream piped straight into `age`, so no
plaintext archive is ever written to disk, and it is bounded: the size of
the archive, the size it expands to, the size of its largest member, the
number of its members and the size of its manifest are all limited.

**The age identity is not in the backup.** It lives in
`.cloudchrome/keys/backup-identity.txt` and it is the only thing that can
decrypt the archive. **Copy it somewhere safe, outside the Codespace.**
If you delete the Codespace without having copied the identity and the
backups, the backups cannot be recovered by anyone.

The VNC password is not in the backup either. It is rotated with
`cloudchrome passwd`.

A restore is one transaction: the archive is validated and staged, the
stack is stopped, the previous state is preserved, the staged state is
installed, it is verified, the restored desktop is started and checked,
and only then is the restore finalised. If any step fails, the restored
stack is stopped first, the previous profile, keyring and passphrase are
put back, the previous desktop is started again, and the command exits
non-zero. The rollback state is kept until a restore has succeeded and the
restored desktop has passed its health checks.

`docs/BACKUP.md` describes the format, the limits, the recovery and the
cleanup.

---

## Security controls

The isolation is built into the image and cannot be turned off from
inside the container:

- **Chrome's own sandbox.** Chrome runs with its setuid and namespace
  sandbox. `--no-sandbox` is refused, and so is every other flag that
  would weaken the browser.
- **A seccomp profile** that is narrower than the Docker default: the
  32-bit `socketcall` compatibility entry point is removed, and the
  namespace and clone flags Chromium actually performs are the only ones
  allowed.
- **No capabilities.** The container has no `CAP_SYS_ADMIN`, and the
  runtime never asks for one.
- **No runtime privilege escalation.** There is no `sudo` rule for the
  runtime user, and the runtime never shells out to `sudo`.
- **A private state directory**, and a refusal to use a path whose owner
  or mode is wrong.
- **Secret handling.** The VNC password, the keyring passphrase and the
  age identity are private files. They are never printed into a log, never
  appear in the process table and never appear in the output of a command
  that is not explicitly asked for.
- **Fail-closed secrets.** Chrome is not started when the Secret Service
  cannot be verified.

`docs/SECURITY.md` has the details and the measured evidence.

---

## Known limitations

**The host-level syscall limitation is not resolved.** The
`int 0x80` compatibility path reaches `socketcall(2)`, whose socket domain
is behind a pointer and therefore cannot be filtered by a seccomp rule.
The profile removes the allowance, which is stricter than the upstream
default, but it does not close the entry point. Closing it needs
`ia32_emulation=0`, and `/proc/sys/abi/ia32_emulation` is a global,
non-namespaced sysctl that is not exposed inside the container. It needs a
host-level control.

**Cloud-Chrome therefore does not claim complete syscall isolation on a
host where this remains unresolved**, and it is not a production-security
guarantee.

Two more, for honesty:

- **A successful noVNC response is not a visual check.** The probe proves
  that the port answers and that the RFB handshake completes. It does not
  prove that a browser renders the desktop. That needs a human.
- **Overwriting a file and unlinking it is not secure erasure.** On
  storage with copy-on-write, wear levelling or snapshots, the old
  content can survive in the free blocks.

---

## Performance

Measured on the `basicLinux32gb` Codespace (2 CPUs, 8 GB), with the
median and the p95 of at least ten samples:

| | |
|---|---|
| the startup of the desktop | about 2.5 s, down from about 4.9 s |
| the shutdown | about 0.5 s to 1.0 s |
| the memory of the desktop | about 360 MiB of proportional set size |
| the idle CPU of the desktop | about 4 % of one CPU |
| the noVNC response | about 4 ms |
| a backup | about 5 s |
| a restore | about 4 s |

`docs/PERFORMANCE.md` has the methodology, the raw samples, the
before-and-after of the optimisation and the remaining bottlenecks.

---

## The documentation

| Document | What is in it |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | every command, every exit code, the state layout, troubleshooting |
| [docs/BACKUP.md](docs/BACKUP.md) | the backup format, the limits, the transaction, the recovery |
| [docs/SECURITY.md](docs/SECURITY.md) | the isolation, the measured evidence, the limitations |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | the methodology and the measurements |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | what is verified automatically, and what still needs a human |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | the release notes |
| [docs/RELEASE.md](docs/RELEASE.md) | the release checklist |

The evidence of the last run against a real Codespace is published as an
issue in this repository.
