# 智能门锁（Quectel Pi M2 / RK3576）

**目前实现四项功能**：摄像头录制与在线查看、入侵检测、快递识别、语音对讲。
不依赖锁体和 MCU，可先跑通再上机械结构。

![方案](docs/design.md)

## 功能现状

| 功能 | 状态 | 目录 | 验证方式 |
| --- | --- | --- | --- |
| 视频录制 / 在线查看 | ✅ 可用 | `device/visiond/` | `/stream` MJPEG、`/media/...` 下载 mp4 |
| 入侵检测 | ✅ 可用（需 RKNN 模型） | `device/visiond/rules.py` | 事件 JSONL + 快照 + 片段 |
| 快递识别 | ✅ 可用（需第二路俯拍 + 微调模型） | `device/visiond/rules.py` | `package.detected` 事件 |
| 语音对讲 / 自动应答 | ✅ 链路通（需 Coze token） | `device/audiood/` | 按门铃 → ASR → Coze → TTS 播报 |

## 快速开始

```bash
sudo apt install -y python3-opencv libopenblas-dev \
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

cd /home/pi/smart-door-lock
uv venv .venv
uv pip install --python .venv/bin/python -r device/visiond/requirements.txt
uv pip install --python .venv/bin/python -r device/audiood/requirements.txt

# 1) 无硬件自测：合成画面跑通录像/缓冲/事件链路
.venv/bin/python -m device.visiond.selftest

# 2) 接 USB 摄像头
VISION_SOURCE=0 .venv/bin/python -m device.visiond.app
#    浏览器打开 http://<板子IP>:8081/stream

# 3) 语音：先验证 Coze 连通性
export COZE_API_TOKEN=pat_xxx
export COZE_WORKFLOW_ID=7466xxxx
.venv/bin/python -m device.audiood.coze_smoke_test "东西放门口了"
.venv/bin/python -m device.audiood.app
```

## 目录

```
docs/design.md          完整方案（硬件 BOM、供电、安全清单、里程碑）
device/visiond/         录像 + 入侵检测 + 快递识别
device/audiood/         门铃对讲 + Coze 自动应答
deploy/systemd/         systemd 服务与 env 模板
models/                 RKNN 模型（不入库）
data/                   事件、快照、录像（不入库）
```

## 三条硬约束

1. **开锁不走 Linux**：指纹比对与锁体驱动必须由独立 MCU 完成（第二阶段），
   M2 挂掉时仍能按指纹/机械钥匙进门。
2. **云端不能开锁**：Coze 只能返回 `request_unlock` 请求，
   最终由 `lockd` 校验后经 HMAC 通道下发 MCU。模型说"开"不等于会开。
3. **断网可用**：录像、预览、事件落盘全本地；语音有固定话术兜底。

详见 `docs/design.md`。
