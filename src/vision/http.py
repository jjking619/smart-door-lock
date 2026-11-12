"""visiond 的 HTTP 接口：在线查看 + 事件查询 + 录像下载。

只做局域网访问，**不要直接暴露到公网**。远程看走 go2rtc / WireGuard。
鉴权：生产环境把 `VISION_TOKEN` 设上，所有请求需带 `?token=` 或 `Authorization: Bearer`。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web

from .app import VisionService
from .config import Config

logger = logging.getLogger("visiond.http")

TOKEN = os.getenv("VISION_TOKEN", "")
AUDIO_CONTROL_BASE = os.getenv("AUDIO_CONTROL_BASE", "http://127.0.0.1:8084").rstrip("/")


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

    def _extract_media_rel(path: str | None) -> str | None:
        if not path:
            return None
        parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
        for marker in ("clips", "recordings"):
            if marker in parts:
                return "/".join(parts[parts.index(marker):])
        cleaned = [p for p in parts if p != ".."]
        return "/".join(cleaned) if cleaned else None

    def _guess_ts_from_rel(rel: str) -> str | None:
        parts = rel.split("/")
        if len(parts) < 2:
            return None
        day = parts[-2]
        stem = Path(parts[-1]).stem
        hhmmss, _, millis = stem.partition("_")
        if len(day) != 8 or not day.isdigit() or len(hhmmss) != 6 or not hhmmss.isdigit():
            return None
        try:
            dt = datetime.strptime(day + hhmmss, "%Y%m%d%H%M%S")
            if millis.isdigit():
                dt = dt.replace(microsecond=int(millis[:3]) * 1000)
            return dt.astimezone().isoformat(timespec="milliseconds")
        except ValueError:
            return None

    def _event_index(path: Path) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not path.exists():
            return out
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-5000:]
        except OSError:
            return out
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            rel = _extract_media_rel(item.get("clip"))
            if rel:
                out[rel] = item
        return out

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "camera": svc.camera_device,
            "fps": round(svc.fps, 1),
            "ring": len(svc.ring),
            "preview_clients": svc.preview_clients,
            "detector": svc.detector.backend if svc.detector else None,
            "detector_error": svc.detector_error,
            "package_detector": svc.package_detector.backend if svc.package_detector else None,
            "package_detector_enabled": bool(svc.cfg.package_model_path),
            "package_detector_error": svc.package_detector_error,
            "clips_active": len(svc.clips),
            "recording": svc.recording_state(),
            "paused": bool(getattr(svc, "paused", False)),
            "ui_clients": svc.ui_clients() if hasattr(svc, "ui_clients") else 0,
        })

    async def ui_heartbeat(req: web.Request) -> web.Response:
        """界面每隔几秒续一次租约；active=false 表示界面正常关闭，立即释放租约。"""
        try:
            payload = await req.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400, text="expected JSON {client: string, active?: boolean}")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="expected JSON object")
        client = payload.get("client")
        active = payload.get("active", True)
        if not isinstance(client, str) or not client or len(client) > 64:
            return web.Response(status=400, text="client must be a non-empty string (<=64 chars)")
        if not isinstance(active, bool):
            return web.Response(status=400, text="active must be a boolean")
        svc.ui_heartbeat(client, active)
        return web.json_response({"ok": True, "paused": svc.paused, "lease_s": cfg.ui_lease_s})

    async def recording(req: web.Request) -> web.Response:
        try:
            payload = await req.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400, text="expected JSON {on: boolean}")
        on = payload.get("on") if isinstance(payload, dict) else None
        if not isinstance(on, bool):
            return web.Response(status=400, text="on must be a boolean")
        svc.request_recording(on)
        return web.json_response({"ok": True, "recording": svc.recording_state()})

    async def snapshot(_req: web.Request) -> web.Response:
        if getattr(svc, "paused", False):
            return web.Response(status=503, text="camera paused: no UI connected")
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
            async for chunk in svc.mjpeg_frames():
                await resp.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def events(_req: web.Request) -> web.Response:
        limit = max(1, min(int(_req.query.get("limit", "1000")), 1000))
        path = Path(cfg.data_dir) / "events.jsonl"
        if not path.exists():
            return web.json_response({"items": []})
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        items = []
        for line in lines:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return web.json_response({"items": items[::-1]})

    async def recordings(req: web.Request) -> web.Response:
        limit = max(1, min(int(req.query.get("limit", "200")), 1000))
        kind = req.query.get("type", "all")
        if kind not in ("all", "event", "continuous"):
            return web.Response(status=400, text="type must be all|event|continuous")

        base = Path(cfg.data_dir).resolve()
        event_idx = _event_index(base / "events.jsonl")
        items: list[dict] = []
        roots: list[tuple[str, Path]] = []
        if kind in ("all", "event"):
            roots.append(("event", base / "clips"))
        if kind in ("all", "continuous"):
            roots.append(("continuous", base / "recordings" / "continuous"))

        for rec_kind, root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.mp4"):
                if not path.is_file():
                    continue
                try:
                    rel = path.resolve().relative_to(base).as_posix()
                except ValueError:
                    continue
                st = path.stat()
                ev = event_idx.get(rel)
                item = {
                    "path": rel,
                    "kind": rec_kind,
                    "size": st.st_size,
                    "ts": (ev or {}).get("ts") or _guess_ts_from_rel(rel),
                    "event_type": (ev or {}).get("type"),
                    "severity": (ev or {}).get("severity"),
                    "snapshot": (ev or {}).get("snapshot"),
                    "mtime": st.st_mtime,
                }
                items.append(item)

        def _sort_key(item: dict) -> float:
            ts = item.get("ts")
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    pass
            return float(item.get("mtime") or 0.0)

        items.sort(key=_sort_key, reverse=True)
        for item in items:
            item.pop("mtime", None)
        return web.json_response({"items": items[:limit]})

    async def talk(req: web.Request) -> web.Response:
        try:
            payload = await req.json()
        except Exception:  # noqa: BLE001
            payload = {}

        on = bool(payload.get("on", True))
        try:
            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{AUDIO_CONTROL_BASE}/talk", json={"on": on}) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        return web.Response(status=503, text=f"audiood {resp.status}: {text}")
                    try:
                        return web.json_response(json.loads(text) if text else {"ok": True})
                    except json.JSONDecodeError:
                        return web.json_response({"ok": True, "raw": text})
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.Response(status=503, text=f"audiood unavailable: {exc}")

    async def media(req: web.Request) -> web.StreamResponse:
        """下载录像/快照。用相对路径拼接并校验，防止路径穿越。

        事件里的路径是相对进程 cwd 的（`data/snapshots/20261103/x.jpg`），
        而这里是相对 data_dir 的。两种写法都要认，否则前端拿到的地址必 404。
        候选路径逐个尝试，命中第一个属于 data_dir 且真实存在的文件。
        """
        rel = req.match_info["path"].lstrip("/")
        base = Path(cfg.data_dir).resolve()

        candidates = [rel]
        # 剥掉 data_dir / data 前缀，兼容 "data/snapshots/x.jpg" 这种写法
        for prefix in (cfg.data_dir.rstrip("/") + "/", "data/"):
            if rel.startswith(prefix):
                candidates.append(rel[len(prefix):])
                break

        for cand in candidates:
            target = (base / cand).resolve()
            if not target.is_relative_to(base):   # Python 3.9+
                continue
            if target.is_file():
                return web.FileResponse(target)
        return web.Response(status=404)

    app.router.add_get("/health", health)
    app.router.add_get("/snapshot.jpg", snapshot)
    app.router.add_get("/stream", stream)
    app.router.add_get("/events", events)
    app.router.add_get("/recordings", recordings)
    app.router.add_post("/talk", talk)
    app.router.add_post("/recording", recording)
    app.router.add_post("/ui/heartbeat", ui_heartbeat)
    app.router.add_get("/media/{path:.+}", media)
    return app


async def serve(svc: VisionService) -> None:
    """在 run() 之外并行启动 HTTP；由 app.py 的 main 组合调用。"""
    app = create_app(svc)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.getenv("VISION_HTTP_PORT", "8083"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP 预览已启动 :%d", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
