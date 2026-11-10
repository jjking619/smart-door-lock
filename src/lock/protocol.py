"""M2 → MCU 的开锁帧格式（design.md §4）。

现在板子上没有 ESP32，这个模块只在换成 MCU 执行单元后才会被用到，
但帧格式先定下来，`drivers.McuDriver` 才有明确的契约可依。

    AA 55 | ver(1) | cmd(1) | seq(1) | len(2, 大端) | payload | hmac(32) | crc16(2, 大端)

* `crc16` 覆盖 frame 头 + payload + hmac，防止串口误码；
* `hmac` 覆盖 payload，MCU 用它校验"这条命令确实来自本机 lockd"；
* `payload` 是紧凑 JSON —— 可读性换调试效率，字段少，体积可忽略。
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import json
import struct
import time
import uuid

SOF = b"\xAA\x55"
VERSION = 1
HEADER_LEN = 7          # SOF(2) + ver + cmd + seq + len(2)
HMAC_LEN = 32
CRC_LEN = 2

CMD_UNLOCK = 0x01
CMD_LOCK = 0x02
CMD_STATUS = 0x03


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE：init 0xFFFF，多项式 0x1021，不做反转。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def unlock_payload(user: str, method: str, *, now: float | None = None,
                   nonce: str | None = None) -> bytes:
    body = {
        "user": user,
        "method": method,
        "ts": int((now if now is not None else time.time()) * 1000),
        "nonce": nonce or uuid.uuid4().hex,
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_frame(cmd: int, seq: int, payload: bytes, key: bytes) -> bytes:
    header = SOF + bytes((VERSION, cmd & 0xFF, seq & 0xFF)) + struct.pack(">H", len(payload))
    signature = hmac_module.new(key, payload, hashlib.sha256).digest()
    body = header + payload + signature
    return body + struct.pack(">H", crc16(body))


def parse_frame(data: bytes, key: bytes) -> dict:
    """解析并校验一帧；任何不合法都抛 ValueError，调用方只管丢包重试。"""
    if len(data) < HEADER_LEN + HMAC_LEN + CRC_LEN:
        raise ValueError("帧太短")
    if not data.startswith(SOF):
        raise ValueError("帧头错误")
    version, cmd, seq = data[2], data[3], data[4]
    (length,) = struct.unpack(">H", data[5:7])
    end = HEADER_LEN + length + HMAC_LEN
    if version != VERSION:
        raise ValueError(f"协议版本不支持：{version}")
    if len(data) < end + CRC_LEN:
        raise ValueError("帧长度不足")
    payload_end = HEADER_LEN + length
    body_end = payload_end + HMAC_LEN
    body, signature = data[:body_end], data[payload_end:body_end]
    (crc,) = struct.unpack(">H", data[body_end:body_end + CRC_LEN])
    if crc != crc16(body):
        raise ValueError("CRC 校验失败")
    payload = data[HEADER_LEN:HEADER_LEN + length]
    if not hmac_module.compare_digest(
        signature, hmac_module.new(key, payload, hashlib.sha256).digest()
    ):
        raise ValueError("HMAC 校验失败")
    return {"cmd": cmd, "seq": seq, "payload": json.loads(payload.decode("utf-8"))}
