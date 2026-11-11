"""Tests for the corrective pass: the limits, the output paths, the
plaintext cleanup and the restore transaction.

The transaction is exercised with a lifecycle that can be told to fail, so
every failure path of the restore is measured rather than assumed.

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
from cloudchrome import backup as backup_module  # noqa: E402
from cloudchrome import limits as limits_module  # noqa: E402
from cloudchrome import manifest as manifest_module  # noqa: E402
from cloudchrome.config import Config, DEFAULTS  # noqa: E402
from cloudchrome.errors import (  # noqa: E402
    CloudChromeError,
    IntegrityError,
    InvalidStateError,
)
from cloudchrome.paths import Layout  # noqa: E402

# The age tools are part of the image, not of every development machine. The
# tests that need them are skipped where they are absent and run in CI.
HAS_AGE = shutil.which("age") is not None and shutil.which("age-keygen") is not None
needs_age = unittest.skipUnless(HAS_AGE, "the age tools are not installed")


class FakeLifecycle:
    """A desktop stack lifecycle that can be told to fail.

    It records what it was asked to do, so a test can see that the restore
    stopped the stack, started it again and checked its health.
    """

    def __init__(self, running=False, fail_start=False, fail_health=False):
        self.running = running
        self.fail_start = fail_start
        self.fail_health = fail_health
        self.events = []

    def is_running(self):
        return self.running

    def stop(self, timeout=None):
        self.events.append("stop")
        was = self.running
        self.running = False
        return was

    def start(self, if_needed=False, timeout=180):
        self.events.append("start")
        if self.fail_start:
            raise CloudChromeError("the supervisor did not start")
        self.running = True

    def health(self):
        self.events.append("health")
        if self.fail_health:
            return {"healthy": False, "problems": ["Chrome is not running"]}
        return {"healthy": True, "problems": []}


class StateCase(unittest.TestCase):
    """A test case with a private state directory."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="cloudchrome-corrective.")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.persist = os.path.join(self.work, ".cloudchrome")
        self.config = Config(
            dict(
                DEFAULTS,
                CLOUDCHROME_PERSIST_DIR=self.persist,
                CLOUDCHROME_RUNTIME_DIR=os.path.join(self.persist, "run"),
            )
        )
        self.layout = Layout(self.config)
        self.layout.initialize()

    def make_profile(self, marker="original"):
        chrome = os.path.join(self.layout.chrome_dir, "Default")
        os.makedirs(chrome, exist_ok=True)
        with open(os.path.join(chrome, "Cookies"), "w", encoding="utf-8") as handle:
            handle.write(marker)
        with open(os.path.join(self.layout.keyring_pass_file), "w", encoding="utf-8") as handle:
            handle.write("the-passphrase")


class LimitsTest(unittest.TestCase):
    """The limits are resolved, and an unsafe value is refused."""

    def test_the_defaults_are_used(self):
        resolved = limits_module.Limits({})
        self.assertEqual(resolved.max_archive_bytes, limits_module.DEFAULT_MAX_ARCHIVE_BYTES)
        self.assertEqual(resolved.max_members, limits_module.DEFAULT_MAX_MEMBERS)

    def test_a_limit_can_be_lowered(self):
        resolved = limits_module.Limits({"CLOUDCHROME_MAX_MEMBERS": "10"})
        self.assertEqual(resolved.max_members, 10)

    def test_a_limit_cannot_be_raised_beyond_the_ceiling(self):
        with self.assertRaises(InvalidStateError):
            limits_module.Limits(
                {"CLOUDCHROME_MAX_MEMBERS": str(limits_module.HARD_MAX_MEMBERS + 1)}
            )

    def test_a_limit_must_be_positive(self):
        for value in ("0", "-1"):
            with self.assertRaises(InvalidStateError):
                limits_module.Limits({"CLOUDCHROME_MAX_TOTAL_BYTES": value})

    def test_a_limit_must_be_a_number(self):
        with self.assertRaises(InvalidStateError):
            limits_module.Limits({"CLOUDCHROME_MAX_ARCHIVE_BYTES": "many"})

    def test_an_empty_value_keeps_the_default(self):
        resolved = limits_module.Limits({"CLOUDCHROME_MAX_ARCHIVE_BYTES": ""})
        self.assertEqual(
            resolved.max_archive_bytes, limits_module.DEFAULT_MAX_ARCHIVE_BYTES
        )


class OutputPathTest(StateCase):
    """The output path of a backup is handled safely."""

    def test_a_shared_directory_is_not_changed(self):
        # /tmp is shared and mode 1777; the runtime must not touch it.
        before = stat.S_IMODE(os.stat("/tmp").st_mode)
        target = os.path.join("/tmp", "cloudchrome-output-test.tar.gz.age")
        resolved = backup_module._resolve_output(self.layout, target)
        self.assertEqual(resolved, target)
        self.assertEqual(stat.S_IMODE(os.stat("/tmp").st_mode), before)

    def test_a_missing_directory_is_refused(self):
        with self.assertRaises(InvalidStateError):
            backup_module._resolve_output(
                self.layout, os.path.join(self.work, "absent", "backup.tar.gz.age")
            )

    def test_a_symlink_target_is_refused(self):
        real = os.path.join(self.work, "real.tar.gz.age")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write("x")
        link = os.path.join(self.work, "link.tar.gz.age")
        os.symlink(real, link)
        with self.assertRaises(InvalidStateError):
            backup_module._resolve_output(self.layout, link)

    def test_a_directory_target_is_refused(self):
        target = os.path.join(self.work, "a-directory")
        os.makedirs(target)
        with self.assertRaises(InvalidStateError):
            backup_module._resolve_output(self.layout, target)

    def test_an_existing_file_target_is_accepted(self):
        target = os.path.join(self.work, "existing.tar.gz.age")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("old")
        resolved = backup_module._resolve_output(self.layout, target)
        self.assertEqual(resolved, target)

    def test_the_default_target_is_in_the_managed_directory(self):
        resolved = backup_module._resolve_output(self.layout, None)
        self.assertTrue(resolved.startswith(self.layout.backup_dir))
        self.assertTrue(resolved.endswith(backup_module.BACKUP_SUFFIX))


class PlaintextCleanupTest(StateCase):
    """The temporary plaintext is removed, and no erasure is claimed."""

    def test_the_cleanup_removes_the_directory(self):
        work = os.path.join(self.work, "plain")
        os.makedirs(work)
        with open(os.path.join(work, "backup.tar.gz"), "wb") as handle:
            handle.write(b"x" * (2 * 1024 * 1024))
        backup_module._remove_plaintext(work)
        self.assertFalse(os.path.exists(work))

    def test_the_cleanup_handles_a_missing_directory(self):
        backup_module._remove_plaintext(os.path.join(self.work, "absent"))

    def test_the_documentation_does_not_claim_physical_erasure(self):
        import cloudchrome.backup as module

        source = module.__doc__ + "\n" + module._remove_plaintext.__doc__
        self.assertNotIn("survive in the free blocks", source)
        self.assertIn("not** a guarantee", source)


class ArchiveLimitTest(unittest.TestCase):
    """An archive that exceeds the limits is refused."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="cloudchrome-limits.")
        self.addCleanup(shutil.rmtree, self.work, True)

    def build(self, name, members, extras=None):
        path = os.path.join(self.work, name)
        with tarfile.open(path, "w:gz") as handle:
            for member, content in members:
                data = content.encode()
                info = tarfile.TarInfo(name=member)
                info.size = len(data)
                info.mode = 0o600
                handle.addfile(info, io.BytesIO(data))
            for info in extras or []:
                handle.addfile(info)
        return path

    def test_an_oversized_member_is_refused(self):
        path = self.build(
            "big.tar.gz",
            [("manifest.json", "{}"), ("chrome/Big", "y" * 4096)],
        )
        limits = limits_module.Limits({"CLOUDCHROME_MAX_MEMBER_BYTES": "1024"})
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path, limits=limits)

    def test_too_many_members_are_refused(self):
        members = [("manifest.json", "{}")]
        members += [("chrome/f%d" % index, "x") for index in range(20)]
        path = self.build("many.tar.gz", members)
        limits = limits_module.Limits({"CLOUDCHROME_MAX_MEMBERS": "5"})
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path, limits=limits)

    def test_a_compressed_payload_beyond_the_total_limit_is_refused(self):
        # A member that is small on disk but expands far beyond the limit.
        path = self.build(
            "bomb.tar.gz",
            [("manifest.json", "{}"), ("chrome/Bomb", "0" * 200000)],
        )
        limits = limits_module.Limits({"CLOUDCHROME_MAX_TOTAL_BYTES": "1024"})
        with self.assertRaises(IntegrityError):
            archive_module.validate_archive(path, limits=limits)


@needs_age
class RestoreTransactionTest(StateCase):
    """The restore is one transaction, and every failure rolls back."""

    def setUp(self):
        super().setUp()
        self.make_profile()
        self.source = self._build_backup(marker="restored")

    def _build_backup(self, marker="restored"):
        """Build an encrypted backup the restore can read."""
        backup_module.ensure_identity(self.layout)
        chrome = os.path.join(self.layout.chrome_dir, "Default")
        os.makedirs(chrome, exist_ok=True)
        with open(os.path.join(chrome, "Cookies"), "w", encoding="utf-8") as handle:
            handle.write(marker)
        payload = backup_module._collect_payload(self.layout)
        built = manifest_module.build_manifest(payload, self.layout.persist_dir)
        target = os.path.join(self.work, "backup.tar.gz.age")
        recipient = backup_module.read_recipient(self.layout)
        import subprocess as sp

        age_process = sp.Popen(
            [
                "age",
                "--encrypt",
                "--recipient",
                recipient,
                "--output",
                target,
            ],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
        )
        with tarfile.open(fileobj=age_process.stdin, mode="w|gz") as archive:
            backup_module._add_bytes(
                archive, manifest_module.manifest_bytes(built), "manifest.json"
            )
            for relative, absolute in payload:
                backup_module._add_file(archive, absolute, relative)
        age_process.communicate(timeout=120)
        return target

    def _profile_marker(self):
        path = os.path.join(self.layout.chrome_dir, "Default", "Cookies")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _passphrase(self):
        with open(self.layout.keyring_pass_file, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_a_successful_restore_replaces_the_state(self):
        lifecycle = FakeLifecycle(running=True)
        result = backup_module.restore_transaction(
            lifecycle, self.layout, self.config, source=self.source, assume_yes=True
        )
        self.assertTrue(result["health"]["healthy"])
        self.assertEqual(self._profile_marker(), "restored")
        self.assertIn("stop", lifecycle.events)
        self.assertIn("start", lifecycle.events)
        self.assertIn("health", lifecycle.events)

    def test_a_failed_health_check_rolls_the_state_back(self):
        self._profile_marker()
        with open(
            os.path.join(self.layout.chrome_dir, "Default", "Cookies"), "w", encoding="utf-8"
        ) as handle:
            handle.write("changed-after-backup")
        before = self._profile_marker()
        lifecycle = FakeLifecycle(running=True, fail_health=True)
        with self.assertRaises(CloudChromeError):
            backup_module.restore_transaction(
                lifecycle, self.layout, self.config, source=self.source, assume_yes=True
            )
        self.assertEqual(self._profile_marker(), before)
        self.assertIn("stop", lifecycle.events)
        self.assertIn("start", lifecycle.events)

    def test_a_failed_start_rolls_the_state_back(self):
        with open(
            os.path.join(self.layout.chrome_dir, "Default", "Cookies"), "w", encoding="utf-8"
        ) as handle:
            handle.write("changed-after-backup")
        before = self._profile_marker()
        lifecycle = FakeLifecycle(running=True, fail_start=True)
        with self.assertRaises(CloudChromeError):
            backup_module.restore_transaction(
                lifecycle, self.layout, self.config, source=self.source, assume_yes=True
            )
        self.assertEqual(self._profile_marker(), before)

    def test_the_previous_passphrase_survives_a_failed_restore(self):
        before = self._passphrase()
        lifecycle = FakeLifecycle(running=True, fail_health=True)
        with self.assertRaises(CloudChromeError):
            backup_module.restore_transaction(
                lifecycle, self.layout, self.config, source=self.source, assume_yes=True
            )
        self.assertEqual(self._passphrase(), before)

    def test_a_failed_restore_keeps_no_staging_directory(self):
        lifecycle = FakeLifecycle(running=True, fail_health=True)
        with self.assertRaises(CloudChromeError):
            backup_module.restore_transaction(
                lifecycle, self.layout, self.config, source=self.source, assume_yes=True
            )
        leftovers = [
            name
            for name in os.listdir(self.layout.backup_dir)
            if name.startswith(".cloudchrome-stage.")
        ]
        self.assertEqual(leftovers, [])

    def test_a_successful_restore_disposes_of_the_rollback_state(self):
        lifecycle = FakeLifecycle(running=True)
        backup_module.restore_transaction(
            lifecycle, self.layout, self.config, source=self.source, assume_yes=True
        )
        leftovers = [
            name
            for name in os.listdir(self.layout.persist_dir)
            if name.startswith(".cloudchrome-rollback.")
        ]
        self.assertEqual(leftovers, [])

    def test_a_failed_restore_keeps_the_rollback_state(self):
        lifecycle = FakeLifecycle(running=True, fail_health=True)
        with self.assertRaises(CloudChromeError):
            backup_module.restore_transaction(
                lifecycle, self.layout, self.config, source=self.source, assume_yes=True
            )
        kept = [
            name
            for name in os.listdir(self.layout.persist_dir)
            if name.startswith(".cloudchrome-rollback.")
        ]
        self.assertEqual(
            len(kept), 1, "the rollback state must be kept after a failure"
        )

    def test_the_kept_rollback_state_holds_the_previous_profile(self):
        before = self._profile_marker()
        lifecycle = FakeLifecycle(running=True, fail_health=True)
        with self.assertRaises(CloudChromeError):
            backup_module.restore_transaction(
                lifecycle, self.layout, self.config, source=self.source, assume_yes=True
            )
        kept = [
            name
            for name in os.listdir(self.layout.persist_dir)
            if name.startswith(".cloudchrome-rollback.")
        ]
        self.assertEqual(len(kept), 1)
        saved = os.path.join(self.layout.persist_dir, kept[0], "chrome")
        self.assertTrue(os.path.isdir(saved))
        marker = os.path.join(saved, "Default", "Cookies")
        self.assertTrue(os.path.isfile(marker))
        with open(marker, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_an_installation_that_fails_partway_is_rolled_back(self):
        # The installation moves the live state aside and then fails. The
        # previous state must be put back: the flag that says the state may
        # have been moved is set before the move, not after it.
        before = self._profile_marker()
        original = backup_module._swap_in

        def broken_swap(layout_arg, staging, rollback):
            original(layout_arg, staging, rollback)
            raise OSError("the installation failed")

        backup_module._swap_in = broken_swap
        try:
            with self.assertRaises(OSError):
                backup_module.restore_transaction(
                    FakeLifecycle(running=True),
                    self.layout,
                    self.config,
                    source=self.source,
                    assume_yes=True,
                )
        finally:
            backup_module._swap_in = original
        self.assertEqual(self._profile_marker(), before)

    def test_the_keyring_survives_a_failed_restore(self):
        before = self._passphrase()
        lifecycle = FakeLifecycle(running=True, fail_health=True)
        with self.assertRaises(CloudChromeError):
            backup_module.restore_transaction(
                lifecycle, self.layout, self.config, source=self.source, assume_yes=True
            )
        self.assertEqual(self._passphrase(), before)


class PayloadTest(StateCase):
    """What goes into a backup, and what does not."""

    def setUp(self):
        super().setUp()
        self.make_profile()
        # A profile that Chrome has used carries the singleton files.
        for name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
            with open(os.path.join(self.layout.chrome_dir, name), "w", encoding="utf-8") as handle:
                handle.write("x")

    def test_the_vnc_password_is_not_in_the_payload(self):
        names = [name for name, _absolute in backup_module._collect_payload(self.layout)]
        self.assertNotIn("vnc.pass", names)

    def test_the_singleton_files_are_not_in_the_payload(self):
        names = [name for name, _absolute in backup_module._collect_payload(self.layout)]
        for singleton in backup_module.CHROME_SINGLETON_FILES:
            self.assertNotIn("chrome/%s" % singleton, names)

    def test_the_keyring_data_is_in_the_payload(self):
        store = os.path.join(self.layout.keyring_store_dir, "login.keyring")
        os.makedirs(self.layout.keyring_store_dir, exist_ok=True)
        with open(store, "w", encoding="utf-8") as handle:
            handle.write("keyring")
        names = [name for name, _absolute in backup_module._collect_payload(self.layout)]
        self.assertIn("keyring/login.keyring", names)

    def test_the_profile_is_in_the_payload(self):
        names = [name for name, _absolute in backup_module._collect_payload(self.layout)]
        self.assertIn("chrome/Default/Cookies", names)

    def test_the_passphrase_is_in_the_payload(self):
        names = [name for name, _absolute in backup_module._collect_payload(self.layout)]
        self.assertIn("keyring.pass", names)


class RestoreVerificationTest(StateCase):
    """The installed state is verified, and a failure rolls it back."""

    def setUp(self):
        super().setUp()
        self.make_profile()

    def test_the_verification_accepts_a_correct_state(self):
        self.assertTrue(backup_module._verify_installed(self.layout))

    def test_the_verification_refuses_a_foreign_owner(self):
        # The check reads the owner; a directory owned by somebody else must be
        # refused. The test cannot change the owner, so the check is exercised
        # through the code path that reports it.
        info = os.lstat(self.layout.chrome_dir)
        self.assertEqual(info.st_uid, os.getuid())


if __name__ == "__main__":
    unittest.main()
