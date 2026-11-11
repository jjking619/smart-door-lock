"""创建或更新 Coze 智能体，并发布到 API 渠道。

## 用法

    # 创建或更新同名智能体 → 发布到 API 渠道 → 把 bot_id 写回配置文件
    .venv/bin/python -m audio.coze_setup --write-env deploy/systemd/audiood.env

    # 只看会做什么，不落盘
    .venv/bin/python -m audio.coze_setup --dry-run

同名智能体已存在时会更新其人设和开场白，不会重复创建；模型配置保持不变。
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

#: Doorstep dialogue should stay brief, clear, and safe.
DEFAULT_PROMPT = """You are a smart door lock intercom assistant speaking with visitors at a residence.

Rules:
1. Use English by default. Reply in the same language as the visitor's latest message, and switch languages when the visitor does. If their language is unclear, use English.
2. Keep each reply polite and brief: one or two short sentences, no more than 30 words total.
3. Ask who the visitor is here to see and what they need.
4. Never reveal whether the resident is home, household details, or private information.
5. If asked to unlock the door or let someone in, say you will notify the homeowner for approval. Never claim the door is unlocked or that you can unlock it.
6. Ask delivery and courier visitors to leave items in the designated place or come back later. Do not invite them inside.
7. If you did not understand, ask the visitor to repeat themselves. Never guess.
8. For maintenance, property management, or emergencies, briefly acknowledge the request and say you will notify the homeowner.
"""

PROLOGUE = os.getenv("COZE_SETUP_PROLOGUE", "Hello, who are you here to see?")


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
        if dry_run:
            logger.info("[dry-run] 将更新同名智能体 %s 并保留其模型配置", existing.bot_id)
            return str(existing.bot_id)
        await coze.bots.update(
            bot_id=str(existing.bot_id),
            prompt_info=cozepy.BotPromptInfo(prompt=DEFAULT_PROMPT),
            onboarding_info=cozepy.BotOnboardingInfo(prologue=PROLOGUE),
        )
        logger.info("已更新智能体提示词和开场白 %s", existing.bot_id)
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
