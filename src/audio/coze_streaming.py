"""Independent streaming ASR, text chat, and TTS session for audiood."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable

import numpy as np
import sounddevice as sd

from .coze_bot import CozeBotClient, requests_unlock
from .coze_client import CozeUnavailable
from .logging_setup import create_session_file_logger

logger = logging.getLogger(__name__)

IN_RATE = 16_000
OUT_RATE = 24_000
FRAME_MS = 20
SILENCE_RMS = float(os.getenv("AUDIO_SILENCE_RMS", "300"))
SILENCE_S = float(os.getenv("AUDIO_VAD_SILENCE_S", "0.7"))
MAX_UTTER_S = float(os.getenv("AUDIO_MAX_UTTER_S", "8"))
ASR_TIMEOUT_S = float(os.getenv("COZE_ASR_TIMEOUT_S", "10"))
TTS_TIMEOUT_S = float(os.getenv("COZE_TTS_TIMEOUT_S", "8"))
VOICE_ID = os.getenv("COZE_VOICE_ID", "")
PROLOGUE = os.getenv("COZE_PROLOGUE", "您好，请问有什么事？")
IDLE_TIMEOUT_S = float(os.getenv("COZE_SESSION_IDLE_S", "15"))
MAX_SESSION_S = float(os.getenv("COZE_SESSION_MAX_S", "120"))
_TEXT_BOUNDARY = re.compile(r"[。！？!?；;\n]")


class _StreamingPlayer:
    def __init__(self, device: int | None) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream = sd.RawOutputStream(
            samplerate=OUT_RATE,
            channels=1,
            dtype="int16",
            blocksize=OUT_RATE // 50,
            device=device,
            callback=self._callback,
        )

    def _callback(self, outdata, frames, _time, _status) -> None:  # noqa: ANN001
        size = frames * 2
        with self._lock:
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
        outdata[: len(chunk)] = chunk
        if len(chunk) < size:
            outdata[len(chunk) :] = b"\x00" * (size - len(chunk))

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer.extend(pcm)

    def busy(self) -> bool:
        with self._lock:
            return bool(self._buffer)

    def __enter__(self) -> _StreamingPlayer:
        self._stream.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stream.stop()
        self._stream.close()


class _TextChunker:
    def __init__(self, max_chars: int = 64) -> None:
        self._pending = ""
        self._max_chars = max_chars

    def feed(self, text: str) -> list[str]:
        self._pending += text
        chunks: list[str] = []
        while self._pending:
            match = _TEXT_BOUNDARY.search(self._pending)
            if match:
                end = match.end()
            elif len(self._pending) >= self._max_chars:
                end = self._pending.rfind(" ", 24, self._max_chars + 1)
                end = end if end > 0 else self._max_chars
            else:
                break
            chunk = self._pending[:end].strip()
            self._pending = self._pending[end:].lstrip()
            if chunk:
                chunks.append(chunk)
        return chunks

    def flush(self) -> list[str]:
        chunk = self._pending.strip()
        self._pending = ""
        return [chunk] if chunk else []


class CozeStreamingSession:
    def __init__(
        self,
        bot: CozeBotClient,
        input_device: int | None,
        output_device: int | None,
        on_update: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        from .coze_client import build_async_client

        self.bot = bot
        self.input_device = input_device
        self.output_device = output_device
        self.on_update = on_update
        self._coze = build_async_client()
        self.transcripts: list[tuple[str, str]] = []
        self._player: _StreamingPlayer | None = None
        self.session_id = uuid.uuid4().hex[:8]
        self._session_logger, self.session_log_path, self._close_session_logger = create_session_file_logger(
            "audiood", self.session_id
        )

    def _sid(self) -> str:
        return f"streaming:{self.session_id}"

    def _log(self, level: int, message: str, *args: object) -> None:
        logger.log(level, message, *args)
        if self._session_logger is not None:
            self._session_logger.log(level, message, *args)

    def _info(self, message: str, *args: object) -> None:
        self._log(logging.INFO, message, *args)

    def _warning(self, message: str, *args: object) -> None:
        self._log(logging.WARNING, message, *args)

    def _debug(self, message: str, *args: object) -> None:
        self._log(logging.DEBUG, message, *args)

    def _emit_update(self, event_type: str, text: str = "") -> None:
        if self.on_update is not None:
            self.on_update({"type": event_type, "text": text})

    async def run(self) -> None:
        self.bot.reset()
        started = time.monotonic()
        last_activity = started
        status = "ok"
        error_text = ""
        self._info(
            "%s session start input_device=%s output_device=%s idle_timeout=%.1fs max_session=%.1fs",
            self._sid(), self.input_device, self.output_device, IDLE_TIMEOUT_S, MAX_SESSION_S,
        )
        self._emit_update("status", "正在连接")
        if self.session_log_path is not None:
            self._info("%s session log file=%s", self._sid(), self.session_log_path)
        with _StreamingPlayer(self.output_device) as player:
            try:
                self._player = player
                if PROLOGUE:
                    self._emit_update("status", "正在播放开场提示")
                    self._info("%s prologue start text_len=%d", self._sid(), len(PROLOGUE))
                    await self._speak_text(PROLOGUE)
                    self._info("%s prologue done", self._sid())
                    last_activity = time.monotonic()

                while time.monotonic() - started < MAX_SESSION_S:
                    idle_left = IDLE_TIMEOUT_S - (time.monotonic() - last_activity)
                    if idle_left <= 0:
                        self._info("%s session idle timeout reached", self._sid())
                        break
                    self._emit_update("status", "请说话")
                    self._info("%s waiting visitor speech idle_left=%.1fs", self._sid(), idle_left)
                    text = await self._transcribe_turn(idle_left)
                    if not text:
                        self._emit_update("status", "没有听到清晰语音")
                        self._info("%s no speech recognized, end session loop", self._sid())
                        break

                    text = text.strip()
                    self.transcripts.append(("visitor", text))
                    self._emit_update("visitor_final", text)
                    unlock_requested = requests_unlock(text)
                    if unlock_requested:
                        self._warning("访客请求开门，已推送界面等待屋主确认；不会自动开锁")
                        self._emit_update("unlock_request", text)
                    self._info("访客: %s", text)
                    self._info("%s ASR final text_len=%d", self._sid(), len(text))
                    self._emit_update("status", "助手正在回复")
                    self._info("%s bot turn start", self._sid())
                    reply = await self._stream_bot_turn(text)
                    self.transcripts.append(("bot", reply.text))
                    self._emit_update("assistant_final", reply.text)
                    self._info("Bot: %s", reply.text)
                    self._info("%s bot turn done reply_len=%d", self._sid(), len(reply.text))
                    if reply.needs_local_auth and not unlock_requested:
                        self._warning("访客请求开门，已推送界面等待屋主确认；不会自动开锁")
                        self._emit_update("unlock_request", text)
                    last_activity = time.monotonic()
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status = "error"
                error_text = str(exc)
                raise
            finally:
                self._player = None
                self._info(
                    "%s session end status=%s turns=%d cost=%.1fs",
                    self._sid(), status, len(self.transcripts), time.monotonic() - started,
                )
                if error_text:
                    self._warning("%s session error=%s", self._sid(), error_text)
                self._close_session_logger()

    async def _transcribe_turn(self, start_timeout: float) -> str:
        from cozepy.websockets.audio.transcriptions import TranscriptionsUpdateEvent
        from cozepy.websockets.ws import InputAudio, WebsocketsEventType

        transcript: list[str] = []
        failed = asyncio.Event()
        errors: list[str] = []
        last_partial = ""

        async def on_update(_client, event) -> None:  # noqa: ANN001
            transcript[:] = [event.data.content]
            nonlocal last_partial
            if event.data.content != last_partial:
                last_partial = event.data.content
                self._emit_update("visitor_partial", event.data.content)
                self._info("%s ASR partial: %s", self._sid(), event.data.content[:80])

        async def on_error(_client, error) -> None:  # noqa: ANN001
            errors.append(str(error))
            failed.set()

        client = self._coze.websockets.audio.transcriptions.create(
            on_event={
                WebsocketsEventType.TRANSCRIPTIONS_MESSAGE_UPDATE: on_update,
                WebsocketsEventType.ERROR: on_error,
            }
        )
        async with client() as ws:
            try:
                self._info("%s ASR websocket open", self._sid())
                await ws.transcriptions_update(
                    TranscriptionsUpdateEvent.Data(
                        input_audio=InputAudio(
                            format="pcm", codec="pcm", sample_rate=IN_RATE, channel=1, bit_depth=16
                        )
                    )
                )
                voiced = await self._stream_microphone(ws, start_timeout, failed, errors)
                if not voiced:
                    self._info("%s ASR no voice before timeout=%.1fs", self._sid(), start_timeout)
                    return ""
                await ws.input_audio_buffer_complete()
                self._info("%s ASR audio complete, waiting final text", self._sid())
                await self._wait_for_ws(
                    ws, None, failed, errors, ASR_TIMEOUT_S, "流式 ASR 等待识别结果"
                )
                if errors:
                    raise CozeUnavailable(f"流式 ASR 错误: {errors[0]}")
                if transcript:
                    self._info("%s ASR final: %s", self._sid(), transcript[0][:120])
                return transcript[0] if transcript else ""
            except asyncio.TimeoutError as exc:
                raise CozeUnavailable("流式 ASR 连接超时") from exc

    async def _stream_microphone(self, ws, start_timeout: float, failed, errors: list[str]) -> bool:  # noqa: ANN001
        from cozepy.websockets.audio.transcriptions import InputAudioBufferAppendEvent

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=40)

        def callback(indata, _frames, _time, status) -> None:  # noqa: ANN001
            if status:
                self._debug("mic status: %s", status)
            loop.call_soon_threadsafe(_offer, queue, bytes(indata))

        preroll: deque[bytes] = deque(maxlen=8)
        voiced = False
        started = time.monotonic()
        speech_started = started
        last_voice = started
        appended_chunks = 0
        end_reason = "timeout_wait_voice"
        with sd.RawInputStream(
            samplerate=IN_RATE,
            channels=1,
            dtype="int16",
            blocksize=IN_RATE * FRAME_MS // 1000,
            device=self.input_device,
            callback=callback,
        ):
            while True:
                if failed.is_set():
                    raise CozeUnavailable(f"流式 ASR 错误: {errors[0] if errors else 'WebSocket error'}")
                now = time.monotonic()
                if not voiced and now - started >= start_timeout:
                    end_reason = "timeout_wait_voice"
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
                now = time.monotonic()
                if not voiced:
                    preroll.append(chunk)
                    if rms > SILENCE_RMS:
                        voiced = True
                        speech_started = now
                        last_voice = now
                        self._info("%s voice detected rms=%.1f threshold=%.1f", self._sid(), rms, SILENCE_RMS)
                        for buffered in preroll:
                            await ws.input_audio_buffer_append(
                                InputAudioBufferAppendEvent.Data(delta=buffered)
                            )
                            appended_chunks += 1
                        preroll.clear()
                    continue

                await ws.input_audio_buffer_append(InputAudioBufferAppendEvent.Data(delta=chunk))
                appended_chunks += 1
                if rms > SILENCE_RMS:
                    last_voice = now
                if now - speech_started >= MAX_UTTER_S or now - last_voice >= SILENCE_S:
                    end_reason = "max_utter" if now - speech_started >= MAX_UTTER_S else "silence"
                    break
        self._info(
            "%s mic stream end voiced=%s chunks=%d reason=%s speech_cost=%.2fs",
            self._sid(), voiced, appended_chunks, end_reason,
            (time.monotonic() - speech_started) if voiced else 0.0,
        )
        return voiced

    async def _stream_bot_turn(self, text: str):
        from cozepy.websockets.audio.speech import InputTextBufferAppendEvent, SpeechUpdateEvent
        from cozepy.websockets.ws import OutputAudio, PCMConfig, WebsocketsEventType

        errors: list[str] = []
        failed = asyncio.Event()
        tts_chunks = 0
        tts_bytes = 0
        got_first_tts = False
        tts_started = time.monotonic()

        async def on_audio(_client, event) -> None:  # noqa: ANN001
            nonlocal tts_chunks, tts_bytes, got_first_tts
            if self._player:
                self._player.feed(event.data.delta)
            tts_chunks += 1
            tts_bytes += len(event.data.delta)
            if not got_first_tts:
                got_first_tts = True
                self._info("%s TTS first chunk in %.0fms", self._sid(), (time.monotonic() - tts_started) * 1000)

        async def on_error(_client, error) -> None:  # noqa: ANN001
            errors.append(str(error))
            failed.set()

        client = self._coze.websockets.audio.speech.create(
            on_event={
                WebsocketsEventType.SPEECH_AUDIO_UPDATE: on_audio,
                WebsocketsEventType.ERROR: on_error,
            }
        )
        async with client() as ws:
            self._info("%s TTS websocket open", self._sid())
            await ws.speech_update(
                SpeechUpdateEvent(
                    data=SpeechUpdateEvent.Data(
                        output_audio=OutputAudio(
                            codec="pcm",
                            pcm_config=PCMConfig(sample_rate=OUT_RATE),
                            voice_id=VOICE_ID or None,
                        )
                    )
                )
            )
            chunker = _TextChunker()

            async def send_text(delta: str) -> None:
                self._emit_update("assistant_delta", delta)
                for chunk in chunker.feed(delta):
                    self._info("%s TTS enqueue text chunk_len=%d", self._sid(), len(chunk))
                    await ws.input_text_buffer_append(InputTextBufferAppendEvent.Data(delta=chunk))

            reply = await self.bot.ask(text, on_delta=send_text)
            for chunk in chunker.flush():
                self._info("%s TTS flush tail chunk_len=%d", self._sid(), len(chunk))
                await ws.input_text_buffer_append(InputTextBufferAppendEvent.Data(delta=chunk))
            await ws.input_text_buffer_complete()
            self._info("%s TTS input complete, waiting audio done", self._sid())
            await self._wait_for_ws(ws, None, failed, errors, TTS_TIMEOUT_S, "流式 TTS 等待音频")
            if errors:
                raise CozeUnavailable(f"流式 TTS 错误: {errors[0]}")
        self._info("%s TTS done chunks=%d bytes=%d", self._sid(), tts_chunks, tts_bytes)
        await self._drain_player()
        return reply

    async def _speak_text(self, text: str) -> None:
        from cozepy.websockets.audio.speech import InputTextBufferAppendEvent, SpeechUpdateEvent
        from cozepy.websockets.ws import OutputAudio, PCMConfig, WebsocketsEventType

        errors: list[str] = []
        failed = asyncio.Event()
        tts_chunks = 0
        tts_bytes = 0
        got_first_tts = False
        tts_started = time.monotonic()

        async def on_audio(_client, event) -> None:  # noqa: ANN001
            nonlocal tts_chunks, tts_bytes, got_first_tts
            if self._player:
                self._player.feed(event.data.delta)
            tts_chunks += 1
            tts_bytes += len(event.data.delta)
            if not got_first_tts:
                got_first_tts = True
                self._info("%s prologue TTS first chunk in %.0fms", self._sid(), (time.monotonic() - tts_started) * 1000)

        async def on_error(_client, error) -> None:  # noqa: ANN001
            errors.append(str(error))
            failed.set()

        client = self._coze.websockets.audio.speech.create(
            on_event={
                WebsocketsEventType.SPEECH_AUDIO_UPDATE: on_audio,
                WebsocketsEventType.ERROR: on_error,
            }
        )
        async with client() as ws:
            self._info("%s prologue TTS websocket open", self._sid())
            await ws.speech_update(
                SpeechUpdateEvent(
                    data=SpeechUpdateEvent.Data(
                        output_audio=OutputAudio(
                            codec="pcm",
                            pcm_config=PCMConfig(sample_rate=OUT_RATE),
                            voice_id=VOICE_ID or None,
                        )
                    )
                )
            )
            await ws.input_text_buffer_append(InputTextBufferAppendEvent.Data(delta=text))
            await ws.input_text_buffer_complete()
            await self._wait_for_ws(ws, None, failed, errors, TTS_TIMEOUT_S, "开场白 TTS 等待音频")
            if errors:
                raise CozeUnavailable(f"流式 TTS 错误: {errors[0]}")
        self._info("%s prologue TTS done chunks=%d bytes=%d", self._sid(), tts_chunks, tts_bytes)
        await self._drain_player()

    async def _drain_player(self) -> None:
        while self._player and self._player.busy():
            await asyncio.sleep(0.02)

    async def _wait_for_ws(self, ws, events, failed, errors, timeout: float, label: str) -> None:  # noqa: ANN001
        event_task = asyncio.create_task(ws.wait(events))
        error_task = asyncio.create_task(failed.wait())
        try:
            done, pending = await asyncio.wait(
                {event_task, error_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if error_task in done and failed.is_set():
                raise CozeUnavailable(f"{label}: {errors[0] if errors else 'WebSocket error'}")
            if event_task in done:
                self._info("%s ws wait done label=%s", self._sid(), label)
                await event_task
                return
            raise CozeUnavailable(f"{label} timeout")
        finally:
            for task in (event_task, error_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(event_task, error_task, return_exceptions=True)


def _offer(queue: asyncio.Queue[bytes], data: bytes) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(data)