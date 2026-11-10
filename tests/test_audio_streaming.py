import asyncio
from types import SimpleNamespace

from cozepy import ChatEventType

from audio.coze_bot import CozeBotClient
from audio.coze_client import CozeUnavailable
from audio.coze_streaming import CozeStreamingSession, _TextChunker, _offer


def test_text_chunker_splits_on_punctuation_and_flushes_tail():
    chunker = _TextChunker()

    chunks = chunker.feed("你好。后面还没结束")

    assert chunks == ["你好。"]
    assert chunker.flush() == ["后面还没结束"]


def test_text_chunker_caps_long_unpunctuated_chunks():
    chunker = _TextChunker(max_chars=32)

    chunks = chunker.feed("中" * 40)

    assert chunks == ["中" * 32]
    assert chunker.flush() == ["中" * 8]


def test_offer_drops_oldest_audio_when_queue_is_full():
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)

    _offer(queue, b"a")
    _offer(queue, b"b")
    _offer(queue, b"c")

    assert queue.get_nowait() == b"b"
    assert queue.get_nowait() == b"c"


def test_bot_chat_forwards_text_deltas_to_stream_callback():
    async def run_test():
        async def event_stream():
            for content in ("你好", "，欢迎。"):
                yield SimpleNamespace(
                    event=ChatEventType.CONVERSATION_MESSAGE_DELTA,
                    message=SimpleNamespace(role="assistant", content_type=None, content=content),
                )
            yield SimpleNamespace(event=ChatEventType.CONVERSATION_CHAT_COMPLETED, chat=None)

        class FakeChat:
            def stream(self, **_kwargs):
                return event_stream()

        bot = object.__new__(CozeBotClient)
        bot._bot_id = "1234567890123456789"
        bot._user_id = "test-user"
        bot._timeout = 2
        bot._coze = SimpleNamespace(chat=FakeChat())
        bot._history = []
        bot._fail_streak = 0
        bot._open_until = 0.0
        deltas: list[str] = []

        async def on_delta(value: str) -> None:
            deltas.append(value)

        reply = await bot.ask("你好", on_delta=on_delta)

        assert deltas == ["你好", "，欢迎。"]
        assert reply.text == "你好，欢迎。"
        assert bot._history[-1].content == reply.text

    asyncio.run(run_test())


def test_websocket_error_interrupts_wait_and_keeps_error_code():
    async def run_test():
        class FakeWebsocket:
            async def wait(self, _events=None):
                await asyncio.Event().wait()

        failed = asyncio.Event()
        errors: list[str] = []

        async def report_error():
            await asyncio.sleep(0.01)
            errors.append("code: 4028")
            failed.set()

        error_task = asyncio.create_task(report_error())
        try:
            await CozeStreamingSession._wait_for_ws(
                FakeWebsocket(), None, failed, errors, 2, "ASR"
            )
        except CozeUnavailable as exc:
            assert "4028" in str(exc)
        else:
            raise AssertionError("WebSocket error should interrupt the wait")
        await error_task

    asyncio.run(run_test())


def test_streaming_talk_release_cancels_active_task():
    async def run_test():
        from audio import app

        previous_mode = app.COZE_MODE
        app.COZE_MODE = "streaming"
        try:
            service = app.TalkService()

            async def block_until_cancelled():
                await asyncio.Event().wait()

            task = asyncio.create_task(block_until_cancelled())
            service._active_task = task

            result = await service.trigger(False)

            assert result == {"ok": True, "cancelled": True}
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert task.cancelled()
        finally:
            app.COZE_MODE = previous_mode

    asyncio.run(run_test())


def test_streaming_quota_error_retries_after_cooldown():
    async def run_test():
        from audio import app

        previous_mode = app.COZE_MODE
        previous_retry = app.COZE_QUOTA_RETRY_S
        app.COZE_MODE = "streaming"
        app.COZE_QUOTA_RETRY_S = 0.01
        try:
            service = app.TalkService()
            service._set_talk_error(app.COZE_QUOTA_ERROR_TEXT, app.COZE_QUOTA_ERROR_CODE)
            service._talk_error_since -= 1.0

            gate = asyncio.Event()

            async def hold_session():
                await gate.wait()

            service._run_once = hold_session  # type: ignore[method-assign]
            result = await service.trigger(True)

            assert result == {"ok": True, "busy": False}
            assert service.talk_error is None
            assert service._talk_error_code is None

            task = service._active_task
            assert task is not None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            app.COZE_MODE = previous_mode
            app.COZE_QUOTA_RETRY_S = previous_retry

    asyncio.run(run_test())


def test_streaming_quota_error_blocks_before_cooldown():
    async def run_test():
        from audio import app

        previous_mode = app.COZE_MODE
        previous_retry = app.COZE_QUOTA_RETRY_S
        app.COZE_MODE = "streaming"
        app.COZE_QUOTA_RETRY_S = 60.0
        try:
            service = app.TalkService()
            service._set_talk_error(app.COZE_QUOTA_ERROR_TEXT, app.COZE_QUOTA_ERROR_CODE)

            result = await service.trigger(True)

            assert result["ok"] is False
            assert "4028" in str(result.get("error"))
            assert "自动重试" in str(result.get("error"))
        finally:
            app.COZE_MODE = previous_mode
            app.COZE_QUOTA_RETRY_S = previous_retry

    asyncio.run(run_test())


def test_talk_service_tracks_partial_and_final_conversation():
    from audio.app import TalkService

    service = TalkService()
    service._conversation_update({"type": "status", "text": "请说话"})
    service._conversation_update({"type": "visitor_partial", "text": "快递到了"})
    service._conversation_update({"type": "visitor_final", "text": "快递到了"})
    service._conversation_update({"type": "assistant_delta", "text": "请放"})
    service._conversation_update({"type": "assistant_delta", "text": "在门口。"})
    service._conversation_update({"type": "assistant_final", "text": "请放在门口。"})

    assert service._conversation_status == "请继续说话"
    assert service._conversation == [
        {"role": "visitor", "text": "快递到了", "partial": False},
        {"role": "assistant", "text": "请放在门口。", "partial": False},
    ]