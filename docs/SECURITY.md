# Cloud-Chrome — Security

Phase 1 security documentation: the enforced policies, the sandbox compatibility
experiment, and the raw evidence collected from a real GitHub Codespaces dev container.

- Architecture and full design rationale: `docs/ARCHITECTURE.md` (Revision 5).
- Pinned versions and checksums: `config/versions.env`.
- Experiment harness: `tests/phase1/sandbox-probe.sh`.

**Evidence status:** the measurements in §3 were taken on **2026-11-10/11** in real GitHub
Codespaces dev containers (region `CentralIndia`, machine `basicLinux32gb` = 2 cores / 8 GB RAM /
32 GB storage, kernel `6.8.0-1064-azure`, Ubuntu 24.04.3 LTS). Raw transcripts are published as
repository issues #1–#3 (full probe output). No measurement below is estimated.

---

## 1. Enforced policies in the Phase 1 image

| Control | Enforcement | Where |
|---|---|---|
| Chrome Sync disabled | Managed policy `SyncDisabled: true` | `/etc/opt/chrome/policies/managed/cloudchrome.json` |
| Browser-level Google account disabled | Managed policy `BrowserSignin: 0` | same file |
| Google **website** sign-in remains available | Not a browser-signin control; verified manually in Phase 2 | — |
| Policy file is root-owned, not writable by the runtime user | `chown root:root`, `chmod 0644`, asserted at build time with `runuser -u vscode -- test ! -w …` and re-checked in CI | `.devcontainer/Dockerfile`, `ci.yml` |
| Policy content is validated at build time | `jq -e '.SyncDisabled == true and .BrowserSignin == 0'` | `.devcontainer/Dockerfile` |
| Container runs as a non-root user | `USER vscode`; verified in CI (`user=vscode uid=1000`) | `.devcontainer/Dockerfile` |
| Only port 6080 is forwarded | `forwardPorts: [6080]` | `.devcontainer/devcontainer.json` |
| Chromium sandbox enabled | Tailored seccomp profile; verified per renderer | `.devcontainer/seccomp/chrome.json` |
| No `--no-sandbox` | Never passed; asserted in CI and re-checked in the probe | `ci.yml`, probe |
| No `CAP_SYS_ADMIN` | Never requested; the container's bounding set is recorded | `ci.yml`, probe |
| No personal browser data, secrets or backups in the image or context | `.dockerignore`, `git ls-files` guard, in-image scan | `.dockerignore`, `ci.yml` |

### Changing the isolation policy

The managed policy directory is administrator-controlled and the file is root-owned, so a normal
`vscode` user cannot remove or edit it at runtime. This is intentional: there is **no runtime switch**
for the isolation policy, and no privileged helper exists to toggle it.

To enable Chrome Sync deliberately:

1. Edit `.devcontainer/chrome-policies/managed/cloudchrome.json` in the repository
   (`BrowserSignin: 1`, `SyncDisabled: false`).
2. Commit the change.
3. **Rebuild the Codespace** so the new image is built and deployed.
4. Restart Chrome and sign in to Chrome from the Chrome UI.

The strict policy remains in force until the rebuilt image is deployed.

---

## 2. Chrome sandbox policy and how it is achieved

1. Prefer a supported Chrome **user-namespace** sandbox.
2. A custom seccomp profile is retained **only because it was measured necessary and proven
   compatible** (§3).
3. Sandbox activation is verified from **Chrome itself** — renderer-level checks plus
   `chrome://sandbox` — never inferred from a probe.
4. `--no-sandbox` is never used.
5. `CAP_SYS_ADMIN` is never added; no automatic capability escalation.
6. If a supported sandbox cannot be established, Cloud-Chrome **fails closed** and reports the blocker.

### The tailored seccomp profile

`.devcontainer/seccomp/chrome.json` is **Docker's default profile with exactly one additive rule**:

```
names: [clone, unshare, setns]   action: SCMP_ACT_ALLOW   excludes: caps [CAP_SYS_ADMIN]
```

- Base: `moby/moby` `profiles/seccomp/default.json`, tag `v28.0.0`,
  SHA-256 `9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76` (recorded in
  `config/versions.env` as `SECCOMP_BASE_SHA256`).
- `clone3` still returns `ENOSYS`, so Chromium falls back to `clone(2)` as it expects.
- The container's capability bounding set is unchanged and still excludes `CAP_SYS_ADMIN`.
- `chroot` was already permitted (the container holds `CAP_SYS_CHROOT`).

**Why the profile must be copied to a host path.** The Docker daemon reads a seccomp profile from
its **own** filesystem, not from the container. In Codespaces the workspace is not reachable at the
container path by the daemon, so `--security-opt seccomp=<workspace path>` made the container fail to
start (measured: with the workspace path the probe never ran; without the option it ran). The
Dev Container `initializeCommand` therefore copies the profile to a host path before the container is
created, and `runArgs` references that path:

```
initializeCommand: cp "${localWorkspaceFolder}/.devcontainer/seccomp/chrome.json" /tmp/cloudchrome-seccomp.json && chmod 0644 /tmp/cloudchrome-seccomp.json
runArgs: ["--shm-size=1g", "--security-opt", "seccomp=/tmp/cloudchrome-seccomp.json"]
```

### Failure classification (Revision 5)

| Class | Meaning | Remedy available inside the container? |
|---|---|---|
| **K1** | User namespaces disabled by a kernel sysctl | No — fail closed |
| **K2** | Required syscall not implemented (`ENOSYS`) | No — fail closed |
| **K3** | Namespace type unsupported | No — fail closed |
| **S** | Blocked by the **container's seccomp** layer | **Yes — a tailored profile** (the case actually observed) |
| **A** | Blocked by **host AppArmor** (`apparmor_restrict_unprivileged_userns`) | No — fail closed |
| **C** | The setuid sandbox helper needs `CAP_SYS_ADMIN` | No — and not an option by policy |
| **X** | Chrome starts but activates no sandbox | No — fail closed |

---

## 3. Phase 1 experiment results (measured)

### 3.1 Environment

| Measurement | Value |
|---|---|
| Kernel | `6.8.0-1064-azure #72~22.04.1-Ubuntu` |
| OS | Ubuntu 24.04.3 LTS |
| Container user | `vscode` (uid 1000) |
| Region | `CentralIndia` |
| Machine | `basicLinux32gb` (2 cores / 8 GB RAM / 32 GB storage) |

### 3.2 Kernel and LSM layer — **not the blocker**

| Measurement | Value | Reading |
|---|---|---|
| `/proc/sys/user/max_user_namespaces` | `31741` | user namespaces enabled |
| `/proc/sys/kernel/unprivileged_userns_clone` | `1` | unprivileged user namespaces allowed |
| `/proc/sys/kernel/apparmor_restrict_unprivileged_userns` | `0` | **AppArmor does not restrict unprivileged user namespaces** |
| `/proc/self/attr/current` | `docker-default (enforce)` | the container is confined by the standard Docker AppArmor profile, which does not block userns creation |

### 3.3 Container seccomp and capability layer — **the blocker (class S)**

| Measurement | Value |
|---|---|
| `Seccomp` | `2` (filter mode), `Seccomp_filters: 1` — Docker's default profile is active |
| `CapEff` / `CapPrm` / `CapAmb` | `0000000000000000` |
| `CapBnd` | `00000000a88425fb` — **no `CAP_SYS_ADMIN`** (the setuid-sandbox path is unavailable) |

With Docker's default profile, every namespace probe failed:

```
unshare -U                    rc=1  unshare failed: Operation not permitted
unshare -Ur --map-root-user   rc=1  unshare failed: Operation not permitted
unshare(CLONE_NEWUSER) failed: errno=1 (EPERM)
```

Chromium therefore fell back to the setuid sandbox, which cannot create namespaces without
`CAP_SYS_ADMIN`, and aborted:

```
Failed to move to new namespace: PID namespaces supported, Network namespace supported,
but failed: errno = Operation not permitted
[FATAL] Zygote process exited prematurely with exit code 82 (setuid sandbox: failed to move to new namespaces)
```

**Classification: class S** — the container's own seccomp layer. Classes K1–K3 and A are ruled out by
the measurements above.

### 3.4 Namespace creation with the tailored profile — **allowed**

```
unshare -U                    rc=0
unshare -U -r                 rc=0
unshare -Ur --map-root-user   rc=0
unshare -U -n                 rc=0
unshare -U -p --fork          rc=0
unshare -U --map-root-user id rc=0   uid=0(root) gid=0(root)
unshare(CLONE_NEWUSER) = 0  (user namespace created)
```

`unshare -U -m` still fails with `cannot change root filesystem propagation: Operation not permitted`
— the kernel refuses to make the mount propagation change from an unprivileged user namespace. This
does **not** affect Chromium's sandbox, which does not require it.

### 3.5 Chrome sandbox activation — **ACTIVE** (the decisive test)

With the tailored profile applied, Google Chrome 157.0.8103.50 started and stayed up:

```
chrome browser process   alive (pid 300)
```

Process tree: browser → 3 zygotes → GPU / network / storage utility processes and **many
`--type=renderer` processes**.

Renderer-level sandbox state (identical for every renderer):

```
[pid 416] --type=renderer
    CapEff:         0000000000000000
    CapBnd:         000001ffffffffff
    NoNewPrivs:     1
    Seccomp:        2
    Seccomp_filters: 2      <-- container filter + Chromium's own filter
    ns/user:        user:[4026532291]   <-- a DIFFERENT user namespace from the browser
```

| Sandbox criterion | Result |
|---|---|
| Renderer processes exist | ✅ |
| `NoNewPrivs: 1` on every renderer | ✅ |
| `Seccomp: 2` with `Seccomp_filters: 2` (container filter **+** Chromium's own) | ✅ |
| `CapEff: 0` in the renderer (no capabilities) | ✅ |
| Distinct `ns/user` per sandboxed process | ✅ namespace sandbox in effect |
| `--no-sandbox` in any argv | ✅ absent |
| `CAP_SYS_ADMIN` in the container bounding set | ✅ absent |

This is the signature of Chromium's **user-namespace sandbox**. `chrome://sandbox` could not be
rendered by the automated probe (it needs a browser client); the manual confirmation over noVNC is a
Phase 2 acceptance step.

### 3.6 TigerVNC compatibility (exact Ubuntu package `1.13.1+dfsg-2build2`)

| Measurement | Result |
|---|---|
| `Xtigervnc -version` | `Xvnc TigerVNC 1.13.1 - built 2024-04-01` (X server release 12101011) |
| Packaged man pages | `/usr/share/man/man1/Xtigervnc.1.gz`, `tigervncsession.1.gz`, `/usr/share/man/man8/tigervncsession.8.gz` |
| `tigervncpasswd -help` | `usage: tigervncpasswd [file]` / `tigervncpasswd -f` |
| Password file created by `tigervncpasswd` | **8 bytes**, mode **0600**, owner `vscode` |
| Authentication with the original password | **accepted** (`SecurityResult=0`) |
| Password file replaced | **atomically** (new file created in the same directory, then renamed), mode 0600 preserved |
| Authentication with the **new** password | **accepted** |
| Authentication with the **old** password | **rejected** (`SecurityResult=1`) |
| Pre-existing connection after the replacement | **still responsive** |

**Conclusion:** the packaged TigerVNC 1.13.1 reads the `-rfbauth` file **per connection**, exactly as
the upstream documentation states ("The file is accessed each time a connection comes in, so it can be
changed on the fly"). A password rotation therefore needs **no server restart** and **no interruption
of the desktop**. The Phase 1 harness confirmed this against the exact installed package rather than
assuming it.

Implementation note for Phase 2: the password file must be written by `tigervncpasswd` itself (so it
owns the file and applies mode 0600); the `-f` form writes to stdout and, when stdin is not a
terminal, also emits the input, so its output must be truncated to the 8-byte key.

### 3.7 Chrome managed policy in the real environment

Chrome logged the policy file being picked up:

```
Found mandatory policy file: /etc/opt/chrome/policies/managed/cloudchrome.json
Taking initial snapshot of POLICY_DOMAIN_CHROME policies
```

### 3.8 Summary

| Question | Answer |
|---|---|
| Is a supported Chrome sandbox achievable in GitHub Codespaces? | **Yes** — the user-namespace sandbox, with the tailored seccomp profile |
| What was the blocker? | Class S — Docker's default seccomp profile, not the kernel or AppArmor |
| Was `CAP_SYS_ADMIN` needed? | **No** — it was never present and never added |
| Was `--no-sandbox` used? | **No** |
| Is a full GUI proven working? | **No.** Chrome rendered on a real X display with an active sandbox, but the noVNC → XFCE → Chrome path is a Phase 2 deliverable |
| Does the packaged TigerVNC support live password rotation? | **Yes** — proven by the functional test |

---

## 4. Notes carried forward to Phase 2+ (not implemented in Phase 1)

- **Backup archive member validation (Phase 3).** A restore treats the archive as untrusted input. The
  design requires a *structured, unambiguous* member-validation strategy: an explicit allow-list of
  member paths, rejection of every member that is not a regular file or directory (symlinks, hard
  links, device nodes, FIFOs and sockets are rejected outright), rejection of absolute paths and of
  `..` traversal components, extraction into a freshly created empty `0700` staging directory that is
  never reused, extraction of explicitly validated member names only, and `--no-same-owner
  --no-same-permissions` so archive metadata cannot set ownership or mode. `--keep-directory-symlink`
  must **not** be used: the GNU tar manual states it *"instructs tar to follow symlinks to directories
  when extracting from the archive"*, which is the opposite of the required behaviour. The protection
  comes from rejecting symlink members and from extracting into an empty directory, where tar's
  default behaviour (remove a same-named symlink, extract a real directory) applies.
- **No plaintext backup archives are left on disk unnecessarily** (Phase 3): backups are age-encrypted
  by default, and temporary plaintext artefacts are removed as soon as the encrypted archive is written.
- **The VNC password file is never part of a backup** (Phase 3).
- **Secret delivery** (Phase 2): `cloudchrome init` is silent; the VNC password is shown only by the
  explicit interactive `cloudchrome passwd`, which refuses to print when stdout is not a terminal.
- **Chrome argument policy** (Phase 2): no user-supplied Chrome flags; the assembled argument vector is
  asserted against a forbidden-flag list before Chrome is launched.
- **Re-running the experiment** (any phase): create a branch from `main` that adds a
  `postCreateCommand` invoking `tests/phase1/sandbox-probe.sh`, create a Codespace from that branch,
  and read the evidence from the resulting repository issue. The probe publishes through the repository
  API (the Codespaces `GITHUB_TOKEN` is a REST credential, not a git credential) and falls back to a
  git push to `phase1/evidence`.
