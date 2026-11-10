# Cloud-Chrome — Security

Security documentation for the dev container image: the enforced policies, the seccomp
baseline and its derivation, the measured sandbox and privilege results, and the notes
carried forward to later phases.

- Pinned versions, checksums and the seccomp baseline reference: `config/versions.env`.
- Profile derivation and verification: `tests/probe/render-seccomp-profile.sh`.
- Regression suite: `tests/probe/seccomp-regression.sh`.
- Platform experiment harness: `tests/probe/sandbox-probe.sh`.

**Evidence status:** every measurement below was taken on **2026-11-10/11** either on a
GitHub-hosted CI runner or inside a real GitHub Codespaces dev container
(region `CentralIndia`, machine `basicLinux32gb` = 2 cores / 8 GB RAM / 32 GB storage,
kernel `6.8.0-1064-azure`, Ubuntu 24.04.3 LTS). Raw transcripts are published as
repository issues #1–#8; the Phase 1.1 run is issue #8.

---

## 1. Enforced policies in the image

| Control | Enforcement | Where |
|---|---|---|
| Chrome Sync disabled | Managed policy `SyncDisabled: true` | `/etc/opt/chrome/policies/managed/cloudchrome.json` |
| Browser-level Google account disabled | Managed policy `BrowserSignin: 0` | same file |
| Google **website** sign-in remains available | Not a browser-signin control; verified in the Phase 2 acceptance test | — |
| Policy file is root-owned and unwritable | `chown root:root`, `chmod 0644`; asserted at build time and in CI, including that the runtime user can neither write the file **nor** replace it | `.devcontainer/Dockerfile`, `ci.yml` |
| **No blanket passwordless root** | `/etc/sudoers.d/vscode` removed; `sudo -n true` must fail | `.devcontainer/Dockerfile` |
| Container runs as a non-root user | `USER vscode`; verified in CI (`user=vscode uid=1000`) | `.devcontainer/Dockerfile` |
| Only port 6080 is forwarded | `forwardPorts: [6080]` | `.devcontainer/devcontainer.json` |
| Chromium sandbox enabled | Tailored seccomp profile; verified per renderer | `.devcontainer/seccomp/chrome.json` |
| No `--no-sandbox` | Never passed; asserted in CI and in the platform probe | `ci.yml`, `tests/probe/seccomp-regression.sh` |
| No `CAP_SYS_ADMIN` in the container | Never requested; the bounding set is recorded in every probe | `ci.yml`, `tests/probe/seccomp-regression.sh` |
| No personal browser data, secrets or backups in the image or build context | `.dockerignore`, `git ls-files` guard, in-image scan | `.dockerignore`, `ci.yml` |

### Changing the isolation policy

The managed policy directory is administrator-controlled and the file is root-owned, so
the runtime user cannot remove or edit it — and no runtime switch exists.

To enable Chrome Sync deliberately:

1. Edit `.devcontainer/chrome-policies/managed/cloudchrome.json` in the repository
   (`BrowserSignin: 1`, `SyncDisabled: false`).
2. Commit the change.
3. **Rebuild the Codespace** so the new image is built and deployed.
4. Restart Chrome and sign in to Chrome from the Chrome UI.

The strict policy remains in force until the rebuilt image is deployed.

---

## 2. Seccomp baseline

| | |
|---|---|
| Upstream project | `github.com/moby/profiles` (the module that provides Docker's default profile) |
| Pinned commit | `7b5ccfab2d6b4e0aa64c6cce401a2705788eafeb` — *"seccomp: restrict socketpair to AF_UNIX and AF_TIPC"*, 2026-11-02 |
| Source URL | `https://raw.githubusercontent.com/moby/profiles/7b5ccfab2d6b4e0aa64c6cce401a2705788eafeb/seccomp/default.json` |
| SHA-256 | `15434cc9c9037d85b35027a60721bf45f381782b4283b01d94663366e71569f8` (18 182 bytes) |
| Recorded in | `config/versions.env` (`SECCOMP_BASE_*`) |

That baseline replaced the old "allow every address family except AF_VSOCK" rule with a
**socket-domain allow-list**, and it restricts `socketpair(2)` to AF_UNIX/AF_TIPC.

### Deviations from the upstream baseline

Three, all deliberate, all generated from the pinned baseline by
`tests/probe/render-seccomp-profile.sh`:

| # | Deviation | Why |
|---|---|---|
| 1 | `unshare` permitted with `CLONE_NEWUSER` only | Chromium's namespace sandbox calls exactly `unshare(CLONE_NEWUSER)` (`sandbox/linux/services/credentials.cc`) |
| 2 | `clone` permitted with `CLONE_NEWUSER\|CLONE_NEWPID\|CLONE_NEWNET` plus the 8-bit termination signal | Chromium's namespace sandbox uses `clone(CLONE_NEWUSER\|SIGCHLD)`, `clone(CLONE_NEWPID\|SIGCHLD)` and the namespace set `{CLONE_NEWUSER, CLONE_NEWPID, CLONE_NEWNET}` (`namespace_sandbox.cc`) |
| 3 | `socketcall` removed from the allow-list, and `personality(ADDR_NO_32BIT)` added | The 32-bit compatibility entry point (`int 0x80`) reaches `socketcall(2)`, whose socket domain sits behind a pointer and therefore cannot be filtered. Removing the allowance and permitting `ADDR_NO_32BIT` are the in-image steps towards closing that path (see §4.4 for the measured outcome) |

**Deliberate loss of 32-bit compatibility:** this image is amd64-only. No 64-bit program in
it uses the 32-bit syscall multiplexer, and the profile no longer permits it.

The upstream baseline is otherwise kept **byte for byte**. `render-seccomp-profile.sh
--verify` regenerates the profile from the pinned baseline and CI diffs it against the
committed file, so an upstream restriction cannot be dropped silently.

### Namespace policy — narrowed to what Chromium actually performs

| Rule | Mask | Permitted |
|---|---|---|
| `unshare` | `18446744073441116159` (`~CLONE_NEWUSER`) | `unshare(CLONE_NEWUSER)` and nothing else |
| `clone` | `18446744071830503168` (`~(CLONE_NEWUSER\|CLONE_NEWPID\|CLONE_NEWNET\|CSIGNAL)`) | `clone(2)` with Chromium's namespace flags plus a termination signal |

`CSIGNAL` is 8 bits (`0x000000FF`). Only those bits are signal bits; the rest of the low
16 bits are other clone flags (`CLONE_VM` is `0x100`), and they are **not** permitted
alongside a namespace flag.

`setns` is **not** permitted: Chromium's sandbox does not call it.
`clone3` still returns `ENOSYS`, so Chromium falls back to `clone(2)`.

---

## 3. Capability model

Two different things are called "CAP_SYS_ADMIN" here, and they must not be conflated:

| Scope | Statement |
|---|---|
| **Container / initial user namespace** | The container's capability bounding set does **not** contain `CAP_SYS_ADMIN` (`CapBnd=00000000a80425fb` on the CI runner, `00000000a88425fb` in the Codespace). The runtime user holds no capabilities at all (`CapEff=0`) and has no sudo rule. The setuid-sandbox path is therefore unavailable. |
| **Chromium's isolated child user namespace** | Chromium's namespace sandbox creates a **child user namespace** for each sandboxed process. Inside that namespace the process legitimately holds namespace-scoped capabilities — including `CAP_SYS_ADMIN` — which are scoped to namespaces it just created and grant no privilege over the container or the host. `CapBnd=000001ffffffffff` on a sandboxed renderer reflects exactly that. |

These are namespace-scoped capabilities, **not** host-level privilege. They are the
mechanism by which the sandbox works, not an escalation.

---

## 4. Measured results

### 4.1 Namespace and socket behaviour — three profiles

The same image and the same probe, three filters. "Upstream baseline" is the pinned
Moby profile unchanged; "daemon default" is whatever the Docker daemon applies on its own.

| Probe | Cloud-Chrome profile | Upstream baseline | Daemon default |
|---|---|---|---|
| `socket(AF_INET)` (control) | ALLOWED | ALLOWED | ALLOWED |
| `socket(AF_ALG=38)` | **DENIED (EPERM)** | DENIED (EPERM) | DENIED (EPERM) |
| `socket(AF_VSOCK=40)` | **DENIED (EPERM)** | DENIED (EPERM) | DENIED (EPERM) |
| `socketcall` | not applicable (x86_64) | not applicable | not applicable |
| `socketpair(AF_UNIX=1)` | ALLOWED | ALLOWED | ALLOWED |
| `socketpair(AF_NETLINK=16)` | DENIED (EPERM, seccomp) | DENIED (EPERM) | DENIED (ESOCKTNOSUPPORT, kernel) |
| `unshare(CLONE_NEWUSER)` | **ALLOWED** | DENIED (EPERM) | DENIED (EPERM) |
| `unshare(CLONE_NEWUSER\|CLONE_NEWNET)` | DENIED (EPERM) | DENIED (EPERM) | DENIED (EPERM) |
| `unshare(CLONE_NEWNET)` | DENIED (EPERM) | DENIED (EPERM) | DENIED (EPERM) |
| `unshare(CLONE_NEWNS)` | DENIED (EPERM) | DENIED (EPERM) | DENIED (EPERM) |
| `unshare(CLONE_NEWUSER\|CLONE_NEWIPC\|CLONE_NEWUTS)` | DENIED (EPERM) | DENIED (EPERM) | DENIED (EPERM) |
| `clone(CLONE_NEWUSER\|SIGCHLD)` | **ALLOWED** | DENIED (EPERM) | DENIED (EPERM) |
| `clone(CLONE_NEWNET\|SIGCHLD)` | DENIED (EPERM) | DENIED (EPERM) | DENIED (EPERM) |
| `clone(CLONE_NEWNS\|SIGCHLD)` | DENIED (EPERM) | DENIED (EPERM) | DENIED (EPERM) |

### 4.2 Namespace flags combined with unrelated clone flags

Each of these is denied **by seccomp** (`EPERM`), not by the kernel
(`EINVAL`/`EFAULT` would mean the kernel refused the combination first):

| Call | Result |
|---|---|
| `clone(CLONE_NEWUSER\|CLONE_VM\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_NEWUSER\|CLONE_FS\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_NEWUSER\|CLONE_FILES\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_NEWUSER\|CLONE_SIGHAND\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_NEWUSER\|CLONE_PIDFD\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_NEWUSER\|CLONE_PTRACE\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_NEWUSER\|CLONE_VFORK\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_NEWUSER\|CLONE_PARENT\|SIGCHLD)` | DENIED:EPERM (seccomp) |
| `clone(CLONE_VM\|CLONE_FS\|SIGCHLD)` (no namespace flag) | **ALLOWED** — thread creation must keep working |
| `unshare(CLONE_NEWUSER)` | **ALLOWED** — Chromium's operation |
| `clone(CLONE_NEWUSER\|SIGCHLD)` | **ALLOWED** — Chromium's operation |

Every clone in the suite runs with its own stack, exits immediately and is reaped;
no child is left behind.

### 4.3 Chrome sandbox activation

Measured on the CI runner and again inside a real Codespaces dev container
(issue #8):

| Check | Result |
|---|---|
| Chrome browser process | alive (CI pid 139; Codespace pid 363) |
| Renderer processes | 8 (both environments) |
| Every renderer `NoNewPrivs` | `1` |
| Every renderer `Seccomp` | `2` (container filter + Chromium's own) |
| Renderer user namespace | distinct from the browser's — the user-namespace sandbox is in effect |
| `--no-sandbox` in any argv | absent |
| `CAP_SYS_ADMIN` in the container's bounding set | absent |
| `unshare(CLONE_NEWUSER)` | `= 0 (user namespace created)` |
| Regression suite result in the Codespace | 30 checks passed, 0 failed |

### 4.4 The AF_ALG compatibility path — measured, and what it means

An x86_64 process can enter the 32-bit syscall ABI with `int 0x80`, where
`socketcall(2)` hides the socket domain behind a pointer that seccomp cannot filter.
The probe therefore invokes `socketcall(SYS_SOCKET)` through `int 0x80` with a
32-bit-compatible argument array in the low 4 GiB, and tries AF_ALG and AF_VSOCK.

| Result | Value |
|---|---|
| `socket(AF_ALG)` — direct path | **EPERM** — the seccomp filter denied it |
| `socketcall_compat(AF_INET, SOCK_STREAM)` — control | **ALLOWED** — the compatibility entry point is reachable |
| `socketcall_compat(AF_ALG, SOCK_RAW)` | **ESOCKTNOSUPPORT** — the *kernel* refused the family |
| `socketcall_compat(AF_VSOCK, SOCK_RAW)` | **ESOCKTNOSUPPORT** — the *kernel* refused the family |
| `personality(ADDR_NO_32BIT)` | **APPLIED** (`previous=0`) — it stops the execution of 32-bit binaries |
| `socketcall_compat(AF_INET, SOCK_STREAM)` with `ADDR_NO_32BIT` | **still ALLOWED** — the instruction path is not closed by it |

**Enforcement layer, stated precisely:**

* On the **direct** path the AF_ALG restriction is enforced by **seccomp**. The
  distinction is the errno: the filter answers `EPERM`, while the kernel answers
  `ESOCKTNOSUPPORT`. The same call through the compatibility path produced
  `ESOCKTNOSUPPORT`, which proves the kernel — not the filter — handled it.
* On the **compatibility** path the call reaches the kernel. Neither the
  container's seccomp filter (the compat syscall architecture is not covered: the
  profile's `socketcall` removal changed nothing there) nor the AppArmor
  `docker-default` profile denied it. **Host LSM enforcement could not be
  demonstrated.**
* AF_ALG is unreachable on the measured hosts **because those kernels do not
  provide the AF_ALG socket family**, not because of a policy decision.

**Consequences, recorded honestly:**

1. `socketcall` was removed from the allow-list anyway: it is strictly stronger
   than upstream, and it removes an allowance that no in-image consumer needs on
   an amd64-only image.
2. `personality(ADDR_NO_32BIT)` was permitted so a process can refuse to
   **execute** 32-bit binaries. It does not close the `int 0x80` instruction
   path, which is recorded as a measurement rather than asserted.
3. **Residual risk:** on a host whose kernel *does* provide AF_ALG, the
   compatibility path would expose AF_ALG despite the profile. This cannot be
   closed from inside the container on the measured engine; the effective
   controls are host-level (disable `CONFIG_IA32_EMULATION`) or not using this
   image on such a host. The Phase 1.1 measurement is published so this is a
   known, documented limitation rather than an assumption.

### 4.5 Runtime privilege audit

Audited side by side on the CI runner, in the exact pinned base image and in the
Cloud-Chrome image:

```
### Base image (mcr.microsoft.com/devcontainers/base:ubuntu-24.04)
$ cat /etc/sudoers.d/vscode
vscode ALL=(root) NOPASSWD:ALL
$ sudo -n true   (as the vscode user)
exit 0 -> the base image grants the runtime user passwordless root

### Cloud-Chrome image
$ sudo -n true   (as the vscode user)
sudo: a password is required
non-zero exit -> ok: no passwordless root
$ sudo -n -l
sudo: a password is required
$ test -w /etc/opt/chrome/policies/managed/cloudchrome.json
not writable -> ok
$ test -w /etc/opt/chrome/policies/managed
not writable -> ok
```

The base image grants the runtime user unrestricted passwordless root. The Cloud-Chrome
image grants nothing, and the runtime user can neither modify nor replace the managed
Chrome policy. No privileged helper was added to compensate.

### 4.6 TigerVNC password file and live replacement

Measured inside a real Codespaces dev container against the exact packaged
`tigervnc-standalone-server 1.13.1+dfsg-2build2`:

```
password file created                          yes (8-byte obfuscated key)
password file mode=600 owner=vscode group=vscode size=8
unix RFB socket                                created
auth with the ORIGINAL password : authenticated (connection left open)
client initialisation complete : ServerInit 640x480 name='vscode@codespaces-b0abfd'
password file replaced          : True - replaced atomically, mode=0o600
existing connection after replace: still responsive (FramebufferUpdate type=0 rects=1)
auth with the NEW password      : authenticated (connection left open)
auth with the OLD password      : rejected (SecurityResult=1)
```

All seven properties are confirmed: an 8-byte password file, mode 0600, correct
ownership, an atomic replacement, an already-authenticated connection that survives the
replacement, a new connection that accepts the new password, and a new connection that
rejects the old one. The binary key is written through a file and never held in a shell
variable.

### 4.7 Image

| Metric | Value |
|---|---|
| Uncompressed image size | 662 073 021 bytes (631 MiB) |
| Runtime user | `vscode` (uid 1000) |

---

## 5. Dependency version policy

Two categories, with different rules:

| Category | Install rule | CI rule | Packages |
|---|---|---|---|
| `PINNED_*` | **exact** apt version requirement; the build fails rather than substituting another version | installed version must match exactly | `tigervnc-standalone-server`, `tigervnc-common`, `tigervnc-tools`, `websockify`, `jq` |
| `TRACKED_*` | installed from the signed Ubuntu archive without an exact requirement, so archive security updates are picked up | installed version must **not be older** than the recorded version; a newer version is reported, not failed | `xfce4`, `xfce4-terminal`, `dbus-x11`, `gnome-keyring`, `libsecret-1-0`, `libsecret-tools`, `xdotool`, `fonts-liberation2`, `fonts-noto-color-emoji` |

Downloaded artefacts (Chrome `.deb`, noVNC and age tarballs) are pinned by URL and
verified against a recorded SHA-256 before anything is unpacked.

This is not "full reproducibility" **and** "automatic tracking" at the same time: the
components Cloud-Chrome drives are reproducible to the exact version, and the operating
system libraries around them are deliberately allowed to move with the archive.

---

## 6. SSHD feature — removed

The `ghcr.io/devcontainers/features/sshd:1.1.0` feature was added during Phase 1 so that a
custom-image Codespace could be reached with `gh codespace ssh` and so that its creation
log could be downloaded. It was never needed for the measurements: the probe publishes its
evidence through the repository API, and the Codespaces API routes for ports, logs and
rebuild are unavailable to the token in use.

The feature is therefore **removed** from `.devcontainer/devcontainer.json`. The image has
no SSH server, no SSH port is opened, and no additional remote-access service exists. A
future experiment that genuinely needs SSH can add the feature on a throwaway branch, the
same way the probe hook is added.

---

## 7. Notes carried forward to later phases (not implemented yet)

- **Backup archive member validation (Phase 3).** A restore treats the archive as untrusted
  input. The design requires a *structured, unambiguous* member-validation strategy: an
  explicit allow-list of member paths, rejection of every member that is not a regular file
  or directory (symlinks, hard links, device nodes, FIFOs and sockets are rejected outright),
  rejection of absolute paths and of `..` traversal components, extraction into a freshly
  created empty `0700` staging directory that is never reused, extraction of explicitly
  validated member names only, and `--no-same-owner --no-same-permissions` so archive
  metadata cannot set ownership or mode. `--keep-directory-symlink` must **not** be used:
  the GNU tar manual states it *"instructs tar to follow symlinks to directories when
  extracting from the archive"*, which is the opposite of the required behaviour. The
  protection comes from rejecting symlink members and from extracting into an empty
  directory, where tar's default behaviour (remove a same-named symlink, extract a real
  directory) applies.
- **No plaintext backup archives are left on disk unnecessarily** (Phase 3): backups are
  age-encrypted by default, and temporary plaintext artefacts are removed as soon as the
  encrypted archive is written.
- **The VNC password file is never part of a backup** (Phase 3).
- **Secret delivery** (Phase 2): `cloudchrome init` is silent; the VNC password is shown
  only by the explicit interactive `cloudchrome passwd`, which refuses to print when stdout
  is not a terminal.
- **Chrome argument policy** (Phase 2): no user-supplied Chrome flags at all; the assembled
  argument vector is asserted against a forbidden-flag list before Chrome is launched.
- **Re-running the experiment**: create a branch from `main` that adds a `postCreateCommand`
  invoking `tests/probe/sandbox-probe.sh` (and optionally
  `tests/probe/seccomp-regression.sh`), create a Codespace from that branch, and read the
  evidence from the repository issue the probe publishes. The `sandbox-probe` workflow does
  the same thing, but needs a repository secret holding a token with the `codespace` scope,
  because `codespaces` is not a valid `GITHUB_TOKEN` permission scope.
