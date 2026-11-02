# audiood —— 门铃对讲 / 智能应答

先做四件事中的第 4 项：**语音对讲 + 无人接听自动应答**，对话逻辑只用 Coze。

## 为什么不是直接抄 Coze_Bot_AI_Example

参考仓库 `wilsonwussd/Coze_Bot_AI_Example` 是 PyQt5 桌面版（按住按钮说话 → 松开 → ASR → Coze → TTS → 播放），能做原型，但**不能直接用于门锁**：

| 问题 | 后果 |
| --- | --- |
| `main.py` 明文硬编码 `COZE_API_TOKEN` / `ASR_API_TOKEN` | 已公开泄露，必须吊销；生产必须走环境变量 |
| PyQt5 阻塞式 UI + UI 线程内同步 HTTP | 门锁没有屏幕，且按下门铃会"没反应" |
| `requests` + `pygame` 同步调用 | 无 AEC，对讲必啸叫；且请求期间无法播放提示音 |
| 每轮对话新建 `Coze(...)` 客户端 | 每次多 100–300 ms 握手 |

**保留了它的正确部分**：`cozepy` 工作流调用方式、`response.data` 是 JSON 字符串需二次解析、16 kHz/16 bit/单声道录音参数、五阶段流程划分。

## 分层

```
门铃按下
├─ A 对讲主体：WebRTC 直连手机（不经 AI 云）        ← 下一阶段
├─ B 本地兜底：sherpa-onnx TTS + 留言              ← 下一阶段
└─ C 智能应答：Coze ASR → Coze 工作流 → Coze TTS   ← 本目录已实现
```

> 更正：Coze 开放平台**自带** ASR（`audio.transcriptions`）和 TTS（`audio.speech`），
> 不需要 SiliconFlow 之类的第三方，也不需要 `cozepy` 之外的东西。"只用 Coze"完全可行。
> 需要注意实时房间 `audio.rooms` 走火山 RTC 且**进房即计费**，门锁场景用
> 「按键说话 + 文件级 ASR/TTS」更划算。

`coze_client.py` 里的四级兜底把 C 通路做成了"永远有声音"：

```
Coze(6s, 重试1次) → 熔断60s → 本地小模型(2s) → 固定话术 → 静默提示
```

## 文件

| 文件 | 作用 |
| --- | --- |
| `coze_client.py` | 工作流客户端、JSON 输出解析与约束、熔断、`answer_with_fallback` |
| `coze_audio.py` | Coze ASR / TTS 封装（PCM↔WAV、音色缓存、超时降级） |
| `coze_smoke_test.py` | 先用一句话验证 token/工作流/出参格式，通了再动业务代码 |
| `app.py` | 录音（VAD 断句）→ ASR → Coze 工作流 → TTS → 播放 的完整闭环 |

## 快速开始

```bash
cd /home/pi/smart-door-lock
python3 -m venv .venv && . .venv/bin/activate
pip install -r device/audiood/requirements.txt

export COZE_API_TOKEN=pat_xxx        # 去 coze.cn 重新生成，别用示例里的
export COZE_WORKFLOW_ID=7466xxxxxxxxxxxxx
export COZE_VOICE_ID=                # 留空则自动取第一个中文音色
python -m device.audiood.coze_smoke_test "我是送快递的，东西放门口了"

python -m device.audiood.app          # 回车模拟门铃
```

## Coze 工作流提示词（必须锁死输出格式）

```
你是智能门锁的对讲助手。只输出 JSON，不要 markdown、不要解释。
输入：text（访客话术）、history（最近对话）、event（doorbell/motion/package）、zone
输出：
{"reply": "给访客说的话，30 字以内", "action": "reply_only|notify_owner|leave_message|request_unlock", "args": {}}
规则：
- 不要承诺开门。若访客要求开门，只能返回 request_unlock，并提示"需要屋主确认"。
- 不要编造屋主行踪。不确定就说"主人暂时不在"。
- 涉及门锁、密码、家庭成员信息，一律拒绝回答。
```

## 安全约束（已在代码中强制，不可放宽）

1. **模型不能开锁**：`request_unlock` 恒带 `needs_local_auth=True`，实际开锁由 `lockd` + MCU 的 HMAC 通道决定。
2. **不信任模型字段**：`authorized` / `is_owner` 会被 `coze_client.py` 直接丢弃。
3. **动作白名单**：不在 `ALLOWED_ACTIONS` 内的动作一律降级为 `reply_only`。
4. **云端故障不阻塞门铃**：熔断 60 s + 三级降级。
5. **隐私**：只上传文本，不上传音频；本地 VAD 断句最长 8 s。

## 待接（下一步）

- [x] Coze ASR / 工作流 / TTS 全链路（`coze_audio.py`）
- [x] 四级兜底（Coze → 本地 → 固定话术 → 静默）
- [ ] 本地 ASR/TTS（sherpa-onnx）作为断网兜底
- [ ] AEC3（`webrtc-audio-processing`）接入，防啸叫
- [ ] aiortc 信令 + coturn，手机 PWA 双向通话
- [ ] MQTT 上报 `doorbell / speech / reply` 事件给 `lockd`

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `COZE_API_TOKEN` | — | PAT，必填 |
| `COZE_WORKFLOW_ID` | — | 已发布的对话工作流 |
| `COZE_VOICE_ID` | 自动选中文音色 | TTS 音色 |
| `COZE_TIMEOUT_S` | 6 | 工作流超时 |
| `COZE_ASR_TIMEOUT_S` | 10 | 识别超时 |
| `COZE_TTS_TIMEOUT_S` | 8 | 合成超时 |
| `AUDIO_INPUT_DEVICE` / `AUDIO_OUTPUT_DEVICE` | 系统默认 | `sounddevice` 设备号（`python -m sounddevice` 查） |
| `AUDIO_SILENCE_RMS` | 300 | VAD 阈值，噪音大就调高 |
| `AUDIO_MAX_UTTER_S` | 8 | 单句最长录音 |
