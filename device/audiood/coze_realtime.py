"""Coze WebSocket 实时语音对话：麦克风 PCM 上行，Bot 语音 PCM 下行。

一条 WebSocket 同时完成 ASR → 智能体 → TTS，省掉"录完整句→上传→等回复→再合成"
的三次往返；服务端 VAD 断句，首包语音通常在说完话后 1 s 内到达。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import sounddevice as sd

from .coze_bot import _UNLOCK_RE, validate_bot_id
from .coze_client import COZE_API_TOKEN, COZE_BASE_URL, CozeConfigError, build_async_client

logger = logging.getLogger(__name__)

IN_RATE = 16_000
OUT_RATE = 24_000
IN_FRAME_MS = 40


def _env_int(name: str, default: int | None) -> int | None:
    value = os.getenv(name, "")
    try:
        return int(value) if value else default
    except ValueError:
        return default


@dataclass(slots=True)
class RealtimeConfig:
    bot_id: str = os.getenv("COZE_BOT_ID", "")
    voice_id: str = os.getenv("COZE_VOICE_ID", "")
    user_id: str = os.getenv("COZE_USER_ID", "doorbell")
    input_device: int | None = _env_int("AUDIO_INPUT_DEVICE", None)
    output_device: int | None = _env_int("AUDIO_OUTPUT_DEVICE", None)
    # half：播放期间不上传麦克风，避免无 AEC 时 Bot 听到自己的声音而自我打断
    duplex: str = os.getenv("AUDIO_DUPLEX", "half").lower()
    echo_tail_ms: int = _env_int("AUDIO_ECHO_TAIL_MS", 400) or 0
    vad_silence_ms: int = _env_int("COZE_VAD_SILENCE_MS", 500) or 500
    idle_timeout_s: float = float(os.getenv("COZE_SESSION_IDLE_S", "15"))
    max_session_s: float = float(os.getenv("COZE_SESSION_MAX_S", "120"))
    prologue: str = os.getenv("COZE_PROLOGUE", "您好，请问有什么事？")


class _Player:
    """回调式播放器：下行音频随到随播，被打断时可整体清空。"""

    def __init__(self, device: int | None) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_audio = 0.0
        self._stream = sd.RawOutputStream(
            samplerate=OUT_RATE, channels=1, dtype="int16",
            blocksize=int(OUT_RATE * 0.02), device=device, callback=self._callback,
        )

    def _callback(self, outdata, frames, _time, _status) -> None:  # noqa: ANN001
        need = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
            if chunk:
                self._last_audio = time.monotonic()
        outdata[: len(chunk)] = chunk
        if len(chunk) < need:
            outdata[len(chunk):] = b"\x00" * (need - len(chunk))

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def busy(self, tail_s: float = 0.0) -> bool:
        with self._lock:
            pending = bool(self._buf)
        return pending or time.monotonic() - self._last_audio < tail_s

    def __enter__(self) -> "_Player":
        self._stream.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stream.stop()
        self._stream.close()


class RealtimeVoiceSession:
    """一次门铃 = 一次会话：连接 → 播开场白 → 自由对话 → 空闲超时挂断。"""

    def __init__(
        self,
        cfg: RealtimeConfig | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.cfg = cfg or RealtimeConfig()
        if not COZE_API_TOKEN:
            raise CozeConfigError("COZE_API_TOKEN 未配置")
        self.cfg.bot_id = validate_bot_id(self.cfg.bot_id)
        self._coze = build_async_client(COZE_API_TOKEN, COZE_BASE_URL)
        self._emit = on_event or (lambda _e: None)
        self._last_activity = 0.0
        self._closed = asyncio.Event()
        self._player: _Player | None = None
        self.transcripts: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    async def run(self) -> None:
        from cozepy import AsyncWebsocketsChatEventHandler  # noqa: PLC0415

        session = self

        class Handler(AsyncWebsocketsChatEventHandler):
            async def on_conversation_audio_delta(self, cli, event):  # noqa: ANN001
                audio = event.data.get_audio()
                if audio and session._player:
                    session._player.feed(audio)
                    session._touch()

            async def on_input_audio_buffer_speech_started(self, cli, event):  # noqa: ANN001
                if session._player:
                    session._player.clear()
                session._touch()

            async def on_conversation_audio_transcript_completed(self, cli, event):  # noqa: ANN001
                session._on_user_text(event.data.content)

            async def on_conversation_message_completed(self, cli, event):  # noqa: ANN001
                msg = event.data
                if getattr(msg, "role", "") == "assistant" and getattr(msg, "type", "") == "answer":
                    session.transcripts.append(("bot", msg.content))
                    logger.info("Bot: %s", msg.content)

            async def on_conversation_chat_failed(self, cli, event):  # noqa: ANN001
                logger.warning("对话失败: %s", getattr(event.data, "last_error", event))

            async def on_error(self, cli, e):  # noqa: ANN001
                logger.error("WebSocket 错误: %s", e)
                if "4101" in str(e):
                    logger.error("→ COZE_BOT_ID 不存在，或智能体未以「API」渠道发布")
                session._closed.set()

            async def on_closed(self, cli):  # noqa: ANN001
                session._closed.set()

        client = self._coze.websockets.chat.create(bot_id=self.cfg.bot_id, on_event=Handler())
        started = time.monotonic()
        self._touch()

        with _Player(self.cfg.output_device) as player:
            self._player = player
            async with client() as ws:
                await ws.chat_update(self._update_payload())
                mic_task = asyncio.create_task(self._pump_mic(ws))
                try:
                    while not self._closed.is_set():
                        await asyncio.sleep(0.2)
                        now = time.monotonic()
                        if now - started > self.cfg.max_session_s:
                            logger.info("达到最长通话时长，挂断")
                            break
                        if not player.busy() and now - self._last_activity > self.cfg.idle_timeout_s:
                            logger.info("访客 %.0f s 无响应，挂断", self.cfg.idle_timeout_s)
                            break
                finally:
                    mic_task.cancel()
                    await asyncio.gather(mic_task, return_exceptions=True)
            # 让最后一句播完
            while player.busy():
                await asyncio.sleep(0.1)
        self._player = None

    # ------------------------------------------------------------------
    def _update_payload(self):  # noqa: ANN202
        from cozepy.websockets.chat import ChatUpdateEvent  # noqa: PLC0415
        from cozepy.websockets.ws import InputAudio, OutputAudio, PCMConfig  # noqa: PLC0415

        TurnDetectionType = ChatUpdateEvent.TurnDetectionType  # noqa: N806
        return ChatUpdateEvent.Data(
            input_audio=InputAudio(
                format="pcm", codec="pcm", sample_rate=IN_RATE, channel=1, bit_depth=16,
            ),
            output_audio=OutputAudio(
                codec="pcm",
                pcm_config=PCMConfig(sample_rate=OUT_RATE),
                voice_id=self.cfg.voice_id or None,
            ),
            turn_detection=ChatUpdateEvent.TurnDetection(
                type=TurnDetectionType.SERVER_VAD,
                prefix_padding_ms=300,
                silence_duration_ms=self.cfg.vad_silence_ms,
            ),
            # 不在云端保存门口对话
            chat_config=ChatUpdateEvent.ChatConfig(
                user_id=self.cfg.user_id, auto_save_history=False,
            ),
            need_play_prologue=bool(self.cfg.prologue),
            prologue_content=self.cfg.prologue or None,
        )

    async def _pump_mic(self, ws) -> None:  # noqa: ANN001
        from cozepy import InputAudioBufferAppendEvent  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        tail_s = self.cfg.echo_tail_ms / 1000

        def callback(indata, _frames, _time, status) -> None:  # noqa: ANN001
            if status:
                logger.debug("mic status: %s", status)
            if self.cfg.duplex == "half" and self._player and self._player.busy(tail_s):
                return
            data = bytes(indata)
            loop.call_soon_threadsafe(_offer, queue, data)

        with sd.RawInputStream(
            samplerate=IN_RATE, channels=1, dtype="int16",
            blocksize=IN_RATE * IN_FRAME_MS // 1000,
            device=self.cfg.input_device, callback=callback,
        ):
            while True:
                chunk = await queue.get()
                await ws.input_audio_buffer_append(
                    InputAudioBufferAppendEvent.Data.model_validate({"delta": chunk})
                )

    def _on_user_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._touch()
        self.transcripts.append(("visitor", text))
        logger.info("访客: %s", text)
        if _UNLOCK_RE.search(text):
            # 只上报，开锁必须由 lockd 本地鉴权
            logger.warning("访客提到开门，已上报待屋主确认（不会自动开锁）")
            self._emit({"type": "visitor.request_unlock", "text": text, "needs_local_auth": True})

    def _touch(self) -> None:
        self._last_activity = time.monotonic()


def _offer(queue: asyncio.Queue[bytes], data: bytes) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(data)
