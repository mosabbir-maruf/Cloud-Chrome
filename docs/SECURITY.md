# Cloud-Chrome — Security

Security documentation for the dev container image: the enforced policies, the seccomp
baseline and its derivation, the measured sandbox and privilege results, and the notes
carried forward.

- Pinned versions, checksums and the seccomp baseline reference: `config/versions.env`.
- Profile derivation and verification: `tests/probe/render-seccomp-profile.sh`.
- Regression suite: `tests/probe/seccomp-regression.sh`.
- Platform experiment harness: `tests/probe/sandbox-probe.sh`.
- Runtime usage, commands and exit codes: `docs/USAGE.md`.
- Backup, restore and pruning: `docs/BACKUP.md`.
- Runtime implementation: `bin/cloudchrome` and `lib/cloudchrome/`.

**Evidence status:** every measurement below was taken on **2026-11-10 to 2026-11-12**
either on a GitHub-hosted CI runner or inside a real GitHub Codespaces dev container
(region `CentralIndia`, machine `basicLinux32gb` = 2 cores / 8 GB RAM / 32 GB storage,
kernel `6.8.0-1070-azure`, Ubuntu 24.04.3 LTS). The raw transcripts are published as
repository issues. The current ones are:

| Evidence | Issue |
|---|---|
| The runtime of the desktop stack, in a Codespace | [#41](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/41) |
| The performance of the desktop stack, in a Codespace | [#40](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/40) |

The earlier issues (#1 to #26) are the history of the measurement and are
kept for the record. They describe earlier states of the code, and a
number that was true then is not a statement about the current build.

---

## 1. Enforced policies in the image

| Control | Enforcement | Where |
|---|---|---|
| Chrome Sync disabled | Managed policy `SyncDisabled: true` | `/etc/opt/chrome/policies/managed/cloudchrome.json` |
| Browser-level Google account disabled | Managed policy `BrowserSignin: 0` | same file |
| Google **website** sign-in is not blocked by the policy | The managed policy controls the browser-level account, not a website sign-in. **Not verified automatically**: it is a pending human acceptance item | — |
| Policy file is root-owned and unwritable | `chown root:root`, `chmod 0644`; asserted at build time and in CI, including that the runtime user can neither write the file **nor** replace it | `.devcontainer/Dockerfile`, `ci.yml` |
| **No blanket passwordless root** | `/etc/sudoers.d/vscode` removed; `sudo -n true` must fail | `.devcontainer/Dockerfile` |
| Container runs as a non-root user | `USER vscode`; verified in CI (`user=vscode uid=1000`) | `.devcontainer/Dockerfile` |
| Chromium sandbox enabled | Tailored seccomp profile; verified per renderer | `.devcontainer/seccomp/chrome.json` |
| No `--no-sandbox` | Never passed; refused by the launch policy; asserted in CI and in the platform probe | `ci.yml`, `lib/cloudchrome/chrome.py`, `tests/probe/seccomp-regression.sh` |
| No `CAP_SYS_ADMIN` in the container | Never requested; the bounding set is recorded in every probe | `ci.yml`, `tests/probe/seccomp-regression.sh` |
| Only port 6080 is forwarded | `forwardPorts: [6080]` | `.devcontainer/devcontainer.json` |
| The raw RFB port is never exposed | TigerVNC runs with `-localhost`; the port table is checked at runtime | `lib/cloudchrome/vnc.py`, `lib/cloudchrome/web.py` |
| The forwarded port stays private | Never configured as a public port | `.devcontainer/devcontainer.json` |
| No DevTools or CDP listener | `--remote-debugging-port` and its relatives are refused; the port is checked at runtime | `lib/cloudchrome/chrome.py` |
| Chrome uses the libsecret password store | `--password-store=gnome-libsecret`; a plaintext store is refused | `lib/cloudchrome/chrome.py` |
| Chrome never runs with a weakened sandbox | `--no-sandbox` and its relatives are refused; the runtime fails closed instead | `lib/cloudchrome/chrome.py` |
| The secret service is verified before Chrome starts | A secret is stored and read back; otherwise Chrome is not started | `lib/cloudchrome/keyring.py` |
| No user-supplied Chrome flags | The argument vector is assembled in one place and checked against a forbidden list | `lib/cloudchrome/chrome.py` |
| The VNC password file is private | Mode 0600, owned by the runtime user; the runtime refuses a loose file | `lib/cloudchrome/vnc.py` |
| The keyring passphrase is private | Mode 0600, never printed, never in the process table | `lib/cloudchrome/keyring.py` |
| The persistent state is private | Mode 0700 directories; the runtime refuses a wrong owner or mode | `lib/cloudchrome/paths.py` |
| No personal browser data, secrets or backups in the image or build context | `.dockerignore`, `git ls-files` guard, in-image scan | `.dockerignore`, `ci.yml` |
| The persistent profile survives a rebuild | The Chrome user-data directory lives in the persistent state directory | `lib/cloudchrome/chrome.py` |
| The keyring survives a restart | The keyring data and the passphrase live in the persistent state directory | `lib/cloudchrome/keyring.py` |
| Backups are encrypted | The tar stream is piped into `age`; no plaintext archive is written to disk | `lib/cloudchrome/backup.py` |
| The age private key is never in a backup | The identity lives outside the archived paths, with mode 0600 | `lib/cloudchrome/backup.py` |
| The VNC password is never in a backup | It is excluded from the payload explicitly | `lib/cloudchrome/backup.py` |
| Every archive is treated as untrusted | Every member is validated before anything is extracted | `lib/cloudchrome/archive.py` |
| The restore is transactional | The whole restore is one transaction; the previous state is put back when any step fails, and a failed restart is reported | `lib/cloudchrome/backup.py` |
| The recovery stops the restored stack first | The restored stack is stopped and confirmed to be stopped before the previous profile is put back | `lib/cloudchrome/backup.py` |
| The installed state is verified where it is used | The Chrome profile, the GNOME Keyring store and the passphrase file are checked for ownership and permissions | `lib/cloudchrome/backup.py` |
| The pruning never leaves the last backup | The newest backup is always protected | `lib/cloudchrome/backup.py` |

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
| 3 | `socketcall` removed from the allow-list | The 32-bit compatibility entry point (`int 0x80`) reaches `socketcall(2)`, whose socket domain sits behind a pointer and therefore cannot be filtered. Removing the allowance is stricter than upstream. It does **not** close the entry point — see §4.4 |

**No `personality(2)` rule is added.** The upstream allow-list is kept exactly as
published (`0`, `8`, `131072`, `131080`, `0xFFFFFFFF`). In particular
`ADDR_NO_RANDOMIZE` (`0x0040000`) is **not** permitted: allowing it would let a
process disable ASLR. There is no personality flag at that value for disabling
32-bit compatibility, and a previous revision of this document wrongly named it
one; that rule has been removed and the profile regenerated from the pinned
baseline. The regression suite now proves that a request to disable ASLR is
refused (§4.5).

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
(the historical run is issue #9; the current run is
[#41](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/41)):

| Check | Result |
|---|---|
| Chrome browser process | alive (CI pid 164; Codespace pid 370) |
| Renderer processes | 8 (both environments) |
| Every renderer `NoNewPrivs` | `1` |
| Every renderer `Seccomp` | `2` (container filter + Chromium's own) |
| Renderer user namespace | distinct from the browser's — the user-namespace sandbox is in effect |
| `--no-sandbox` in any argv | absent |
| `CAP_SYS_ADMIN` in the container's bounding set | absent |
| `unshare(CLONE_NEWUSER)` | `= 0 (user namespace created)` |
| Regression suite result | 29 checks passed, 0 failed (CI runner and Codespace) |

### 4.4 The AF_ALG compatibility path — measured, and what it does not prove

An x86_64 process can enter the 32-bit syscall ABI with `int 0x80`, where
`socketcall(2)` hides the socket domain behind a pointer that seccomp cannot
filter. The probe therefore invokes `socketcall(SYS_SOCKET)` through `int 0x80`
with a 32-bit-compatible argument array in the low 4 GiB.

| Result | CI runner | Codespace | Reading |
|---|---|---|---|
| `socket(AF_ALG)` — direct path | `EPERM` | `EPERM` | denied by **seccomp**: the filter answers `EPERM`, whereas the kernel answers `ESOCKTNOSUPPORT` (see the rows below) |
| `socketcall_compat(AF_INET, SOCK_STREAM)` — control | `ALLOWED` | `ALLOWED` | the compatibility entry point **is reachable** |
| `socketcall_compat(AF_ALG, SOCK_RAW)` | `ESOCKTNOSUPPORT` | `ESOCKTNOSUPPORT` | refused by the **kernel**, which does not provide the family |
| `socketcall_compat(AF_VSOCK, SOCK_RAW)` | `ESOCKTNOSUPPORT` | `ESOCKTNOSUPPORT` | refused by the **kernel**, which does not provide the family |

**Enforcement layer, stated precisely:**

* The **direct** path is enforced by seccomp. The errno is the discriminator:
  the filter answers `EPERM`, the kernel answers `ESOCKTNOSUPPORT`.
* On the **compatibility** path the call reaches the kernel. Neither the
  container's seccomp filter nor the AppArmor `docker-default` profile denied it.
  **Host LSM enforcement could not be demonstrated.**
* Removing `socketcall` from the profile's allow-list did **not** block this
  path: the compatibility syscall architecture is not covered by the container's
  filter, so the allowance is not consulted there.
* Because the tested kernels do not provide the AF_ALG family at all, the
  AF_ALG compatibility probe is reported as
  **`NOT_VERIFIABLE_ON_THIS_HOST`** — it is **not** a security pass. On a host
  whose kernel *does* provide AF_ALG, that path would expose it.

**Correction to an earlier revision of this document.** A previous revision
claimed that `personality(ADDR_NO_32BIT)` closed this path. That was wrong on
two counts: the value used (`0x0040000`) is `ADDR_NO_RANDOMIZE`, which
disables ASLR and has nothing to do with compatibility mode, and
`personality(0xFFFFFFFF)` is a *query* for the current persona, not an attempt
to change it. The rule has been removed; the profile is regenerated from the
pinned upstream baseline; and the ASLR regression test now proves that an
ASLR-disabling request is refused (§4.5).

**Is there a container-level control? No — it is host level.**

| Probe | Result |
|---|---|
| `/proc/sys/abi/ia32_emulation` inside the container | **not exposed** on either the CI runner or the Codespace |
| writable by the container user | no |

`/proc/sys/abi/ia32_emulation` is a **global, non-namespaced** sysctl: it is
host state, it is not visible inside the container, and writing it would need
`CAP_SYS_ADMIN` in the initial namespace, which this container does not have.
The effective controls are therefore **host level**: set that sysctl to `0` on
the host, or boot the host with `ia32_emulation=0`.

> **Cloud-Chrome does not claim complete syscall isolation on a host where the
> 32-bit compatibility entry point remains reachable.** The AF_ALG restriction
> is enforced by the profile on the native syscall path only. The residual risk
> is stated here, measured, and not hidden.

### 4.5 ASLR regression — a personality request must be refused

| Probe | CI runner | Codespace |
|---|---|---|
| persona before | `00000000` | `00000000` |
| `personality(ADDR_NO_RANDOMIZE)` | **REFUSED** (`EPERM`) | **REFUSED** (`EPERM`) |
| `personality(UNAME26\|ADDR_NO_RANDOMIZE)` | **REFUSED** (`EPERM`) | **REFUSED** (`EPERM`) |
| persona after | `00000000` | `00000000` |
| `personality(0xFFFFFFFF)` (ordinary query) | REFUSED (`EPERM`) | REFUSED (`EPERM`) |
| `/proc/sys/abi/ia32_emulation` | not exposed, not writable | not exposed, not writable |

* An ASLR-disabling request is **refused**. The kernel would accept that
  persona, so the refusal comes from the filter or the LSM hook, not from the
  kernel.
* The test process ends with the persona unchanged
  (`ADDR_NO_RANDOMIZE` bit = `False`), and it restores the default personality
  immediately if a request ever succeeds, so no test process is left with ASLR
  disabled.
* The ordinary `personality(0xFFFFFFFF)` query is **not usable** in this
  environment: the upstream profile permits that value, but the LSM hook refuses
  the call (`EPERM`). This is upstream behaviour and is not weakened here.

### 4.6 Runtime privilege audit

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

### 4.7 TigerVNC password file and live replacement

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

### 4.8 Image

| Metric | Value |
|---|---|
| Uncompressed image size | 662 223 923 bytes (631 MiB) |
| Runtime user | `vscode` (uid 1000) |

---

## 4.9 The desktop stack, measured in a Codespace

A real GitHub Codespaces dev container (`basicLinux32gb`, Ubuntu 24.04.3 LTS,
kernel `6.8.0-1070-azure`) was created from a branch that runs the runtime
probe. The raw transcript is published as repository issue
[#41](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/41).

| Measurement | Result |
|---|---|
| The stack starts | `start` exits 0; the supervisor, the X/VNC server, the D-Bus session bus, the GNOME Keyring, the XFCE session, the noVNC bridge and Chrome are all running |
| The runtime user | `uid=1000(vscode)`; no sudo rule; `CapBnd=00000000a88425fb` (no `CAP_SYS_ADMIN`) |
| The RFB port 5900 | listening on `0100007F` and `::1` only; **no non-loopback listener** |
| The noVNC port 6080 | bound; the client is served from the image and answers `HTTP 200` |
| The VNC password | the file is mode 0600, 8 bytes; the server offers `VncAuth` only, sends a 16-byte challenge and **rejects a wrong password** |
| The password rotation | the file changes atomically, stays 8 bytes and mode 0600, and the password is not printed |
| The secret service | stores and returns a probe secret (`stored=True retrieved=True matched=True`) |
| Chrome's argument vector | `--user-data-dir=<state>/chrome --password-store=gnome-libsecret --no-first-run --no-default-browser-check --ozone-platform=x11`; no forbidden flag, no DevTools listener |
| Chrome's sandbox | the browser has `Seccomp=2` and `Seccomp_filters=1`; every zygote and renderer has `NoNewPrivs=1` and `Seccomp=2`, and the renderers run in a **different user namespace** (`user:[4026532291]` against the browser's `user:[4026531837]`) |
| The backup | written as an age file (`6167`), 104 939 bytes, 125 members, with the VNC password file **absent** |
| The diagnostics | **all checks pass** (`doctor` exits 0) |
| The secrets | the keyring passphrase is absent from the logs, from the process table and from every command's output |
| The host-level limitation | `/proc/sys/abi/ia32_emulation` is not exposed; the `int 0x80` compatibility path stays reachable |

**What this table does and does not prove.** Everything in it is measured
by a script: the ports, the processes, the sandbox state, the secrets and
the backup. A successful HTTP response from the noVNC client says that
the client is served; it does not say that a browser renders the desktop,
and a rejected wrong password says that the authentication works; it does
not say that a human saw the screen. Those two need a person, and they
are listed as pending in `docs/ACCEPTANCE.md`.

### 4.9.1 The failures that were found this way

The Codespace measurements found four real defects that no other test
showed, and each of them is now covered by a test that runs in CI:

| Defect | Fix |
|---|---|
| The XFCE session exited with code 1, so the supervisor stopped the whole stack and Chrome never really ran | `xfce4-session` rejects every option except `--disable-tcp`, `--help` and `--version`, and the XFCE documentation is explicit that `startxfce4` is the correct entry point. The session is started that way now, and the XDG cache and configuration directories it needs are created with the state |
| The secret service could not be used: `Object does not exist at path ".../collection/login"` | gnome-keyring activates a second, *locked* daemon on the first client call, and that daemon wins the `org.freedesktop.secrets` name. The runtime waits until its own daemon owns the name and the login keyring exists, and it displaces a competing daemon when there is one |
| The diagnostics reported that Chrome did not use the libsecret password store, although the command line plainly showed it | Chrome rewrites its argument vector into a single string, so a check that looks at the individual arguments never matches. The checks look at the joined command line |
| The keyring daemon created no control socket | `GNOME_KEYRING_CONTROL` names the control socket of a daemon that already runs; pointing it at a socket that does not exist yet made the daemon skip creating its own. The variable is now only set when the daemon reported the path or the socket exists |

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

The `ghcr.io/devcontainers/features/sshd:1.1.0` feature was added so that a
custom-image Codespace could be reached with `gh codespace ssh` and so that its creation
log could be downloaded. It was never needed for the measurements: the probe publishes its
evidence through the repository API, and the Codespaces API routes for ports, logs and
rebuild are unavailable to the token in use.

The feature is therefore **removed** from `.devcontainer/devcontainer.json`. The image has
no SSH server, no SSH port is opened, and no additional remote-access service exists. A
future experiment that genuinely needs SSH can add the feature on a throwaway branch, the
same way the probe hook is added.

---

## 7. The runtime, as implemented

The runtime is `bin/cloudchrome` and the package in `lib/cloudchrome/`. The
separation of concerns is deliberate: the CLI parses arguments and reports,
the supervisor owns the process lifecycle, and the modules hold one piece of
shared logic each.

### 7.1 The service lifecycle

| Requirement | How it is met |
|---|---|
| One supervisor for the whole stack | `lib/cloudchrome/supervisor.py` starts the X/VNC server, the D-Bus session bus, the GNOME Keyring, the XFCE session and window manager, the noVNC bridge and Chrome, in that order |
| Dependency order | Every component is verified before the next one starts; the keyring must serve the Secret Service before Chrome is started |
| Graceful shutdown | SIGTERM and SIGINT stop the components in the reverse order, with a timeout before SIGKILL |
| Dead-process detection | A component that exits unexpectedly stops the whole stack and is reported, instead of leaving a half-working desktop |
| No stale PID files | A PID file records the process start time as well as the PID, so a recycled PID is never mistaken for the running supervisor |
| No duplicate stacks | The supervisor holds an exclusive lock for its whole lifetime; the control lock serialises the mutating commands |
| Only its own processes are signalled | The process identity is verified before anything is signalled, and only descendants of a recorded PID are terminated |

### 7.2 The secret service

The GNOME Keyring is started with `--unlock --replace`. The `--replace` is not
optional: gnome-keyring installs a D-Bus service that activates a **second**,
locked daemon the moment the first client calls the Secret Service, and that
activated daemon wins the `org.freedesktop.secrets` name. It never received the
unlock passphrase, so it has no login collection and every store fails with
*Object does not exist at path .../collection/login*.

The runtime therefore waits until **its own** daemon owns the name and the
login keyring exists before any client call happens. The name owner is asked
from the bus directly, because a daemon that is alive but lost the race
satisfies every other check.

The passphrase is written to the daemon's standard input and is never in the
process table, in a log or in the environment.

### 7.3 The backup archive is untrusted input

A restore validates every member **before** anything is extracted:

| Rule | What it refuses |
|---|---|
| the name is relative | `/etc/passwd` |
| the name has no `..` component | `../../etc/passwd` |
| the name has no backslash | `chrome\file` |
| the member is a regular file or a directory | symbolic links, hard links, device nodes, FIFOs, sockets |
| the member is in the approved set | anything outside `chrome/`, `keyring/` and the two top-level files |
| the member appears once | a duplicate path |
| the staging directory is empty | an extraction into a reused directory |

The extraction writes every file with the runtime's own permissions and never
follows a symbolic link, so a link in the archive cannot be used to write
outside the staging directory. The ownership and the permissions of the
archive are never trusted.

The archive is read back through a pipe, so the plaintext never lands on
the filesystem at all. The fallback path, which writes it into a private
mode-0700 directory first, removes it on every way out of the restore, and
overwrites it before the unlink.

That is **not** a secure-erasure guarantee. On storage with copy-on-write,
wear levelling or snapshots the old content can survive in the free blocks.
What is guaranteed is that the plaintext is not left in a file, in a
directory another user can read, or in the backup directory.

The archive is also bounded: its own size, the total size it expands to, the
size of its largest member, its number of members and the size of its
manifest are all limited, and the limits cannot be raised beyond a hard
ceiling. The limits are listed in `docs/BACKUP.md`.

### 7.4 The restore is one transaction

The whole restore is one operation under one lock: the archive is validated
and staged, the stack is stopped, the state is replaced, the restored
desktop is started, and the restored Chrome and the Secret Service are
checked. Only then is the restore finalised and the rollback state disposed
of.

Any failure puts the previous profile, keyring and passphrase back, starts
the previous desktop again, and exits non-zero. A failed restore never
reports success and never leaves a partly installed state. When the previous
desktop cannot be started either, the rollback state is kept and the message
says where it is, so the previous data stays recoverable.

The rollback state keeps its own copy of the previous data: the previous
state is put back by copying it into place, so the recoverable copy is
still there when the state that was put back turns out to be damaged. A
failure in the middle of the installation is rolled back too, even when the
live profile had already been moved aside.

### 7.5 The recovery stops the restored stack before it rolls back

The recovery does not infer which stack is running from `is_running()`. It
records what the transaction did: whether the original stack was running,
whether the transaction stopped it, and whether it attempted to start the
restored stack.

The order is fixed:

1. the restored stack is stopped, and it is confirmed to be stopped, before
   anything of the live state is touched — a restored desktop that is still
   up may be writing to the profile that is about to be replaced. A start
   that failed half way leaves a stack behind, and that stack is stopped
   the same way;
2. the previous profile, keyring and passphrase are put back from the
   retained rollback copy;
3. the previous desktop is started again and its health is **verified**,
   when the transaction stopped it;
4. a stack that was stopped before the restore is left stopped.

A stack the transaction did not start is never stopped: a failure before
the original stack was stopped leaves that stack running with its own data.

When the recovery itself fails — the restored stack cannot be stopped, the
rollback does not complete, or the previous desktop cannot be started or
is not usable — the command reports the failure, exits non-zero and keeps
the rollback copy. Nothing recoverable is discarded and no success is
reported.

### 7.6 The verification checks the paths that are used

The ownership and the permissions of the installed state are verified
against the paths the runtime actually uses: the Chrome profile, the
**GNOME Keyring store** — the directory the keyring daemon is told to use —
and the keyring passphrase file. A directory that belongs to somebody else
is refused, a symbolic link is never followed, and the permissions of a
directory that belongs to the runtime user are restored.

The legacy `.cloudchrome/keyring` directory is **not** accepted as proof
that the live keyring store is safe. It holds nothing, so a check that
looked at it would pass while the store Chrome actually uses was exposed.

A backup is taken with the stack stopped, because a profile that Chrome is
still writing to is not a consistent snapshot.

### 7.7 The pruning stays inside the managed area

Only the files in the managed backup directory that carry the Cloud-Chrome
backup name are considered. A symbolic link is never followed, a file outside
the directory is never touched, an explicit retention policy is required, the
files that are about to be deleted are shown, and the newest backup is never
pruned.

### 7.8 The secret delivery

`cloudchrome init` is silent and never prompts. The VNC password is shown
only by the explicit interactive `cloudchrome passwd`, which refuses to print
when stdout is not a terminal — and then does not change the password either,
because a password that was changed and cannot be shown would lock the user
out of their own desktop.

### 7.9 The Chrome argument policy

No user-supplied Chrome flags at all. The argument vector is assembled in one
place and asserted against a forbidden-flag list before Chrome is launched:
no `--no-sandbox`, no remote debugging, no plaintext password store and no
user data directory other than the persistent one.

### 7.10 The failure paths

The failures are injected with real failures rather than with hooks: a missing
or invalid VNC password file, a keyring passphrase file with unsafe
permissions, an unreadable profile directory, a corrupt backup, and a SIGTERM
delivered to the supervisor while the stack is still coming up. Each one must
be refused, must leave the user's data intact and must not leave a stale PID
file or a leftover process behind.

### 7.11 Re-running the experiment

Create a branch from `main` that adds a `postCreateCommand` invoking
`tests/probe/runtime-probe.sh`, create a Codespace from that branch, and read
the evidence from the repository issue the probe publishes. The
`runtime-probe` workflow does the same thing, but needs a repository secret
holding a token with the `codespace` scope, because `codespaces` is not a
valid `GITHUB_TOKEN` permission scope.

The performance probe is run the same way. It takes the same branch, the
same `postCreateCommand` and the same publication, and it publishes its own
issue. `CLOUDCHROME_PERF_COMPARE_COMMIT` makes it measure the same startup
twice in one container, once with the runtime of the checkout and once with
the runtime of another commit, so that a before and after does not depend
on the load of the host. The method and the results are in
`docs/PERFORMANCE.md`.

Neither probe is a substitute for a person looking at the browser. They
measure the ports, the processes, the secrets and the transactions; the
visual acceptance is listed in `docs/ACCEPTANCE.md` and is still pending.
