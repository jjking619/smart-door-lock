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
| **默认只存事件片段** | 事件前后录制；在线连续录像需在 UI 手动开启 |
| **硬编码优先，软编码兜底** | `mpph264enc` 已实测可用，CPU ≈ 0；没装 GStreamer 时自动退到 OpenCV |

## 在线录像

首页实时画面旁的“开始录像”按钮默认关闭。开启后，录像按 5 分钟切分为 MP4，保存在
`data/recordings/continuous/YYYYMMDD/`；默认最多保留约 5 GB，达到上限后优先删除最早片段。
可通过 `VISION_CONTINUOUS_MAX_GB` 调整容量。事件触发录像仍单独保存在 `data/clips/`，不受此开关影响。

HTTP 控制接口为 `POST /recording`，JSON 请求体为 `{"on": true}` 或 `{"on": false}`；
启用 `VISION_TOKEN` 时需要按其他受保护接口的方式提供令牌。`/health` 的 `recording`
字段分别报告请求状态和实际运行状态。

## 规则为什么这样定

**入侵检测：分层，不用"画面有变化"**

```
人形检测 → 落在 doorstep ROI → 连续停留 ≥ 10 s → 夜间阈值减半 → 告警
          ↘ 门磁/振动/防拆（MCU 上报）作为并行证据
```

- 只看 `person` 类，猫、树影、雨滴直接过滤掉。
- 用**框底边中点**判断是否站在门口，比几何中心更贴地。
- 夜间（22:00–07:00）阈值减半、告警级别升为 `critical`。
- 默认人员区域和包裹区域都覆盖整幅画面（含边界）；可通过环境变量单独缩小。

**快递识别：靠时序，不靠单帧**

单帧看到"地上有个箱子"容易误报（可能是你自己的鞋柜）。规则是：

```
1. ground ROI 内连续检测到包裹，并稳定 ≥ 5 s
2. 同一个包裹还没有上报过
   → package.detected
```

无需检测到人员。检测中断或包裹明显移动会重新计算稳定时间；上报后短暂遮挡不会重复提醒，
连续消失达到 `VISION_DEDUPE_S` 后才允许再次上报。多个包裹分别计时和去重。
启动时门口已存在的包裹也会在稳定后上报，进程重启后去重状态不会保留。
建议调整相机角度，确保摄像头能看到包裹并尽量避免遮挡。

默认使用两个模型顺序推理同一帧：`yolov5s-rk3576.rknn` 检测人员等 COCO 类别；
`parcel-yolov5n-rk3576-fp.rknn` 检测 `defect-parcel`、`good-parcel` 两类。
包裹模型来自公开的 [包裹检测权重](https://github.com/Y47ya/parcels-object-detection-and-classification-Yolov5/tree/main/runs/detect/trainV4/weights)，
已转换为 RK3576 的未量化 RKNN。COCO 的 `backpack`、`handbag`、`suitcase` 默认不会触发包裹事件。

```bash
cd /home/pi/smart-door-lock
.venv/bin/python main.py
```

使用 `VISION_PACKAGE_MODEL=""` 可禁用第二模型并恢复单模型模式；
此时包裹候选默认改回 `suitcase,backpack,handbag`，不能识别普通纸箱。
事件会落盘并显示在界面，可选 MQTT；不包含手机推送。部署后检查 `/health` 的 `detector`
和 `package_detector` 均为 `rknn`，且 `detector_error`、`package_detector_error` 均为空。
两个模型顺序运行会降低帧率，包裹模型在非包裹样例上也有误检，务必用真实门口、空场景、
白天和夜间验证；全画面判断可能增加误报，必要时可缩小 ROI 或调整阈值。

## 快速开始

```bash
cd /home/pi/smart-door-lock
uv pip install --python .venv/bin/python -r src/vision/requirements.txt

# 1) 无硬件自测（合成画面 + 强制无模型），验证录像/缓冲/HTTP 骨架
.venv/bin/python -m vision.selftest

# 2) 接真实 USB 摄像头，不接模型（纯录像 + 预览）
VISION_MODEL=models/__missing__.rknn VISION_PACKAGE_MODEL="" \
  .venv/bin/python -m vision.app

# 3) 接 RKNN 模型
VISION_MODEL=models/yolov5s-rk3576.rknn \
  .venv/bin/python main.py --backend-only
```

RK3576 上默认使用 `models/yolov5s-rk3576.rknn`。Quectel PI 系统的
`quectel-pi-rknpu-test` 包提供预编译模型和 `librknnrt`；若模型文件尚未复制到项目目录：

```bash
mkdir -p models
cp /usr/share/model/yolov5/RK3576/yolov5s-640-640.rknn models/yolov5s-rk3576.rknn
uv pip install --python .venv/bin/python \
  https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v2.3.2/rknn-toolkit-lite2/packages/rknn_toolkit_lite2-2.3.2-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
```

Lite2 与系统 `librknnrt` 版本必须匹配；上面的 2.3.2 是本 Quectel 镜像实测版本。
启动后检查 `/health` 的 `detector` 为 `rknn`，确认走 NPU 而不是 CPU 回退。

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
| `VISION_SOURCE` | `auto` | `auto` 自动扫描 USB 摄像头 by-id 路径；也可填 by-id 路径 / 设备号 / `test` |
| `VISION_WIDTH` / `VISION_HEIGHT` | 1280 / 720 | 采集分辨率 |
| `VISION_FPS` | 10 | 门锁场景 10 fps 足够，省电省存储 |
| `VISION_RING_SECONDS` | 10 | 事前缓冲秒数 |
| `VISION_MODEL` | `models/yolov5s-rk3576.rknn` | 人员等主模型；`.rknn` 走 NPU，`.onnx` 走 CPU |
| `VISION_PACKAGE_MODEL` | `models/parcel-yolov5n-rk3576-fp.rknn` | 包裹模型；空字符串禁用 |
| `VISION_CLASS_NAMES` | 内置 COCO 80 类 | 自定义模型类别名，逗号分隔，顺序必须匹配模型 |
| `VISION_PACKAGE_MODEL_CLASS_NAMES` | `defect-parcel,good-parcel` | 包裹模型类别，顺序必须匹配模型 |
| `VISION_PACKAGE_CLASSES` | `defect-parcel,good-parcel` | 触发包裹事件的类别；禁用第二模型时默认行李类 |
| `VISION_CONF` / `VISION_NMS` | 0.35 / 0.45 | 检测阈值 |
| `VISION_INFER_EVERY_N` | 3 | 每 N 帧推理一次 |
| `VISION_DOORSTEP_POLY` | 全画面 | 归一化多边形 `x1,y1;x2,y2;...` |
| `VISION_GROUND_POLY` | 全画面 | 包裹 ROI（快递专用），可配置为更小的多边形 |
| `VISION_DWELL_S` | 10 | 停留多久算入侵 |
| `VISION_STATIC_S` | 5 | 连续检测到物体并保持静止多久才上报 |
| `VISION_DEDUPE_S` | 10 | 已上报包裹连续消失多久后允许重新上报 |
| `VISION_CLIP_AFTER_S` | 20 | 事件后录多久 |
| `VISION_TOKEN` | 空 | 设置后所有请求需鉴权（**公网必须设**） |
| `MQTT_HOST` / `MQTT_TOPIC` | 空 / `lock/event` | 不配则只落本地 JSONL |

## 模型准备

RK3576 的 Quectel 镜像已附带上述 YOLOv5s RKNN 模型，直接按快速开始部署即可。
转换其他模型可在 x86 电脑上进行，也可在本机安装 ARM64 版 RKNN-Toolkit2；
建议使用单独的虚拟环境，避免影响项目的 Lite2 运行环境。
本机已用 Toolkit2 2.3.2 将包裹 ONNX 转成 RK3576 的未量化 RKNN，并验证可以通过 Lite2 加载和推理：

```bash
uv venv .venv-rknn
uv pip install --python .venv-rknn/bin/python \
  'rknn-toolkit2==2.3.2' 'numpy<2' 'setuptools<81'
```

转换时指定 `target_platform='rk3576'`，输入归一化参数与模型一致；INT8 量化需要具有代表性的
校准图。`models/parcel-yolov5n-rk3576-fp.rknn` 是本机转换的未量化验证模型，其输出为单个
`[1, 6, 8400]` 张量，现由检测器解码并与人员模型的结果合并。
该模型没有人员类别；以真实门口场景验证误报和漏报。

## 自测结果（RK3576 实测）

```
[OK] YOLOv5s RKNN + Lite2 2.3.2 + driver 0.9.8，visiond health detector=rknn
[OK] RKNN Model Zoo bus.jpg: 4 persons + 1 bus，app Detector 结果与厂商 demo 一致
[OK] rknn_benchmark 平均推理 21.08 ms；厂商 demo 含前后处理平均 24.59 ms
```

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
- ROI 只过滤事件，不会遮挡画面或录像；像素级隐私打码尚未实现。
- 事件片段用 JPEG 重编码，会丢一点画质；要无损需要改成 GStreamer `tee` 直接落盘。

## 下一步

- [ ] 雷达（LD2410C）触发唤醒，替代常开
- [ ] ByteTrack 多目标跟踪
- [ ] 人脸识别（SCRFD + MobileFaceNet RKNN）
- [ ] go2rtc 接入，出 WebRTC 低延迟流
- [ ] 按天自动清理（保留 7 天）
