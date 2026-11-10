from dataclasses import replace
import fcntl
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import process_manager as manager


class ProcessDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "project"
        self.root.mkdir()
        self.proc = base / "proc"
        self.proc.joinpath("net").mkdir(parents=True)
        self.proc.joinpath("net/tcp").write_text(
            "header\n0: 00000000:1F93 00000000:0000 0A 0 0 0 1000 0 123\n"
        )
        self.proc.joinpath("net/tcp6").write_text("header\n")
        for name, value in (("ROOT", self.root), ("PROC", self.proc)):
            patcher = patch.object(manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def process(self, pid=42, parent=1, script=None, arguments=("--backend-only",), unit="session-1.scope"):
        directory = self.proc / str(pid)
        directory.joinpath("fd").mkdir(parents=True)
        directory.joinpath("exe").symlink_to("/usr/bin/python3.11")
        directory.joinpath("cwd").symlink_to(self.root)
        directory.joinpath("fd/3").symlink_to("socket:[123]")
        directory.joinpath("stat").write_text(
            f"{pid} (python (worker)) " + " ".join(["S", str(parent)] + ["0"] * 17 + ["987"])
        )
        argv = ("python", str(script or self.root / "main.py"), *arguments)
        directory.joinpath("cmdline").write_bytes(b"\0".join(value.encode() for value in argv) + b"\0")
        directory.joinpath("cgroup").write_text(f"0::/user.slice/{unit}\n")
        return directory

    def test_discovers_legacy_backend_by_actual_listening_socket(self):
        self.process()
        backend = manager._find_backend(8083)
        self.assertIsNotNone(backend)
        self.assertEqual((backend.pid, backend.parent, backend.started), (42, 1, "987"))
        self.assertIsNone(backend.unit)

    def test_refuses_foreign_project_on_same_port(self):
        self.process(script=self.root.parent / "other-project/main.py")
        self.assertIsNone(manager._find_backend(8083))

    def test_refuses_desktop_or_other_program_listening_on_port(self):
        self.process(arguments=())
        self.assertIsNone(manager._find_backend(8083))

    def test_does_not_match_another_port(self):
        self.process()
        self.assertIsNone(manager._find_backend(8084))

    def test_does_not_match_established_socket(self):
        self.process()
        self.proc.joinpath("net/tcp").write_text(
            "header\n0: 00000000:1F93 00000000:0000 01 0 0 0 1000 0 123\n"
        )
        self.assertIsNone(manager._find_backend(8083))

    def test_discovers_ipv6_listener(self):
        self.process()
        self.proc.joinpath("net/tcp").write_text("header\n")
        self.proc.joinpath("net/tcp6").write_text(
            "header\n0: 00000000000000000000000000000000:1F93 0:0000 0A 0 0 0 1000 0 123\n"
        )
        self.assertIsNotNone(manager._find_backend(8083))

    def test_ipv6_may_be_disabled(self):
        self.process()
        self.proc.joinpath("net/tcp6").unlink()
        self.assertIsNotNone(manager._find_backend(8083))

    def test_refuses_shared_socket_with_multiple_owners(self):
        self.process(42)
        self.process(43)
        self.assertIsNone(manager._find_backend(8083))

    def test_records_systemd_service(self):
        self.process(unit="visiond.service")
        self.assertEqual(manager._find_backend(8083).unit, "visiond.service")

    def test_only_matches_current_user(self):
        self.process()
        with patch("process_manager.os.getuid", return_value=self.proc.stat().st_uid + 1):
            self.assertIsNone(manager._find_backend(8083))

    def test_unreadable_unrelated_process_does_not_block_verified_backend(self):
        self.process(42)
        self.process(43)
        original = Path.iterdir

        def iterdir(path):
            if path == self.proc / "43/fd":
                raise PermissionError("not dumpable")
            return original(path)

        with patch.object(Path, "iterdir", iterdir):
            self.assertEqual(manager._find_backend(8083).pid, 42)

    def test_startup_lock_keeps_same_file_between_launches(self):
        with manager.startup_lock(8083):
            inode = self.root.joinpath("data/run/startup-8083.lock").stat().st_ino
        with manager.startup_lock(8083):
            self.assertEqual(self.root.joinpath("data/run/startup-8083.lock").stat().st_ino, inode)

    def test_startup_lock_excludes_competing_launch_until_released(self):
        with manager.startup_lock(8083):
            fd = os.open(self.root / "data/run/startup-8083.lock", os.O_RDWR)
            self.addCleanup(os.close, fd)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)


class ExistingBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = manager.Process(
            42, 41, "987", ("python", str(manager.ROOT / "main.py"), "--backend-only"),
            manager.ROOT, None,
        )
        self.parent = manager.Process(
            41, 1, "986", ("python", str(manager.ROOT / "main.py")), manager.ROOT, None,
        )
        self.finder = patch("process_manager._find_backend", return_value=self.backend)
        self.reader = patch("process_manager._read_process", return_value=None)
        self.stopper = patch("process_manager._stop")
        self.finder.start()
        self.read = self.reader.start()
        self.stop = self.stopper.start()
        self.addCleanup(self.finder.stop)
        self.addCleanup(self.reader.stop)
        self.addCleanup(self.stopper.stop)

    def test_reuses_ready_standalone_backend(self):
        self.assertTrue(manager.reuse_or_stop_backend(8083, True))
        self.stop.assert_not_called()

    def test_restarts_unready_standalone_backend(self):
        self.assertFalse(manager.reuse_or_stop_backend(8083, False))
        self.stop.assert_called_once_with(self.backend, 8)

    def test_replaces_previous_desktop_even_when_backend_is_ready(self):
        self.read.return_value = self.parent
        self.assertFalse(manager.reuse_or_stop_backend(8083, True))
        self.assertEqual(self.stop.call_args_list, [call(self.parent, 30), call(self.backend, 8)])

    def test_does_not_stop_unrelated_parent(self):
        self.read.return_value = replace(self.parent, argv=("python", "/elsewhere/main.py"))
        manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_called_once_with(self.backend, 8)

    def test_unreadable_parent_does_not_block_restarting_verified_backend(self):
        self.read.side_effect = PermissionError("/proc/4511/exe")
        self.assertFalse(manager.reuse_or_stop_backend(8083, False))
        self.stop.assert_called_once_with(self.backend, 8)

    def test_unreadable_parent_does_not_block_reusing_ready_backend(self):
        self.read.side_effect = PermissionError("/proc/4511/exe")
        self.assertTrue(manager.reuse_or_stop_backend(8083, True))
        self.stop.assert_not_called()

    def test_unreadable_parent_does_not_bypass_service_protection(self):
        self.read.side_effect = PermissionError("/proc/4511/exe")
        with patch("process_manager._find_backend", return_value=replace(self.backend, unit="visiond.service")):
            with self.assertRaisesRegex(RuntimeError, "service manager"):
                manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_not_called()

    def test_does_not_stop_recycled_parent_pid(self):
        self.read.return_value = replace(self.parent, started="999")
        manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_called_once_with(self.backend, 8)

    def test_reuses_healthy_systemd_service(self):
        with patch("process_manager._find_backend", return_value=replace(self.backend, unit="visiond.service")):
            self.assertTrue(manager.reuse_or_stop_backend(8083, True))
        self.stop.assert_not_called()

    def test_never_kills_unready_systemd_service(self):
        with patch("process_manager._find_backend", return_value=replace(self.backend, unit="visiond.service")):
            with self.assertRaisesRegex(RuntimeError, "service manager"):
                manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_not_called()

    def test_waits_for_reconnecting_systemd_service_then_reuses_it(self):
        recheck = Mock(side_effect=[False, True])
        with patch("process_manager._find_backend", return_value=replace(self.backend, unit="visiond.service")), \
                patch("process_manager.time.sleep"):
            self.assertTrue(manager.reuse_or_stop_backend(8083, False, recheck=recheck, wait=30))
        self.assertEqual(recheck.call_count, 2)
        self.stop.assert_not_called()

    def test_unready_user_service_error_names_user_manager_command(self):
        with patch("process_manager._find_backend", return_value=replace(self.backend, unit="visiond.service")), \
                patch("process_manager.Path.read_text",
                      return_value="0::/user.slice/user-1000.slice/user@1000.service/app.slice/visiond.service"):
            with self.assertRaisesRegex(RuntimeError, "systemctl --user restart visiond.service"):
                manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_not_called()

    def test_unready_system_service_error_names_system_manager_command(self):
        with patch("process_manager._find_backend", return_value=replace(self.backend, unit="visiond.service")), \
                patch("process_manager.Path.read_text", return_value="0::/system.slice/visiond.service"):
            with self.assertRaisesRegex(RuntimeError, "sudo systemctl restart visiond.service"):
                manager.reuse_or_stop_backend(8083, False)

    def test_does_not_kill_systemd_managed_desktop(self):
        self.read.return_value = replace(self.parent, unit="doorlock.service")
        with self.assertRaisesRegex(RuntimeError, "doorlock.service"):
            manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_not_called()

    def test_refuses_unknown_port_owner(self):
        with patch("process_manager._find_backend", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "no process was terminated"):
                manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_not_called()

    def test_reports_denied_inspection_without_attempting_stop(self):
        with patch("process_manager._find_backend", side_effect=PermissionError("blocked")):
            with self.assertRaisesRegex(RuntimeError, "Cannot safely inspect"):
                manager.reuse_or_stop_backend(8083, False)
        self.stop.assert_not_called()


class ProcessTerminationTests(unittest.TestCase):
    def setUp(self):
        self.process = manager.Process(42, 1, "987", ("python", "/project/main.py"), Path("/project"), None)
        self.patches = [
            patch("process_manager.os.pidfd_open", return_value=123),
            patch("process_manager.os.close"),
            patch("process_manager._read_process", return_value=self.process),
            patch("process_manager.signal.pidfd_send_signal"),
            patch("process_manager.select.select", return_value=([123], [], [])),
        ]
        self.open, self.close, self.read, self.send, self.select = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def test_gracefully_stops_verified_process(self):
        manager._stop(self.process, 8)
        self.send.assert_called_once_with(123, signal.SIGTERM)
        self.select.assert_called_once_with([123], [], [], 8)
        self.close.assert_called_once_with(123)

    def test_escalates_only_after_grace_period(self):
        self.select.side_effect = [([], [], []), ([123], [], [])]
        manager._stop(self.process, 8)
        self.assertEqual(self.send.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])

    def test_reports_failure_to_exit_after_kill(self):
        self.select.return_value = ([], [], [])
        with self.assertRaisesRegex(RuntimeError, "did not exit"):
            manager._stop(self.process, 8)
        self.close.assert_called_once_with(123)

    def test_never_signals_recycled_pid(self):
        self.read.return_value = replace(self.process, started="999")
        with self.assertRaisesRegex(RuntimeError, "changed identity"):
            manager._stop(self.process, 8)
        self.send.assert_not_called()
        self.close.assert_called_once_with(123)

    def test_never_signals_changed_command(self):
        self.read.return_value = replace(self.process, argv=("python", "/other.py"))
        with self.assertRaisesRegex(RuntimeError, "changed identity"):
            manager._stop(self.process, 8)
        self.send.assert_not_called()

    def test_tolerates_process_exiting_before_open(self):
        self.open.side_effect = ProcessLookupError()
        manager._stop(self.process, 8)
        self.send.assert_not_called()
        self.close.assert_not_called()

    def test_tolerates_process_exiting_after_open(self):
        self.read.return_value = None
        manager._stop(self.process, 8)
        self.send.assert_not_called()
        self.close.assert_called_once_with(123)

    def test_refuses_unidentifiable_live_process(self):
        self.read.return_value = None
        self.select.return_value = ([], [], [])
        with self.assertRaisesRegex(RuntimeError, "Cannot confirm"):
            manager._stop(self.process, 8)
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
