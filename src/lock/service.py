"""开锁决策：谁能开、什么时候开、开多久、事后能不能查。

这个模块**不认识 GPIO，也不认识指纹协议**，只认 `LockDriver` 和一条"请求"。
好处是换锁体只改驱动；换识别方式（指纹 → 人脸 → 密码）只改请求来源。

策略（全部可在环境变量里改，默认值见 `Policy`）：

| 策略 | 默认 | 为什么 |
| --- | --- | --- |
| 只有已登记用户能开 | 开 | AS608 里可能有别人手工录入的裸模板，不能凭"匹配成功"就开门 |
| 分数门槛 | ≥ 60 | AS608 一般 50 分以上就能认，60 把偶发误识挡掉 |
| 同人防重复 | 5 s | 手指没抬起会被反复匹配，不防就会连续开锁 |
| 连续失败锁定 | 5 次 → 60 s | 有人蹲在门口一个个试指纹 |
| 开锁存证 | 抓拍一张 | 事后能查"几点、谁、几分" |
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from aiohttp import web

from event_log import append_event
from lock import drivers

logger = logging.getLogger("lockd")

HTTP_PORT = int(os.getenv("LOCK_HTTP_PORT", "8086"))


@dataclass
class Policy:
    require_registered: bool = True
    min_score: int = 60
    repeat_guard_s: float = 5.0
    max_failures: int = 5
    lockout_s: float = 60.0
    pulse_s: float = 5.0

    @classmethod
    def from_env(cls) -> "Policy":
        return cls(
            require_registered=os.getenv("LOCK_REQUIRE_REGISTERED", "1") != "0",
            min_score=int(os.getenv("LOCK_MIN_SCORE", "60")),
            repeat_guard_s=float(os.getenv("LOCK_REPEAT_GUARD_S", "5")),
            max_failures=int(os.getenv("LOCK_MAX_FAILURES", "5")),
            lockout_s=float(os.getenv("LOCK_LOCKOUT_S", "60")),
            pulse_s=float(os.getenv("LOCK_PULSE_S", "5")),
        )


class LockService:
    def __init__(
        self,
        data_dir: str | Path = "data",
        driver: drivers.LockDriver | None = None,
        policy: Policy | None = None,
        clock=time.monotonic,
        snapshot_url: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.users_path = self.data_dir / "fingerprint" / "users.json"
        self.policy = policy or Policy.from_env()
        self.driver = driver or drivers.MockDriver(self.policy.pulse_s)
        self.snapshot_url = snapshot_url if snapshot_url is not None else os.getenv(
            "LOCK_SNAPSHOT_URL", f"http://127.0.0.1:{os.getenv('VISION_HTTP_PORT', '8083')}/snapshot.jpg"
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._unlock_lock = threading.Lock()
        self._last_unlock: dict[str, float] = {}
        self._failures = 0
        self._locked_until = 0.0
        self._unlocked_until = 0.0
        self._last_result: dict[str, Any] | None = None
        self._unlock_count = 0
        self._reject_count = 0

    # ---------------- 只读状态 ----------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            locked_out = self._locked_until > now
            driver_status = self.driver.status()
            state = "unlocked" if self._unlocked_until > now else "locked"
            if driver_status.get("error"):
                state = "fault"
            return {
                "service": "lockd",
                "state": state,
                "locked_out": locked_out,
                "locked_out_remaining": round(self._locked_until - now, 1) if locked_out else 0.0,
                "failures": self._failures,
                "unlock_count": self._unlock_count,
                "reject_count": self._reject_count,
                "last_result": dict(self._last_result) if self._last_result else None,
                "policy": {
                    "require_registered": self.policy.require_registered,
                    "min_score": self.policy.min_score,
                    "repeat_guard_s": self.policy.repeat_guard_s,
                    "max_failures": self.policy.max_failures,
                    "lockout_s": self.policy.lockout_s,
                    "pulse_s": self.policy.pulse_s,
                },
                "driver": driver_status,
            }

    def registered_users(self) -> dict[int, dict[str, str]]:
        try:
            raw = json.loads(self.users_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.error("读取已登记用户失败：%s", exc)
            return {}
        users: dict[int, dict[str, str]] = {}
        for item in raw if isinstance(raw, list) else []:
            try:
                users[int(item["id"])] = {
                    "name": str(item.get("name") or ""),
                    "finger": str(item.get("finger") or ""),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return users

    # ---------------- 决策 ----------------
    def request_unlock(self, *, user_id: int | None = None, name: str = "",
                       score: int = 0, source: str = "finger", method: str = "指纹") -> dict[str, Any]:
        """`source`：finger / ui / remote。返回结构化结果，绝不抛异常给调用方。"""
        with self._lock:
            now = self._clock()
            if self._locked_until > now:
                return self._reject(
                    f"连续失败过多，已锁定，{int(self._locked_until - now) + 1} 秒后再试",
                    source=source, user_id=user_id, name=name, reason_code="locked_out",
                )

            # 本机面板二次确认不携带指纹 ID；指纹请求仍必须校验登记与分数。
            if self.policy.require_registered and source != "ui":
                users = self.registered_users()
                if user_id is None or user_id not in users:
                    return self._reject(
                        "该指纹未在界面登记，不能开门",
                        source=source, user_id=user_id, name=name, reason_code="unregistered",
                    )
                name = name or users[user_id].get("name") or ""
            elif user_id is not None:
                users = self.registered_users()
                name = name or users.get(user_id, {}).get("name", "")

            if source != "ui" and self.policy.min_score and score < self.policy.min_score:
                return self._reject(
                    f"匹配度 {score} 低于门槛 {self.policy.min_score}",
                    source=source, user_id=user_id, name=name, reason_code="low_score",
                )

            guard_key = f"{source}:{user_id}"
            last = self._last_unlock.get(guard_key)
            if last is not None and now - last < self.policy.repeat_guard_s:
                return self._reject(
                    "刚刚已经开过锁，请稍候", source=source, user_id=user_id,
                    name=name, reason_code="repeat",
                )

            self._failures = 0
            self._last_unlock[guard_key] = now
            self._unlocked_until = now + self.policy.pulse_s
            result = {
                "ok": True,
                "user_id": user_id,
                "name": name,
                "score": score,
                "source": source,
                "method": method,
                "seconds": self.policy.pulse_s,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self._last_result = result

        snapshot = self._capture(snapshot_url=self.snapshot_url)
        failure = self._drive(result)
        if failure is not None:
            # 策略允许了，但锁体没按下去。必须如实告诉界面，不能让用户以为门开了。
            rejected = {
                **result,
                "ok": False,
                "reason": f"锁体未响应：{failure}",
                "reason_code": "driver",
            }
            with self._lock:
                self._last_result = rejected
            self._emit("lock.fault", "critical", user=result.get("name", ""),
                       source=source, reason=str(failure))
            return rejected
        with self._lock:
            self._unlock_count += 1
        self._emit("door.opened", "info", snapshot=snapshot, user=result["name"] or "未登记", **{
            k: result[k] for k in ("user_id", "name", "score", "source", "method", "seconds")
        })
        self._emit("door.closed", "info", user=result.get("name", ""))
        return result

    def _drive(self, result: dict[str, Any]) -> str | None:
        """串行化给锁体通电，避免两次请求交叉开锁。

        调用方（HTTP handler）本身跑在 `asyncio.to_thread` 里，所以这里阻塞
        不会卡住事件循环；阻塞式还有一个好处：通电期间不会有第二次开锁插进来。

        返回 None 表示锁体真的动作了；否则返回给界面看的原因。
        """
        with self._unlock_lock:
            try:
                self.driver.unlock(self.policy.pulse_s)
            except Exception as exc:  # noqa: BLE001 —— 驱动异常不能让服务退出
                logger.exception("驱动锁体失败")
                with self._lock:
                    self._unlocked_until = 0.0
                return str(exc)
        with self._lock:
            self._unlocked_until = 0.0
        return None

    def lock_now(self) -> dict[str, Any]:
        with self._unlock_lock:
            try:
                self.driver.lock()
            except drivers.LockDriverError as exc:
                logger.exception("手动回锁失败")
                result = {"ok": False, "reason_code": "driver", "reason": str(exc)}
                with self._lock:
                    self._last_result = result
                self._emit("lock.fault", "critical", reason=str(exc), source="manual")
                return result
        with self._lock:
            self._unlocked_until = 0.0
            self._last_result = {"ok": True, "source": "manual", "method": "手动上锁"}
        self._emit("door.closed", "info", user="", source="manual")
        return {"ok": True}

    def _reject(self, reason: str, **detail: Any) -> dict[str, Any]:
        self._failures += 1
        self._reject_count += 1
        if self.policy.max_failures and self._failures >= self.policy.max_failures:
            self._locked_until = self._clock() + self.policy.lockout_s
            self._failures = 0
            logger.warning("连续失败达到上限，锁定 %s 秒", self.policy.lockout_s)
        result = {"ok": False, "reason": reason, "at": datetime.now().astimezone().isoformat(timespec="seconds"), **detail}
        self._last_result = result
        self._emit("lock.rejected", "warn", reason=reason, **{
            k: detail[k] for k in ("user_id", "name", "source") if detail.get(k) is not None
        })
        return result

    # ---------------- 存证 ----------------
    def _capture(self, snapshot_url: str | None = None) -> str | None:
        """开锁瞬间向 visiond 要一帧存下来；失败不影响开门。"""
        url = snapshot_url or self.snapshot_url
        if not url:
            return None
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                data = response.read()
            if not data:
                return None
        except (OSError, ValueError, urllib.error.URLError) as exc:
            logger.info("开锁存证抓拍失败（不影响开门）：%s", exc)
            return None
        stamp = datetime.now()
        folder = self.data_dir / "snapshots" / stamp.strftime("%Y%m%d")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{stamp.strftime('%H%M%S_%f')[:-3]}_unlock.jpg"
            path.write_bytes(data)
        except OSError as exc:
            logger.info("写入开锁存证失败：%s", exc)
            return None
        return str(path)

    def _emit(self, kind: str, severity: str, **details: Any) -> None:
        event = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "type": kind,
            "source": "lockd",
            "severity": severity,
            "conf": 0,
            "zone": "access",
            **{k: v for k, v in details.items() if v is not None},
        }
        try:
            append_event(self.data_dir / "events.jsonl", event)
        except OSError:
            logger.exception("无法写入开锁事件")

    def close(self) -> None:
        try:
            with self._unlock_lock:
                self.driver.close()
        except Exception:  # noqa: BLE001
            logger.exception("关闭锁体驱动失败")


# ---------------- HTTP ----------------

def _local(remote: str | None) -> bool:
    if not remote:
        return False
    return remote in ("127.0.0.1", "::1") or remote.startswith("127.") or remote.startswith("::ffff:127.")


def create_app(service: LockService, allow_remote: bool = False) -> web.Application:
    app = web.Application(client_max_size=8 * 1024)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(service.status))

    async def unlock(request: web.Request) -> web.Response:
        if not allow_remote and not _local(request.remote):
            return web.json_response(
                {"ok": False, "error": "只允许本机请求开锁（面板按钮或指纹服务）"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        source = str(payload.get("source") or "ui")
        user_id = payload.get("user_id", payload.get("id"))
        if isinstance(user_id, bool) or not isinstance(user_id, (int, type(None))):
            return web.json_response({"ok": False, "error": "user_id 必须是整数"}, status=400)
        try:
            score = int(payload.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        result = await asyncio.to_thread(
            service.request_unlock,
            user_id=user_id,
            name=str(payload.get("name") or ""),
            score=score,
            source=source,
            method=str(payload.get("method") or ("指纹" if source == "finger" else "界面")),
        )
        # 界面需要看"为什么没开"，所以被拒也返回 200 + ok=false
        return web.json_response(result)

    async def lock(_request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(service.lock_now))

    app.router.add_get("/health", health)
    app.router.add_post("/unlock", unlock)
    app.router.add_post("/lock", lock)
    return app


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    data_dir = Path(os.getenv("VISION_DATA_DIR", "data"))
    # 本机门锁机构是 Pin33 上的 SG90；调试无硬件时显式设置 LOCK_DRIVER=mock。
    kind = os.getenv("LOCK_DRIVER", "servo").strip().lower()
    driver = drivers.build_driver(
        kind,
        pulse=float(os.getenv("LOCK_PULSE_S", "5")),
        chip=os.getenv("LOCK_GPIO_CHIP", "gpiochip0"),
        line=int(os.getenv("LOCK_GPIO_LINE", "0")),
        active_low=os.getenv("LOCK_GPIO_ACTIVE_LOW", "0") == "1",
        port=os.getenv("LOCK_MCU_PORT", ""),
        key=os.getenv("DOORLOCK_MCU_KEY", "").encode(),
        locked_angle=float(os.getenv("LOCK_SERVO_LOCKED_ANGLE", "0")),
        unlocked_angle=float(os.getenv("LOCK_SERVO_UNLOCKED_ANGLE", "90")),
        min_us=float(os.getenv("LOCK_SERVO_MIN_US", "500")),
        max_us=float(os.getenv("LOCK_SERVO_MAX_US", "2500")),
        move_s=float(os.getenv("LOCK_SERVO_MOVE_S", "0.5")),
    )
    if kind == "servo":
        # 启动时先回到回锁位置；PWM 未授权时保持服务在线，让界面显示可操作的原因。
        try:
            driver.lock()
        except drivers.LockDriverError as exc:
            logger.error("舵机初始化失败：%s；lockd 继续运行，但开锁会失败", exc)
    if kind == "gpio":
        problem = drivers.GpioRelayDriver.probe(os.getenv("LOCK_GPIO_CHIP", "gpiochip0"))
        if problem:
            logger.error("GPIO 不可用：%s；lockd 继续运行，但开锁会失败", problem)
    service = LockService(data_dir, driver)
    runner = web.AppRunner(create_app(service), access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(runner, os.getenv("LOCK_HTTP_HOST", "0.0.0.0"), HTTP_PORT)
        await site.start()
        logger.info("lockd HTTP listening on %s:%d, driver=%s, pulse=%.1fs",
                    os.getenv("LOCK_HTTP_HOST", "0.0.0.0"), HTTP_PORT, kind,
                    service.policy.pulse_s)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopped.set)
        await stopped.wait()
    finally:
        await runner.cleanup()
        service.close()
