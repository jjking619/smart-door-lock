import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import audio.app as audio_app
import audio.coze_streaming as streaming


class UnlockRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.events = Path(self.tmp.name) / "events.jsonl"
        patcher = mock.patch.object(audio_app, "EVENTS_PATH", self.events)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.service = audio_app.TalkService()
        self.service._schedule_reply = lambda _text: None
        self.client = TestClient(TestServer(audio_app._build_http_app(self.service)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_streaming_update_raises_pending_request_in_health(self) -> None:
        self.service._conversation_update({"type": "unlock_request", "text": "请帮我开门"})
        data = await (await self.client.get("/health")).json()
        req = data["unlock_request"]
        self.assertEqual(req["text"], "请帮我开门")
        self.assertGreater(req["expires_in"], 0)
        self.assertNotIn("expires", req)

    async def test_repeated_request_keeps_same_id(self) -> None:
        first = self.service.raise_unlock_request("开门")
        second = self.service.raise_unlock_request("快开门")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["text"], "快开门")

    async def test_allow_decision_clears_request_and_logs_event(self) -> None:
        req = self.service.raise_unlock_request("让我进去")
        resp = await self.client.post("/unlock_request",
                                      json={"id": req["id"], "decision": "allow", "unlocked": True})
        self.assertEqual(resp.status, 200)
        self.assertIsNone((await (await self.client.get("/health")).json())["unlock_request"])
        line = self.events.read_text(encoding="utf-8").strip()
        self.assertIn('"type": "unlock.request"', line)
        self.assertIn('"decision": "allow"', line)
        self.assertIn('"unlocked": true', line)
        self.assertEqual(self.service._conversation[-1]["text"], audio_app.UNLOCK_REPLY_TEXT["allow"])

    async def test_failed_unlock_tells_visitor_door_not_opened(self) -> None:
        req = self.service.raise_unlock_request("开门")
        await self.client.post("/unlock_request",
                               json={"id": req["id"], "decision": "allow", "unlocked": False,
                                     "reason": "锁体未响应"})
        self.assertEqual(self.service._conversation[-1]["text"], audio_app.UNLOCK_REPLY_TEXT["deny"])
        self.assertIn("锁体未响应", self.events.read_text(encoding="utf-8"))

    async def test_rejects_bad_or_stale_decisions(self) -> None:
        req = self.service.raise_unlock_request("开门")
        bad = await self.client.post("/unlock_request", json={"id": req["id"], "decision": "maybe"})
        self.assertEqual(bad.status, 400)
        stale = await self.client.post("/unlock_request", json={"id": "nope", "decision": "deny"})
        self.assertEqual(stale.status, 404)
        self.assertIsNotNone(self.service.current_unlock_request())

    async def test_expired_request_is_dropped_and_logged(self) -> None:
        req = self.service.raise_unlock_request("开门")
        req["expires"] = time.time() - 1
        self.assertIsNone(self.service.current_unlock_request())
        self.assertIn('"decision": "timeout"', self.events.read_text(encoding="utf-8"))

    async def test_realtime_event_raises_request(self) -> None:
        self.service._realtime_event({"type": "visitor.request_unlock", "text": "开锁"})
        self.assertEqual(self.service.current_unlock_request()["text"], "开锁")

    async def test_streaming_pushes_request_before_bot_timeout_or_tts_failure(self) -> None:
        for error in ("bot timeout after 8.0s", "TTS unavailable"):
            with self.subTest(error=error):
                self.service._unlock_request = None
                updates = []

                def on_update(event):
                    updates.append(event)
                    self.service._conversation_update(event)

                async def fail_reply(_text):
                    response = await self.client.get("/health")
                    pending = (await response.json())["unlock_request"]
                    self.assertEqual(pending["text"], "开锁。")
                    raise streaming.CozeUnavailable(error)

                with mock.patch.object(streaming, "PROLOGUE", ""), \
                     mock.patch.object(streaming, "_StreamingPlayer"), \
                     mock.patch("audio.coze_client.build_async_client"), \
                     mock.patch.object(streaming, "create_session_file_logger",
                                       return_value=(None, None, mock.Mock())):
                    session = streaming.CozeStreamingSession(mock.Mock(), None, None, on_update)
                    session._transcribe_turn = mock.AsyncMock(return_value="开锁。")
                    session._stream_bot_turn = fail_reply
                    with self.assertRaisesRegex(streaming.CozeUnavailable, error):
                        await session.run()

                pending = (await (await self.client.get("/health")).json())["unlock_request"]
                self.assertEqual(pending["text"], "开锁。")
                self.assertEqual(sum(e["type"] == "unlock_request" for e in updates), 1)

    async def test_streaming_success_pushes_once_and_normal_speech_does_not_push(self) -> None:
        for text, expected in (("请帮我开门", 1), ("你好", 0)):
            with self.subTest(text=text):
                self.service._unlock_request = None
                updates = []

                def on_update(event):
                    updates.append(event)
                    self.service._conversation_update(event)

                with mock.patch.object(streaming, "PROLOGUE", ""), \
                     mock.patch.object(streaming, "_StreamingPlayer"), \
                     mock.patch("audio.coze_client.build_async_client"), \
                     mock.patch.object(streaming, "create_session_file_logger",
                                       return_value=(None, None, mock.Mock())):
                    session = streaming.CozeStreamingSession(mock.Mock(), None, None, on_update)
                    session._transcribe_turn = mock.AsyncMock(side_effect=[text, ""])
                    session._stream_bot_turn = mock.AsyncMock(
                        return_value=mock.Mock(text="请稍等", needs_local_auth=bool(expected)),
                    )
                    await session.run()

                self.assertEqual(sum(e["type"] == "unlock_request" for e in updates), expected)
                self.assertEqual(self.service.current_unlock_request() is not None, bool(expected))


if __name__ == "__main__":
    unittest.main()
