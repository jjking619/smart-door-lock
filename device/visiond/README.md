# visiond —— 录像 / 入侵检测 / 快递识别

一个进程解决三件事：**摄像头录制与在线查看**、**入侵检测**、**快递识别**。
不依赖锁体、不依赖 MCU，可以在门装好之前就用 USB 摄像头跑起来。

## 为什么这样设计

| 决策 | 原因 |
| --- | --- |
| **雷达/门铃触发才开相机**（预留） | 24 h 常开会让 SoC 长期 60 ℃+，也不符合隐私最小化原则 |
| **环形缓冲 pre-roll 10 s** | 事件发生时能补上"事发前"，证据才完整；只存 JPEG 缩略图，内存可控 |
| **推理与录像解耦** | 推理每 3 帧一次，录像用缓冲补帧，推理慢不会丢画面 |
| **预览帧复用缓冲** | 无论 1 个还是 100 个客户端，编码开销都只有一次 |
| **只存事件片段** | 30 s 片段 × 50 次/天 ≈ 3 GB/天，7 天循环；需要 24 h 再挂 SSD |
| **硬编码优先，软编码兜底** | `mpph264enc` 已实测可用，CPU ≈ 0；没装 GStreamer 时自动退到 OpenCV |

## 规则为什么这样定

**入侵检测：分层，不用"画面有变化"**

```
人形检测 → 落在 doorstep ROI → 连续停留 ≥ 10 s → 夜间阈值减半 → 告警
          ↘ 门磁/振动/防拆（MCU 上报）作为并行证据
```

- 只看 `person` 类，猫、树影、雨滴直接过滤掉。
- 用**框底边中点**判断是否站在门口，比几何中心更贴地。
- 夜间（22:00–07:00）阈值减半、告警级别升为 `critical`。

**快递识别：靠时序，不靠单帧**

单帧看到"地上有个箱子"完全不能当证据（可能是你自己的鞋柜）。证据链是：

```
1. 人形进入 doorstep ROI，记录 first_seen
2. 人形离开且 20 s 内未返回
3. ground ROI 出现静止物体，连续稳定 ≥ 10 s
4. 该位置 5 min 内未上报过
   → package.detected
```

四个条件缺一不可。这也是为什么**第二路俯拍摄像头价值很大**：正面摄像头里包裹永远被腿挡住。

## 快速开始

```bash
cd /home/pi/smart-door-lock
uv pip install --python .venv/bin/python -r device/visiond/requirements.txt

# 1) 无硬件自测（合成画面 + 强制无模型），验证录像/缓冲/HTTP 骨架
.venv/bin/python -m device.visiond.selftest

# 2) 接真实 USB 摄像头，不接模型（纯录像 + 预览）
VISION_SOURCE=0 .venv/bin/python -m device.visiond.app

# 3) 接 RKNN 模型
VISION_SOURCE=0 VISION_MODEL=models/yolo11n.rknn \
  .venv/bin/python -m device.visiond.app
```

浏览器打开：

| 地址 | 用途 |
| --- | --- |
| `http://<板子IP>:8081/health` | 帧率、缓冲深度、检测后端 |
| `http://<板子IP>:8081/stream` | MJPEG 实时预览（带 ROI 框） |
| `http://<板子IP>:8081/snapshot.jpg` | 当前帧 |
| `http://<板子IP>:8081/events?limit=20` | 最近事件 |
| `http://<板子IP>:8081/media/clips/20261102/182743_906.mp4` | 下载录像 |

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `VISION_SOURCE` | `0` | 设备号 / `/dev/videoN` / `test` |
| `VISION_WIDTH` / `VISION_HEIGHT` | 1280 / 720 | 采集分辨率 |
| `VISION_FPS` | 10 | 门锁场景 10 fps 足够，省电省存储 |
| `VISION_RING_SECONDS` | 10 | 事前缓冲秒数 |
| `VISION_MODEL` | `models/yolo11n.rknn` | `.rknn` 走 NPU，`.onnx` 走 CPU |
| `VISION_CONF` / `VISION_NMS` | 0.35 / 0.45 | 检测阈值 |
| `VISION_INFER_EVERY_N` | 3 | 每 N 帧推理一次 |
| `VISION_DOORSTEP_POLY` | 全画面 | 归一化多边形 `x1,y1;x2,y2;...` |
| `VISION_GROUND_POLY` | 下 45% | 地面 ROI（快递专用） |
| `VISION_DWELL_S` | 10 | 停留多久算入侵 |
| `VISION_STATIC_S` | 10 | 物体静止多久算包裹 |
| `VISION_CLIP_AFTER_S` | 20 | 事件后录多久 |
| `VISION_TOKEN` | 空 | 设置后所有请求需鉴权（**公网必须设**） |
| `MQTT_HOST` / `MQTT_TOPIC` | 空 / `lock/event` | 不配则只落本地 JSONL |

## 模型准备（在 x86 上做，不要在板子上跑 rknn-toolkit2）

```bash
# 1. 导出 ONNX
yolo export model=yolo11n.pt format=onnx opset=12 imgsz=640
# 2. 用 RKNN-Toolkit2 量化（Docker 镜像 rknn-toolkit2:2.x）
#    关键参数：mean/std 用 0/255 归一化，keep_ratio=True（letterbox）
# 3. 得到 yolo11n.rknn，拷到 models/
```

快递专用模型：先拿 COCO 的 `suitcase/backpack/handbag` 冷启动，
自采 200–500 张门口地面图，加 `box` / `package` 两类微调后再量化。

## 自测结果（RK3576 实测）

```
[OK] 录像后端=gstreamer-mpp 文件=data/selftest/clips/selftest.mp4 大小=12372 B
[OK] 环形缓冲 3 帧, 缩略图 640x360
[OK] events.jsonl 首行: {"ts": "...", "type": "person.detected", ..., "snapshot": "...jpg", "clip": "...mp4"}
```

见到的 `mpp: unable to create enc vp8 ... unsupported` 是无害的：RK3576 无 VP8 硬编，
mpp 探测后会自动走 H.264，录像正常生成。

## 已知边界

- 单线程采集：多路摄像头需要改成"每路一个采集线程 + 共享推理队列"。
- 追踪是极简的最近邻关联，两人交叉会短暂串轨；要更稳可换 ByteTrack（RKNN 上跑得动）。
- 隐私遮罩目前靠 ROI 调参，还没做像素级打码（下一阶段做）。
- 事件片段用 JPEG 重编码，会丢一点画质；要无损需要改成 GStreamer `tee` 直接落盘。

## 下一步

- [ ] 雷达（LD2410C）触发唤醒，替代常开
- [ ] ByteTrack 多目标跟踪
- [ ] 人脸识别（SCRFD + MobileFaceNet RKNN）
- [ ] go2rtc 接入，出 WebRTC 低延迟流
- [ ] 按天自动清理（保留 7 天）
