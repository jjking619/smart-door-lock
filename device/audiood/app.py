"""audiood —— 门铃 → 对讲 / 自动应答主循环（先做录音-应答-播放闭环）。

本文件是第二阶段的最小可运行骨架：
  * 不引入 Qt，无 GUI（门锁没有屏幕）。
  * 麦克风用 sounddevice，ARM64 上免编译。
  * 对讲音频先走"文件闭环"验证链路，WebRTC（aiortc）在下一阶段接入。
  * 所有云端调用都有超时，失败必降级，保证门铃永远有声音。

运行：
    COZE_API_TOKEN=... COZE_WORKFLOW_ID=... python -m audiood.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import numpy as np
import sounddevice as sd

from .coze_client import CozeUnavailable, CozeWorkflowClient, answer_with_fallback

logger = logging.getLogger("audiood")

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SILENCE_RMS = float(os.getenv("AUDIO_SILENCE_RMS", "300"))
MAX_UTTER_S = float(os.getenv("AUDIO_MAX_UTTER_S", "8"))


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
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        coze = CozeWorkflowClient()
    except CozeUnavailable as exc:
        logger.warning("Coze 未启用（%s），将只用本地话术", exc)
        coze = None

    audio: CozeAudio | None
    try:
        audio = CozeAudio()
    except CozeUnavailable as exc:
        logger.warning("Coze 语音未启用（%s），无法识别与播报", exc)
        audio = None

    recorder = Recorder(device=_int_or_none(os.getenv("AUDIO_INPUT_DEVICE")))
    out_dev = _int_or_none(os.getenv("AUDIO_OUTPUT_DEVICE"))

    logger.info("audiood ready; 按 Ctrl+C 退出")
    loop = asyncio.get_running_loop()
    while True:
        # 阶段占位：真实实现由门铃 GPIO 事件唤醒
        await loop.run_in_executor(None, _wait_for_bell)
        logger.info("门铃触发")

        t0 = time.monotonic()
        pcm = await loop.run_in_executor(None, recorder.record_utterance)
        if not pcm:
            logger.info("没有检测到说话，走固定话术")
            ans = await answer_with_fallback("", None)
            reply_pcm = b""
        else:
            visitor_text = await safe_transcribe(audio, pcm)
            ans = await answer_with_fallback(visitor_text or "", coze)
            reply_pcm, rate = await safe_synthesize(audio, ans.text)
            if reply_pcm:
                await loop.run_in_executor(None, play_pcm, reply_pcm, rate, out_dev)

        logger.info(
            "应答 source=%s action=%s auth=%s cost=%.0fms text=%s",
            ans.source, ans.action, ans.needs_local_auth,
            (time.monotonic() - t0) * 1000, ans.text,
        )
        if ans.needs_local_auth:
            # 只发事件，不做开锁动作 —— 由 lockd 校验后经 HMAC 下发 MCU
            logger.warning("访客请求开锁，已上报待人工确认（不会自动开门）")

        _dump_wav(pcm)


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
