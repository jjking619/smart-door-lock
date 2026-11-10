import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from vision.http import create_app


class RecordingsHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        (self.data_dir / "clips" / "20261106").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "recordings" / "continuous" / "20261106").mkdir(parents=True, exist_ok=True)

        (self.data_dir / "clips" / "20261106" / "204251_860.mp4").write_bytes(b"event")
        (self.data_dir / "recordings" / "continuous" / "20261106" / "205000_000.mp4").write_bytes(b"cont")
        (self.data_dir / "events.jsonl").write_text(
            '{"ts":"2026-11-06T20:42:51.860+08:00","type":"person.detected",'
            '"severity":"info","clip":"data/clips/20261106/204251_860.mp4"}\n',
            encoding="utf-8",
        )

        self.svc = SimpleNamespace(
            cfg=SimpleNamespace(data_dir=str(self.data_dir)),
            fps=0.0,
            ring=[],
            preview_clients=0,
            detector=None,
            clips=[],
            recording_state=lambda: {"requested": False, "active": False},
            request_recording=lambda _on: None,
        )
        self.client = TestClient(TestServer(create_app(self.svc)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_lists_both_event_and_continuous_recordings(self):
        with patch("vision.http.TOKEN", ""):
            response = await self.client.get("/recordings?limit=10&type=all")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        kinds = {item["kind"] for item in payload["items"]}
        self.assertEqual(kinds, {"event", "continuous"})

    async def test_filters_by_type(self):
        with patch("vision.http.TOKEN", ""):
            response = await self.client.get("/recordings?type=event")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["items"])
        self.assertTrue(all(item["kind"] == "event" for item in payload["items"]))


if __name__ == "__main__":
    unittest.main()
