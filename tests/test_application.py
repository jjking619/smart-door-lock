from contextlib import ExitStack, nullcontext
import errno
from pathlib import Path
import subprocess
import unittest
from unittest.mock import ANY, Mock, patch

from application import (
    ROOT, _can_reuse_existing_backend, _lock_service_running,
    main, stop_process, ui_python, wait_ready,
)


class ApplicationTests(unittest.TestCase):
    def test_ui_python_uses_explicit_override(self):
        with patch.dict("application.os.environ", {"DOORLOCK_UI_PYTHON": "/custom/python"}):
            self.assertEqual(ui_python(), "/custom/python")

    def test_ui_python_prefers_isolated_environment(self):
        with patch.dict("application.os.environ", {}, clear=True):
            with patch("application.Path.is_file", return_value=True):
                self.assertEqual(ui_python(), str(ROOT / ".venv-ui" / "bin" / "python"))

    def test_ui_python_falls_back_when_environment_missing(self):
        with patch.dict("application.os.environ", {}, clear=True):
            with patch("application.Path.is_file", return_value=False):
                with patch("application.sys.executable", "/original/python"):
                    self.assertEqual(ui_python(), "/original/python")

    def test_root_points_to_project_directory(self):
        self.assertEqual(ROOT, Path(__file__).resolve().parents[1])

    def test_servo_launcher_does_not_silently_reuse_mock_lockd(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"service":"lockd","driver":{"driver":"mock"}}'
        with patch.dict("os.environ", {"LOCK_DRIVER": "servo"}):
            with patch("application.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "mock, requested servo"):
                    _lock_service_running()

    def test_default_launcher_refuses_old_mock_lockd(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"service":"lockd","driver":{"driver":"mock"}}'
        with patch.dict("os.environ", {}, clear=True):
            with patch("application.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "mock, requested servo"):
                    _lock_service_running()

    def test_servo_launcher_reuses_matching_driver(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"service":"lockd","driver":{"driver":"servo"}}'
        with patch.dict("os.environ", {"LOCK_DRIVER": "servo"}):
            with patch("application.urllib.request.urlopen", return_value=response):
                self.assertTrue(_lock_service_running())

    def test_stop_only_owned_live_process(self):
        process = Mock()
        process.poll.return_value = 0
        stop_process(process)
        process.terminate.assert_not_called()

    def test_stop_escalates_after_timeout(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("vision", 8), 0]
        stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_readiness_rejects_cpu_for_npu_model(self):
        process = Mock()
        process.poll.return_value = None
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"ring":1,"detector":"onnx-cpu"}'
        with patch("application.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "NPU backend"):
                wait_ready(process, "http://localhost:8083", 1, True)

    def test_existing_backend_probe_reports_true_when_health_ok(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"ring":1,"camera":"/dev/v4l/by-id/cam","detector":"rknn"}'
        with patch("application.urllib.request.urlopen", return_value=response):
            self.assertTrue(_can_reuse_existing_backend("http://localhost:8083"))

    def test_existing_backend_probe_rejects_backend_without_camera_frames(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"ring":0,"camera":"/dev/v4l/by-id/cam","detector":"rknn"}'
        with patch("application.urllib.request.urlopen", return_value=response):
            self.assertFalse(_can_reuse_existing_backend("http://localhost:8083"))

    def test_existing_backend_probe_rejects_backend_with_closed_camera(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"ring":10,"camera":null,"detector":"rknn"}'
        with patch("application.urllib.request.urlopen", return_value=response):
            self.assertFalse(_can_reuse_existing_backend("http://localhost:8083"))

    def test_existing_backend_probe_reports_false_on_connection_error(self):
        with patch("application.urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(_can_reuse_existing_backend("http://localhost:8083"))

    def test_reuse_rejects_backend_without_package_detector(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"ring":1,"camera":"/dev/v4l/by-id/cam","detector":"rknn"}'
        with patch("application.urllib.request.urlopen", return_value=response):
            self.assertFalse(_can_reuse_existing_backend("http://localhost:8083", "parcel.rknn"))

    def test_readiness_reports_package_failure(self):
        process = Mock()
        process.poll.return_value = None
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"package_detector_error":"load failed"}'
        with patch("application.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Package detector failed"):
                wait_ready(process, "http://localhost:8083", 1, True, "parcel.rknn")


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict("os.environ", {}, clear=True))
        for name in ("application.os.chdir", "application.signal.signal", "application.stop_process"):
            self.stack.enter_context(patch(name))
        self.stack.enter_context(patch("application.ui_python", return_value="/ui/python"))
        self.lock = self.stack.enter_context(patch("application.startup_lock", return_value=nullcontext()))
        self.ready = self.stack.enter_context(patch("application.wait_ready"))
        self.probe = self.stack.enter_context(patch("socket.socket"))
        self.probe.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, "in use")
        self.health = self.stack.enter_context(patch("application._can_reuse_existing_backend", return_value=False))
        self.recover = self.stack.enter_context(patch("application.reuse_or_stop_backend", return_value=False))
        self.fingerprint_running = self.stack.enter_context(
            patch("application._fingerprint_service_running", return_value=True)
        )
        self.lock_service = self.stack.enter_context(
            patch("application._start_lock_service", return_value=None)
        )
        self.spawn = self.stack.enter_context(patch("application.subprocess.Popen"))
        self.ui = Mock(returncode=0)
        self.ui.poll.return_value = 0

    def test_restarts_old_backend_before_opening_ui(self):
        backend = Mock()
        self.spawn.side_effect = [backend, self.ui]
        events = Mock()
        events.attach_mock(self.recover, "recover")
        events.attach_mock(self.spawn, "spawn")
        self.assertEqual(main(["--no-audio"]), 0)
        self.assertEqual([entry[0] for entry in events.mock_calls], ["recover", "spawn", "spawn"])
        self.lock.assert_called_once_with(8083)
        self.recover.assert_called_once_with(8083, False, recheck=ANY, wait=30.0)
        self.assertEqual(self.spawn.call_args_list[0].args[0][-1], "--backend-only")
        self.assertEqual(self.spawn.call_args_list[1].args[0], ["/ui/python", "-m", "ui.app", "--base",
                                                            "http://127.0.0.1:8083"])
        self.ready.assert_called_once_with(backend, "http://127.0.0.1:8083", 20, True,
                                          "models/parcel-yolov5n-rk3576-fp.rknn")

    def test_ready_standalone_backend_is_reused_without_another_capture_process(self):
        self.health.return_value = True
        self.recover.return_value = True
        self.spawn.return_value = self.ui
        self.assertEqual(main([]), 0)
        self.recover.assert_called_once_with(8083, True, recheck=ANY, wait=30.0)
        self.health.reset_mock()
        self.recover.call_args.kwargs["recheck"]()
        self.health.assert_called_once_with("http://127.0.0.1:8083", "models/parcel-yolov5n-rk3576-fp.rknn")
        self.spawn.assert_called_once()
        self.ready.assert_not_called()

    def test_starts_fingerprint_sidecar_when_not_already_running(self):
        self.fingerprint_running.return_value = False
        backend, fingerprint, ui = Mock(), Mock(), self.ui
        self.spawn.side_effect = [backend, fingerprint, ui]
        self.assertEqual(main(["--no-audio"]), 0)
        self.assertEqual(self.spawn.call_args_list[1].args[0][-1], "--fingerprint-only")

    def test_starts_lock_service_before_fingerprint_so_first_press_can_unlock(self):
        self.fingerprint_running.return_value = False
        self.lock_service.return_value = Mock()
        backend, fingerprint = Mock(), Mock()
        self.spawn.side_effect = [backend, fingerprint, self.ui]
        events = Mock()
        events.attach_mock(self.lock_service, "lock")
        events.attach_mock(self.spawn, "spawn")

        self.assertEqual(main(["--no-audio"]), 0)

        names = [entry[0] for entry in events.mock_calls]
        self.assertEqual(names, ["spawn", "lock", "spawn", "spawn"])
        self.lock_service.assert_called_once_with(False)
        self.assertEqual(self.spawn.call_args_list[1].args[0][-1], "--fingerprint-only")

    def test_lock_service_can_be_skipped(self):
        self.spawn.return_value = self.ui
        self.assertEqual(main(["--no-audio", "--no-lock"]), 0)
        self.lock_service.assert_called_once_with(True)

    def test_foreign_port_owner_is_not_replaced(self):
        self.recover.side_effect = RuntimeError("unverified process")
        self.assertEqual(main([]), 1)
        self.spawn.assert_not_called()

    def test_ui_only_does_not_inspect_or_replace_backend(self):
        self.spawn.return_value = self.ui
        self.assertEqual(main(["--ui-only"]), 0)
        self.lock.assert_not_called()
        self.probe.assert_not_called()
        self.recover.assert_not_called()
        self.spawn.assert_called_once()


if __name__ == "__main__":
    unittest.main()