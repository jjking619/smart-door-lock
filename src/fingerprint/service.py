from __future__ import annotations

import asyncio
from datetime import datetime
import glob
import json
import logging
import os
from pathlib import Path
import queue
import signal
import threading
import time
from typing import Any

from aiohttp import web

from event_log import append_event

logger = logging.getLogger("fingerprintd")

HTTP_PORT = int(os.getenv("FINGERPRINT_HTTP_PORT", "8085"))
BAUDRATE = int(os.getenv("FINGERPRINT_BAUDRATE", "57600"))
SERIAL_GLOBS = (
    "/dev/serial/by-id/*",
    "/dev/serial/by-path/*",
)
SERIAL_PORTS = ("/dev/serial0", "/dev/serial1")

ERROR_TEXT = {
    0x01: "模块收包有错",
    0x02: "未检测到手指",
    0x03: "指纹图像采集失败",
    0x04: "指纹太干或太淡，无法生成特征",
    0x05: "指纹太湿或太糊，无法生成特征",
    0x06: "指纹图像太乱，无法生成特征",
    0x07: "有效特征点太少或按压面积太小",
    0x08: "两次采集的指纹不匹配",
    0x09: "未找到匹配指纹",
    0x0A: "两次按压的指纹无法合并",
    0x0B: "模板编号超出指纹库范围",
    0x0C: "读出的指纹模板无效",
    0x0D: "上传特征失败",
    0x0E: "模块不能接收后续数据包",
    0x0F: "上传图像失败",
    0x10: "删除指纹模板失败",
    0x11: "清空指纹库失败",
    0x13: "模块口令不正确",
    0x15: "缓冲区内没有有效原始图像",
    0x18: "模块 FLASH 读写出错",
    0x19: "模块未定义错误",
    0x1A: "无效寄存器号",
    0x1B: "寄存器设定内容错误",
    0x1C: "记事本页码指定错误",
    0x1D: "端口操作失败",
    0x1E: "自动注册失败",
    0x1F: "指纹库已满",
}
OK = 0x00
NOFINGER = 0x02
NOTFOUND = 0x09
MERGE_FAILED = 0x0A
# Per the manual's flowchart these GetImage failures are retried rather than ending the operation.
TRANSIENT_IMAGE_CODES = frozenset({0x01, 0x03})
# GenChar quality failures: ask the user to press again.
RETRY_FEATURE_CODES = frozenset({0x04, 0x05, 0x06, 0x07, 0x15})
MAX_PRESS_ATTEMPTS = 5
MAX_MERGE_ATTEMPTS = 3
POWER_ON_DELAY = 0.2


def describe_code(code: int, action: str) -> str:
    return ERROR_TEXT.get(code, f"{action}失败，错误码 0x{code:02X}")


def _default_unlock_notifier(user_id: int, name: str, score: int) -> dict | None:
    """把识别结果交给 lockd。开关由 FINGERPRINT_UNLOCK=0 关掉（只记录不开门）。

    fingerprintd 自己不做开锁决策：它不认识锁体，也不知道该不该开。
    """
    if os.getenv("FINGERPRINT_UNLOCK", "1") == "0":
        return None
    from lock import client

    if client.health() is None:
        return None
    return client.request_unlock({
        "source": "finger",
        "user_id": user_id,
        "name": name,
        "score": score,
        "method": "AS608",
    })


class FingerprintOperationError(RuntimeError):
    pass


def find_serial_device() -> str | None:
    configured = os.getenv("FINGERPRINT_SERIAL", "").strip()
    if configured:
        return configured
    for pattern in SERIAL_GLOBS:
        devices = sorted(glob.glob(pattern))
        if devices:
            return devices[0]
    for device in SERIAL_PORTS:
        if os.path.exists(device):
            return device
    return None


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class FingerprintService:
    def __init__(
        self,
        data_dir: str | Path = "data",
        serial_factory=None,
        sensor_factory=None,
        serial_resolver=find_serial_device,
        operation_timeout: float = 30,
        unlock_notifier=None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._users_path = self.data_dir / "fingerprint" / "users.json"
        self._users_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._commands: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial_factory = serial_factory
        self._sensor_factory = sensor_factory
        self._serial_resolver = serial_resolver
        self.operation_timeout = operation_timeout
        # 匹配成功后把结果交给 lockd；识别与开锁是两个进程，这里只发请求不决策。
        self._unlock_notifier = unlock_notifier or _default_unlock_notifier
        self._serial = None
        self._sensor = None
        self._template_locations: list[int] = []
        self._users = self._load_users()
        self._state: dict[str, Any] = {
            "connected": False,
            "device": None,
            "error": None,
            "template_count": 0,
            "library_size": 0,
            "action": None,
            "step": "",
            "notice": None,
            "last_match": None,
            "last_unlock": None,
        }

    def _load_users(self) -> dict[int, dict[str, str]]:
        try:
            raw = json.loads(self._users_path.read_text(encoding="utf-8"))
            return {
                int(item["id"]): {
                    "name": str(item["name"]),
                    "finger": str(item.get("finger") or ""),
                    "created_at": str(item.get("created_at") or ""),
                }
                for item in raw
            }
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.error("读取指纹用户映射失败: %s", exc)
            return {}

    def _save_users(self) -> None:
        with self._users_lock:
            rows = [{"id": user_id, **user} for user_id, user in sorted(self._users.items())]
            _atomic_json(self._users_path, rows)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._worker, name="fingerprint-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
        self._close_device()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def templates(self) -> list[dict[str, Any]]:
        status = self.status()
        if not status["connected"]:
            raise RuntimeError("指纹模块未连接")
        locations = list(self._template_locations)
        capacity = int(status["library_size"] or 0)
        with self._users_lock:
            users = dict(self._users)
        return [
            {
                "id": location,
                "name": users.get(location, {}).get("name", ""),
                "finger": users.get(location, {}).get("finger", ""),
                "registered": location in users,
            }
            for location in locations
            if location < capacity
        ]

    def begin_enroll(self, name: str, finger: str) -> None:
        self._begin("enroll", {"name": name, "finger": finger})

    def begin_delete(self, location: int) -> None:
        with self._users_lock:
            if location not in self._users:
                raise ValueError("只能删除已在界面登记的指纹，未登记模板请先通过模块管理")
        self._begin("delete", {"id": location})

    def begin_rename(self, location: int, name: str, finger: str) -> None:
        with self._users_lock:
            if location not in self._users:
                raise ValueError("未找到已登记的指纹")
        self._begin("rename", {"id": location, "name": name, "finger": finger})

    def cancel(self) -> None:
        if self.status()["action"] == "enroll":
            self._cancel.set()

    def _begin(self, action: str, payload: dict[str, Any]) -> None:
        if not self.status()["connected"]:
            raise RuntimeError("指纹模块未连接")
        with self._state_lock:
            if self._state["action"]:
                raise RuntimeError("已有指纹操作正在进行")
            self._state.update(action=action, step="准备中", error=None, notice=None)
        self._cancel.clear()
        self._commands.put((action, payload))

    def _set_state(self, **updates: Any) -> None:
        with self._state_lock:
            self._state.update(updates)

    def _connect(self) -> None:
        device = self._serial_resolver()
        if not device:
            raise RuntimeError("未找到串口；设置 FINGERPRINT_SERIAL=/dev/serial/by-id/...")
        if self._serial_factory is None or self._sensor_factory is None:
            import serial
            from adafruit_fingerprint import Adafruit_Fingerprint

            def serial_factory(port, **kwargs):
                # Exclusive open so a second process can't steal reply bytes.
                return serial.Serial(port, exclusive=True, **kwargs)

            sensor_factory = Adafruit_Fingerprint
        else:
            serial_factory = self._serial_factory
            sensor_factory = self._sensor_factory
        uart = serial_factory(device, baudrate=BAUDRATE, timeout=1)
        try:
            # The manual says the module needs about 0.1 s after power-up; flush stale bytes before talking to it.
            time.sleep(POWER_ON_DELAY)
            self._serial = uart
            self._flush_input()
            sensor = sensor_factory(uart)
            try:
                sensor.check_module()
            except Exception as exc:  # GetEcho (0x53) is not in the AS608 manual; some modules don't support it
                logger.info("AS608 未响应 GetEcho，忽略：%s", exc)
                self._flush_input()
            if sensor.read_templates() != OK:
                raise RuntimeError("读取 AS608 模板列表失败")
            if sensor.count_templates() != OK:
                raise RuntimeError("读取 AS608 模板数量失败")
        except Exception:
            self._serial = None
            uart.close()
            raise
        self._serial = uart
        self._sensor = sensor
        self._template_locations = list(sensor.templates)
        self._set_state(
            connected=True,
            device=device,
            error=None,
            template_count=int(sensor.template_count or 0),
            library_size=int(sensor.library_size or 0),
        )
        logger.info("AS608 connected on %s, templates=%d/%d",
                    device, sensor.template_count, sensor.library_size)

    def _close_device(self) -> None:
        uart, self._serial = self._serial, None
        self._sensor = None
        self._template_locations = []
        if uart is not None:
            try:
                uart.close()
            except OSError:
                logger.exception("关闭指纹串口失败")
        self._set_state(connected=False, device=None)

    def _flush_input(self) -> None:
        uart = self._serial
        reset = getattr(uart, "reset_input_buffer", None)
        if reset is not None:
            reset()

    def _call(self, method: str, *args: Any) -> int:
        """Call the sensor; on a bad or missing reply frame, flush the receive buffer and resend once."""
        assert self._sensor is not None
        try:
            return getattr(self._sensor, method)(*args)
        except RuntimeError as exc:
            logger.warning("AS608 %s 应答异常（%s），清空接收缓冲后重试", method, exc)
            time.sleep(0.05)
            self._flush_input()
            return getattr(self._sensor, method)(*args)

    def _worker(self) -> None:
        while not self._stop.is_set():
            if self._sensor is None:
                try:
                    self._connect()
                except Exception as exc:
                    self._close_device()
                    self._set_state(error=str(exc))
                    logger.warning("AS608 connect failed: %s", exc)
                    self._stop.wait(3)
                    continue
            try:
                try:
                    action, payload = self._commands.get_nowait()
                except queue.Empty:
                    self._scan_once()
                    continue
                if action == "enroll":
                    self._enroll(payload["name"], payload["finger"])
                elif action == "delete":
                    self._delete(int(payload["id"]))
                elif action == "rename":
                    self._rename(int(payload["id"]), payload["name"], payload["finger"])
            except (InterruptedError, TimeoutError, FingerprintOperationError) as exc:
                logger.info("指纹操作已结束：%s", exc)
                self._set_state(error=str(exc), action=None, step="")
                self._cancel.clear()
                if not isinstance(exc, InterruptedError):
                    self._emit("finger.reject", severity="warn", reason=str(exc))
            except Exception as exc:
                logger.exception("AS608 operation failed")
                message = str(exc)
                if isinstance(exc, OSError):
                    message = f"指纹串口通信中断（USB 串口掉线或模块供电不足？），正在重连：{exc}"
                self._set_state(error=message, action=None, step="")
                self._emit("finger.reject", severity="warn", reason=message)
                self._close_device()

    def _scan_once(self) -> None:
        assert self._sensor is not None
        code = self._call("get_image")
        if code == NOFINGER or code in TRANSIENT_IMAGE_CODES:
            return
        if code != OK:
            raise FingerprintOperationError(describe_code(code, "采集指纹"))
        code = self._call("image_2_tz", 1)
        if code != OK:
            self._emit("finger.reject", severity="warn", reason=describe_code(code, "生成指纹特征"))
            self._wait_release()
            return
        try:
            code = self._search()
        except FingerprintOperationError:
            self._wait_release()
            raise
        if code == OK:
            location = int(self._sensor.finger_id)
            with self._users_lock:
                user = dict(self._users.get(location, {}))
            score = int(self._sensor.confidence or 0)
            result = {
                "id": location,
                "name": user.get("name", ""),
                "finger": user.get("finger", ""),
                "confidence": score,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self._set_state(last_match=result, error=None)
            self._emit("finger.match", severity="info", user=result["name"] or "未登记指纹",
                       finger_id=location, score=score, method="AS608")
            self._request_unlock(location, result["name"], score)
        elif code == NOTFOUND:
            self._set_state(error=None, last_match={
                "id": None,
                "name": "",
                "confidence": 0,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            self._emit("finger.reject", severity="warn", reason="未找到匹配指纹", method="AS608")
        else:
            self._wait_release()
            raise FingerprintOperationError(describe_code(code, "指纹搜索"))
        self._wait_release()

    def _request_unlock(self, user_id: int, name: str, score: int) -> None:
        """识别成功后请 lockd 判断能不能开门。识别结果已经落库，开锁失败不影响识别。"""
        try:
            answer = self._unlock_notifier(user_id, name, score)
        except Exception:  # noqa: BLE001 —— 通知链路绝不能打断指纹循环
            logger.exception("请求开锁失败")
            self._set_state(last_unlock={"ok": False, "reason": "请求开锁失败"})
            return
        if answer is None:
            self._set_state(last_unlock={"ok": False, "reason": "未接入开锁服务"})
            return
        ok = bool(answer.get("ok"))
        self._set_state(last_unlock={
            "ok": ok,
            "reason": answer.get("reason") or answer.get("error") or "",
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        if not ok:
            logger.info("指纹匹配但未开锁：%s", answer.get("reason") or answer.get("error"))

    def _search(self) -> int:
        assert self._sensor is not None
        code = self._call("finger_fast_search")
        if code != 0x17:
            return code
        # This module returns undocumented 0x17 even when Match can identify
        # the same captured features. Keep buffer 1 and compare using buffer 2.
        logger.warning("AS608 高速搜索返回 0x17，改用逐模板比对")
        for location in self._template_locations:
            if self._stop.is_set():
                raise InterruptedError("指纹服务正在关闭")
            code = self._call("load_model", location, 2)
            if code != OK:
                raise FingerprintOperationError(
                    f"读取模板 {location} 失败：{describe_code(code, '读取模板')}"
                )
            code = self._call("compare_templates")
            if code == OK:
                confidence = self._sensor.confidence
                # Adafruit 2.2.25 returns a one-element tuple from Match.
                self._sensor.confidence = int(
                    confidence[0] if isinstance(confidence, tuple) else confidence
                )
                self._sensor.finger_id = location
                return OK
            if code != 0x08:
                raise FingerprintOperationError(describe_code(code, "指纹逐模板比对"))
        return NOTFOUND

    def _wait_release(self) -> None:
        assert self._sensor is not None
        deadline = time.monotonic() + 10
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._call("get_image") == NOFINGER:
                return
            time.sleep(0.1)

    def _wait_finger(self, prompt: str) -> None:
        assert self._sensor is not None
        self._set_state(step=prompt)
        deadline = time.monotonic() + self.operation_timeout
        while not self._stop.is_set():
            if self._cancel.is_set():
                raise InterruptedError("已取消录入")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{prompt}超时，请重试")
            code = self._call("get_image")
            if code == OK:
                return
            if code != NOFINGER and code not in TRANSIENT_IMAGE_CODES:
                raise FingerprintOperationError(describe_code(code, prompt))
            time.sleep(0.1)
        raise InterruptedError("指纹服务正在关闭")

    def _wait_no_finger(self, prompt: str = "请移开手指") -> None:
        assert self._sensor is not None
        self._set_state(step=prompt)
        deadline = time.monotonic() + self.operation_timeout
        while not self._stop.is_set():
            if self._cancel.is_set():
                raise InterruptedError("已取消录入")
            if time.monotonic() >= deadline:
                raise TimeoutError("移开手指超时，请重试")
            code = self._call("get_image")
            if code == NOFINGER:
                return
            if code != OK and code not in TRANSIENT_IMAGE_CODES:
                raise FingerprintOperationError(describe_code(code, "检测手指移开"))
            time.sleep(0.1)
        raise InterruptedError("指纹服务正在关闭")

    def _capture(self, slot: int, prompt: str) -> None:
        """GetImage → GenChar(slot); on a poor-quality image, ask the user to press again, as the manual's enroll flowchart does."""
        for _attempt in range(MAX_PRESS_ATTEMPTS):
            self._wait_finger(prompt)
            code = self._call("image_2_tz", slot)
            if code == OK:
                return
            reason = describe_code(code, "生成指纹特征")
            if code not in RETRY_FEATURE_CODES:
                raise FingerprintOperationError(reason)
            logger.info("录入第 %d 次按压特征生成失败：%s", slot, reason)
            self._wait_no_finger(f"{reason}，请移开手指")
            prompt = f"{reason}，请重新按压（第 {slot} 次）"
        raise FingerprintOperationError(f"连续 {MAX_PRESS_ATTEMPTS} 次未采到清晰指纹，请清洁传感器和手指后重试")

    def _enroll(self, name: str, finger: str) -> None:
        assert self._sensor is not None
        try:
            first_prompt = "请按压手指（第 1 次）"
            for _attempt in range(MAX_MERGE_ATTEMPTS):
                self._capture(1, first_prompt)
                self._wait_no_finger("第 1 次采集成功，请移开手指")
                self._capture(2, "请再次按压同一手指（第 2 次）")
                self._set_state(step="正在合并指纹特征")
                code = self._call("create_model")
                if code == OK:
                    break
                if code != MERGE_FAILED:
                    raise FingerprintOperationError(describe_code(code, "合并指纹特征"))
                self._wait_no_finger("两次按压差异较大，请移开手指")
                first_prompt = "两次按压不一致，请重新按压手指（第 1 次）"
            else:
                raise FingerprintOperationError("两次按压的指纹多次无法合并，请用同一手指、相同位置重新录入")
            self._set_state(step="正在保存指纹模板")
            code = self._call("read_templates")
            if code != OK:
                raise FingerprintOperationError("无法读取模块模板列表")
            self._template_locations = list(self._sensor.templates)
            capacity = int(self._sensor.library_size or 0)
            occupied = set(self._sensor.templates)
            location = next((i for i in range(capacity) if i not in occupied), None)
            if location is None:
                raise FingerprintOperationError("指纹库已满")
            code = self._call("store_model", location, 1)
            if code != OK:
                raise FingerprintOperationError(describe_code(code, "保存指纹模板"))
            self._template_locations = sorted(set(self._template_locations) | {location})
            with self._users_lock:
                self._users[location] = {
                    "name": name,
                    "finger": finger,
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            try:
                self._save_users()
            except OSError as exc:
                rollback_code = self._sensor.delete_model(location)
                with self._users_lock:
                    self._users.pop(location, None)
                if rollback_code == OK:
                    self._template_locations = [
                        item for item in self._template_locations if item != location
                    ]
                else:
                    logger.error("用户信息写入失败，AS608 模板 %s 回滚也失败，错误码 0x%02X",
                                 location, rollback_code)
                    raise FingerprintOperationError(
                        f"保存用户信息失败，且模块模板回滚失败（错误码 0x{rollback_code:02X}）"
                    ) from exc
                raise FingerprintOperationError(f"保存用户信息失败：{exc}") from exc
            self._call("count_templates")
            self._set_state(step="录入成功，请移开手指")
            self._wait_release()
            self._set_state(template_count=int(self._sensor.template_count or 0),
                            notice=f"指纹录入成功：{name}（编号 {location}）",
                            last_match={"id": location, "name": name, "finger": finger})
            self._emit("finger.enrolled", severity="info", user=name, finger=finger, finger_id=location)
        finally:
            self._cancel.clear()
            self._set_state(action=None, step="")

    def _delete(self, location: int) -> None:
        assert self._sensor is not None
        code = self._call("delete_model", location)
        if code != OK:
            raise FingerprintOperationError(describe_code(code, "删除模板"))
        self._template_locations = [
            item for item in self._template_locations if item != location
        ]
        with self._users_lock:
            user = self._users.pop(location, {})
        self._sensor.count_templates()
        self._set_state(template_count=int(self._sensor.template_count or 0))
        try:
            self._save_users()
        except OSError as exc:
            raise FingerprintOperationError(f"模板已删除，但用户信息保存失败：{exc}") from exc
        finally:
            self._set_state(action=None, step="")
        code = self._sensor.read_templates()
        if code != OK:
            raise FingerprintOperationError("模板已删除，但无法刷新模块模板列表")
        self._template_locations = list(self._sensor.templates)
        self._emit("finger.deleted", severity="info", user=user.get("name", ""),
                   finger_id=location)

    def _rename(self, location: int, name: str, finger: str) -> None:
        with self._users_lock:
            user = self._users[location]
            previous = dict(user)
            user.update(name=name, finger=finger)
        try:
            self._save_users()
        except OSError:
            with self._users_lock:
                self._users[location] = previous
            raise
        self._set_state(action=None, step="")
        self._emit("finger.renamed", severity="info", user=name, finger=finger,
                   finger_id=location)

    def _emit(self, kind: str, severity: str, **details: Any) -> None:
        event = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "type": kind,
            "source": "fingerprintd",
            "severity": severity,
            "conf": 0,
            "zone": "access",
            **details,
        }
        try:
            append_event(self.data_dir / "events.jsonl", event)
        except OSError:
            logger.exception("无法写入指纹事件")


def create_app(service: FingerprintService) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"service": "fingerprintd", **service.status()})

    async def templates(_request: web.Request) -> web.Response:
        try:
            return web.json_response({"items": service.templates()})
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)

    async def enroll(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "请求需要 JSON"}, status=400)
        name = str(payload.get("name") or "").strip() if isinstance(payload, dict) else ""
        finger = str(payload.get("finger") or "").strip() if isinstance(payload, dict) else ""
        if not name or len(name) > 40 or len(finger) > 40:
            return web.json_response({"error": "姓名必填且不超过 40 个字符，手指备注不超过 40 个字符"}, status=400)
        try:
            service.begin_enroll(name, finger)
        except (RuntimeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response({"ok": True}, status=202)

    async def delete(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            location = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(location, bool) or not isinstance(location, int) or not 0 <= location <= 65535:
                raise ValueError
        except (json.JSONDecodeError, ValueError, TypeError):
            return web.json_response({"error": "id 必须为有效整数"}, status=400)
        try:
            service.begin_delete(location)
        except (RuntimeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response({"ok": True}, status=202)

    async def rename(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "请求需要 JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "请求需要 JSON 对象"}, status=400)
        location, name = payload.get("id"), str(payload.get("name") or "").strip()
        finger = str(payload.get("finger") or "").strip()
        if isinstance(location, bool) or not isinstance(location, int) or not name or len(name) > 40 or len(finger) > 40:
            return web.json_response({"error": "id 或姓名/手指备注无效"}, status=400)
        try:
            service.begin_rename(location, name, finger)
        except (RuntimeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response({"ok": True}, status=202)

    async def cancel(_request: web.Request) -> web.Response:
        service.cancel()
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    app.router.add_get("/templates", templates)
    app.router.add_post("/enroll", enroll)
    app.router.add_post("/delete", delete)
    app.router.add_post("/rename", rename)
    app.router.add_post("/cancel", cancel)
    return app


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    root = Path(os.getenv("FINGERPRINT_DATA_DIR", "data"))
    service = FingerprintService(root)
    service.start()
    runner = web.AppRunner(create_app(service), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HTTP_PORT)
    await site.start()
    logger.info("fingerprintd HTTP listening on 127.0.0.1:%d", HTTP_PORT)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await stopped.wait()
    finally:
        service.stop()
        await runner.cleanup()
