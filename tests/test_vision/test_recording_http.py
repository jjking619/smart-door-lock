import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from vision.http import create_app


class RecordingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.svc = SimpleNamespace(
            cfg=SimpleNamespace(data_dir="data"),
            camera_device="/dev/v4l/by-id/cam",
            fps=0.0,
            ring=[],
            preview_clients=0,
            detector=None,
            detector_error=None,
            package_detector=None,
            package_detector_error="load failed",
            clips=[],
            recording_state=lambda: {"requested": True, "active": False},
            request_recording=Mock(),
        )
        self.client = TestClient(TestServer(create_app(self.svc)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_health_reports_package_model_failure(self):
        self.svc.cfg.package_model_path = "missing.rknn"
        response = await self.client.get("/health")
        health = await response.json()
        self.assertTrue(health["package_detector_enabled"])
        self.assertIsNone(health["package_detector"])
        self.assertEqual(health["package_detector_error"], "load failed")

    async def test_valid_request_updates_requested_state(self):
        with patch("vision.http.TOKEN", ""):
            response = await self.client.post("/recording", json={"on": True})
        self.assertEqual(response.status, 200)
        self.svc.request_recording.assert_called_once_with(True)
        self.assertEqual((await response.json())["recording"]["requested"], True)

    async def test_non_boolean_request_is_rejected(self):
        with patch("vision.http.TOKEN", ""):
            response = await self.client.post("/recording", json={"on": "yes"})
        self.assertEqual(response.status, 400)
        self.svc.request_recording.assert_not_called()

    async def test_events_endpoint_returns_latest_up_to_1000(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(f'{{"index": {i}}}\n' for i in range(1200)))
            self.svc.cfg.data_dir = tmp
            with patch("vision.http.TOKEN", ""):
                response = await self.client.get("/events?limit=1000")
            data = await response.json()
        self.assertEqual(len(data["items"]), 1000)
        self.assertEqual(data["items"][0]["index"], 1199)
        self.assertEqual(data["items"][-1]["index"], 200)


if __name__ == "__main__":
    unittest.main()