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
from unittest import mock

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
    """A desktop stack lifecycle that records the order of the operations.

    The recovery of a failed restore is about the *order* of the
    operations: the restored stack has to be stopped before the previous
    state is put back, and the previous stack has to be started again
    afterwards. The test therefore measures the sequence, not only the
    files afterwards.
    """

    def __init__(
        self,
        running=False,
        fail_start=False,
        fail_health=False,
        fail_health_at=None,
        fail_start_at=None,
        partial_start=False,
    ):
        self.running = running
        self.fail_start = fail_start
        self.fail_health = fail_health
        # The health call, counted from one, that fails. None means that
        # every health call passes.
        self.fail_health_at = 1 if fail_health else fail_health_at
        # The start call, counted from one, that fails. None means that
        # every start call succeeds.
        self.fail_start_at = 1 if fail_start else fail_start_at
        # A start that fails half way leaves a stack behind.
        self.partial_start = partial_start
        self.events = []
        self.health_calls = 0
        self.start_calls = 0

    def is_running(self):
        return self.running

    def stop(self, timeout=None):
        self.events.append("stop")
        was = self.running
        self.running = False
        return was

    def start(self, if_needed=False, timeout=180):
        self.events.append("start")
        self.start_calls += 1
        if self.fail_start_at == self.start_calls:
            if self.partial_start:
                # The start failed half way: a stack is still there, and the
                # recovery has to clean it up.
                self.running = True
            raise CloudChromeError("the supervisor did not start")
        self.running = True

    def health(self):
        self.events.append("health")
        self.health_calls += 1
        if self.fail_health_at == self.health_calls:
            return {"healthy": False, "problems": ["the desktop is not usable"]}
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


class BackupFixture(StateCase):
    """A state with a real encrypted backup the restore can read."""

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

    def _rollback_copies(self):
        """The rollback directories that are kept."""
        return sorted(
            name
            for name in os.listdir(self.layout.persist_dir)
            if name.startswith(".cloudchrome-rollback.")
        )

    def _record_the_rollback(self, lifecycle):
        """Make the rollback visible in the recorded order.

        The recovery has to stop the restored stack *before* the previous
        state is put back, and the rollback is a file operation that the
        lifecycle does not know about. Recording it in the same sequence
        is what makes the order measurable.
        """
        original = backup_module._rollback

        def recording(layout_arg, rollback_arg):
            lifecycle.events.append("rollback")
            return original(layout_arg, rollback_arg)

        backup_module._rollback = recording
        self.addCleanup(setattr, backup_module, "_rollback", original)


@needs_age
class RestoreTransactionTest(BackupFixture):
    """The restore is one transaction, and every failure rolls back."""

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


@needs_age
class RestoreRecoveryTest(BackupFixture):
    """The recovery of a failed restore, in the order it happens.

    The recovery has to stop the restored stack before it puts the previous
    state back: a restored desktop that is still up may be writing to the
    profile that is about to be replaced. The tests therefore measure the
    *order* of the operations and not only the files afterwards.
    """

    def _restore(self, lifecycle, source=None):
        return backup_module.restore_transaction(
            lifecycle,
            self.layout,
            self.config,
            source=source or self.source,
            assume_yes=True,
        )

    def test_the_health_failure_stops_the_restored_stack_before_the_rollback(self):
        # The original stack is running, the restore starts the restored
        # stack, its health check fails. The recovery has to stop the
        # restored stack, put the previous state back, and start the
        # previous stack again.
        before = self._profile_marker()
        lifecycle = FakeLifecycle(running=True, fail_health_at=1)
        self._record_the_rollback(lifecycle)

        with self.assertRaises(CloudChromeError):
            self._restore(lifecycle)

        self.assertEqual(
            lifecycle.events,
            ["stop", "start", "health", "stop", "rollback", "start", "health"],
            "the restored stack has to be stopped before the rollback",
        )
        # The previous profile is back, and the previous stack runs again.
        self.assertEqual(self._profile_marker(), before)
        self.assertTrue(lifecycle.running)
        # The rollback copy is kept, with the previous data in it.
        kept = self._rollback_copies()
        self.assertEqual(len(kept), 1)
        saved = os.path.join(self.layout.persist_dir, kept[0], "chrome")
        self.assertTrue(
            os.path.isfile(os.path.join(saved, "Default", "Cookies")),
            "the kept rollback state has to hold the previous profile",
        )

    def test_the_previous_stack_is_healthy_after_the_recovery(self):
        # The health of the previous stack is checked, not assumed.
        lifecycle = FakeLifecycle(running=True, fail_health_at=1)
        self._record_the_rollback(lifecycle)

        with self.assertRaises(CloudChromeError):
            self._restore(lifecycle)

        # The first health call is the restored stack's and fails, the
        # second one is the previous stack's and passes.
        self.assertEqual(lifecycle.health_calls, 2)

    def test_a_start_that_fails_partly_leaves_nothing_running(self):
        # A start that fails half way may have left a stack behind. The
        # recovery has to detect it and stop it before the rollback.
        before = self._profile_marker()
        lifecycle = FakeLifecycle(
            running=True, fail_start_at=1, partial_start=True
        )
        self._record_the_rollback(lifecycle)

        with self.assertRaises(CloudChromeError):
            self._restore(lifecycle)

        self.assertEqual(
            lifecycle.events,
            ["stop", "start", "stop", "rollback", "start", "health"],
            "a partly started stack has to be stopped before the rollback",
        )
        self.assertEqual(self._profile_marker(), before)
        self.assertTrue(lifecycle.running)
        self.assertEqual(len(self._rollback_copies()), 1)

    def test_a_stopped_stack_is_left_stopped_after_a_failed_restore(self):
        # The stack was stopped before the restore. The restore starts the
        # restored stack, its health check fails, and the stack has to be
        # left stopped: the previous stack was not running either.
        lifecycle = FakeLifecycle(running=False, fail_health_at=1)
        self._record_the_rollback(lifecycle)

        with self.assertRaises(CloudChromeError):
            self._restore(lifecycle)

        self.assertEqual(
            lifecycle.events,
            ["start", "health", "stop", "rollback"],
            "a stack that was stopped before the restore has to be left stopped",
        )
        self.assertFalse(lifecycle.running)
        self.assertEqual(len(self._rollback_copies()), 1)

    def test_a_failure_before_the_stop_does_not_stop_the_original_stack(self):
        # The archive cannot be decrypted, so the failure happens before
        # the original stack is stopped. The original stack must not be
        # touched: it is still running with its own data.
        corrupt = os.path.join(self.work, "corrupt.tar.gz.age")
        with open(corrupt, "wb") as handle:
            handle.write(b"this is not an age file")
        lifecycle = FakeLifecycle(running=True)
        self._record_the_rollback(lifecycle)

        with self.assertRaises(CloudChromeError):
            self._restore(lifecycle, source=corrupt)

        self.assertEqual(
            lifecycle.events,
            [],
            "a failure before the stop must not stop the original stack",
        )
        self.assertTrue(lifecycle.running)
        self.assertEqual(self._rollback_copies(), [])

    def test_the_recovery_reports_the_failure_and_never_reports_success(self):
        # A failed restore raises, so the command exits non-zero and never
        # prints a success message.
        lifecycle = FakeLifecycle(running=True, fail_health_at=1)

        with self.assertRaises(CloudChromeError) as caught:
            self._restore(lifecycle)

        self.assertIn("the restored desktop is not usable", str(caught.exception))

    def test_a_rollback_that_fails_keeps_the_previous_state(self):
        # When the recovery cannot put the previous state back, the error
        # says so and the rollback copy is kept.
        original = backup_module._rollback

        def broken(layout_arg, rollback_arg):
            raise OSError("the rollback failed")

        backup_module._rollback = broken
        self.addCleanup(setattr, backup_module, "_rollback", original)

        lifecycle = FakeLifecycle(running=True, fail_health_at=1)

        with self.assertRaises(CloudChromeError) as caught:
            self._restore(lifecycle)

        self.assertIn("could not be put back", str(caught.exception))
        self.assertEqual(len(self._rollback_copies()), 1)

    def test_a_previous_stack_that_does_not_start_keeps_the_rollback(self):
        # The previous stack cannot be started either. The error says so,
        # and the previous data stays recoverable.
        lifecycle = FakeLifecycle(running=True, fail_health_at=1)
        self._record_the_rollback(lifecycle)
        # The second start is the one of the previous stack.
        original_start = lifecycle.start
        calls = {"count": 0}

        def failing_start(if_needed=False, timeout=180):
            calls["count"] += 1
            if calls["count"] == 2:
                raise CloudChromeError("the supervisor did not start")
            return original_start(if_needed=if_needed, timeout=timeout)

        lifecycle.start = failing_start

        with self.assertRaises(CloudChromeError) as caught:
            self._restore(lifecycle)

        self.assertIn("could not be started", str(caught.exception))
        self.assertEqual(len(self._rollback_copies()), 1)


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


def _stat_with_uid(info, uid):
    """A stat result with a different owner."""
    fields = list(info)
    fields[4] = uid
    return os.stat_result(tuple(fields))


class RestoreVerificationTest(StateCase):
    """The installed state is verified against the paths that are used.

    The verification has to look at the directory the keyring daemon
    actually uses, and not at the legacy directory that holds nothing.
    """

    def setUp(self):
        super().setUp()
        self.make_profile()

    def _with_a_foreign_owner(self, path):
        """Make the verification see *path* as owned by somebody else.

        A test cannot change the owner of a directory without root, so the
        stat result is made to report a different owner instead.
        """
        real_lstat = os.lstat
        target = os.path.abspath(path)

        def fake_lstat(entry):
            info = real_lstat(entry)
            if os.path.abspath(entry) == target:
                return _stat_with_uid(info, info.st_uid + 1)
            return info

        return mock.patch("os.lstat", fake_lstat)

    def test_the_verification_accepts_a_correct_state(self):
        self.assertTrue(backup_module._verify_installed(self.layout))

    def test_the_verification_refuses_a_foreign_owner(self):
        # The check reads the owner; a directory owned by somebody else must be
        # refused. The test cannot change the owner, so the check is exercised
        # through the code path that reports it.
        info = os.lstat(self.layout.chrome_dir)
        self.assertEqual(info.st_uid, os.getuid())

    def test_the_keyring_store_directory_is_the_one_that_is_checked(self):
        # The verification has to look at the directory the keyring daemon
        # uses. The legacy directory holds nothing, so a check that looked
        # at it would prove nothing about the live store.
        checked = []
        real_lstat = os.lstat

        def recording_lstat(entry):
            checked.append(os.path.abspath(entry))
            return real_lstat(entry)

        with mock.patch("os.lstat", recording_lstat):
            self.assertTrue(backup_module._verify_installed(self.layout))

        self.assertIn(os.path.abspath(self.layout.keyring_store_dir), checked)
        self.assertNotIn(os.path.abspath(self.layout.keyring_dir), checked)

    def test_a_foreign_owner_on_the_keyring_store_is_refused(self):
        with self._with_a_foreign_owner(self.layout.keyring_store_dir):
            with self.assertRaises(InvalidStateError) as caught:
                backup_module._verify_installed(self.layout)
        self.assertIn(self.layout.keyring_store_dir, str(caught.exception))

    def test_the_legacy_keyring_directory_cannot_hide_a_bad_store(self):
        # The legacy directory is private and owned by the runtime user, so
        # it would satisfy a check that looked at it. The real store is
        # invalid here, and the verification has to refuse it.
        legacy = self.layout.keyring_dir
        os.chmod(legacy, 0o700)
        info = os.lstat(legacy)
        self.assertEqual(info.st_uid, os.getuid())

        with self._with_a_foreign_owner(self.layout.keyring_store_dir):
            with self.assertRaises(InvalidStateError):
                backup_module._verify_installed(self.layout)

    def test_the_keyring_store_permissions_are_corrected(self):
        # The store belongs to the runtime user, so the runtime may restore
        # the permissions it is supposed to have.
        os.chmod(self.layout.keyring_store_dir, 0o755)
        backup_module._verify_installed(self.layout)
        mode = stat.S_IMODE(os.stat(self.layout.keyring_store_dir).st_mode)
        self.assertEqual(mode, 0o700)

    def test_the_keyring_store_is_not_a_symbolic_link(self):
        # A link is never followed: the chmod would otherwise change the
        # mode of whatever the link points at.
        store = self.layout.keyring_store_dir
        elsewhere = os.path.join(self.work, "elsewhere")
        os.makedirs(elsewhere, exist_ok=True)
        shutil.rmtree(store)
        os.symlink(elsewhere, store)
        with self.assertRaises(InvalidStateError):
            backup_module._verify_installed(self.layout)

    def test_a_foreign_owner_on_the_passphrase_is_refused(self):
        with self._with_a_foreign_owner(self.layout.keyring_pass_file):
            with self.assertRaises(InvalidStateError) as caught:
                backup_module._verify_installed(self.layout)
        self.assertIn(self.layout.keyring_pass_file, str(caught.exception))

    def test_the_passphrase_permissions_are_corrected(self):
        os.chmod(self.layout.keyring_pass_file, 0o644)
        backup_module._verify_installed(self.layout)
        mode = stat.S_IMODE(os.stat(self.layout.keyring_pass_file).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
