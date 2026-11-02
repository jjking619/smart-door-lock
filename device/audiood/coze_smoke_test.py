"""Coze 工作流连通性自检（先跑通再改业务代码，能省掉一半排查时间）。

用法：
    export COZE_API_TOKEN=pat_xxx
    export COZE_WORKFLOW_ID=7466xxxxxxxxxxxxxxxxx
    python -m device.audiood.coze_smoke_test "门口有人快递"

期望：打印 latency / action / text。若 action 不是 reply_only 而工作流
只是问答场景，说明提示词没按 JSON 约束输出，属正常降级。
"""

from __future__ import annotations

import asyncio
import json
import sys

from .coze_client import CozeUnavailable, CozeWorkflowClient


async def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "你好，我是送快递的，东西放门口了"
    try:
        client = CozeWorkflowClient()
    except CozeUnavailable as exc:
        print(f"[配置错误] {exc}")
        print("请先 export COZE_API_TOKEN / COZE_WORKFLOW_ID")
        return 2

    try:
        reply = await client.ask(text, context={"event": "doorbell", "zone": "doorstep"})
    except CozeUnavailable as exc:
        print(f"[不可用] {exc}")
        print("→ 生产环境应走 answer_with_fallback 的本地话术分支")
        return 1

    print(json.dumps(
        {
            "latency_ms": reply.latency_ms,
            "action": reply.action,
            "needs_local_auth": reply.needs_local_auth,
            "text": reply.text,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
