"""lockd 的 HTTP 客户端（服务端之间用，不涉及 Qt）。

fingerprintd 匹配成功后用它把结果交给 lockd —— 识别与开锁仍然是两个进程，
fingerprintd 自己不开锁，也不会因为 lockd 挂了而卡住：超时即放弃，识别继续。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("lockd.client")


def lock_base(port: int | str | None = None) -> str:
    import os

    return f"http://127.0.0.1:{int(port or os.getenv('LOCK_HTTP_PORT', '8086'))}"


def request_unlock(payload: dict, *, port: int | str | None = None,
                   timeout: float = 15.0) -> dict:
    """POST /unlock；任何异常都转成 `{"ok": False, "error": ...}`，调用方不必 try。"""
    url = lock_base(port) + "/unlock"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        return json.loads(body.decode("utf-8")) if body else {"ok": False, "error": "空响应"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        logger.warning("lockd 拒绝请求：HTTP %s %s", exc.code, detail)
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        logger.warning("无法连接 lockd：%s", exc)
        return {"ok": False, "error": f"lockd 不可用：{exc}"}


def health(*, port: int | str | None = None, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(lock_base(port) + "/health", timeout=timeout) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None
