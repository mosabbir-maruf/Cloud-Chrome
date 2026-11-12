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
