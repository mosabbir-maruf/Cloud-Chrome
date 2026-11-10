"""Adversarial tests for the backup, the restore and the archive validation.

These tests build **hostile** archives on purpose and check that the runtime
refuses them. A backup that only works for well-formed input is not a
backup.

The archives are built with the standard library only:

    python3 -m unittest discover -s tests/unit -t tests/unit
"""

import io
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))

from cloudchrome import archive as archive_module  # noqa: E402
from cloudchrome import manifest as manifest_module  # noqa: E402
from cloudchrome.errors import IntegrityError  # noqa: E402


def build_archive(path, members, extra_tarinfo=None):
    """Build a tar archive with the given members.

    ``members`` is a list of ``(name, content)`` pairs. ``extra_tarinfo`` is
    a list of ``TarInfo`` objects that are added as they are, which is how
    the hostile members are created.
    """
    with tarfile.open(path, "w:gz") as handle:
        for name, content in members:
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o600
            handle.addfile(info, io.BytesIO(data))
        for info in extra_tarinfo or []:
            if info.size:
                handle.addfile(info, io.BytesIO(b"\0" * info.size))
            else:
                handle.addfile(info)
    return path


def symlink_member(name, target):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.mode = 0o777
    return info


def hardlink_member(name, target):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    info.mode = 0o600
    return info


def fifo_member(name):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.FIFOTYPE
    info.mode = 0o600
    return info


def device_member(name):
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.CHRTYPE
    info.devmajor = 1
    info.devminor = 3
    info.mode = 0o600
    return info


VALID_MANIFEST = {
    "format": "cloudchrome-backup",
    "format_version": 1,
    "created_utc": "2026-01-01T00:00:00Z",
    "application": "cloudchrome",
    "application_version": "2.0.0",
    "label": "",
    "members": [],
}


class ArchiveValidationTest(unittest.TestCase):
    """Every hostile member must be refused."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="cloudchrome-archive.")
        self.addCleanup(shutil.rmtree, self.work, True)

    def path(self, name="archive.tar.gz"):
        return os.path.join(self.work, name)

    def test_a_valid_archive_is_accepted(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}"), ("keyring.pass", "secret")],
        )
        approved = archive_module.validate_archive(path)
        self.assertIn("manifest.json", approved)
        self.assertIn("keyring.pass", approved)

    def test_an_absolute_path_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}"), ("/etc/passwd", "root")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_traversal_component_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}"), ("../../etc/passwd", "root")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_traversal_in_the_middle_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}"), ("chrome/../../etc/passwd", "root")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_symlink_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}")],
            [symlink_member("chrome/passwd", "/etc/passwd")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_symlink_inside_an_approved_directory_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}")],
            [symlink_member("chrome/link", "../../etc/passwd")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_hard_link_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}")],
            [hardlink_member("chrome/hard", "manifest.json")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_fifo_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}")],
            [fifo_member("chrome/pipe")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_device_node_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}")],
            [device_member("chrome/zero")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_duplicate_member_is_refused(self):
        path = build_archive(
            self.path(),
            [
                ("manifest.json", "{}"),
                ("keyring.pass", "one"),
                ("keyring.pass", "two"),
            ],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_member_outside_the_approved_set_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}"), ("etc/shadow", "secret")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_missing_manifest_is_refused(self):
        path = build_archive(self.path(), [("keyring.pass", "secret")])
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_corrupted_archive_is_refused(self):
        path = self.path()
        with open(path, "wb") as handle:
            handle.write(b"this is not a tar archive at all")
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_truncated_archive_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}"), ("keyring.pass", "secret" * 100)],
        )
        size = os.path.getsize(path)
        with open(path, "r+b") as handle:
            handle.truncate(size // 2)
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)

    def test_a_backslash_in_a_path_is_refused(self):
        path = build_archive(
            self.path(),
            [("manifest.json", "{}"), ("chrome\\file", "x")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path)


class ExtractionTest(unittest.TestCase):
    """The extraction itself must be safe."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="cloudchrome-extract.")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.staging = os.path.join(self.work, "staging")

    def test_the_staging_directory_must_be_empty(self):
        os.makedirs(self.staging)
        with open(os.path.join(self.staging, "leftover"), "w", encoding="utf-8") as h:
            h.write("x")
        path = build_archive(
            os.path.join(self.work, "a.tar.gz"),
            [("manifest.json", "{}"), ("keyring.pass", "secret")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.extract_archive(path, self.staging)

    def test_extraction_creates_private_directories(self):
        path = build_archive(
            os.path.join(self.work, "a.tar.gz"),
            [("manifest.json", "{}"), ("chrome/Default/History", "data")],
        )
        archive_module.extract_archive(path, self.staging)
        extracted = os.path.join(self.staging, "chrome", "Default", "History")
        self.assertTrue(os.path.isfile(extracted))
        mode = stat.S_IMODE(os.stat(extracted).st_mode)
        self.assertEqual(mode, 0o600)

    def test_extraction_does_not_create_a_symlink(self):
        path = build_archive(
            os.path.join(self.work, "a.tar.gz"),
            [("manifest.json", "{}")],
            [symlink_member("chrome/link", "/etc/passwd")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.extract_archive(path, self.staging)
        self.assertFalse(os.path.lexists(os.path.join(self.staging, "chrome", "link")))

    def test_extraction_does_not_follow_an_existing_symlink(self):
        os.makedirs(self.staging)
        outside = os.path.join(self.work, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.staging, "chrome"))
        path = build_archive(
            os.path.join(self.work, "a.tar.gz"),
            [("manifest.json", "{}"), ("chrome/History", "data")],
        )
        with self.assertRaises(IntegrityError):
            archive_module.extract_archive(path, self.staging)
        # Nothing was written through the link.
        self.assertEqual(os.listdir(outside), [])


class ManifestTest(unittest.TestCase):
    """The manifest must be validated before anything is restored."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="cloudchrome-manifest.")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_a_valid_manifest_is_accepted(self):
        built = manifest_module.parse_manifest(
            manifest_module.manifest_bytes(
                dict(VALID_MANIFEST, members=[{"path": "keyring.pass", "size": 3, "sha256": "0" * 64}])
            )
        )
        self.assertEqual(built["format"], "cloudchrome-backup")

    def test_a_malformed_manifest_is_refused(self):
        with self.assertRaises(IntegrityError):
            manifest_module.parse_manifest(b"not json")

    def test_a_manifest_of_the_wrong_format_is_refused(self):
        with self.assertRaises(IntegrityError):
            manifest_module.parse_manifest(
                manifest_module.manifest_bytes(
                    dict(VALID_MANIFEST, format="something-else")
                )
            )

    def test_a_future_format_version_is_refused(self):
        with self.assertRaises(IntegrityError):
            manifest_module.parse_manifest(
                manifest_module.manifest_bytes(
                    dict(VALID_MANIFEST, format_version=99)
                )
            )

    def test_a_manifest_with_no_members_is_refused(self):
        with self.assertRaises(IntegrityError):
            manifest_module.parse_manifest(
                manifest_module.manifest_bytes(dict(VALID_MANIFEST, members=[]))
            )

    def test_a_manifest_with_a_bad_digest_is_refused(self):
        with self.assertRaises(IntegrityError):
            manifest_module.parse_manifest(
                manifest_module.manifest_bytes(
                    dict(
                        VALID_MANIFEST,
                        members=[{"path": "keyring.pass", "size": 3, "sha256": "xyz"}],
                    )
                )
            )

    def test_a_manifest_with_a_duplicate_member_is_refused(self):
        entry = {"path": "keyring.pass", "size": 3, "sha256": "0" * 64}
        with self.assertRaises(IntegrityError):
            manifest_module.parse_manifest(
                manifest_module.manifest_bytes(
                    dict(VALID_MANIFEST, members=[entry, dict(entry)])
                )
            )

    def test_a_manifest_with_a_negative_size_is_refused(self):
        with self.assertRaises(IntegrityError):
            manifest_module.parse_manifest(
                manifest_module.manifest_bytes(
                    dict(
                        VALID_MANIFEST,
                        members=[{"path": "keyring.pass", "size": -1, "sha256": "0" * 64}],
                    )
                )
            )

    def test_a_wrong_hash_is_detected(self):
        root = os.path.join(self.work, "root")
        os.makedirs(root)
        target = os.path.join(root, "keyring.pass")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("abc")
        built = manifest_module.build_manifest(
            [("keyring.pass", target)], root
        )
        # Corrupt the file after the manifest was built.
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("xyz")
        with self.assertRaises(IntegrityError):
            manifest_module.verify_members(built, root)

    def test_a_missing_member_is_detected(self):
        root = os.path.join(self.work, "root")
        os.makedirs(root)
        target = os.path.join(root, "keyring.pass")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("abc")
        built = manifest_module.build_manifest(
            [("keyring.pass", target)], root
        )
        os.unlink(target)
        with self.assertRaises(IntegrityError):
            manifest_module.verify_members(built, root)

    def test_an_unlisted_member_is_detected(self):
        root = os.path.join(self.work, "root")
        os.makedirs(root)
        target = os.path.join(root, "keyring.pass")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("abc")
        built = manifest_module.build_manifest(
            [("keyring.pass", target)], root
        )
        extra = os.path.join(root, "surprise")
        with open(extra, "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertRaises(IntegrityError):
            manifest_module.verify_members(built, root)

    def test_a_symlink_in_the_payload_is_refused(self):
        root = os.path.join(self.work, "root")
        os.makedirs(root)
        target = os.path.join(root, "link")
        os.symlink("/etc/passwd", target)
        with self.assertRaises(IntegrityError):
            manifest_module.build_manifest([("link", target)], root)


if __name__ == "__main__":
    unittest.main()
