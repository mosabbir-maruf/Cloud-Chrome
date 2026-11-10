# Cloud-Chrome — Security

Phase 1 security documentation: the enforced policies, the sandbox
compatibility experiment, and the raw evidence collected from a real GitHub
Codespaces dev container.

- Architecture and full design rationale: `docs/ARCHITECTURE.md` (Revision 5).
- Pinned versions and checksums: `config/versions.env`.

---

## 1. Enforced policies in the Phase 1 image

| Control | Enforcement | Where |
|---|---|---|
| Chrome Sync disabled | Managed policy `SyncDisabled: true` | `/etc/opt/chrome/policies/managed/cloudchrome.json` |
| Browser-level Google account disabled | Managed policy `BrowserSignin: 0` | same file |
| Google **website** sign-in remains available | Not a browser-signin control; verified in the Phase 2 acceptance test | — |
| Policy file is root-owned and not writable by the runtime user | `chown root:root`, `chmod 0644`, asserted at build time with `runuser -u vscode -- test ! -w …` | `.devcontainer/Dockerfile` |
| Policy content is validated at build time | `jq -e '.SyncDisabled == true and .BrowserSignin == 0'` | `.devcontainer/Dockerfile` |
| Container runs as a non-root user | `USER vscode` | `.devcontainer/Dockerfile` |
| Only port 6080 is forwarded | `forwardPorts: [6080]` | `.devcontainer/devcontainer.json` |
| No `--no-sandbox` | No flag in any image or probe; asserted by CI | Phase 1 CI |
| No `CAP_SYS_ADMIN` | Never requested; the capability set is recorded by the probe | Phase 1 probe |
| No personal browser data, secrets or backups in the image or context | `.dockerignore`, CI guard over `git ls-files`, in-image scan | `.dockerignore`, `ci.yml` |

### Changing the isolation policy

The managed policy directory is administrator-controlled and the file is
root-owned, so a normal `vscode` user cannot remove or edit it at runtime.
This is intentional: there is **no runtime switch** for the isolation policy.

To enable Chrome Sync deliberately:

1. Edit `.devcontainer/chrome-policies/managed/cloudchrome.json` in the
   repository (`BrowserSignin: 1`, `SyncDisabled: false`).
2. Commit the change.
3. **Rebuild the Codespace** so the new image is built and deployed.
4. Restart Chrome and sign in to Chrome from the Chrome UI.

The strict policy remains in force until the rebuilt image is deployed.

---

## 2. Chrome sandbox policy (Revision 5, unchanged)

1. Prefer a supported Chrome **user-namespace** sandbox.
2. A custom seccomp profile is retained **only if** the real Codespaces
   environment proves it works (it addresses the container seccomp layer only).
3. Sandbox activation is verified from **Chrome itself** — `chrome://sandbox`
   and renderer-level checks. A successful `unshare` probe is **not** proof.
4. `--no-sandbox` is never used.
5. `CAP_SYS_ADMIN` is never added. No automatic capability escalation.
6. If a supported sandbox cannot be established, Cloud-Chrome **fails closed**
   and reports the blocker. It does not ship an insecure workaround.

### Failure classification

| Class | Meaning | Remedy available inside the container? |
|---|---|---|
| **K1** | User namespaces disabled by a kernel sysctl | No — fail closed |
| **K2** | Required syscall not implemented (`ENOSYS`) | No — fail closed |
| **K3** | Namespace type unsupported | No — fail closed |
| **S** | Blocked by the **container's seccomp** layer | Possibly — a tailored profile may address it |
| **A** | Blocked by **host AppArmor** (`apparmor_restrict_unprivileged_userns`) | No — fail closed |
| **C** | The setuid sandbox helper cannot create namespaces without `CAP_SYS_ADMIN` | No — and **not an option by policy** |
| **X** | Chrome starts but does not activate a sandbox | No — fail closed |

A custom seccomp profile can only influence class **S**. It cannot override
host AppArmor or kernel restrictions.

---

## 3. Phase 1 experiment results

> Status: **pending**. This section is filled in with the raw evidence from a
> real GitHub Codespaces dev container as soon as the
> `.github/workflows/sandbox-probe.yml` run completes.
>
> The probe is `tests/phase1/sandbox-probe.sh`. It invokes the installed
> binaries directly (`Xtigervnc`, `google-chrome`, `tigervncpasswd`); it is a
> measurement harness and is not part of the runtime architecture.

### 3.1 Environment

_Filled in from the probe output._

### 3.2 Kernel / LSM layer

_Filled in from the probe output._

### 3.3 Container seccomp and capability layer

_Filled in from the probe output._

### 3.4 Namespace creation probes

_Filled in from the probe output._

### 3.5 Chrome sandbox activation (decisive)

_Filled in from the probe output._

### 3.6 TigerVNC password-file behaviour

_Filled in from the probe output._

### 3.7 Conclusion

_Filled in after the experiment._

---

## 4. Notes carried forward to Phase 2+ (not implemented in Phase 1)

These are design commitments recorded in Revision 5. None of them exist in the
Phase 1 image.

- **Backup archive member validation (Phase 3).** A restore treats the archive
  as untrusted input. The design requires a *structured, unambiguous*
  member-validation strategy: an explicit allow-list of member paths, rejection
  of every member that is not a regular file or directory (symlinks, hard
  links, device nodes, FIFOs and sockets are rejected outright), rejection of
  absolute paths and of `..` traversal components, extraction into a freshly
  created empty `0700` staging directory that is never reused, extraction of
  explicitly validated member names only, and `--no-same-owner
  --no-same-permissions` so archive metadata cannot set ownership or mode.
  `--keep-directory-symlink` must **not** be used: the GNU tar manual states it
  *"instructs tar to follow symlinks to directories when extracting from the
  archive"*, which is the opposite of the required behaviour. The protection
  comes from rejecting symlink members and from extracting into an empty
  directory, where tar's default behaviour (remove a same-named symlink,
  extract a real directory) applies.
- **No plaintext backup archives are left on disk unnecessarily** (Phase 3):
  backups are age-encrypted by default, and temporary plaintext artefacts are
  removed as soon as the encrypted archive is written.
- **The VNC password file is never part of a backup** (Phase 3).
- **Secret delivery** (Phase 2): `cloudchrome init` is silent; the VNC
  password is shown only by the explicit interactive `cloudchrome passwd`.
- **Chrome argument policy** (Phase 2): no user-supplied Chrome flags; the
  assembled argument vector is asserted against a forbidden-flag list before
  Chrome is launched.
