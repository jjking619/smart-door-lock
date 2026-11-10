"""MQTT 事件总线：向 lockd 广播事件，同时保留本地 JSONL 落盘。

MQTT 断线不影响本地功能：事件永远先写 JSONL，再尝试 publish。
门锁离线时你仍然能在本地翻到"什么时候有人来过"。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from event_log import append_event

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self, host: str = "", port: int = 1883, topic: str = "lock/event",
                 username: str = "", password: str = "", data_dir: str = "data") -> None:
        self.topic = topic
        self._client = None
        self._lock = threading.Lock()
        self._jsonl = os.path.join(data_dir, "events.jsonl")
        os.makedirs(data_dir, exist_ok=True)

        if not host:
            logger.info("未配置 MQTT_HOST，事件仅本地落盘")
            return
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415

            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="visiond")
            if username:
                c.username_pw_set(username, password)
            c.connect_async(host, port, keepalive=30)
            c.loop_start()
            self._client = c
            logger.info("MQTT 连接 %s:%d", host, port)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MQTT 初始化失败（仅本地落盘）: %s", exc)

    def emit(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            try:
                append_event(self._jsonl, event)
            except OSError as exc:
                logger.warning("写事件日志失败: %s", exc)

        if self._client is not None:
            kind = event.get("type", "event")
            try:
                self._client.publish(f"{self.topic}/{kind}", line, qos=1)
            except Exception as exc:  # noqa: BLE001
                logger.debug("publish 失败: %s", exc)

    def close(self) -> None:
        if self._client is not None:
            try:
                time.sleep(0.2)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
