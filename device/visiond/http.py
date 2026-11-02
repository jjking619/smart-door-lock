"""visiond 的 HTTP 接口：在线查看 + 事件查询 + 录像下载。

只做局域网访问，**不要直接暴露到公网**。远程看走 go2rtc / WireGuard。
鉴权：生产环境把 `VISION_TOKEN` 设上，所有请求需带 `?token=` 或 `Authorization: Bearer`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from .app import VisionService
from .config import Config

logger = logging.getLogger("visiond.http")

TOKEN = os.getenv("VISION_TOKEN", "")


def _authorized(request: web.Request) -> bool:
    if not TOKEN:
        return True
    supplied = request.query.get("token") or ""
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:]
    return supplied == TOKEN


@web.middleware
async def auth_mw(request: web.Request, handler):
    if request.path == "/health" or _authorized(request):
        return await handler(request)
    return web.Response(status=401, text="unauthorized")


def create_app(svc: VisionService) -> web.Application:
    app = web.Application(middlewares=[auth_mw])
    cfg: Config = svc.cfg

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "fps": round(svc.fps, 1),
            "ring": len(svc.ring),
            "preview_clients": svc.preview_clients,
            "detector": svc.detector.backend if svc.detector else None,
            "clips_active": len(svc.clips),
        })

    async def snapshot(_req: web.Request) -> web.Response:
        frame = svc.ring.latest()
        if frame is None:
            return web.Response(status=503, text="no frame yet")
        return web.Response(body=frame.jpeg, content_type="image/jpeg")

    async def stream(_req: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store",
            },
        )
        await resp.prepare(_req)
        try:
            for chunk in svc.mjpeg_frames():
                await resp.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def events(_req: web.Request) -> web.Response:
        limit = int(_req.query.get("limit", "50"))
        path = Path(cfg.data_dir) / "events.jsonl"
        if not path.exists():
            return web.json_response({"items": []})
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 500)):]
        items = []
        for line in lines:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return web.json_response({"items": items[::-1]})

    async def media(req: web.Request) -> web.StreamResponse:
        """下载录像/快照。用相对路径拼接并校验，防止路径穿越。"""
        rel = req.match_info["path"]
        base = Path(cfg.data_dir).resolve()
        target = (base / rel).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            return web.Response(status=404)
        return web.FileResponse(target)

    app.router.add_get("/health", health)
    app.router.add_get("/snapshot.jpg", snapshot)
    app.router.add_get("/stream", stream)
    app.router.add_get("/events", events)
    app.router.add_get("/media/{path:.+}", media)
    return app


async def serve(svc: VisionService) -> None:
    """在 run() 之外并行启动 HTTP；由 app.py 的 main 组合调用。"""
    app = create_app(svc)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.getenv("VISION_HTTP_PORT", "8081"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP 预览已启动 :%d", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
