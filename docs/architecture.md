# 当前实现架构（as-built）

> 本文只描述**已经写进代码并在 RK3576 上跑通的部分**，不是规划。
> 规划与硬件方案见 `design.md`。提交：`61a7aec`。

---

## 1. 总览：两个独立进程

```
┌─────────────────────────────────────────────────────────────────────┐
│  Quectel Pi M2 / RK3576 / Debian ARM64 / Python 3.11 (uv venv)      │
│                                                                     │
│  ┌───────────────────────────┐        ┌───────────────────────────┐ │
│  │  visiond  (systemd)       │        │  audiood  (systemd)       │ │
│  │  :8081 HTTP               │        │  无 HTTP                  │ │
│  │                           │        │                           │ │
│  │  录像 / 入侵 / 快递        │        │  门铃对讲 / 自动应答       │ │
│  └──────────┬────────────────┘        └──────────┬────────────────┘ │
│             │ MQTT lock/event/*                  │ MQTT             │
│             └──────────────┬──────────────────────┘                  │
│                            ▼                                        │
│                   ┌──────────────────┐                              │
│                   │ mosquitto (待接) │  ← 也可以只读 events.jsonl    │
│                   └──────────────────┘                              │
└─────────────────────────────────────────────────────────────────────┘
         │ 独立进程、独立崩溃域：visiond 挂掉不影响对讲，反之亦然
```

两者**没有直接 import 依赖**，只通过 MQTT 主题与磁盘文件交互。这是刻意的：
门铃响了不能因为摄像头异常而没声音。

| 进程 | 入口 | 线程模型 | 崩溃影响 |
| --- | --- | --- | --- |
| `visiond` | `python -m device.visiond.app` | 1 采集线程 + asyncio HTTP + N 录像线程 | 无预览、无录像；对讲与他业务不受影响 |
| `audiood` | `python -m device.audiood.app` | 1 录音线程（executor）+ asyncio | 无自动应答；录像不受影响 |

---

## 2. visiond：数据流

```
                        ┌──────────────── 采集线程 (executor) ────────────────┐
                        │                                                     │
  V4L2 / CSI ──────────►│ open_camera()                                       │
  或 SyntheticCamera    │      │                                              │
                        │      ▼                                              │
                        │ cap.read() ──失败5次──► release + 重开（热插拔）      │
                        │      │                                              │
                        │      ├─► 每 3 帧 ──► Detector.detect() ──► Rules.update()
                        │      │                 (RKNN / ONNX)          │
                        │      │                                        │ RuleEvent
                        │      ├─► resize 640w ──► JPEG ──► RingBuffer  │
                        │      │                          (10 s)        │
                        │      └─► 有客户端时 ──► draw() ──► preview_jpeg│
                        │                                          │    │
                        └──────────────────────────────────────────┼────┘
                                                                   │
                                              _handle_event() ◄────┘
                                                    │
                        ┌───────────────────────────┼──────────────────────────┐
                        ▼                           ▼                          ▼
                  快照 jpg                    录像任务 ClipJob             EventBus
              snapshots/YYYYMMDD/        （pre-roll + 20 s）         ┌──────┴──────┐
                                                    │               │             │
                                                    ▼          events.jsonl   MQTT publish
                                            clips/YYYYMMDD/
                                              (gstreamer-mpp
                                               或 opencv)
```

### 2.1 关键设计点（每条都有具体理由）

| 设计 | 代码位置 | 为什么 |
| --- | --- | --- |
| 采集在**单线程**里跑 | `app.py: VisionService._loop` | 相机句柄跨线程会抢帧、读到旧帧 |
| 推理每 N 帧一次 | `config.infer_every_n=3` | 10 fps 采集、3.3 fps 推理足够，省 NPU |
| 环形缓冲只存 **640 宽 JPEG** | `ring.py: RingBuffer` | 原始帧 10 s 要 276 MB，JPEG 约 3 MB |
| pre-roll 用**单调时钟**索引 | `app.py: _handle_event` | 环形缓冲只认 `monotonic`，墙上时钟会因 NTP 跳变丢帧 |
| 事件时间戳用**墙上时钟** | `rules.py: wall = time.time()` | 已修 bug：早期版本落库变成 1970 年 |
| 录像**独立线程** | `recorder.py: ClipJob` | 编码阻塞不能拖慢采集与推理 |
| 硬编码优先 | `recorder.py: _has_gst_mpp()` | RK3576 有 VPU，`mpph264enc` 实测可用，CPU ≈ 0 |
| 预览**几十个客户端共用一份编码** | `app.py: mjpeg_frames` | 编码开销与客户端数量无关 |
| MJPEG 生成器是 **async** | `app.py: mjpeg_frames` | 已修 bug：同步 `time.sleep` 会阻塞事件循环 |
| 事件先落 JSONL 再 publish | `events.py: EventBus.emit` | MQTT 断了也要能本地查"谁来过" |

### 2.2 模块清单

| 文件 | 行数 | 职责 | 关键符号 |
| --- | --- | --- | --- |
| `config.py` | 98 | 全部配置来自环境变量 | `Config` |
| `ring.py` | 61 | 环形缓冲 + 按天分目录 | `Frame`, `RingBuffer`, `new_path` |
| `detector.py` | 208 | 推理 + letterbox + NMS | `Detector`, `Detection`, `letterbox`, `draw` |
| `rules.py` | 246 | 入侵 / 快递规则 | `Rules`, `RuleEvent`, `_in_poly` |
| `recorder.py` | 178 | 片段写入（双后端） | `ClipWriter`, `ClipJob` |
| `events.py` | 67 | JSONL + MQTT | `EventBus` |
| `app.py` | 305 | 采集主循环 | `VisionService`, `SyntheticCamera` |
| `http.py` | 122 | 预览 / 事件 / 下载 | `create_app`, `serve` |
| `selftest.py` | 102 | 无硬件自测 | `main` |

### 2.3 规则引擎状态机

`Rules` 是有状态的，每帧 `update()` 一次，内部维护：

```
_person_first_seen   人形首次进入 doorstep ROI 的单调时刻
_person_last_seen    最后一次在 ROI 内
_person_box          用于快照框
_visit_reported      本次到访是否已告警（防重复）
_tracks[]            极简最近邻跟踪（同类别 + 中心距阈值）
_last_emit{}         按事件类型去重时间
_package_reported{}  已上报包裹的位置网格（24 px）
```

**入侵判定**

```
person 类 且 框底边中点 ∈ doorstep_poly
    └─ 停留 ≥ dwell_seconds × (夜间 ? 0.5 : 1.0)
         └─ 未上报过 → intrusion (夜间 critical / 白天 warn)
人形离开 ROI 超过 person_leave_grace_s → 复位
```

**快递判定**（四个条件缺一不可）

```
① 曾有人形进入 ROI（_person_last_seen 有值）
② 人形离开 ≥ 20 s
③ ground_poly 内出现 package_class 物体（suitcase/backpack/handbag/box/package/bag）
   且同一 track 稳定 ≥ static_seconds
④ 该网格位置 5 min 内未上报，且全局 package 去重未触发
   → package.detected
```

单帧看到"地上有箱子"不构成事件，这是刻意的：鞋柜、地垫、雨伞都长这样。

---

## 3. visiond：HTTP 接口

全部在 `http.py`，aiohttp，默认 `:8081`。

| 方法 | 路径 | 返回 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | JSON | `fps` / `ring` / `preview_clients` / `detector` 后端 / `clips_active` |
| GET | `/snapshot.jpg` | JPEG | 缓冲里最新帧（640 宽） |
| GET | `/stream` | MJPEG | 带 ROI 框与检测框，仅在有客户端时编码 |
| GET | `/events?limit=20` | JSON | 倒序读 `events.jsonl` 尾部 |
| GET | `/media/{path}` | 文件 | 录像/快照下载，**做了路径穿越校验** |

鉴权：`VISION_TOKEN` 非空时所有请求需带 `?token=` 或 `Authorization: Bearer`，
`/health` 例外（便于监控）。**公网必须设置。**

实测：

```
/health                     200  {"ok":true,"fps":8.4,"ring":46,...}
/snapshot.jpg               200  5874 B   JPEG 640x360
/stream (3 s)               1.03 MiB   multipart/x-mixed-replace
/health 与 /stream 并发      1.5 ms     ← 预览不阻塞其他接口
```

---

## 4. audiood：链路与兜底

```
门铃触发（当前：stdin 回车模拟；后续：MCU UART 事件）
    │
    ▼
Recorder.record_utterance()          16 kHz / 16 bit / 单声道
    │  等开口 ≤5 s，录到静音 0.7 s 或 8 s 上限
    ▼
CozeAudio.transcribe()               audio.transcriptions.create
    │  PCM → WAV 封包，10 MB / 30 min 上限
    ▼
answer_with_fallback()
    │  ┌─ L1  CozeWorkflowClient.ask()   6 s，重试 1 次
    │  ├─ L2  本地 LLM 回调              2 s
    │  ├─ L3  固定话术
    │  └─ L4  静默提示
    ▼
CozeAudio.synthesize()               audio.speech.create → WAV → PCM
    │
    ▼
sounddevice 播放（采样率由 TTS 返回，不写死 16 k）
```

### 4.1 模块清单

| 文件 | 行数 | 职责 | 关键符号 |
| --- | --- | --- | --- |
| `coze_client.py` | 262 | 工作流 + 熔断 + 兜底链 | `CozeWorkflowClient`, `CozeReply`, `parse_reply`, `answer_with_fallback`, `build_async_client` |
| `coze_audio.py` | 164 | Coze ASR / TTS | `CozeAudio`, `pcm_to_wav`, `wav_to_pcm`, `safe_transcribe`, `safe_synthesize` |
| `app.py` | 176 | 录音→识别→应答→播报 | `Recorder`, `play_pcm`, `main` |
| `coze_smoke_test.py` | 51 | 连通性自检 | `main` |

### 4.2 熔断与降级

`CozeWorkflowClient` 内部：

```
连续 3 次失败 → 熔断 60 s（_open_until）
熔断期内直接抛 CozeUnavailable，不消耗超时等待
鉴权类错误（401 / auth）不重试，立刻熔断
```

理由：门铃是**用户在场的一次性交互**，等 6 s 再等 6 s 是灾难。
熔断是为了让"云端挂了"这种情况在 100 ms 内就降级到本地话术。

### 4.3 输出约束（安全边界）

`parse_reply()` 对模型输出做强制收敛：

```
动作白名单  ALLOWED_ACTIONS = {reply_only, notify_owner, leave_message, request_unlock}
           白名单外 → 静默降级为 reply_only
开锁请求    request_unlock → needs_local_auth = True（恒定）
伪造字段    丢弃 args["authorized"] / args["is_owner"]
格式不合规  模型返回自然语言 → 当作纯文本回复，动作降级
```

**模型说"开"不等于会开。** 真正的开锁要 `lockd` 校验后经 HMAC 通道下发给 MCU，
而 MCU 才是唯一持有锁体驱动的实体（第二阶段）。

### 4.4 Coze 能力边界（已实测 `cozepy 0.20.0`）

| 能力 | 接口 | 实测 |
| --- | --- | --- |
| 文件 ASR | `audio.transcriptions.create(file=...)` | ✅ |
| 流式 ASR | `websockets.audio.transcriptions` | ✅ 存在，未接 |
| TTS | `audio.speech.create(input, voice_id, response_format, sample_rate)` | ✅ WAV/PCM/OGG_OPUS/MP3 |
| 音色 | `audio.voices.list()` / `.clone()` | ✅ 首个中文音色自动选 |
| 实时房间 | `audio.rooms.create()` | ✅ 存在，**进房即计费**，门锁场景不用 |

---

## 5. 事件契约

MQTT 主题：`lock/event/<type>`，QoS 1，同时追加到 `data/events.jsonl`。

```json
{
  "ts": "2026-11-02T18:36:23.266+08:00",
  "type": "person.detected" | "intrusion" | "package.detected",
  "source": "visiond",
  "severity": "info" | "warn" | "critical",
  "conf": 0.93,
  "zone": "doorstep" | "ground",
  "snapshot": "data/snapshots/20261102/183623_266.jpg",
  "clip":     "data/clips/20261102/183623_266.mp4",
  "detail":   { "...": "每种类型不同" }
}
```

| type | detail 关键字段 |
| --- | --- |
| `person.detected` | `bbox` |
| `intrusion` | `dwell_s`, `night`, `bbox` |
| `package.detected` | `object`, `static_s`, `bbox`, `person_left_s` |

`ts` 是**墙上时钟 ISO8601 带时区**，可直接被前端/规则消费；
内部计时一律用 `time.monotonic()`，两者不混用（这是踩过的坑）。

---

## 6. 磁盘布局

```
data/
├── events.jsonl                    事件流水（永不删除，用于审计）
├── snapshots/YYYYMMDD/HHMMSS_mmm.jpg
└── clips/YYYYMMDD/HHMMSS_mmm.mp4   事件前 10 s + 后 20 s
```

容量估算：30 s 片段 × 50 次/天 ≈ 3 GB/天 → 64 GB eMMC 可存约 20 天。
**自动清理尚未实现**（见待办），上线前必须加，否则会写满 eMMC。

---

## 7. 部署

```
deploy/systemd/
├── visiond.service              SupplementaryGroups=video render
│                                CPUQuota=300%  MemoryMax=1G
├── visiond.env.example          VISION_SOURCE / ROI / MQTT ...
├── audiood.service              SupplementaryGroups=audio
│                                CPUQuota=200%  MemoryMax=512M
└── audiood.env.example          COZE_API_TOKEN / 超时 / 音频设备
```

```bash
sudo install -d -m 700 /etc/doorlock
sudo cp deploy/systemd/*.env.example /etc/doorlock/   # 改成 .env 并填密钥
sudo chmod 600 /etc/doorlock/*.env
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now visiond audiood
```

`CPUQuota` 不是为了省电，是为了**保证对讲不卡**：视频编码吃满 CPU 会让
WebRTC 音频抖动到无法通话。

---

## 8. 已验证 / 未验证

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| H.264 硬编码 | ✅ 实测 | `gstreamer-mpp`，12372 B mp4 正常 |
| 环形缓冲 + 快照 | ✅ 实测 | 46 帧，JPEG 640×360 可解码 |
| MJPEG 非阻塞 | ✅ 实测 | 并发 `/health` 1.5 ms |
| 事件落盘时间戳 | ✅ 实测 | `2026-11-02T18:36:23.266+08:00` |
| 路径穿越防护 | ✅ 代码 | `http.py: media()` resolve + startswith |
| Coze SDK 接口 | ✅ 实测 | 8 个属性链全部存在 |
| **RKNN 推理** | ❌ 未测 | 缺 `.rknn` 模型文件 |
| **真实摄像头** | ❌ 未测 | 当前只有 `source=test` |
| **MQTT 发布** | ❌ 未测 | 未配 broker |
| **真实麦克风/喇叭** | ❌ 未测 | 无音频设备 |
| **快递准确率** | ❌ 未测 | 需自采数据微调 |

---

## 9. 已知技术债

| 问题 | 影响 | 建议 |
| --- | --- | --- |
| 单线程采集 | 双摄需要改多线程 + 共享推理队列 | 快递识别上第二路时重构 |
| 最近邻跟踪 | 两人交叉会串轨 | 换 ByteTrack（RKNN 跑得动） |
| 片段走 JPEG 重编码 | 有画质损失、多一次编解码 | 改 GStreamer `tee` 直接落盘 |
| **无自动清理** | eMMC 会写满 | 上线前必须加 7 天保留策略 |
| 门铃靠 `input()` 模拟 | 不是真实事件源 | 接 MCU UART / 雷达 GPIO |
| 无 AEC | 对讲会啸叫 | 接 `webrtc-audio-processing` AEC3 |
| 无 WebRTC | 只能 MJPEG 局域网看 | 接 go2rtc 或 aiortc |
| ROI 靠环境变量手调 | 现场调试麻烦 | 做一个网页画框工具 |
