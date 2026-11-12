# Cloud-Chrome — Acceptance

What has been verified automatically, and what still needs a person.

The two lists are kept apart on purpose. An automated check that proves
the noVNC port answers **HTTP 200** proves that the port answers. It does
not prove that a browser shows a desktop, and no automated check in this
repository claims that it does.

- [Automated and evidenced](#automated-and-evidenced)
- [Human browser acceptance](#human-browser-acceptance)
- [The state of this document](#the-state-of-this-document)

---

## Automated and evidenced

Every item below is checked by a script that runs in GitHub Actions or in
a real Codespaces dev container, and the raw transcripts are published as
issues in this repository.

| # | What is verified | How | Where |
|---|---|---|---|
| 1 | The image builds from a pinned base image | `docker build` with the digest from `config/versions.env` | CI, "Build and verify the image" |
| 2 | The static validation passes | shellcheck, hadolint, actionlint, the JSON documents, the version pins, the seccomp profile against its upstream baseline, the unit tests | CI, "Static validation" |
| 3 | The unit tests pass | `python3 -m unittest discover` — 134 tests, run both on the runner and inside the image, where the `age` tools are present and the restore tests are not skipped | CI, "Static validation" and "Build and verify the image" |
| 4 | The desktop stack comes up and goes down | the supervisor, Xvnc, D-Bus, the Keyring, XFCE, the noVNC bridge and Chrome are started, checked and stopped; no duplicate is left behind | CI, "Runtime tests (Phase 2)"; the runtime probe |
| 5 | The stack survives a stop and a start, and a restart of the container | the profile and the keyring are in the persistent state directory | CI, "Runtime tests (Phase 2)" |
| 6 | The security regression tests pass | the namespace flags, the socket domains, the `socketcall` compatibility path, the ASLR request, the capabilities, the forbidden Chrome flags, the DevTools port, the secret service, the VNC password file and its live replacement | CI, "Security audit and failure injection (Phase 4)"; the sandbox probe |
| 7 | Chrome's sandbox is active | every zygote and renderer has `NoNewPrivs=1` and `Seccomp=2`, and the renderers run in a different user namespace | CI, "Build and verify the image"; the runtime probe |
| 8 | The processes are cleaned up | after a stop, no supervised process is left; the pid files are removed; after a `SIGTERM` the supervisor exits, removes its pid file and leaves no component behind | CI, "Runtime tests (Phase 2)" and "Security audit and failure injection (Phase 4)" |
| 9 | The backup is encrypted and complete | the archive is an age file; the manifest hashes match the archive; the VNC password and the age identity are absent | CI, "Backup and restore tests (Phase 3)" |
| 10 | The restore is a transaction | the order of the operations is measured: the restored stack is stopped, the previous state is put back, the previous stack is started and checked; a stack that was stopped before the restore is left stopped; a failure before the stop leaves the original stack alone | CI, "Backup and restore tests (Phase 3)"; the runtime probe |
| 11 | The rollback keeps the previous data | the rollback copy is kept after a failure and still holds the previous profile, the keyring and the passphrase | CI, "Backup and restore tests (Phase 3)" |
| 12 | A hostile archive is refused | absolute paths, path traversal, symbolic links, hard links, FIFOs, device nodes, duplicate members, unapproved members, a missing manifest, a corrupted archive and a truncated archive | CI, "Backup and restore tests (Phase 3)" |
| 13 | The resource limits hold | the size of the archive, the size it expands to, the size of its largest member, the number of its members and the size of its manifest | CI, "Backup and restore tests (Phase 3)" |
| 14 | The Keyring and the Secret Service work | a secret is stored and read back; the daemon owns the `org.freedesktop.secrets` name; the login keyring exists; Chrome is not started when the service cannot be verified | CI, "Runtime tests (Phase 2)"; the runtime probe |
| 15 | The runtime state is private | every directory is mode 0700, every secret file is mode 0600, and a wrong owner or mode is refused | CI, "Runtime tests (Phase 2)"; `cloudchrome doctor` |
| 16 | The secrets never leak | the keyring passphrase is absent from the logs, from the process table and from the output of every command | CI, "Backup and restore tests (Phase 3)"; the runtime probe |
| 17 | The ports are as they should be | the RFB port listens on the loopback interface only; the noVNC port is bound; no DevTools listener; no unexpected listener; no SSH server | CI, "Build and verify the image"; the runtime probe |
| 18 | The current Codespaces probe passes | the whole stack, the backup, the restore, the transaction, the diagnostics and the host-level limitation, measured in a real Codespace | the runtime probe, published as an issue |
| 19 | The performance is measured | the startup, the shutdown, the warm start, the memory, the idle CPU, the noVNC latency, the RFB handshake, the backup and the restore, with the samples and the summary | the performance probe, published as an issue; `docs/PERFORMANCE.md` |
| 20 | The probes are well formed | the syntax, the isolation from the real state, the cleanup and the publication | CI, "Static validation" |

---

## Human browser acceptance

**None of these is verified by a script.** Each one needs a person, a
browser and a look at the screen.

| # | What has to be done | How it is judged | State |
|---|---|---|---|
| 1 | Open the forwarded port 6080 | The noVNC client loads in the browser. The port answering `HTTP 200` is **not** enough: the page has to render | **Pending** |
| 2 | Connect with the correct VNC password | The desktop appears. A rejected wrong password is checked automatically, but a successful connection is not | **Pending** |
| 3 | Confirm that XFCE and Chrome are visible | The window manager is on the screen and a Chrome window shows a page | **Pending** |
| 4 | Visit a normal website | The page loads over the network | **Pending** |
| 5 | Verify the Google website sign-in separately from the Chrome browser sign-in | The managed policy sets `BrowserSignin: 0` and `SyncDisabled: true`, which are **browser-level** controls. They do not and cannot block a sign-in to a *website*. Whether a Google website sign-in works in the desktop is a separate question, and it is **not** verified | **Pending** |
| 6 | Verify that the website session survives a stop and a start | Sign in, `cloudchrome stop`, `cloudchrome start`, and the session is still there | **Pending** |
| 7 | Verify the profile and the keyring across an actual Codespaces rebuild | Rebuild the Codespace and check that the profile, the keyring and the backups are still there. The design puts them on the persistent volume; **no rebuild has been performed end to end** | **Pending** |

### Why these are pending and not done

The repository is built and measured by scripts that run in CI and in a
Codespaces container. A person has never driven the browser against a
desktop, so every item above is marked pending. They are not marked
complete on the strength of an HTTP status code.

---

## The state of this document

| | |
|---|---|
| The automated items | verified by the CI run and the published Codespaces evidence: issue [#41](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/41) for the runtime and issue [#40](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/40) for the performance |
| The human items | **all seven are pending** |
| The known host-level limitation | unresolved; it needs a host-level control and it is documented in `docs/SECURITY.md` |

An item is only marked done when there is a transcript or a screenshot
behind it. No such record exists for the human items yet.
