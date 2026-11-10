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
repository issues #1–#6.

---

## 1. Enforced policies in the image

| Control | Enforcement | Where |
|---|---|---|
| Chrome Sync disabled | Managed policy `SyncDisabled: true` | `/etc/opt/chrome/policies/managed/cloudchrome.json` |
| Browser-level Google account disabled | Managed policy `BrowserSignin: 0` | same file |
| Google **website** sign-in remains available | Not a browser-signin control; verified in the Phase 2 acceptance test | — |
| Policy file is root-owned and unwritable | `chown root:root`, `chmod 0644`; asserted at build time and in CI, including that the runtime user cannot write the file **or** replace it | `.devcontainer/Dockerfile`, `ci.yml` |
| **No blanket passwordless root** | `/etc/sudoers.d/vscode` removed; `sudo -n true` must fail | `.devcontainer/Dockerfile` |
| Container runs as a non-root user | `USER vscode`; verified in CI (`user=vscode uid=1000`) | `.devcontainer/Dockerfile` |
| Only port 6080 is forwarded | `forwardPorts: [6080]` | `.devcontainer/devcontainer.json` |
| Chromium sandbox enabled | Tailored seccomp profile; verified per renderer | `.devcontainer/seccomp/chrome.json` |
| No `--no-sandbox` | Never passed; asserted in CI and in the platform probe | `ci.yml`, `tests/probe/seccomp-regression.sh` |
| No `CAP_SYS_ADMIN` | Never requested; the capability bounding set is recorded in every probe | `ci.yml`, `tests/probe/seccomp-regression.sh` |
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
**socket-domain allow-list**, and it restricts `socketpair(2)` to AF_UNIX/AF_TIPC. Its
consequences for this project:

* **AF_ALG (38) is denied** — direct access to the kernel crypto API, the vector behind
  CVE-2026-31431 (GHSA-2274-3hgr-wxv6), is closed at the container boundary.
* **AF_VSOCK (40) is denied** — no host/guest communication channel.
* `socketcall(2)` remains in the upstream allow-list because on socketcall ABIs the
  socket arguments sit behind a pointer and cannot be filtered. Upstream's mitigation is
  the `socketpair(2)` restriction, which the baseline carries and which is tested here.

### Derivation and drift protection

`.devcontainer/seccomp/chrome.json` is **exactly** that baseline plus two declared rules.
`tests/probe/render-seccomp-profile.sh` is the single source of truth for the derivation:

* `--write` regenerates the file from the pinned baseline.
* `--verify` (run by CI) regenerates it in a temporary directory and diffs it against the
  committed file, so **an upstream restriction cannot be silently dropped**.

### Namespace policy — narrowed to what Chromium actually performs

Chromium's namespace sandbox uses exactly these operations (Chromium source,
`sandbox/linux/services/namespace_sandbox.cc` and `sandbox/linux/services/credentials.cc`):

| Call | Flags |
|---|---|
| `unshare` | `CLONE_NEWUSER` only (`credentials.cc`) |
| `clone` | `CLONE_NEWUSER \| SIGCHLD` (`credentials.cc`) and `CLONE_NEWPID \| SIGCHLD` (`namespace_sandbox.cc`) |
| namespace set | `CLONE_NEWUSER \| CLONE_NEWPID \| CLONE_NEWNET` (`kSupportedTypes`) |

`setns` is **not** used by the sandbox and is therefore **not permitted**.

The two added rules express this with argument filters. In the upstream JSON dialect
`SCMP_CMP_MASKED_EQ` with `valueTwo: 0` means "the bits in `value` must be zero", so the
mask is the complement of the permitted flags:

| Rule | Mask | Permitted |
|---|---|---|
| `unshare` | `18446744073441116159` (`~CLONE_NEWUSER`) | `unshare(CLONE_NEWUSER)` and nothing else |
| `clone` | `18446744071830437888` (`~(CLONE_NEWUSER\|CLONE_NEWPID\|CLONE_NEWNET\|signal bits)`) | `clone(2)` with Chromium's namespace flags plus a termination signal |

`clone3` still returns `ENOSYS` (upstream rule), so Chromium falls back to `clone(2)`.
The container never holds `CAP_SYS_ADMIN`.

---

## 3. Measured results

### 3.1 Namespace and socket behaviour — Cloud-Chrome profile vs the daemon default

Both runs used the same image and the same probe; only the seccomp filter differed.

| Probe | Cloud-Chrome profile | Docker daemon default profile |
|---|---|---|
| `socket(AF_INET)` (control) | ALLOWED | ALLOWED |
| `socket(AF_ALG=38)` | **DENIED (EPERM)** | DENIED (EPERM) |
| `socket(AF_VSOCK=40)` | **DENIED (EPERM)** | DENIED (EPERM) |
| `socketcall` | not applicable (x86_64) | not applicable (x86_64) |
| `socketpair(AF_UNIX=1)` | ALLOWED | ALLOWED |
| `socketpair(AF_NETLINK=16)` | DENIED (EPERM, seccomp) | DENIED (ESOCKTNOSUPPORT, kernel) |
| `unshare(CLONE_NEWUSER)` | **ALLOWED** | DENIED (EPERM) |
| `unshare(CLONE_NEWUSER\|CLONE_NEWNET)` | DENIED (EPERM) | DENIED (EPERM) |
| `unshare(CLONE_NEWNET)` | DENIED (EPERM) | DENIED (EPERM) |
| `unshare(CLONE_NEWNS)` | DENIED (EPERM) | DENIED (EPERM) |
| `unshare(CLONE_NEWUSER\|CLONE_NEWIPC\|CLONE_NEWUTS)` | DENIED (EPERM) | DENIED (EPERM) |
| `clone(CLONE_NEWUSER\|SIGCHLD)` | **ALLOWED** | DENIED (EPERM) |
| `clone(CLONE_NEWNET\|SIGCHLD)` | DENIED (EPERM) | DENIED (EPERM) |
| `clone(CLONE_NEWNS\|SIGCHLD)` | DENIED (EPERM) | DENIED (EPERM) |
| `clone(CLONE_NEWPID\|SIGCHLD)` outside a user namespace | DENIED (EPERM — the kernel requires `CAP_SYS_ADMIN`; the filter permits the flag) | DENIED (EPERM) |

Reading of the comparison:

* The daemon's default profile on the CI runner already denies AF_ALG and AF_VSOCK, so the
  socket behaviour is no longer a differentiator there — pinning the profile to the
  maintained upstream baseline is what keeps it that way, rather than depending on the
  daemon's version.
* The namespace operations are the real difference: the default profile denies them, which
  is why Chromium could not start before; the Cloud-Chrome profile permits exactly the two
  calls Chromium makes and denies everything else.
* `socketpair(AF_NETLINK)` is denied by the **seccomp layer** under the Cloud-Chrome profile
  and by the **kernel** under the daemon default — evidence that the pinned profile is
  stricter than the runner's built-in one for that call.

### 3.2 Chrome sandbox activation

Measured on the CI runner with the Cloud-Chrome profile, and again inside a real
Codespaces dev container:

| Check | Result |
|---|---|
| Chrome browser process | alive (CI: pid 93; Codespace: pid 287) |
| Renderer processes | 8 (CI and Codespace) |
| Every renderer `NoNewPrivs` | `1` |
| Every renderer `Seccomp` | `2` (container filter + Chromium's own) |
| Renderer user namespace | distinct from the browser's (`ns/user` inode differs) — the user-namespace sandbox is in effect |
| `--no-sandbox` in any argv | absent |
| `CAP_SYS_ADMIN` in the bounding set | absent (`CapBnd=00000000a80425fb` on the CI runner, `00000000a88425fb` in the Codespace) |
| `unshare(CLONE_NEWUSER)` | `= 0 (user namespace created)` |

### 3.3 Runtime privilege audit

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

### 3.4 TigerVNC password file and live replacement

Measured inside a real Codespaces dev container against the exact packaged
`tigervnc-standalone-server 1.13.1+dfsg-2build2`:

```
password file created                          yes (8-byte obfuscated key)
password file mode=600 owner=vscode group=vscode size=8
unix RFB socket                                created
auth with the ORIGINAL password : authenticated (connection left open)
client initialisation complete : ServerInit 640x480 name='vscode@codespaces-8d744e'
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

### 3.5 Image

| Metric | Value |
|---|---|
| Uncompressed image size | 662 073 021 bytes (631 MiB) |
| Runtime user | `vscode` (uid 1000) |

---

## 4. Dependency version policy

Two categories, with different rules:

| Category | Install rule | CI rule | Packages |
|---|---|---|---|
| `PINNED_*` | **exact** apt version requirement; the build fails rather than substituting another version | installed version must match exactly | `tigervnc-standalone-server`, `tigervnc-common`, `tigervnc-tools`, `websockify`, `jq` |
| `TRACKED_*` | installed from the signed Ubuntu archive without an exact requirement, so archive security updates are picked up | installed version must **not be older** than the recorded version; a newer version is reported, not failed | `xfce4`, `xfce4-terminal`, `dbus-x11`, `gnome-keyring`, `libsecret-1-0`, `libsecret-tools`, `xdotool`, `fonts-liberation2`, `fonts-noto-color-emoji` |

Downloaded artefacts (Chrome `.deb`, noVNC and age tarballs) are pinned by URL and verified
against a recorded SHA-256 before anything is unpacked.

This is not "full reproducibility" **and** "automatic tracking" at the same time: the
components Cloud-Chrome drives are reproducible to the exact version, and the operating
system libraries around them are deliberately allowed to move with the archive.

---

## 5. SSHD feature — removed

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

## 6. Notes carried forward to later phases (not implemented yet)

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
  invoking `tests/probe/sandbox-probe.sh`, create a Codespace from that branch, and read the
  evidence from the repository issue the probe publishes. The `sandbox-probe` workflow does
  the same thing, but needs a repository secret holding a token with the `codespace` scope,
  because `codespaces` is not a valid `GITHUB_TOKEN` permission scope.
