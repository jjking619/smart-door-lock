"""Coze 连通性自检 —— 填完 token 后**先跑这个**，别直接起服务。

用法：
    # 1) 看看账号下到底有什么（工作空间/工作流/智能体/音色）
    python -m device.audiood.coze_smoke_test --list

    # 2) 验证配置能否跑通（先 export 或 source env 文件）
    python -m device.audiood.coze_smoke_test "我是送快递的，东西放门口了"

排查顺序（报错时按这个查，别猜）：
    token 无效      → 401 / code 4100  → 去 coze.cn/open/oauth/pats 重新生成
    填了名称而非 ID → 本地直接报 CozeConfigError（已提前拦截）
    工作流不存在    → code 4000        → 先 --list 看看有没有内容
    工作流未发布    → code 4200        → console 里点「发布」
"""

from __future__ import annotations

import asyncio
import json
import sys

from .coze_client import (
    COZE_API_TOKEN,
    COZE_BASE_URL,
    CozeConfigError,
    CozeUnavailable,
    CozeWorkflowClient,
    build_async_client,
)


async def _list_resources() -> int:
    """列出账号下的工作空间、工作流、智能体、音色。"""
    if not COZE_API_TOKEN:
        print("[配置错误] COZE_API_TOKEN 未设置")
        return 2
    client = build_async_client()

    try:
        spaces = await client.workspaces.list(page_size=20)
        items = list(spaces.items)
    except Exception as exc:  # noqa: BLE001
        print(f"[token 无效或网络异常] {exc}")
        print("→ 去 https://www.coze.cn/open/oauth/pats 检查 token 是否过期/被撤销")
        return 1

    if not items:
        print("[!] 没有任何工作空间")
        return 1

    for sp in items:
        # cozepy 的 Workspace 字段是 workspace_type / role_type，没有 type
        wtype = getattr(sp, "workspace_type", "") or getattr(sp, "role_type", "")
        print(f"\n工作空间: {sp.name}  id={sp.id}  type={wtype}")

        try:
            wfs = await client.workflows.list(workspace_id=sp.id, page_size=30)
            wf_items = list(wfs.items)
        except Exception as exc:  # noqa: BLE001
            print(f"  工作流列表失败: {exc}")
            wf_items = []

        if wf_items:
            print(f"  工作流 ({len(wf_items)}):")
            for wf in wf_items:
                print(
                    f"    id={wf.workflow_id}  name={wf.name}  "
                    f"status={getattr(wf, 'status', '?')}"
                )
            print("  ↑ 把上面的 id 填到 COZE_WORKFLOW_ID")
        else:
            print("  工作流: 0 个  ← 需要在 console 里新建并「发布」")

        try:
            bots = await client.bots.list(space_id=sp.id, page_size=30)
            bot_items = list(bots.items)
        except Exception:  # noqa: BLE001
            bot_items = []
        if bot_items:
            print(f"  智能体 ({len(bot_items)}):")
            for b in bot_items:
                print(f"    id={b.bot_id}  name={b.name}")
            print("  ↑ 若只有智能体没有工作流，应改用 chat 接口而非 workflows.runs")

    try:
        voices = await client.audio.voices.list(page_size=20)
        all_v = list(voices.items)
        zh = [v for v in all_v if getattr(v, "language_code", "").startswith("zh")]
        print(f"\n可用音色: {len(all_v)} 个（中文 {len(zh)} 个）")
        for v in (zh or all_v)[:3]:
            print(f"  {v.voice_id}  {v.language_code}  {v.name}")
        if zh:
            print(f"  ← 可设 COZE_VOICE_ID={zh[0].voice_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n音色列表失败: {exc}")

    return 0


async def main() -> int:
    if "--list" in sys.argv or "-l" in sys.argv:
        return await _list_resources()

    text = sys.argv[1] if len(sys.argv) > 1 else "你好，我是送快递的，东西放门口了"

    try:
        client = CozeWorkflowClient()
    except CozeConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    except CozeUnavailable as exc:
        print(f"[配置错误] {exc}")
        return 2

    print(f"base_url={COZE_BASE_URL}")
    try:
        reply = await client.ask(text, context={"event": "doorbell", "zone": "doorstep"})
    except CozeUnavailable as exc:
        msg = str(exc)
        print(f"[不可用] {msg}")
        if "4200" in msg:
            print("→ 工作流存在但未发布。在 coze.cn 编辑页点右上角「发布」，再重试。")
        elif "4000" in msg:
            print("→ 参数错误。运行 `--list` 确认真实的 workflow_id。")
        else:
            print("→ 生产环境会走 answer_with_fallback 的本地话术分支")
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
    if reply.action != "reply_only":
        print("\n提示：动作不是 reply_only。若只是问答场景，说明提示词"
              "没按 JSON 约束输出（功能仍可用，但动作会降级）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
