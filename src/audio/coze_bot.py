
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .coze_client import (
    COZE_API_TOKEN,
    COZE_BASE_URL,
    COZE_MAX_TURNS,
    CozeConfigError,
    CozeReply,
    CozeUnavailable,
    build_async_client,
)

logger = logging.getLogger(__name__)

COZE_BOT_ID = os.getenv("COZE_BOT_ID", "")
COZE_BOT_TIMEOUT_S = float(os.getenv("COZE_BOT_TIMEOUT_S", "8.0"))
COZE_USER_ID = os.getenv("COZE_USER_ID", "doorbell")

#: 明确表达"要求开门"的说法。只作为**提示**上报，绝不据此开锁。
_UNLOCK_HINTS = (
    "开门", "开锁", "把门打开", "帮我开", "让我进去", "帮我开门",
    "密码", "钥匙", "怎么进去",
)
_UNLOCK_RE = re.compile("|".join(_UNLOCK_HINTS))


def validate_bot_id(bot_id: str) -> str:
    bid = (bot_id or "").strip()
    if not bid:
        raise CozeConfigError(
            "COZE_BOT_ID 为空。\n"
            "在 coze.cn 打开你的智能体，地址栏形如\n"
            "  https://www.coze.cn/space/<space>/bot/7466xxxxxxxxxxxxxxx\n"
            "末尾那串数字就是 bot_id；也可以运行\n"
            "  python -m audio.coze_smoke_test --list\n"
            "自动列出你账号下的智能体。"
        )
    if not bid.isdigit():
        raise CozeConfigError(
            f"COZE_BOT_ID 必须是纯数字 ID，收到的是 {bid!r}。\n"
            "注意：不要填智能体的**名字**（如 smart_door_lock），要填 URL 里的数字 ID。"
        )
    if len(bid) < 10:
        raise CozeConfigError(f"COZE_BOT_ID 长度异常（{len(bid)} 位），不像有效 ID")
    return bid


@dataclass(slots=True)
class _Turn:
    role: str
    content: str


class CozeBotClient:
    """智能体对话客户端。接口与 CozeWorkflowClient 一致（都有 `ask`），

    所以 `answer_with_fallback` 可以无差别接受二者。
    """

    def __init__(
        self,
        token: str = COZE_API_TOKEN,
        bot_id: str = COZE_BOT_ID,
        base_url: str = COZE_BASE_URL,
        timeout_s: float = COZE_BOT_TIMEOUT_S,
        user_id: str = COZE_USER_ID,
    ) -> None:
        if not token:
            raise CozeConfigError("COZE_API_TOKEN 未配置")
        self._bot_id = validate_bot_id(bot_id)
        self._timeout = timeout_s
        self._user_id = user_id
        self._coze = build_async_client(token, base_url)
        self._history: list[_Turn] = []
        self._fail_streak = 0
        self._open_until = 0.0

    # -- 会话管理 --------------------------------------------------
    def reset(self) -> None:
        """新访客到来时清空上下文。由门铃事件触发。"""
        self._history.clear()

    def _messages(self, text: str) -> list:
        from cozepy import Message  # noqa: PLC0415

        msgs = []
        for t in self._history[-COZE_MAX_TURNS * 2 :]:
            if t.role == "user":
                msgs.append(Message.build_user_question_text(t.content))
            else:
                msgs.append(Message.build_assistant_answer(t.content))
        msgs.append(Message.build_user_question_text(text))
        return msgs

    # -- 熔断 ------------------------------------------------------
    def _guard(self) -> None:
        if time.monotonic() < self._open_until:
            raise CozeUnavailable("circuit open")

    def _record(self, ok: bool) -> None:
        if ok:
            self._fail_streak = 0
            return
        self._fail_streak += 1
        if self._fail_streak >= 3:
            self._open_until = time.monotonic() + 60
            self._fail_streak = 0
            logger.warning("bot circuit opened for 60s")

    # -- 主入口 ----------------------------------------------------
    async def ask(
        self,
        text: str,
        context: dict | None = None,  # noqa: ARG002 - 保持与 workflow 客户端同签名
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> CozeReply:
        self._guard()
        from cozepy import ChatEventType, MessageContentType  # noqa: PLC0415

        started = time.monotonic()
        first_token_ms = 0
        chunks: list[str] = []

        try:
            async with asyncio.timeout(self._timeout):
                stream = self._coze.chat.stream(
                    bot_id=self._bot_id,
                    user_id=self._user_id,
                    additional_messages=self._messages(text),
                    # 关键：不在云端留访客对话
                    auto_save_history=False,
                )
                async for event in stream:
                    etype = event.event

                    if etype == ChatEventType.CONVERSATION_MESSAGE_DELTA:
                        msg = event.message
                        if msg is None:
                            continue
                        # 只取助手正文，忽略工具输出/思考过程
                        if getattr(msg, "role", "assistant") == "user":
                            continue
                        if getattr(msg, "content_type", None) == MessageContentType.AUDIO:
                            continue
                        if msg.content:
                            if not chunks:
                                first_token_ms = int(
                                    (time.monotonic() - started) * 1000
                                )
                            chunks.append(msg.content)
                            if on_delta is not None:
                                await on_delta(msg.content)

                    elif etype == ChatEventType.CONVERSATION_CHAT_FAILED:
                        err = getattr(event.chat, "last_error", None) if event.chat else None
                        raise CozeUnavailable(f"chat failed: {err}")

                    elif etype == ChatEventType.ERROR:
                        raise CozeUnavailable(f"stream error: {event.unknown}")

                    elif etype in (
                        ChatEventType.CONVERSATION_CHAT_COMPLETED,
                        ChatEventType.DONE,
                    ):
                        break

        except asyncio.TimeoutError as exc:
            self._record(False)
            raise CozeUnavailable(f"bot timeout after {self._timeout}s") from exc
        except CozeUnavailable:
            self._record(False)
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权/协议统一降级
            self._record(False)
            msg = str(exc).lower()
            if any(k in msg for k in ("auth", "401", "4000", "invalid")):
                raise CozeConfigError(f"bot 调用失败（配置类）: {exc}") from exc
            raise CozeUnavailable(f"bot 调用失败: {exc}") from exc

        reply_text = "".join(chunks).strip()
        latency = int((time.monotonic() - started) * 1000)

        if not reply_text:
            self._record(False)
            raise CozeUnavailable("bot 返回空内容（检查智能体是否已发布、是否配了模型）")

        self._record(True)
        self._history.append(_Turn("user", text))
        self._history.append(_Turn("assistant", reply_text))
        del self._history[: -COZE_MAX_TURNS * 2]

        action, needs_auth = self._infer_action(text, reply_text)
        logger.info(
            "bot ok first_token=%dms total=%dms action=%s",
            first_token_ms, latency, action,
        )
        return CozeReply(
            text=reply_text[:500],
            action=action,
            args={"bot_id": self._bot_id},
            needs_local_auth=needs_auth,
            latency_ms=latency,
            raw=reply_text,
        )

    # -- 动作推断 --------------------------------------------------
    @staticmethod
    def _infer_action(user_text: str, reply_text: str) -> tuple[str, bool]:
        """Bot 输出是自然语言，没有 JSON 契约，所以动作只能**保守推断**。

        原则：
          · 默认 reply_only，什么都不做。
          · 命中"要求开门"的说法 → request_unlock，且 needs_local_auth 恒为 True。
          · 这条路径**不会**导致开锁，只是让 lockd 知道"有人想进"。
        """
        hit = bool(_UNLOCK_RE.search(user_text))
        if not hit:
            return "reply_only", False
        logger.warning("访客提到开门相关词，标记待授权（不会自动开锁）: %s", user_text[:60])
        return "request_unlock", True


# ------------------------------------------------------------------ 测试用

async def _self_check() -> int:
    """不联网的自检：只验证配置解析与动作推断。"""
    from .coze_client import CozeConfigError as CfgErr

    cases = [
        ("", "空 bot_id"),
        ("smart_door_lock", "填了名字"),
        ("123", "长度异常"),
    ]
    for val, desc in cases:
        try:
            validate_bot_id(val)
            print(f"[FAIL] {desc} 应该报错却没报")
        except CfgErr as exc:
            print(f"[OK] {desc} → {str(exc).splitlines()[0]}")

    for text, expect in [
        ("你好", "reply_only"),
        ("帮我开一下门", "request_unlock"),
        ("快递放门口了", "reply_only"),
    ]:
        action, needs = CozeBotClient._infer_action(text, "")
        flag = "OK" if action == expect else "FAIL"
        print(f"[{flag}] {text!r} → {action} needs_auth={needs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_self_check()))
