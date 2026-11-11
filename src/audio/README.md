# audiood —— 门铃对讲 / 智能应答

先做四件事中的第 4 项：**语音对讲 + 无人接听自动应答**，对话逻辑只用 Coze。

## 流式对话模式

设置 `COZE_MODE=streaming` 可使用独立的 Coze 流式 ASR WebSocket、Bot 文本流和流式 TTS WebSocket。麦克风按 20 ms PCM 分片上送，静音检测结束一轮；Bot 回复按标点分段送入 TTS，音频分片到达后立即播放。此模式仍需要已发布的 Coze Bot 和 `COZE_BOT_ID`；它不创建 Bot，也不绕过 Coze 额度。`realtime` 模式仍保留为 Coze 一体化语音会话。若 Coze 返回 `4028`，服务会在冷却后自动重试（默认 60 秒）。

本地开锁请求识别支持中文及 `open lock`、`open the door`、`unlock`、`let me in`，
仅推送屋主确认，不直接驱动门锁。流式云端回复失败时，界面和播报统一使用
匹配访客中文/英文的兜底话术；开锁确认结果也按请求语言回复。

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

# 0) 先验证 Coze 配置（强烈建议，能省掉猜错误码的时间）
set -a; . ./deploy/systemd/audiood.env; set +a
.venv/bin/python -m audio.coze_smoke_test --list
#   ↑ 没有工作流会显示 "工作流: 0 个"，先去 coze.cn 建一个并「发布」
#   ↑ 有工作流时会把 id 打出来，填进 COZE_WORKFLOW_ID

.venv/bin/python -m audio.coze_smoke_test "东西放门口了"

# 1) 看实时日志（journal）
journalctl --user -u audiood.service -f -o cat

# 2) 看文件日志（滚动）
tail -f data/logs/audiood.log

# 3) 看单次会话日志（按日期分目录）
ls -ltr data/logs/sessions/*/session-*.log | tail
tail -f data/logs/sessions/$(date +%F)/session-<sid>.log
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

## 访客请求开门 → 屋主在界面确认

访客说出"开门 / 开锁 / 让我进去…"时，audiood **只挂起一条请求**，绝不自己开锁：

1. 三种模式（streaming / realtime / dialog）都会调用 `TalkService.raise_unlock_request()`；
   同一时间只保留一条，重复说会刷新原话和过期时间（`AUDIO_UNLOCK_REQUEST_TTL_S`，默认 60 s）。
   streaming 模式在 ASR 最终识别文本命中开门关键词时立即推送，不等待 Bot 回复；
   后续 Bot 超时或 TTS 失败不会丢失已推送的请求。
2. `GET /health` 的 `unlock_request` 字段给出 `{id, text, ts, expires_in}`，界面每 1.5 s 轮询一次，
   弹出"访客请求开门"窗口，附门口抓拍和访客原话。
3. 屋主点"允许开门" → 界面走 lockd `POST /unlock`（`method=访客语音请求`），再把结果交回
   `POST /unlock_request {id, decision: "allow", unlocked, reason?}`；点"拒绝"或关掉弹窗 →
   `{id, decision: "deny"}`。
4. 每次结果（允许 / 拒绝 / 超时）都会写入 `events.jsonl`，类型为 `unlock.request`，记录页可查；
   成功开锁另有 lockd 的 `door.opened`。没有会话占用扬声器时，会播报一句结果给访客。

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
| `COZE_MODE` | `realtime` | `streaming`：独立流式 ASR → Bot Chat → 流式 TTS；`realtime`：Coze 实时语音会话 |
| `COZE_API_TOKEN` | — | PAT，必填 |
| `COZE_WORKFLOW_ID` | — | 已发布的对话工作流 |
| `COZE_VOICE_ID` | 自动选中文音色 | TTS 音色 |
| `COZE_TIMEOUT_S` | 6 | 工作流超时 |
| `COZE_ASR_TIMEOUT_S` | 10 | 识别超时 |
| `COZE_TTS_TIMEOUT_S` | 8 | 合成超时 |
| `COZE_QUOTA_RETRY_S` | 60 | 命中 `4028` 后再次尝试 Coze 的冷却时间（秒） |
| `AUDIO_INPUT_DEVICE` / `AUDIO_OUTPUT_DEVICE` | 系统默认 | `sounddevice` 设备号（`python -m sounddevice` 查） |
| `AUDIO_SILENCE_RMS` | 300 | VAD 阈值，噪音大就调高 |
| `AUDIO_MAX_UTTER_S` | 8 | 单句最长录音 |
| `AUDIO_UNLOCK_REQUEST_TTL_S` | 60 | 访客开门请求在界面上等待屋主处理的时长（秒） |
| `AUDIO_LOG_DIR` | `data/logs` | 文件日志目录（相对路径按项目根目录解析） |
| `AUDIO_LOG_FILE` | `audiood.log` | 日志文件名（可用绝对路径） |
| `AUDIO_LOG_MAX_MB` | `10` | 单个日志文件最大大小（MB），超过后滚动 |
| `AUDIO_LOG_BACKUP_COUNT` | `5` | 保留历史日志文件数量 |
| `AUDIO_LOG_CONSOLE` | `1` | 是否保留控制台日志（`0` 仅写文件） |
| `AUDIO_SESSION_LOG_ENABLED` | `1` | 是否启用单次会话独立日志文件 |
| `AUDIO_SESSION_LOG_DIR` | `data/logs/sessions` | 会话日志目录 |
| `AUDIO_SESSION_LOG_LAYOUT` | `date` | `date` 按日期建子目录，`flat` 直接平铺 |
| `AUDIO_SESSION_LOG_FILE_TEMPLATE` | `session-{sid}.log` | 会话文件名模板，支持 `{sid}` |
| `AUDIO_SESSION_LOG_LEVEL` | 跟随 `LOG_LEVEL` | 会话日志级别 |

## 常见配置错误

| 报错 | 原因 | 修法 |
| --- | --- | --- |
| `COZE_WORKFLOW_ID 必须是纯数字 ID，收到的是名称` | 把工作流**名字**当 ID 填了 | 跑 `--list`，或用 console 地址栏的 `workflow_id` 参数 |
| `code 4000 workflow_id is invalid` | ID 不存在 | 同上 |
| `code 4200` | 工作流存在但**未发布** | console 里点「发布」 |
| `code 4100 access_token required` | token 缺失/过期 | 重新生成 PAT |
| `code 4028 Insufficient coze credits balance` | Coze API 积分不足；与 Bot 免费消息次数不是同一额度 | 核对当前 `COZE_API_TOKEN` 所属账号 / 空间的开放平台 API 积分；冷却后需手动再次发起语音会话，服务不会自动重试 |
