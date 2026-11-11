import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from vision.app import VisionService
from vision.config import Config
from vision.http import create_app


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _service(tmp: str, lease: float = 0.4, enabled: bool = True) -> VisionService:
    cfg = Config()
    cfg.source = "test"
    cfg.width, cfg.height, cfg.fps = 320, 180, 30
    cfg.model_path = "models/__missing__.rknn"
    cfg.package_model_path = ""
    cfg.data_dir = tmp
    cfg.mqtt_host = ""
    cfg.ui_lease_s = lease
    cfg.pause_without_ui = enabled
    return VisionService(cfg)


class PauseLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = _service(self.tmp.name)
        self.svc._ui_grace_until = 0.0

    def tearDown(self):
        self.svc.bus.close()
        self.tmp.cleanup()

    def test_pauses_without_any_client(self):
        self.assertTrue(self.svc.should_pause())

    def test_heartbeat_keeps_running_until_lease_expires(self):
        self.svc.ui_heartbeat("ui-1")
        now = time.monotonic()
        self.assertFalse(self.svc.should_pause(now))
        self.assertTrue(self.svc.should_pause(now + self.svc.cfg.ui_lease_s + 0.1))

    def test_release_pauses_immediately_but_only_for_that_client(self):
        self.svc.ui_heartbeat("old-window")
        self.svc.ui_heartbeat("new-window")
        self.svc.ui_heartbeat("old-window", active=False)
        self.assertFalse(self.svc.should_pause())
        self.svc.ui_heartbeat("new-window", active=False)
        self.assertTrue(self.svc.should_pause())

    def test_preview_recording_and_pending_clips_keep_running(self):
        self.svc.preview_clients = 1
        self.assertFalse(self.svc.should_pause())
        self.svc.preview_clients = 0
        self.svc.request_recording(True)
        self.assertFalse(self.svc.should_pause())
        self.svc.request_recording(False)
        self.svc.clips = [Mock()]
        self.assertFalse(self.svc.should_pause())

    def test_startup_grace_period(self):
        self.svc._ui_grace_until = time.monotonic() + 60
        self.assertFalse(self.svc.should_pause())

    def test_disabled_never_pauses(self):
        self.svc.cfg.pause_without_ui = False
        self.assertFalse(self.svc.should_pause())


class PauseLoopTests(unittest.TestCase):
    def test_loop_releases_camera_and_resumes_on_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp)
            worker = threading.Thread(target=svc._loop, daemon=True)
            worker.start()
            try:
                self.assertTrue(_wait_for(lambda: len(svc.ring) > 0), "capture never started")
                self.assertTrue(_wait_for(lambda: svc.paused), "never paused after grace period")
                self.assertIsNone(svc.camera_device)
                self.assertEqual(len(svc.ring), 0)
                self.assertEqual(svc.fps, 0.0)

                svc.ui_heartbeat("ui-1")
                self.assertTrue(_wait_for(lambda: not svc.paused and len(svc.ring) > 0), "did not resume")
                self.assertEqual(svc.camera_device, "test")
            finally:
                svc.stop()
                worker.join(timeout=5)
                svc.bus.close()
            self.assertFalse(worker.is_alive())


class HeartbeatHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = _service(self.tmp.name)
        self.client = TestClient(TestServer(create_app(self.svc)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.svc.bus.close()
        self.tmp.cleanup()

    async def test_heartbeat_registers_and_releases_client(self):
        with patch("vision.http.TOKEN", ""):
            response = await self.client.post("/ui/heartbeat", json={"client": "ui-1"})
            self.assertEqual(response.status, 200)
            self.assertEqual((await (await self.client.get("/health")).json())["ui_clients"], 1)
            response = await self.client.post("/ui/heartbeat", json={"client": "ui-1", "active": False})
            self.assertEqual(response.status, 200)
            self.assertEqual((await (await self.client.get("/health")).json())["ui_clients"], 0)

    async def test_heartbeat_rejects_bad_payload(self):
        with patch("vision.http.TOKEN", ""):
            for body in ({}, {"client": ""}, {"client": "x" * 65}, {"client": "ui", "active": "no"}):
                response = await self.client.post("/ui/heartbeat", json=body)
                self.assertEqual(response.status, 400, body)

    async def test_heartbeat_requires_token(self):
        with patch("vision.http.TOKEN", "secret"):
            response = await self.client.post("/ui/heartbeat", json={"client": "ui-1"})
        self.assertEqual(response.status, 401)

    async def test_paused_health_and_snapshot(self):
        self.svc.paused = True
        health = await (await self.client.get("/health")).json()
        self.assertTrue(health["paused"])
        with patch("vision.http.TOKEN", ""):
            response = await self.client.get("/snapshot.jpg")
        self.assertEqual(response.status, 503)


if __name__ == "__main__":
    unittest.main()
