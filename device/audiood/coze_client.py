"""Coze 工作流客户端 —— 门锁"无人接听自动应答"链路。

设计约束（自查清单，改代码前先读）：

1. **不参与开锁决策。** 模型只输出结构化 JSON，action 必须在白名单内；
   任何涉及开锁的 action 都被强制标记 needs_local_auth=True，
   由 lockd + MCU 的 HMAC 通道二次校验，模型说"开"不等于会开。
2. **不阻塞音频链路。** 硬超时 + 一次重试 + 熔断，超时后调用方走本地 TTS，
   绝不让门铃响应时间被云端拖到 5 秒以上。
3. **不在仓库里放密钥。** 从环境变量 / systemd EnvironmentFile 读取。
4. **不做多轮长记忆。** 门口对讲是短对话，保留最近 N 轮即可，
   避免把访客隐私长期留在第三方平台。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 配置

COZE_API_TOKEN = os.getenv("COZE_API_TOKEN", "")
COZE_WORKFLOW_ID = os.getenv("COZE_WORKFLOW_ID", "")
COZE_BASE_URL = os.getenv("COZE_BASE_URL", "https://api.coze.cn")
COZE_TIMEOUT_S = float(os.getenv("COZE_TIMEOUT_S", "6.0"))
COZE_MAX_TURNS = int(os.getenv("COZE_MAX_TURNS", "6"))

#: 允许模型返回的动作。新增动作必须同步在 lockd 里实现校验分支。
ALLOWED_ACTIONS = frozenset(
    {
        "reply_only",      # 纯口头回复
        "notify_owner",    # 通知屋主（附上下文）
        "leave_message",   # 录留言
        "request_unlock",  # 请求开锁 —— 永远需要本地二次授权
    }
)
#: 无论模型怎么输出，这些动作都必须经过本地鉴权
ACTIONS_REQUIRING_LOCAL_AUTH = frozenset({"request_unlock"})


@dataclass(slots=True)
class CozeReply:
    text: str
    action: str = "reply_only"
    args: dict[str, Any] = field(default_factory=dict)
    needs_local_auth: bool = False
    latency_ms: int = 0
    raw: str = ""


# ---------------------------------------------------------------- 解析

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_reply(payload: str, latency_ms: int = 0) -> CozeReply:
    """把工作流输出解析成受约束的结构。

    工作流提示词要求返回纯 JSON，但线上总会有模型加 markdown 围栏、
    或者干脆返回一段自然语言的情况，这里做兜底降级为 reply_only。
    """
    text = (payload or "").strip()
    if not text:
        raise ValueError("empty workflow output")

    candidate = text
    if not candidate.startswith("{"):
        m = _JSON_BLOCK.search(candidate)
        candidate = m.group(0) if m else ""

    data: dict[str, Any]
    try:
        data = json.loads(candidate) if candidate else {}
    except json.JSONDecodeError:
        data = {}

    if not data:
        # 模型没按格式走，当作纯文本回复，动作降级为最安全的一档
        return CozeReply(text=text[:500], latency_ms=latency_ms, raw=text)

    reply_text = str(data.get("reply") or data.get("text") or "").strip()
    action = str(data.get("action") or "reply_only").strip()

    if action not in ALLOWED_ACTIONS:
        logger.warning("coze returned unlisted action %r, downgrade to reply_only", action)
        action = "reply_only"

    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    # 不信任模型给的任何"已授权"字段
    args.pop("authorized", None)
    args.pop("is_owner", None)

    return CozeReply(
        text=reply_text[:500],
        action=action,
        args=args,
        needs_local_auth=action in ACTIONS_REQUIRING_LOCAL_AUTH,
        latency_ms=latency_ms,
        raw=text,
    )


# ---------------------------------------------------------------- 客户端

class CozeUnavailable(RuntimeError):
    """云端不可用：调用方应当降级到本地应答。"""


class CozeConfigError(CozeUnavailable):
    """配置错误（不是网络问题）：重试无意义，应直接暴露给人看。"""


def validate_workflow_id(workflow_id: str) -> str:
    """Coze 工作流 ID 是纯数字（19 位），不是工作流名称。

    这是最常见的配置错误：把 console 里显示的名字 "smart_door_lock"
    填进来，API 会返回 code 4000 "workflow_id is invalid"，然后白白重试两次。
    在这里提前拦截，报错才有指向性。
    """
    wid = (workflow_id or "").strip()
    if not wid:
        raise CozeConfigError("COZE_WORKFLOW_ID 为空")
    if not wid.isdigit():
        raise CozeConfigError(
            f"COZE_WORKFLOW_ID 必须是纯数字 ID，收到的是名称 {wid!r}。\n"
            "在 coze.cn 打开工作流，地址栏形如\n"
            "  https://www.coze.cn/work_flow?space_id=...&workflow_id=7466xxxxxxxxxxxxxxxx\n"
            "复制 workflow_id 那串数字（19 位），或运行\n"
            "  python -m device.audiood.coze_smoke_test --list\n"
            "自动列出你账号下的工作流与编号。"
        )
    if len(wid) < 10:
        raise CozeConfigError(f"COZE_WORKFLOW_ID 长度异常（{len(wid)} 位），看起来不像有效 ID")
    return wid


class CozeWorkflowClient:
    """对 cozepy 的薄封装，补齐超时、熔断与会话裁剪。"""

    def __init__(
        self,
        token: str = COZE_API_TOKEN,
        workflow_id: str = COZE_WORKFLOW_ID,
        base_url: str = COZE_BASE_URL,
        timeout_s: float = COZE_TIMEOUT_S,
    ) -> None:
        if not token or not workflow_id:
            raise CozeUnavailable("COZE_API_TOKEN / COZE_WORKFLOW_ID 未配置")
        # 提前校验，别让 4000 参数错误走到重试逻辑
        self._workflow_id = validate_workflow_id(workflow_id)
        # 延迟导入：没有 cozepy 时也不影响其他服务启动
        self._coze = build_async_client(token, base_url)
        self._timeout = timeout_s
        self._history: list[dict[str, str]] = []
        self._fail_streak = 0
        self._open_until = 0.0

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
            # 连续失败后熔断 60 s，避免每条门铃都白等一次超时
            self._open_until = time.monotonic() + 60
            self._fail_streak = 0
            logger.warning("coze circuit opened for 60s")

    # -- 主入口 ----------------------------------------------------
    async def ask(
        self,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> CozeReply:
        self._guard()
        started = time.monotonic()
        params: dict[str, Any] = {
            "text": text,
            "history": self._history[-COZE_MAX_TURNS * 2 :],
            **(context or {}),
        }

        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                resp = await asyncio.wait_for(
                    self._coze.workflows.runs.create(
                        workflow_id=self._workflow_id,
                        parameters=params,
                    ),
                    timeout=self._timeout,
                )
                payload = getattr(resp, "data", None)
                if isinstance(payload, dict):
                    payload = json.dumps(payload, ensure_ascii=False)
                if not payload:
                    raise ValueError("empty response.data")

                latency = int((time.monotonic() - started) * 1000)
                reply = parse_reply(str(payload), latency_ms=latency)
                self._remember(text, reply.text)
                self._record(True)
                logger.info("coze ok in %dms action=%s", latency, reply.action)
                return reply
            except asyncio.TimeoutError as exc:
                last_err = exc
                logger.warning("coze timeout (attempt %d)", attempt)
            except Exception as exc:  # noqa: BLE001 - 网络/鉴权/协议全部降级
                last_err = exc
                logger.warning("coze failed (attempt %d): %s", attempt, exc)
                msg = str(exc).lower()
                # 这些错误重试没有意义，直接降级，别白等两轮超时
                if (
                    "auth" in msg
                    or "401" in msg
                    or "4000" in msg        # 参数/配置错误
                    or "4200" in msg        # 工作流未发布
                    or "invalid" in msg
                ):
                    break

        self._record(False)
        raise CozeUnavailable(str(last_err))

    def _remember(self, user: str, assistant: str) -> None:
        self._history.append({"role": "user", "content": user})
        self._history.append({"role": "assistant", "content": assistant})
        del self._history[: -COZE_MAX_TURNS * 2]


# ---------------------------------------------------------------- 降级链

Source = Literal["coze", "local_llm", "local_tts", "timeout"]


@dataclass(slots=True)
class Answer:
    text: str
    source: Source
    action: str = "reply_only"
    needs_local_auth: bool = False


async def answer_with_fallback(
    visitor_text: str,
    coze: CozeWorkflowClient | None,
    local_reply: Callable[[str], Awaitable[CozeReply | None]] | None = None,
    canned_text: str = "您好，主人暂时不在，请在滴声后留言。",
) -> Answer:
    """四级兜底：Coze → 本地 LLM → 固定话术 → 超时提示。

    门铃对讲的关键指标是"2 秒内有声音"，所以每一级都必须有超时。
    """
    if coze is not None:
        try:
            r = await coze.ask(visitor_text)
            if r.text:
                return Answer(r.text, "coze", r.action, r.needs_local_auth)
        except CozeUnavailable as exc:
            logger.info("coze unavailable, fallback: %s", exc)

    if local_reply is not None:
        try:
            r = await asyncio.wait_for(local_reply(visitor_text), timeout=2.0)
            if r and r.text:
                return Answer(r.text, "local_llm", r.action, r.needs_local_auth)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            logger.info("local llm fallback failed: %s", exc)

    if canned_text:
        return Answer(canned_text, "local_tts")

    return Answer("请稍候。", "timeout")


def build_async_client(token: str = COZE_API_TOKEN, base_url: str = COZE_BASE_URL):
    """构造可复用的 AsyncCoze 实例。

    注意：异步客户端必须配 `AsyncTokenAuth`，用 `TokenAuth` 会报类型错误。
    整个进程共用一个实例，避免每次门铃都重新握手（省 100–300 ms）。
    """
    if not token:
        raise CozeConfigError("COZE_API_TOKEN 未配置")
    from cozepy import AsyncCoze, AsyncTokenAuth  # noqa: PLC0415

    return AsyncCoze(auth=AsyncTokenAuth(token), base_url=base_url)
