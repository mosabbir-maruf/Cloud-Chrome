# Cloud-Chrome — Release checklist

What has to be true before the runtime is published as a release. The
publication itself is the repository owner's decision; this checklist is
what has to be ticked first.

- [The build](#the-build)
- [The evidence](#the-evidence)
- [The documentation](#the-documentation)
- [The artefacts](#the-artefacts)
- [The known limitations](#the-known-limitations)
- [The publication](#the-publication)
- [Public launch](#public-launch)

---

## The build

- [ ] The CI run of the release commit is green, and every job passed.
- [ ] The unit tests pass inside the image, with **no** unexpected skips.
      The restore tests need the `age` tools, so a run of the unit tests
      on a bare runner is not enough.
- [ ] The image builds from the pinned base image, and the digest in
      `config/versions.env` still matches the one in the Dockerfile.
- [ ] The image size is recorded, and it has not grown without a reason.
      The size of the last build is in the summary of the build job.
- [ ] The dependency pins are reviewed: the pinned packages are still
      pinned, and the tracked packages are not older than the recorded
      versions.
- [ ] The shell scripts are lint clean, the Dockerfile passes hadolint and
      the workflow passes actionlint.

## The evidence

- [ ] The runtime probe has been run against a real Codespace and its
      transcript is published as an issue.
- [ ] The performance probe has been run against a real Codespace and its
      transcript is published as an issue.
- [ ] `cloudchrome doctor` passes in that Codespace.
- [ ] The security evidence is current: the seccomp profile still matches
      its pinned upstream baseline, and the sandbox, the capability and
      the namespace measurements are from the release commit.
- [ ] The backup and the restore were verified in the image: the manifest
      hashes match the archive, the hostile archives are refused, the
      rollback keeps the previous data and the transaction order is
      measured.
- [ ] No evidence contains a personal cookie, a browsing history, a
      password, an age identity, a passphrase or a VNC credential.

## The documentation

- [ ] The version in `lib/cloudchrome/version.py`, the version the
      `cloudchrome version` command prints, the version in
      `docs/CHANGELOG.md` and the version in this checklist are the same.
- [ ] The dependency pins in `docs/CHANGELOG.md` match
      `config/versions.env`.
- [ ] `README.md`, `docs/USAGE.md`, `docs/BACKUP.md`, `docs/SECURITY.md`,
      `docs/PERFORMANCE.md` and `docs/ACCEPTANCE.md` agree with each
      other and with the code.
- [ ] The README quick start, troubleshooting links, and license scope are
      accurate; the root MIT license covers only original project material.
- [ ] The state layout in `docs/USAGE.md` matches what the runtime
      creates, including the GNOME Keyring store at `xdg/keyrings/`.
- [ ] The evidence issues that the documents refer to are the current
      ones, and the earlier ones are only mentioned as history.
- [ ] Every claim about a rebuild, a Google website sign-in or a rendered
      desktop is either backed by a record or marked as pending.

## The artefacts

- [ ] `git status` is clean, apart from the files that are deliberately
      not tracked.
- [ ] No temporary probe branch and no Codespace is left behind.
- [ ] No personal browser data, no credential, no backup and no private
      key is tracked by Git.
- [ ] The build context excludes the persistent state directory.
- [ ] The image carries no profile, no keyring, no backup and no password
      file; CI scans for them.

## The known limitations

- [ ] The host-level `int 0x80` limitation is still documented as
      unresolved, in `docs/SECURITY.md` and in `README.md`.
- [ ] Nothing in the documentation claims complete syscall isolation or
      unrestricted production security.
- [ ] The statement that overwriting and unlinking is not secure erasure
      is still there.
- [ ] The manual acceptance items in `docs/ACCEPTANCE.md` are either
      done with a record behind them, or they are still marked pending.

## The publication

The publication is the repository owner's. When it happens:

- [ ] The tag is the version of `lib/cloudchrome/version.py`.
- [ ] The release notes are the section of `docs/CHANGELOG.md` for that
      version.
- [ ] The image is built from the release commit and its digest is
      recorded.
- [ ] For a public launch, the linked GHCR package visibility is explicitly
      set to Public so anonymous users can pull the prebuilt image.
- [ ] The publishing workflow retains only the current tested image version;
      documentation-only changes do not publish an image.
- [ ] If the GHCR package is public, check whether GitHub's 5,000-download
      deletion limit blocks old versions; the workflow must report blocked
      cleanup without claiming one-version retention.
- [ ] The human acceptance items are done, or the release notes say that
      they are not.

## Public launch

Repository and package visibility are separate owner actions. Making the
repository public does **not** make its GHCR package public. Do not announce
that anonymous prebuilt-image use works until the separate package setting
and an anonymous pull have both been checked.

### Repository owner's launch checklist

- [ ] Complete real-Codespace acceptance testing in
      [`ACCEPTANCE.md`](ACCEPTANCE.md): render the browser desktop, confirm
      website login and session persistence, and test an actual Codespaces
      rebuild. Keep unverified items marked pending.
- [ ] Confirm the forwarded port `6080` opens the noVNC desktop in a normal
      browser, with XFCE and Chrome visible.
- [ ] Confirm website login behavior and that the relevant website session
      survives a Codespace stop/start. Chrome browser-level sign-in and Sync
      policy are a separate matter.
- [ ] Confirm that the Chrome profile, GNOME Keyring and backups behave as
      documented across an actual Codespace rebuild.
- [ ] Review the user runbook, security limitations, troubleshooting, and the
      root [`LICENSE`](../LICENSE); confirm they accurately describe this
      repository and the third-party boundaries.
- [ ] When intentionally launching publicly, change repository visibility
      to **Public** in the repository settings.
- [ ] Separately change the linked GHCR package visibility to **Public** if
      anonymous Codespaces pulls are intended. Repository visibility alone is
      insufficient.
- [ ] From an anonymous environment, verify that
      `ghcr.io/mosabbir-maruf/cloud-chrome:latest` can be pulled without
      credentials. Do not use an authenticated pull as evidence of anonymous
      access.
- [ ] Record the published image digest and verify it corresponds to the
      tested image artifact/release commit.
- [ ] Test Codespaces creation from the public repository using the published
      image, including initialization and the browser workflow.
- [ ] Confirm the latest CI publisher and retention result separately; a PR
      run does not publish. The publisher runs only for eligible successful
      `main` pushes or manual dispatches; verify its run and remote retention.
- [ ] Review whether GitHub's public-package 5,000-download deletion limit
      affects retention, and describe any blocked cleanup accurately.

The repository visibility change is separate from the GHCR package visibility
change above. Neither visibility change is part of this documentation task.

### Normal users

- [ ] Create a personal Codespace; repository read access does not grant
      upstream write permission.
- [ ] Keep port `6080` private and do not expose port `5900`.
- [ ] Store the VNC password and backup age identity privately. Before
      deleting a Codespace, preserve both the encrypted backup archive and its
      matching age identity outside the Codespace.
- [ ] Check personal Codespaces quota and spending limits. Stop the Codespace
      when it is not in use; storage billing may continue under GitHub's
      current policies.
