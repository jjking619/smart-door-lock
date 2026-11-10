"""一次性把 Coze 智能体建好并发布到 API 渠道，免去控制台手工操作。

## 为什么非要一个 bot_id

Coze 的 PAT（`COZE_API_TOKEN`）只回答「你是谁」，它里面不含任何对话配置：
没有提示词、没有模型、没有音色、没有渠道。WebSocket 实时语音的每一帧音频
都必须带一个 `bot_id`，服务端才知道要把这句话交给哪个智能体、用哪套人设回。

换句话说，API 不是对话服务本身，而是「操作智能体」的遥控器。
所以「只配 API 就能说话」的完整体验 = 用 API 把 bot 建好、发布好，
之后运行时确实只需要 token + bot_id 两个变量。

## 用法

    # 创建（或复用同名智能体）→ 发布到 API 渠道 → 把 bot_id 写回配置文件
    .venv/bin/python -m audio.coze_setup --write-env deploy/systemd/audiood.env

    # 只看会做什么，不落盘
    .venv/bin/python -m audio.coze_setup --dry-run

幂等：同名的智能体已存在时不会重复创建；已经发布过时 publish 会直接返回。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from pathlib import Path

from .coze_client import COZE_API_TOKEN, COZE_BASE_URL, CozeConfigError, build_async_client

logger = logging.getLogger("coze_setup")

BOT_NAME = os.getenv("COZE_SETUP_BOT_NAME", "门锁访客助手")

#: 门口对讲的人设。刻意写得很短：门口对话要的是快和稳，不是能聊。
DEFAULT_PROMPT = """你是装在住户门口的门锁对讲助手，隔着门和访客说话。

规则：
1. 普通话回答，每次一到两句，单句不超过 30 字，不要长篇大论。
2. 礼貌、简短，先确认来意：找谁、什么事。
3. 绝不透露住户是否在家、门牌内布局、家人姓名电话等隐私。
4. 访客提到「开门」时，只能说「我先帮你通知屋主确认」，绝不承诺已开门，
   也绝不声称自己可以开门。
5. 快递、外卖：请对方放在指定位置或稍后再说，不要让其进入。
6. 听不清就请对方再说一遍，不要猜测。
7. 遇到维修、物业、紧急求助（火警、漏水、有人受伤）时，记录来意并说明会立刻通知屋主。
"""

PROLOGUE = os.getenv("COZE_SETUP_PROLOGUE", "您好，请问找哪位？")


def _mask(value: str) -> str:
    return "***" if value else "<empty>"


async def _pick_space(coze) -> str:  # noqa: ANN001
    """取账号下第一个空间。个人账号通常只有一个「个人空间」。"""
    page = await coze.workspaces.list(page_size=20)
    async for workspace in page:
        logger.info("使用空间: %s (%s)", workspace.id, getattr(workspace, "name", ""))
        return str(workspace.id)
    raise CozeConfigError("账号下没有任何空间，请先在 coze.cn 创建空间")


async def _find_bot(coze, space_id: str, name: str):  # noqa: ANN001, ANN202
    page = await coze.bots.list(space_id=space_id, page_size=50)
    async for bot in page:
        if getattr(bot, "bot_name", "") == name:
            return bot
    return None


async def ensure_bot(coze, space_id: str, name: str, *, dry_run: bool) -> str:  # noqa: ANN001
    """创建（或复用）智能体，返回 bot_id。"""
    import cozepy  # noqa: PLC0415

    existing = await _find_bot(coze, space_id, name)
    if existing is not None:
        logger.info("已存在同名智能体 %s (published=%s)", existing.bot_id, existing.is_published)
        return str(existing.bot_id)

    if dry_run:
        logger.info("[dry-run] 将创建智能体 %r", name)
        return "0"

    bot = await coze.bots.create(
        space_id=space_id,
        name=name,
        description="门口对讲助手：简短应答、不透露隐私、开锁只上报不执行",
        prompt_info=cozepy.BotPromptInfo(prompt=DEFAULT_PROMPT),
        onboarding_info=cozepy.BotOnboardingInfo(prologue=PROLOGUE),
    )
    logger.info("已创建智能体 bot_id=%s", bot.bot_id)
    return str(bot.bot_id)


async def publish_bot(coze, bot_id: str, *, dry_run: bool) -> None:  # noqa: ANN001
    """发布到 API 渠道（connector 1024）。未发布时 WebSocket 会返回 4101。"""
    if dry_run:
        logger.info("[dry-run] 将发布 bot_id=%s 到 API 渠道 1024", bot_id)
        return
    await coze.bots.publish(bot_id=bot_id, connector_ids=["1024"])
    logger.info("已提交发布到 API 渠道")


async def verify(coze, bot_id: str) -> bool:  # noqa: ANN001
    bot = await coze.bots.retrieve(bot_id=bot_id, is_published=True)
    ok = bool(getattr(bot, "bot_id", ""))
    logger.info("发布校验: bot_id=%s name=%r", getattr(bot, "bot_id", ""), getattr(bot, "name", ""))
    return ok


_ENV_LINE = re.compile(r"(?m)^\s*COZE_BOT_ID\s*=.*$")


def write_env(path: Path, bot_id: str) -> None:
    """把 bot_id 写回 env 文件，缺失则追加；不动其它行、不改权限。"""
    text = path.read_text(encoding="utf-8")
    line = f"COZE_BOT_ID={bot_id}"
    if _ENV_LINE.search(text):
        text = _ENV_LINE.sub(line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")
    logger.info("已写入 %s", path)


async def _run(args: argparse.Namespace) -> int:
    if not COZE_API_TOKEN:
        raise CozeConfigError("COZE_API_TOKEN 未配置，先从 coze.cn 生成 PAT 再运行")
    logger.info("token: %s  base_url: %s", _mask(COZE_API_TOKEN), COZE_BASE_URL)

    coze = build_async_client(COZE_API_TOKEN, COZE_BASE_URL)
    space_id = await _pick_space(coze)
    bot_id = await ensure_bot(coze, space_id, args.name, dry_run=args.dry_run)
    await publish_bot(coze, bot_id, dry_run=args.dry_run)
    if not args.dry_run:
        await verify(coze, bot_id)

    print(f"\nCOZE_BOT_ID={bot_id}")
    if args.write_env and not args.dry_run:
        write_env(Path(args.write_env), bot_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建并发布 Coze 智能体，输出/写入 COZE_BOT_ID")
    parser.add_argument("--name", default=BOT_NAME, help=f"智能体名称，默认 {BOT_NAME!r}")
    parser.add_argument("--write-env", metavar="PATH", help="把 COZE_BOT_ID 写回该 env 文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不创建不发布")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    try:
        return asyncio.run(_run(args))
    except CozeConfigError as exc:
        logger.error("%s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.error("失败: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
