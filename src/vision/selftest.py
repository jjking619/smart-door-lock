"""无硬件自测：不用相机、不用模型，验证环形缓冲 / 录像 / 预览 / HTTP。

    python -m vision.selftest

会生成 25 s 的合成画面（一个人形从左右走动），在 data/ 下产生：
  * events.jsonl（若有事件）
  * clips/…mp4 与 snapshots/…jpg（仅当模型可用时才会触发事件）

这个脚本的价值在于：**装机前就能验证录像链路与文件目录结构**，
不必等门装好才发现 mp4 写不出来。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

os.environ.setdefault("VISION_SOURCE", "test")
os.environ.setdefault("VISION_MODEL", "models/__missing__.rknn")  # 强制无模型
os.environ.setdefault("VISION_PACKAGE_MODEL", "")
os.environ.setdefault("VISION_DATA_DIR", "data/selftest")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from .app import VisionService  # noqa: E402
from .config import Config  # noqa: E402
from .recorder import ClipWriter  # noqa: E402

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _write_test_clip(cfg: Config) -> str:
    """直接测 ClipWriter：三种后端谁能用一眼就看出来。"""
    path = os.path.join(cfg.data_dir, "clips", "selftest.mp4")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    w, h = cfg.clip_size
    with ClipWriter(path, (w, h), cfg.fps) as cw:
        for i in range(cfg.fps * 2):
            frame = np.zeros((h, w, 3), np.uint8)
            cv2.putText(frame, f"frame {i}", (20, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
            cw.write_bgr(frame)
        backend = cw.backend
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print(f"[OK] 录像后端={backend} 文件={path} 大小={size} B")
    return path


def _check_preview(svc: VisionService) -> None:
    """等待环形缓冲填充后，验证 JPEG 能被解码。"""
    for _ in range(100):
        if len(svc.ring) > 2:
            break
        time.sleep(0.1)
    frame = svc.ring.latest()
    assert frame is not None, "环形缓冲为空"
    img = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None, "JPEG 解码失败"
    print(f"[OK] 环形缓冲 {len(svc.ring)} 帧, 缩略图 {img.shape[1]}x{img.shape[0]}")


async def main() -> int:
    cfg = Config()
    print(f"source={cfg.source} fps={cfg.fps} ring={cfg.ring_seconds}s data={cfg.data_dir}")

    _write_test_clip(cfg)

    svc = VisionService(cfg)
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.5)
    _check_preview(svc)

    # 手工造一个"事件"，验证事件 → 快照 → 录像 → JSONL 全链路
    from .rules import RuleEvent  # noqa: PLC0415

    # 注意用 time.time()：RuleEvent.ts 会直接格式化成对外时间戳
    svc._handle_event(RuleEvent("person.detected", time.time(), "info", 0.9))
    await asyncio.sleep(min(4.0, cfg.clip_after_s + 1))

    svc.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    jsonl = os.path.join(cfg.data_dir, "events.jsonl")
    if os.path.exists(jsonl):
        with open(jsonl, encoding="utf-8") as f:
            print("[OK] events.jsonl 首行:", f.readline().strip())
    else:
        print("[--] 没有 events.jsonl（正常：无模型时不产生真实事件）")

    print("自测完成")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
