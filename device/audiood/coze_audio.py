"""Coze 语音能力封装：ASR（语音识别）+ TTS（语音合成）。

补充纠正上一轮的说法：Coze 开放平台**是**提供独立的语音接口的，不需要第三方：

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 语音识别（文件） | `coze.audio.transcriptions.create(file=...)` | wav/ogg/mp3，≤10 MB，≤30 min |
| 语音识别（流式） | `coze.websockets.audio.transcriptions` | 边说边出字 |
| 语音合成 | `coze.audio.speech.create(...)` | 支持 `wav/pcm/ogg_opus/mp3`，可指定采样率 |
| 音色列表 / 复刻 | `coze.audio.voices.list()` / `.clone()` | 门锁自用音色一次配好即可 |
| 实时全双工对话 | `coze.audio.rooms.create(bot_id=...)` | 走火山 RTC，需端上 RTC SDK |

所以"只用 Coze"是可行的：ASR、对话、TTS 都能在同一个 token 下完成。
唯一要注意的是**计费**：语音识别/合成按音频时长与字符计费，实时房间按通话时长计费
（进房即计费，空等 3 分钟也会收费），不要在做实验时反复建房间。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import wave
from dataclasses import dataclass

from .coze_client import CozeUnavailable, build_async_client

logger = logging.getLogger(__name__)

ASR_TIMEOUT_S = float(os.getenv("COZE_ASR_TIMEOUT_S", "10"))
TTS_TIMEOUT_S = float(os.getenv("COZE_TTS_TIMEOUT_S", "8"))
COZE_VOICE_ID = os.getenv("COZE_VOICE_ID", "")


def pcm_to_wav(pcm: bytes, sample_rate: int = 16_000, channels: int = 1) -> bytes:
    """PCM(16 bit) → WAV 字节流。ASR 接口只吃文件，需要包头。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes, expect_rate: int = 16_000) -> tuple[bytes, int]:
    """WAV → (PCM, 采样率)。若采样率不符由调用方重采样（或直接指定 TTS 采样率）。"""
    with wave.open(io.BytesIO(data), "rb") as wf:
        rate = wf.getframerate()
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("仅支持单声道 16 bit WAV")
        if rate != expect_rate:
            logger.debug("wav rate %d != %d，交给播放层重采样", rate, expect_rate)
        return wf.readframes(wf.getnframes()), rate


async def _body_bytes(resp) -> bytes:  # noqa: ANN001 - cozepy FileHTTPResponse
    raw = resp._raw_response  # noqa: SLF001 - SDK 未提供公开读取方法
    try:
        return raw.content
    except Exception:  # noqa: BLE001 - 异步响应可能未读取
        return await raw.aread()


@dataclass(slots=True)
class CozeAudio:
    """ASR / TTS 封装。实例可复用，内部缓存音色 id。"""

    client: object | None = None
    voice_id: str = COZE_VOICE_ID
    sample_rate: int = 16_000

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = build_async_client()
        self._voice_lock = asyncio.Lock()

    async def _ensure_voice(self) -> str:
        if self.voice_id:
            return self.voice_id
        async with self._voice_lock:
            if self.voice_id:
                return self.voice_id
            voices = await self.client.audio.voices.list(page_size=20)
            if not voices.items:
                raise CozeUnavailable("账号下没有可用音色")
            # 优先挑中文系统音色，否则取第一个
            for v in voices.items:
                if getattr(v, "language_code", "").startswith("zh"):
                    self.voice_id = v.voice_id
                    break
            else:
                self.voice_id = voices.items[0].voice_id
            logger.info("使用音色 %s", self.voice_id)
            return self.voice_id

    async def transcribe(self, pcm: bytes, sample_rate: int = 16_000) -> str:
        """短句语音识别。失败抛 CozeUnavailable，由上层降级。"""
        if not pcm:
            return ""
        wav = pcm_to_wav(pcm, sample_rate)
        if len(wav) > 10 * 1024 * 1024:
            raise CozeUnavailable("音频超过 10 MB，应改用流式识别")
        try:
            resp = await asyncio.wait_for(
                self.client.audio.transcriptions.create(file=("audio.wav", wav)),
                timeout=ASR_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise CozeUnavailable("ASR 超时") from exc
        except Exception as exc:  # noqa: BLE001
            raise CozeUnavailable(f"ASR 失败: {exc}") from exc

        text = (getattr(resp, "text", "") or "").strip()
        logger.info("ASR: %s", text)
        return text

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """文本转 PCM，返回 (pcm, 采样率)。"""
        if not text:
            return b"", self.sample_rate
        voice_id = await self._ensure_voice()
        try:
            from cozepy import AudioFormat  # noqa: PLC0415

            resp = await asyncio.wait_for(
                self.client.audio.speech.create(
                    input=text[:1024],  # 接口限制 1024 字节
                    voice_id=voice_id,
                    response_format=AudioFormat.WAV,
                    sample_rate=self.sample_rate,
                ),
                timeout=TTS_TIMEOUT_S,
            )
            data = await _body_bytes(resp)
        except asyncio.TimeoutError as exc:
            raise CozeUnavailable("TTS 超时") from exc
        except Exception as exc:  # noqa: BLE001
            raise CozeUnavailable(f"TTS 失败: {exc}") from exc

        pcm, rate = wav_to_pcm(data, self.sample_rate)
        logger.info("TTS: %d 字 → %d ms @%d Hz", len(text), len(pcm) // 32, rate)
        return pcm, rate


async def safe_transcribe(audio: CozeAudio | None, pcm: bytes) -> str:
    if audio is None:
        return ""
    try:
        return await audio.transcribe(pcm)
    except CozeUnavailable as exc:
        logger.warning("ASR 不可用: %s", exc)
        return ""


async def safe_synthesize(audio: CozeAudio | None, text: str) -> tuple[bytes, int]:
    if audio is None:
        return b"", 16_000
    try:
        return await audio.synthesize(text)
    except CozeUnavailable as exc:
        logger.warning("TTS 不可用: %s", exc)
        return b"", 16_000
