"""Unit tests for the Cloud-Chrome runtime's pure logic.

These tests cover the modules that have no side effects on the machine: the
configuration resolution, the path validation, the logging and redaction, the
locks, the PID-file handling, the Chrome launch policy, the VNC argument
vector and the network model.

They run with the standard library only:

    python3 -m unittest discover -s tests/unit -t .
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))

from cloudchrome import chrome as chrome_policy  # noqa: E402
from cloudchrome import config as config_module  # noqa: E402
from cloudchrome import keyring as keyring_policy  # noqa: E402
from cloudchrome import logutil  # noqa: E402
from cloudchrome import paths as paths_module  # noqa: E402
from cloudchrome import procs  # noqa: E402
from cloudchrome import vnc as vnc_policy  # noqa: E402
from cloudchrome import web as web_policy  # noqa: E402
from cloudchrome.errors import (  # noqa: E402
    BusyError,
    InvalidStateError,
    NotInitializedError,
    UnsupportedError,
)
from cloudchrome.locking import FileLock, is_locked  # noqa: E402
from cloudchrome.supervisor import Component, COMPONENT_ORDER, Supervisor  # noqa: E402


class TempStateCase(unittest.TestCase):
    """A test case with a private temporary state directory."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="cloudchrome-test.")
        self.persist = os.path.join(self.work, ".cloudchrome")
        self.addCleanup(shutil.rmtree, self.work, True)

    def layout(self, **overrides):
        values = {
            "CLOUDCHROME_PERSIST_DIR": self.persist,
            "CLOUDCHROME_RUNTIME_DIR": os.path.join(self.persist, "run"),
        }
        values.update(overrides)
        config = config_module.Config(dict(config_module.DEFAULTS, **values))
        return paths_module.Layout(config)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = config_module.load_config(environ={})
        self.assertEqual(config.display, ":99")
        self.assertEqual(config.http_port, 6080)
        self.assertEqual(config.vnc_port, 5900)

    def test_environment_overrides_the_file(self):
        config = config_module.load_config(
            environ={"CLOUDCHROME_DISPLAY": ":77"}, overrides=None
        )
        self.assertEqual(config.display, ":77")

    def test_explicit_override_wins(self):
        config = config_module.load_config(
            environ={"CLOUDCHROME_DISPLAY": ":77"},
            overrides={"CLOUDCHROME_DISPLAY": ":88"},
        )
        self.assertEqual(config.display, ":88")

    def test_persist_dir_follows_the_checkout(self):
        previous = os.environ.get("CLOUDCHROME_HOME")
        os.environ["CLOUDCHROME_HOME"] = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        try:
            config = config_module.load_config(environ=dict(os.environ))
            self.assertTrue(config.persist_dir.endswith("/.cloudchrome"))
        finally:
            if previous is None:
                os.environ.pop("CLOUDCHROME_HOME", None)
            else:
                os.environ["CLOUDCHROME_HOME"] = previous

    def test_config_file_parsing(self):
        import tempfile as _tempfile

        handle, path = _tempfile.mkstemp()
        os.close(handle)
        try:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("# a comment\n")
                stream.write("CLOUDCHROME_DISPLAY=':42'\n")
                stream.write("CLOUDCHROME_DEPTH=24\n")
                stream.write("not a key value line\n")
            values = config_module.parse_config_file(path)
            self.assertEqual(values["CLOUDCHROME_DISPLAY"], ":42")
            self.assertEqual(values["CLOUDCHROME_DEPTH"], "24")
            self.assertNotIn("not a key value line", values)
        finally:
            os.unlink(path)

    def test_integer_values(self):
        config = config_module.Config(
            dict(config_module.DEFAULTS, CLOUDCHROME_HTTP_PORT="6080")
        )
        self.assertEqual(config.http_port, 6080)
        self.assertIsInstance(config.http_port, int)


class PathTest(TempStateCase):
    def test_ensure_dir_creates_private_directories(self):
        target = os.path.join(self.work, "private")
        paths_module.ensure_dir(target)
        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(mode, 0o700)

    def test_ensure_dir_refuses_a_loose_directory(self):
        target = os.path.join(self.work, "loose")
        os.makedirs(target)
        os.chmod(target, 0o755)
        with self.assertRaises(InvalidStateError):
            paths_module.ensure_dir(target)

    def test_ensure_dir_can_tighten_a_loose_directory(self):
        # 'init' establishes the state, so it restores the permissions of a
        # directory it owns. The runtime commands do not.
        target = os.path.join(self.work, "loose")
        os.makedirs(target)
        os.chmod(target, 0o755)
        paths_module.ensure_dir(target, tighten=True)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)

    def test_initialize_tightens_the_state_directory(self):
        layout = self.layout()
        os.makedirs(layout.persist_dir, mode=0o755)
        os.chmod(layout.persist_dir, 0o755)
        layout.initialize()
        self.assertEqual(stat.S_IMODE(os.stat(layout.persist_dir).st_mode), 0o700)

    def test_require_initialized_refuses_a_loose_state_directory(self):
        layout = self.layout()
        layout.initialize()
        os.chmod(layout.persist_dir, 0o755)
        with self.assertRaises(InvalidStateError):
            layout.require_initialized()

    def test_ensure_dir_refuses_a_symlink(self):
        real = os.path.join(self.work, "real")
        os.makedirs(real)
        link = os.path.join(self.work, "link")
        os.symlink(real, link)
        with self.assertRaises(InvalidStateError):
            paths_module.ensure_dir(link)

    def test_assert_safe_file_rejects_a_loose_file(self):
        target = os.path.join(self.work, "secret")
        with open(target, "w", encoding="utf-8") as stream:
            stream.write("x")
        os.chmod(target, 0o644)
        with self.assertRaises(InvalidStateError):
            paths_module.assert_safe_file(target)

    def test_assert_safe_file_rejects_a_missing_file(self):
        with self.assertRaises(NotInitializedError):
            paths_module.assert_safe_file(os.path.join(self.work, "absent"))

    def test_write_private_file_sets_the_mode(self):
        target = os.path.join(self.work, "written")
        paths_module.write_private_file(target, "secret")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_write_private_file_replaces_atomically(self):
        target = os.path.join(self.work, "written")
        paths_module.write_private_file(target, "one")
        paths_module.write_private_file(target, "two")
        with open(target, "r", encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "two")
        # No temporary file is left behind.
        leftovers = [n for n in os.listdir(self.work) if n.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_require_initialized_reports_a_missing_state(self):
        layout = self.layout()
        with self.assertRaises(NotInitializedError):
            layout.require_initialized()

    def test_layout_paths(self):
        layout = self.layout()
        self.assertTrue(layout.chrome_dir.startswith(self.persist))
        self.assertTrue(layout.keyring_pass_file.startswith(self.persist))
        self.assertTrue(layout.vnc_pass_file.startswith(self.persist))
        self.assertTrue(layout.backup_dir.startswith(self.persist))
        self.assertTrue(layout.supervisor_pid_file.startswith(self.persist))


class LoggingTest(TempStateCase):
    def test_secrets_are_redacted(self):
        logutil.clear_secrets()
        logutil.register_secret("super-secret-value")
        self.assertNotIn("super-secret-value", logutil.redact("x super-secret-value y"))

    def test_short_values_are_not_registered(self):
        logutil.clear_secrets()
        logutil.register_secret("ab")
        self.assertIn("ab", logutil.redact("ab"))

    def test_append_creates_the_file(self):
        target = os.path.join(self.work, "log.txt")
        logutil.append(target, "hello")
        self.assertTrue(os.path.exists(target))
        with open(target, "r", encoding="utf-8") as stream:
            self.assertIn("hello", stream.read())

    def test_rotation_keeps_the_generations(self):
        target = os.path.join(self.work, "log.txt")
        for index in range(50):
            logutil.append(target, "line %d" % index, max_bytes=200, keep=2)
        self.assertTrue(os.path.exists(target + ".1"))
        self.assertFalse(os.path.exists(target + ".3"))

    def test_read_tail_returns_the_last_lines(self):
        target = os.path.join(self.work, "log.txt")
        for index in range(10):
            logutil.append(target, "line %d" % index)
        tail = logutil.read_tail(target, 3)
        self.assertEqual(len(tail), 3)
        self.assertIn("line 9", tail[-1])


class LockTest(TempStateCase):
    def test_lock_is_exclusive(self):
        path = os.path.join(self.work, "lock")
        first = FileLock(path, timeout=0)
        first.acquire()
        try:
            self.assertTrue(is_locked(path))
            with self.assertRaises(BusyError):
                FileLock(path, timeout=0.1).acquire()
        finally:
            first.release()
        self.assertFalse(is_locked(path))

    def test_lock_is_released_after_use(self):
        path = os.path.join(self.work, "lock")
        with FileLock(path, timeout=0):
            pass
        second = FileLock(path, timeout=0)
        second.acquire()
        second.release()


class ProcessTest(TempStateCase):
    """The process tests need /proc, so they only run on Linux.

    They run in CI, which is Linux, and in the Codespace, which is Linux.
    """

    @unittest.skipUnless(
        os.path.isdir("/proc"), "the process tests need /proc (Linux only)"
    )
    def test_pid_file_round_trip(self):
        path = os.path.join(self.work, "pid")
        procs.write_pid_file(path, os.getpid(), "test")
        record = procs.read_pid_file(path)
        self.assertIsNotNone(record)
        self.assertEqual(record["pid"], os.getpid())
        self.assertTrue(procs.pid_matches(record["pid"], record["start_time"]))

    @unittest.skipUnless(
        os.path.isdir("/proc"), "the process tests need /proc (Linux only)"
    )
    def test_a_stale_pid_does_not_match(self):
        path = os.path.join(self.work, "pid")
        procs.write_pid_file(path, os.getpid(), "test")
        record = procs.read_pid_file(path)
        self.assertFalse(procs.pid_matches(record["pid"], record["start_time"] + 1))

    def test_a_missing_pid_file_is_not_an_error(self):
        self.assertIsNone(procs.read_pid_file(os.path.join(self.work, "absent")))

    def test_a_malformed_pid_file_is_not_an_error(self):
        path = os.path.join(self.work, "pid")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("not a pid file\n")
        self.assertIsNone(procs.read_pid_file(path))

    def test_a_dead_pid_does_not_match(self):
        self.assertFalse(procs.pid_matches(999999, 1))

    @unittest.skipUnless(
        os.path.isdir("/proc"), "the process tests need /proc (Linux only)"
    )
    def test_terminate_leaves_nothing_running(self):
        import subprocess
        import sys

        process = subprocess.Popen(
            [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"],
            start_new_session=True,
        )
        start = procs.proc_start_time(process.pid)
        self.assertTrue(procs.terminate(process.pid, start, timeout=1))
        process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(
            [member for member in procs.process_group_members(process.pid) if member[1] != "Z"],
            [],
        )

    @unittest.skipUnless(
        os.path.isdir("/proc"), "the process tests need /proc (Linux only)"
    )
    def test_terminate_refuses_a_recycled_pid(self):
        # The recorded start time does not match, so nothing is signalled.
        self.assertTrue(procs.terminate(os.getpid(), 1, timeout=1))
        self.assertTrue(procs.pid_alive(os.getpid()))

    @unittest.skipUnless(os.path.isdir("/proc"), "the process tests need /proc (Linux only)")
    def test_terminate_requires_owned_process_group(self):
        import subprocess
        import sys

        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            start = procs.proc_start_time(process.pid)
            self.assertFalse(procs.terminate(process.pid, start, timeout=0.1))
            self.assertTrue(procs.pid_alive(process.pid))
        finally:
            process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(os.path.isdir("/proc"), "the process tests need /proc (Linux only)")
    def test_terminate_refuses_a_reused_group_after_leader_exit(self):
        import subprocess
        import sys
        import time

        process = subprocess.Popen(
            [sys.executable, "-c", "import os,time; pid=os.fork(); time.sleep(30) if pid == 0 else time.sleep(30)"],
            start_new_session=True,
        )
        start = procs.proc_start_time(process.pid)
        deadline = time.monotonic() + 3
        child = None
        while time.monotonic() < deadline:
            members = procs.process_group_members(process.pid)
            child = next((pid for pid, state, _ticks in members if pid != process.pid and state != "Z"), None)
            if child:
                break
            time.sleep(0.02)
        self.assertIsNotNone(child, "the test child was not started")
        os.kill(process.pid, 15)
        process.wait(timeout=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and procs.proc_identity(process.pid) is not None:
            time.sleep(0.02)
        leader_identity = procs.proc_identity(process.pid)
        if leader_identity is None:
            self.assertTrue(procs.terminate(process.pid, start, timeout=0.1))
            self.assertTrue(procs.pid_alive(child))
        else:
            # A zombie leader still pins its PGID. The owned group may be
            # signalled safely until its descendants exit.
            self.assertTrue(procs.terminate(process.pid, start, timeout=0.1))

    @unittest.skipUnless(
        os.path.isdir("/proc"), "the process tests need /proc (Linux only)"
    )
    def test_read_cmdline(self):
        argv = procs.read_cmdline(os.getpid())
        self.assertTrue(argv)

    def test_stop_all_reports_failed_component_and_continues_reverse_order(self):
        config = config_module.Config(dict(config_module.DEFAULTS))
        layout = self.layout()
        supervisor = Supervisor(config, layout)
        calls = []

        class FakeComponent:
            process = None
            start_time = 123
            log_handle = None
            pid = 4321

            def __init__(self, name, result):
                self.name = name
                self.result = result

            def alive(self):
                return True

            def stop(self, _timeout):
                calls.append(self.name)
                return self.result

        supervisor.components = {
            "xvnc": FakeComponent("xvnc", True),
            "dbus": FakeComponent("dbus", False),
            "chrome": FakeComponent("chrome", True),
        }
        original_members = procs.process_group_members
        procs.process_group_members = lambda _pgid: [(4321, "S", 123)]
        self.addCleanup(setattr, procs, "process_group_members", original_members)

        self.assertFalse(supervisor.stop_all())
        self.assertEqual(calls, ["chrome", "dbus", "xvnc"])
        self.assertEqual(supervisor.state, "failed")
        self.assertIn("dbus", supervisor.error)
        self.assertEqual(supervisor.components, {})


class ChromePolicyTest(TempStateCase):
    def setUp(self):
        super().setUp()
        self.config = config_module.Config(dict(config_module.DEFAULTS))
        self.layout_obj = self.layout()

    def test_the_vector_is_accepted(self):
        arguments = chrome_policy.build_arguments(self.config, self.layout_obj)
        self.assertTrue(chrome_policy.assert_safe_arguments(arguments))

    def test_the_vector_uses_the_persistent_profile(self):
        arguments = chrome_policy.build_arguments(self.config, self.layout_obj)
        self.assertIn(
            "--user-data-dir=%s" % self.layout_obj.chrome_dir, arguments
        )

    def test_the_vector_selects_libsecret(self):
        arguments = chrome_policy.build_arguments(self.config, self.layout_obj)
        self.assertIn("--password-store=gnome-libsecret", arguments)

    def test_the_vector_has_no_forbidden_flag(self):
        arguments = chrome_policy.build_arguments(self.config, self.layout_obj)
        for argument in arguments:
            self.assertNotIn(argument.split("=", 1)[0], chrome_policy.FORBIDDEN_FLAGS)

    def test_every_forbidden_flag_is_refused(self):
        for flag in chrome_policy.FORBIDDEN_FLAGS:
            with self.assertRaises(InvalidStateError):
                chrome_policy.assert_safe_arguments(["--user-data-dir=/tmp", flag])

    def test_a_forbidden_flag_with_a_value_is_refused(self):
        with self.assertRaises(InvalidStateError):
            chrome_policy.assert_safe_arguments(
                ["--user-data-dir=/tmp", "--remote-debugging-port=9222"]
            )

    def test_a_plaintext_password_store_is_refused(self):
        with self.assertRaises(InvalidStateError):
            chrome_policy.assert_safe_arguments(
                ["--user-data-dir=/tmp", "--password-store=basic"]
            )

    def test_a_missing_password_store_is_refused(self):
        with self.assertRaises(InvalidStateError):
            chrome_policy.assert_safe_arguments(["--user-data-dir=/tmp"])

    def test_the_secret_service_must_be_usable(self):
        with self.assertRaises(UnsupportedError):
            chrome_policy.assert_secret_service_available(None)
        with self.assertRaises(UnsupportedError):
            chrome_policy.assert_secret_service_available({"stored": False})
        self.assertTrue(
            chrome_policy.assert_secret_service_available(
                {"stored": True, "retrieved": True}
            )
        )


class VncTest(TempStateCase):
    def test_the_argument_vector_restricts_the_rfb_port(self):
        argv = vnc_policy.vnc_arguments(":99", "1440x900", 24, 5900, "/tmp/x")
        self.assertIn("-localhost", argv)
        self.assertIn("VncAuth", argv)
        self.assertIn("-rfbauth", argv)
        self.assertIn("5900", argv)

    def test_the_argument_vector_has_no_bogus_option(self):
        argv = vnc_policy.vnc_arguments(":99", "1440x900", 24, 5900, "/tmp/x")
        self.assertNotIn("-NeverShared=0", argv)

    def test_password_file_status_of_a_missing_file(self):
        status = vnc_policy.password_file_status("/nonexistent/path")
        self.assertFalse(status["present"])

    def test_the_generated_password_is_long_enough(self):
        password = vnc_policy._generate_password()
        self.assertGreaterEqual(len(password), 8)


class KeyringTest(TempStateCase):
    def test_the_daemon_arguments_have_no_password(self):
        argv = keyring_policy.keyring_arguments()
        self.assertIn("--unlock", argv)
        self.assertIn("--components=secrets", argv)
        for argument in argv:
            self.assertNotIn("pass", argument.lower())

    def test_the_passphrase_file_is_private(self):
        path = os.path.join(self.work, "keyring.pass")
        keyring_policy.create_passphrase_file(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_the_passphrase_is_not_regenerated(self):
        path = os.path.join(self.work, "keyring.pass")
        keyring_policy.create_passphrase_file(path)
        with open(path, "r", encoding="utf-8") as stream:
            first = stream.read()
        keyring_policy.create_passphrase_file(path)
        with open(path, "r", encoding="utf-8") as stream:
            self.assertEqual(stream.read(), first)

    def test_the_dbus_arguments_use_a_private_socket(self):
        argv = keyring_policy.dbus_arguments("/tmp/x.sock")
        self.assertIn("--session", argv)
        self.assertTrue(any(a.startswith("--address=unix:path=") for a in argv))


class NetworkTest(TempStateCase):
    def test_the_novnc_bridge_targets_the_loopback_rfb_port(self):
        argv = web_policy.websockify_arguments(6080, 5900, "/usr/local/share/novnc")
        self.assertTrue(argv[-1].startswith("127.0.0.1:"))
        self.assertIn("--web=/usr/local/share/novnc", argv)

    def test_the_rfb_check_reports_a_non_listening_port(self):
        result = web_policy.check_rfb_is_loopback_only(5900)
        self.assertIn("listening", result)


if __name__ == "__main__":
    unittest.main()
