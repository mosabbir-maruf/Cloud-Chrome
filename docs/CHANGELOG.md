# Cloud-Chrome — Changelog

The release notes of the runtime. The version is the one that
`cloudchrome version` prints, and the same one that the backup manifest
records.

- [2.0.0](#200)
- [The versions](#the-versions)

---

## 2.0.0

The first complete release candidate of the runtime. It is a release
candidate and not a published release: the human acceptance items in
`docs/ACCEPTANCE.md` are still pending, and the host-level syscall
limitation is still unresolved.

### The runtime architecture

The whole runtime is one program, `cloudchrome`, and it supervises six
components in dependency order: Xvnc on the loopback interface, a private
D-Bus session bus, an unlocked GNOME Keyring that owns the Secret Service,
an XFCE session with its window manager, the noVNC bridge, and Google
Chrome with its own sandbox.

The order is the security property: the Secret Service has to answer
before Chrome starts, and a keyring that cannot be unlocked is an error
rather than a degraded mode.

**A supervisor that is stopped takes its components with it.** A stop
request stops the supervisor *and* everything the supervisor started, so
no orphaned desktop is left behind. A stop request that arrives while the
stack is still coming up is honoured as well: the waits of the startup
honour it, including the wait for the Secret Service.

Everything lives in the dev container image, which is built by GitHub
Actions and pinned by digest. The runtime downloads nothing, asks for no
capability, and never shells out to `sudo`.

### The CLI

| Command | What it does |
|---|---|
| `init` | create the persistent state, silently |
| `start`, `stop`, `restart` | the lifecycle of the desktop stack |
| `status`, `logs`, `doctor` | what is running, and what is wrong with it |
| `passwd` | rotate the VNC password and print it, once, on a terminal |
| `url` | the noVNC URL of the Codespace |
| `backup`, `restore`, `prune` | the encrypted backup |
| `version` | the version of the runtime |

Every command that changes the state takes the control lock, so a start, a
stop, a backup and a restore never run at the same time. The exit codes
are part of the contract and are documented in `docs/USAGE.md`.

### The persistent profile and the Keyring

The Chrome profile, the GNOME Keyring data and the passphrase live in the
Codespaces persistent volume, under `.cloudchrome/`. Every directory is
mode 0700 and every secret file is mode 0600, and the runtime refuses a
path whose owner or mode is wrong rather than repairing it silently.

The Keyring is fail-closed: the runtime waits until the daemon owns the
`org.freedesktop.secrets` name and the login keyring exists, stores a
probe secret and reads it back, and only then starts Chrome.

### The encrypted backup and the transactional restore

A backup is a tar stream piped straight into `age`. No plaintext archive
is ever written to disk, the VNC password is never in it, and the age
identity is not in it either — the identity is the only thing that can
decrypt the archive, so it has to be copied somewhere safe, outside the
Codespace.

A restore is one transaction under one lock: validate and stage, stop,
preserve, install, verify, start, check the restored Chrome and the Secret
Service, and only then finalise. Any failure stops the restored stack
first, puts the previous profile, keyring and passphrase back, starts the
previous desktop again, checks it, and exits non-zero. The rollback state
is kept until a restore has succeeded and the restored desktop has passed
its health checks.

### The security controls

- Chrome's own sandbox, never `--no-sandbox`, never a DevTools port.
- A seccomp profile that is narrower than the Docker default.
- No `CAP_SYS_ADMIN`, no runtime `sudo`, no public port.
- The raw RFB port is loopback only; only the noVNC port is forwarded, and
  it stays private.
- The VNC password, the keyring passphrase and the age identity never
  reach a log, the process table or a command's output.
- The archive is untrusted input: every member is validated, and the
  size, the member size, the member count and the manifest size are
  bounded.

### The performance

Measured in a real Codespace on the `basicLinux32gb` machine. The startup,
the shutdown and the warm start use ten samples each, the backup and the
restore five each, and the memory, the idle CPU and the latency are
measured once over a window. The startup of the desktop is about 2.5 s,
the shutdown about half a second to a second, the desktop holds about
360 MiB of proportional set size, it uses about 4 % of one CPU while it
is idle, the noVNC client answers in about 4 ms, a backup takes about
5 s and a restore about 4 s.

The startup was halved by removing the waiting from it: a fixed sleep
after Chrome became a condition, and the polls that waited for the state
of the stack poll often enough that the time spent waiting for the poll
is no longer time the user spends waiting for the desktop. The
methodology and the before-and-after are in `docs/PERFORMANCE.md`.

### The known limitations

- **The host-level syscall limitation is unresolved.** The native socket
  path is filtered by the seccomp profile, but the `int 0x80`
  compatibility path reaches `socketcall(2)`, which cannot be filtered by
  a seccomp rule, and `/proc/sys/abi/ia32_emulation` is a global sysctl
  that is not exposed inside the container. Removing `socketcall` from the
  allow-list does **not** close that path. Cloud-Chrome does **not**
  claim complete syscall isolation on a host where this is unresolved.
- **Overwriting a file and unlinking it is not secure erasure.** On
  storage with copy-on-write, wear levelling or snapshots, the old
  content can survive in the free blocks.
- **A successful noVNC response is not a visual check.** The probes prove
  that the port answers and that the RFB handshake completes. A person
  has still to confirm that a browser renders the desktop.
- **The Google website sign-in is a separate question** from the
  browser-level policy. The policy blocks the browser-level account; it
  does not block a sign-in to a website, and that has not been verified.

### The manual acceptance items

Seven items need a person: opening the forwarded port, connecting with
the password, seeing XFCE and Chrome, visiting a website, the Google
website sign-in, a session that survives a stop and a start, and the
profile and the keyring across an actual Codespaces rebuild. They are
listed in `docs/ACCEPTANCE.md` and they are all pending.

---

## The versions

The runtime and the pinned dependencies. The pins live in
`config/versions.env`, and CI refuses a build whose pins are not the
recorded ones.

| Component | Version |
|---|---|
| The runtime | 2.0.0 |
| The backup manifest format | 1 |
| The base image | `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`, pinned by digest |
| Google Chrome | 157.0.8103.50-1 |
| noVNC | 1.7.0 |
| age | 1.3.2 |
| The operating system | Ubuntu 24.04.3 LTS |

The XFCE, the D-Bus, the GNOME Keyring, the Libsecret, the xdotool and
the fonts are tracked rather than pinned: they take the security updates
of the signed Ubuntu archive, and CI asserts that they are never older
than the recorded version.
