import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import audio.app as audio_app
import audio.coze_streaming as streaming
from audio.coze_bot import requests_unlock


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
        for text, error in (
            ("Open lock.", "bot timeout after 8.0s"),
            ("Open lock.", "TTS unavailable"),
            ("开锁。", "bot timeout after 8.0s"),
            ("开锁。", "TTS unavailable"),
        ):
            with self.subTest(text=text, error=error):
                self.service._unlock_request = None
                updates = []

                def on_update(event):
                    updates.append(event)
                    self.service._conversation_update(event)

                async def fail_reply(_text):
                    response = await self.client.get("/health")
                    pending = (await response.json())["unlock_request"]
                    self.assertEqual(pending["text"], text)
                    raise streaming.CozeUnavailable(error)

                with mock.patch.object(streaming, "PROLOGUE", ""), \
                     mock.patch.object(streaming, "_StreamingPlayer"), \
                     mock.patch("audio.coze_client.build_async_client"), \
                     mock.patch.object(streaming, "create_session_file_logger",
                                       return_value=(None, None, mock.Mock())):
                    session = streaming.CozeStreamingSession(mock.Mock(), None, None, on_update)
                    session._transcribe_turn = mock.AsyncMock(return_value=text)
                    session._stream_bot_turn = fail_reply
                    with self.assertRaisesRegex(streaming.CozeUnavailable, error):
                        await session.run()

                pending = (await (await self.client.get("/health")).json())["unlock_request"]
                self.assertEqual(pending["text"], text)
                self.assertEqual(sum(e["type"] == "unlock_request" for e in updates), 1)

    async def test_unlock_phrases_support_english_without_substring_matches(self) -> None:
        for text in ("Open lock.", "OPEN THE DOOR", "unlock the door", "Let me in", "请帮我开锁"):
            with self.subTest(text=text):
                self.assertTrue(requests_unlock(text))
        for text in ("hello", "open the window", "the door is locked", "clock", "unlocked"):
            with self.subTest(text=text):
                self.assertFalse(requests_unlock(text))

    async def test_english_decision_uses_english_text_and_speech(self) -> None:
        for decision, unlocked, expected in (
            ("allow", True, audio_app.UNLOCK_REPLY_TEXT_EN["allow"]),
            ("allow", False, audio_app.UNLOCK_REPLY_TEXT_EN["deny"]),
            ("deny", None, audio_app.UNLOCK_REPLY_TEXT_EN["deny"]),
        ):
            req = self.service.raise_unlock_request("Open lock.")
            with mock.patch.object(self.service, "_schedule_reply") as speak:
                self.service.resolve_unlock_request(req["id"], decision, unlocked=unlocked)
                self.assertEqual(self.service._conversation[-1]["text"], expected)
                speak.assert_called_once_with(expected)

    async def test_timeout_fallback_matches_display_and_visitor_language(self) -> None:
        for visitor, expected in (("Open lock.", "Sorry,"), ("请开门", "您好")):
            with self.subTest(visitor=visitor):
                self.service.bot = mock.Mock()
                self.service._conversation.clear()

                async def fail():
                    self.service._conversation_update({"type": "visitor_final", "text": visitor})
                    self.service._conversation_update({"type": "assistant_delta", "text": "Partial reply"})
                    raise streaming.CozeUnavailable("bot timeout after 8.0s")

                with mock.patch.object(streaming, "CozeStreamingSession") as factory, \
                     mock.patch.object(audio_app, "_play_fallback", new_callable=mock.AsyncMock) as speak:
                    factory.return_value.run = fail
                    await self.service._run_streaming_once()
                    reply = self.service._conversation[-1]
                    self.assertFalse(reply["partial"])
                    self.assertTrue(reply["text"].startswith(expected))
                    speak.assert_awaited_once_with(self.service.out_dev, reply["text"])
                    self.assertEqual(self.service._conversation_status, "云端对话失败，已结束")

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
