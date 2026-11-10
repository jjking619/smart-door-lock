"""audiood —— 门铃 → 对讲 / 自动应答（默认走 Coze 智能体，不需要工作流）。

  * 不引入 Qt，无 GUI（门锁没有屏幕）。
  * 麦克风用 sounddevice，ARM64 上免编译。
    * `COZE_MODE=realtime`：Coze WebSocket Bot 实时语音对话；
    * `COZE_MODE=streaming`：独立流式 ASR → Bot 文本流 → 流式 TTS；
    * `bot` / `workflow`：逐句录音的旧链路。
  * 实时连接失败时降级为本地固定话术播报，保证门铃永远有声音。

运行：
    COZE_BOT_ID=7466... python -m audio.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import numpy as np
import sounddevice as sd
from aiohttp import web

from .coze_audio import CozeAudio, safe_synthesize, safe_transcribe
from .coze_client import CozeConfigError, CozeUnavailable, answer_with_fallback
from .logging_setup import configure_audio_logging

logger = logging.getLogger("audiood")

COZE_MODE = os.getenv("COZE_MODE", "realtime").strip().lower()
COZE_QUOTA_RETRY_S = float(os.getenv("COZE_QUOTA_RETRY_S", "60"))
COZE_QUOTA_ERROR_CODE = 4028
COZE_QUOTA_ERROR_TEXT = "Coze 余额不足（4028），请充值或等待额度刷新"

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SILENCE_RMS = float(os.getenv("AUDIO_SILENCE_RMS", "300"))
MAX_UTTER_S = float(os.getenv("AUDIO_MAX_UTTER_S", "8"))
AUDIO_HTTP_PORT = int(os.getenv("AUDIO_HTTP_PORT", "8084"))
AUDIO_USE_STDIN_BELL = os.getenv("AUDIO_USE_STDIN_BELL", "0") == "1"


def _realtime_config_error() -> str | None:
    if not os.getenv("COZE_API_TOKEN", "").strip():
        return "COZE_API_TOKEN 未配置"
    if not os.getenv("COZE_BOT_ID", "").strip():
        return "COZE_BOT_ID 未配置"
    return None


def _build_dialog_client():
    """按 COZE_MODE 构造对话客户端。返回 None 表示只用本地话术。

    Bot 与 Workflow 客户端都有 `ask(text, context)`，所以下游无需分支。
    """
    try:
        if COZE_MODE == "workflow":
            from .coze_client import CozeWorkflowClient  # noqa: PLC0415

            logger.info("对话模式: workflow")
            return CozeWorkflowClient()
        from .coze_bot import CozeBotClient  # noqa: PLC0415

        logger.info("对话模式: bot（无需工作流）")
        return CozeBotClient()
    except CozeConfigError as exc:
        logger.error("配置错误，对话功能关闭：\n%s", exc)
        return None
    except CozeUnavailable as exc:
        logger.warning("Coze 未启用（%s），将只用本地话术", exc)
        return None


class Recorder:
    """带回声消除占位的最小录音器：按静音切句。

    真正的 AEC 在下一阶段用 webrtc-audio-processing 的 AEC3 处理，
    这里只做 VAD 断句 —— 没有它，自动应答会一直录到超时。
    """

    def __init__(self, device: int | None = None) -> None:
        self._device = device

    def record_utterance(self) -> bytes:
        frames: list[np.ndarray] = []
        voiced = False
        started = time.monotonic()

        def callback(indata, _frames, _time, status) -> None:  # noqa: ANN001
            nonlocal voiced
            if status:
                logger.debug("audio status: %s", status)
            block = indata.copy().reshape(-1)
            frames.append(block)
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
            if rms > SILENCE_RMS:
                voiced = True

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=callback,
        ):
            # 先等开口（最多 5 s），再录到静音或超时
            while not voiced and time.monotonic() - started < 5:
                sd.sleep(FRAME_MS)
            if not voiced:
                return b""

            quiet_since = time.monotonic()
            while time.monotonic() - started < MAX_UTTER_S:
                sd.sleep(FRAME_MS)
                if frames and float(np.sqrt(np.mean(frames[-1].astype(np.float32) ** 2))) < SILENCE_RMS:
                    if time.monotonic() - quiet_since > 0.7:
                        break
                else:
                    quiet_since = time.monotonic()

        return b"".join(f.tobytes() for f in frames)


def play_pcm(pcm: bytes, rate: int = SAMPLE_RATE, device: int | None = None) -> None:
    """播放 16 bit 单声道 PCM（采样率由 TTS 决定，默认 16 kHz）。"""
    if not pcm:
        return
    audio = np.frombuffer(pcm, dtype=np.int16)
    sd.play(audio, samplerate=rate, device=device)
    sd.wait()


async def main() -> None:
    log_path = configure_audio_logging("audiood")
    if log_path is not None:
        logger.info("audiood log file: %s", log_path)
    service = TalkService()
    await service.start()

    if AUDIO_HTTP_PORT > 0:
        await _serve_http(service, AUDIO_HTTP_PORT)
        return

    await service.stdin_loop()


async def _realtime_loop() -> None:
    from .coze_realtime import RealtimeVoiceSession  # noqa: PLC0415

    try:
        RealtimeVoiceSession()
    except CozeConfigError as exc:
        logger.error("实时语音配置错误：\n%s", exc)
        return

    out_dev = _int_or_none(os.getenv("AUDIO_OUTPUT_DEVICE"))
    loop = asyncio.get_running_loop()
    logger.info("audiood ready (realtime); 按 Ctrl+C 退出")
    while True:
        await loop.run_in_executor(None, _wait_for_bell)
        logger.info("门铃触发，建立实时语音会话")
        t0 = time.monotonic()
        session = RealtimeVoiceSession()
        try:
            await session.run()
        except Exception as exc:  # noqa: BLE001
            logger.error("实时会话失败，降级为本地话术: %s", exc)
            await _play_fallback(out_dev)
        logger.info("会话结束，时长 %.1f s，%d 句", time.monotonic() - t0, len(session.transcripts))


class TalkService:
    def __init__(self) -> None:
        self.bot = None
        self.audio: CozeAudio | None = None
        self.recorder: Recorder | None = None
        self.out_dev: int | None = None
        self._active_task: asyncio.Task | None = None
        self.talk_error: str | None = None
        self._talk_error_code: int | None = None
        self._talk_error_since = 0.0
        self._conversation: list[dict[str, object]] = []
        self._conversation_status = "空闲"

    def _conversation_update(self, event: dict[str, str]) -> None:
        event_type = event.get("type")
        text = event.get("text", "")
        if event_type == "status":
            self._conversation_status = text
            return

        if event_type == "visitor_partial":
            if (
                self._conversation
                and self._conversation[-1]["role"] == "visitor"
                and self._conversation[-1]["partial"]
            ):
                self._conversation[-1]["text"] = text
            else:
                self._conversation.append({"role": "visitor", "text": text, "partial": True})
            return

        if event_type == "visitor_final":
            if (
                self._conversation
                and self._conversation[-1]["role"] == "visitor"
                and self._conversation[-1]["partial"]
            ):
                self._conversation[-1].update(text=text, partial=False)
            elif text:
                self._conversation.append({"role": "visitor", "text": text, "partial": False})
            self._conversation_status = "助手正在回复"
            return

        if event_type == "assistant_delta":
            if (
                self._conversation
                and self._conversation[-1]["role"] == "assistant"
                and self._conversation[-1]["partial"]
            ):
                self._conversation[-1]["text"] = str(self._conversation[-1]["text"]) + text
            else:
                self._conversation.append({"role": "assistant", "text": text, "partial": True})
            return

        if event_type == "assistant_final":
            if (
                self._conversation
                and self._conversation[-1]["role"] == "assistant"
                and self._conversation[-1]["partial"]
            ):
                self._conversation[-1].update(text=text, partial=False)
            elif text:
                self._conversation.append({"role": "assistant", "text": text, "partial": False})
            self._conversation_status = "请继续说话"

    def _set_talk_error(self, message: str, code: int | None = None) -> None:
        self.talk_error = message
        self._talk_error_code = code
        self._talk_error_since = time.monotonic()

    def _clear_talk_error(self) -> None:
        self.talk_error = None
        self._talk_error_code = None
        self._talk_error_since = 0.0

    async def start(self) -> None:
        logger.info("audiood start mode=%s", COZE_MODE)
        if COZE_MODE in {"realtime", "streaming"}:
            config_error = _realtime_config_error()
            if config_error:
                self._set_talk_error(config_error)
                logger.warning("实时语音不可用：%s", self.talk_error)

        if COZE_MODE == "streaming" and self.talk_error is None:
            try:
                from .coze_bot import CozeBotClient

                self.bot = CozeBotClient()
            except CozeConfigError as exc:
                self._set_talk_error(str(exc))
                logger.error("流式语音配置错误：%s", exc)
        elif COZE_MODE not in {"realtime", "streaming"}:
            self.bot = _build_dialog_client()
            if self.bot is None:
                logger.warning("对话客户端未启用，将只用本地话术")

        if COZE_MODE != "streaming":
            try:
                self.audio = CozeAudio()
            except CozeUnavailable as exc:
                logger.warning("Coze 语音未启用（%s），语音识别/播报可能不可用", exc)
                self.audio = None

        self.recorder = Recorder(device=_int_or_none(os.getenv("AUDIO_INPUT_DEVICE")))
        self.out_dev = _int_or_none(os.getenv("AUDIO_OUTPUT_DEVICE"))

    async def trigger(self, on: bool) -> dict:
        logger.info("talk trigger request on=%s mode=%s", on, COZE_MODE)
        if not on:
            if COZE_MODE == "streaming":
                task = self._active_task
                if task is not None and not task.done():
                    logger.info("talk trigger stopping active streaming session")
                    task.cancel()
                    return {"ok": True, "cancelled": True}
                logger.info("talk trigger stop ignored: no active streaming session")
                return {"ok": True, "cancelled": False}
            if COZE_MODE == "realtime":
                # PTT 语义：松开仅表示"停止继续说"，不应立刻打断 Bot 回答。
                busy = self._active_task is not None and not self._active_task.done()
                logger.info("talk trigger release realtime busy=%s", busy)
                return {"ok": True, "released": True, "busy": busy}
            return {"ok": True, "cancelled": False}

        if self.talk_error:
            if self._talk_error_code == COZE_QUOTA_ERROR_CODE:
                elapsed = time.monotonic() - self._talk_error_since
                if elapsed >= COZE_QUOTA_RETRY_S:
                    logger.info("quota error cooldown elapsed, retrying Coze session")
                    self._clear_talk_error()
                else:
                    retry_after = max(1, int(COZE_QUOTA_RETRY_S - elapsed))
                    message = f"{self.talk_error}（{retry_after}s 后自动重试）"
                    logger.warning("talk trigger rejected by talk_error: %s", message)
                    return {"ok": False, "error": message}
            if self.talk_error:
                logger.warning("talk trigger rejected by talk_error: %s", self.talk_error)
                return {"ok": False, "error": self.talk_error}

        if self._active_task is not None and not self._active_task.done():
            logger.info("talk trigger ignored: session already busy")
            return {"ok": True, "busy": True}

        self._conversation.clear()
        self._conversation_status = "正在连接"
        self._active_task = asyncio.create_task(self._run_once())
        logger.info("talk trigger accepted: session task created")
        return {"ok": True, "busy": False}

    async def stdin_loop(self) -> None:
        logger.info("audiood ready; 按 Ctrl+C 退出")
        loop = asyncio.get_running_loop()
        while True:
            await loop.run_in_executor(None, _wait_for_bell)
            logger.info("门铃触发")
            result = await self.trigger(True)
            if result.get("busy"):
                logger.info("上一次会话尚未结束，忽略本次触发")

    async def _run_once(self) -> None:
        try:
            if COZE_MODE == "realtime":
                await self._run_realtime_once()
            elif COZE_MODE == "streaming":
                await self._run_streaming_once()
            else:
                await self._run_dialog_once()
        except asyncio.CancelledError:
            self._conversation_status = "对话已结束"
            raise
        except Exception:
            self._conversation_status = "对话出错"
            raise
        else:
            if self._conversation_status in {
                "正在连接", "正在播放开场提示", "请说话",
                "助手正在回复", "请继续说话",
            }:
                self._conversation_status = "对话结束"

    async def _run_streaming_once(self) -> None:
        from .coze_streaming import CozeStreamingSession

        if self.bot is None:
            logger.error("streaming session skipped: bot client not initialized")
            return
        session = CozeStreamingSession(
            self.bot,
            input_device=_int_or_none(os.getenv("AUDIO_INPUT_DEVICE")),
            output_device=self.out_dev,
            on_update=self._conversation_update,
        )
        logger.info("streaming session created sid=%s", session.session_id)
        if session.session_log_path is not None:
            logger.info("streaming session file sid=%s path=%s", session.session_id, session.session_log_path)
        try:
            await session.run()
            if self._talk_error_code == COZE_QUOTA_ERROR_CODE:
                self._clear_talk_error()
            logger.info("streaming session completed sid=%s turns=%d", session.session_id, len(session.transcripts))
        except asyncio.CancelledError:
            logger.info("流式语音会话已取消 sid=%s", session.session_id)
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            logger.error("流式语音会话失败 sid=%s，降级为本地话术: %s", session.session_id, message)
            self._conversation_status = "云端对话失败，已结束"
            if str(COZE_QUOTA_ERROR_CODE) in message:
                self._set_talk_error(COZE_QUOTA_ERROR_TEXT, COZE_QUOTA_ERROR_CODE)
            await _play_fallback(self.out_dev)

    async def _run_realtime_once(self) -> None:
        from .coze_realtime import RealtimeVoiceSession  # noqa: PLC0415

        try:
            session = RealtimeVoiceSession()
        except CozeConfigError as exc:
            logger.error("实时语音配置错误：\n%s", exc)
            return

        t0 = time.monotonic()
        try:
            await session.run()
        except asyncio.CancelledError:
            logger.info("实时语音会话已取消")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("实时会话失败，降级为本地话术: %s", exc)
            await _play_fallback(self.out_dev)

        if session.last_error_code == COZE_QUOTA_ERROR_CODE:
            self._set_talk_error(COZE_QUOTA_ERROR_TEXT, COZE_QUOTA_ERROR_CODE)
            logger.error("实时语音不可用：%s", self.talk_error)
        elif session.last_error_code is not None:
            logger.warning("实时语音会话异常结束：code=%s", session.last_error_code)
        elif self._talk_error_code == COZE_QUOTA_ERROR_CODE:
            # 会话成功结束时，自动清理旧的配额告警（例如额度已恢复）
            self._clear_talk_error()

        logger.info("会话结束，时长 %.1f s，%d 句", time.monotonic() - t0, len(session.transcripts))

    async def _run_dialog_once(self) -> None:
        if self.recorder is None:
            return
        # 新访客 → 清空对话上下文，避免上一位访客的话影响这一次
        reset = getattr(self.bot, "reset", None)
        if callable(reset):
            reset()

        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(None, self.recorder.record_utterance)
        if not pcm:
            logger.info("没有检测到说话，走固定话术")
            ans = await answer_with_fallback("", None)
            reply_pcm = b""
        else:
            visitor_text = await safe_transcribe(self.audio, pcm)
            ans = await answer_with_fallback(visitor_text or "", self.bot)
            reply_pcm, rate = await safe_synthesize(self.audio, ans.text)
            if reply_pcm:
                await loop.run_in_executor(None, play_pcm, reply_pcm, rate, self.out_dev)

        logger.info(
            "应答 source=%s action=%s auth=%s cost=%.0fms text=%s",
            ans.source, ans.action, ans.needs_local_auth,
            (time.monotonic() - t0) * 1000, ans.text,
        )
        if ans.needs_local_auth:
            # 只发事件，不做开锁动作 —— 由 lockd 校验后经 HMAC 下发 MCU
            logger.warning("访客请求开锁，已上报待人工确认（不会自动开门）")

        _dump_wav(pcm)


async def _serve_http(service: TalkService, port: int) -> None:
    logger.info("audiood HTTP 控制已启动 :%d", port)

    async def health(_req: web.Request) -> web.Response:
        busy = service._active_task is not None and not service._active_task.done()
        conversation = [
            {"role": item["role"], "text": item["text"], "partial": item["partial"]}
            for item in service._conversation
        ]
        return web.json_response({
            "ok": True,
            "mode": COZE_MODE,
            "busy": busy,
            "talk_available": service.talk_error is None,
            "talk_error": service.talk_error,
            "talk_error_code": service._talk_error_code,
            "conversation_status": service._conversation_status,
            "conversation": conversation,
        })

    async def talk(req: web.Request) -> web.Response:
        try:
            payload = await req.json()
        except Exception:  # noqa: BLE001
            payload = {}
        on = bool(payload.get("on", True))
        result = await service.trigger(on)
        status = 200 if result.get("ok") else 503
        logger.info("http /talk on=%s status=%s result=%s", on, status, result)
        return web.json_response(result, status=status)

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/talk", talk)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    try:
        if AUDIO_USE_STDIN_BELL:
            await service.stdin_loop()
        else:
            await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def _play_fallback(out_dev: int | None) -> None:
    ans = await answer_with_fallback("", None)
    try:
        audio = CozeAudio()
    except CozeUnavailable:
        logger.warning("无法播报兜底话术: %s", ans.text)
        return
    pcm, rate = await safe_synthesize(audio, ans.text)
    if pcm:
        await asyncio.get_running_loop().run_in_executor(None, play_pcm, pcm, rate, out_dev)


def _wait_for_bell() -> None:
    input("按 Enter 模拟门铃按下…")


def _dump_wav(pcm: bytes) -> None:
    import wave

    os.makedirs("data/utterances", exist_ok=True)
    path = f"data/utterances/{int(time.time())}.wav"
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    logger.debug("saved %s", path)


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
