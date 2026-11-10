# 智能门锁（Quectel Pi M2 / RK3576）

**目前实现四项功能**：摄像头录制与在线查看、入侵检测、快递识别、语音对讲。
不依赖锁体和 MCU，可先跑通再上机械结构。

![方案](docs/design.md)

## 功能现状

| 功能 | 状态 | 目录 | 验证方式 |
| --- | --- | --- | --- |
| 视频录制 / 在线查看 | ✅ 可用 | `src/vision/` | `/stream` MJPEG、`/media/...` 下载 mp4 |
| 入侵检测 | ✅ 可用（需 RKNN 模型） | `src/vision/rules.py` | 事件 JSONL + 快照 + 片段 |
| 快递识别 | ✅ 双模型推理与事件规则可用，需现场验证准确率 | `src/vision/rules.py` | `package.detected` 事件 |
| 语音对讲 / 自动应答 | ✅ 链路通（需 Coze token） | `src/audio/` | 按门铃 → ASR → Coze → TTS 播报 |

## 快速开始

桌面系统统一入口（使用项目已有虚拟环境）：

```bash
cd /home/pi/smart-door-lock
.venv/bin/python main.py
```

默认 `--source auto`：每次打开或重连摄像头时都会扫描 `/dev/v4l/by-id/*-video-index0`，
因此摄像头从 `/dev/video73` 变成其他编号、或热插拔后都能自动找回。使用 RK3576 人员和包裹 NPU 模型，在 8083 启动视觉后端，
等待检测器和摄像头帧就绪后打开界面。关闭界面会停止本次启动的后端并释放设备。
重复运行会安全替换同一项目的旧桌面实例：先请求旧启动器退出并清理子进程，再启动新实例。
独立启动且健康的视觉后端会被复用；没有画面的旧后端会先退出再重启。
父进程信息无权读取时会输出警告并保留父进程，不影响对已确认身份的视觉后端进行复用或重启。
端口被其他程序占用时不会结束该进程，而是报错退出；由 systemd 管理但未就绪的后端（如摄像头正在重连）会先等待最多 30 秒（`DOORLOCK_MANAGED_BACKEND_WAIT`），
仍未就绪才报错，并按服务实际所属的管理器给出 `systemctl --user restart visiond.service`（用户级）或 `sudo systemctl restart visiond.service`（系统级）。
包裹模型文件需保存在 `models/parcel-yolov5n-rk3576-fp.rknn`；通过 `/health` 检查两路后端状态。
包裹模型的实际门口误报、漏报仍需现场验证，事件显示在界面并可选发布 MQTT，不含手机推送。

桌面入口会自动启动本机指纹服务（`127.0.0.1:8085`），该服务独占 AS608 串口。
默认依次扫描 `/dev/serial/by-id/`、`/dev/serial/by-path/`，再检查树莓派板载 UART `/dev/serial0`、`/dev/serial1`；
若未自动找到模块，设置 `FINGERPRINT_SERIAL` 为实际设备路径（直连板载 UART 通常使用 `/dev/serial0`）。
录入、改名、删除无需 PIN，界面上任何人都可直接操作；指纹服务只监听本机 `127.0.0.1`。
指纹模板仅存于 AS608，项目只在 `data/fingerprint/users.json` 保存 ID、姓名和手指备注；
识别记录追加到 `data/events.jsonl`。
部分 AS608 固件高速搜索会返回手册未定义的 `0x17`；此时保留当前指纹特征，
将库内模板逐个加载到另一缓冲区比对，仅在模块明确返回匹配成功时记录身份。
逐模板比对会随模板数量增加而变慢；其他通信或模板读取错误仍会明确报错，不会当作匹配成功。
UART TX/RX 必须使用 3.3V TTL 电平；运行用户需要对串口设备有读写权限（常见为 `dialout` 组）。
Quectel Pi M2（RK3576）出厂内核未编译 CH340/CH341 驱动，CH340 USB-TTL 插上后不会出现 `/dev/ttyUSB0`；
执行 `bash deploy/install_ch341_driver.sh`（需要 sudo）编译并安装驱动，开机自动加载，内核升级后需重新执行。
桌面启动时，`FINGERPRINT_SERIAL` 需由启动桌面的 shell/服务提供（自动发现失败时才需要）。
开发所需依赖：`uv pip install --python .venv/bin/python -e '.[fingerprint]'`。
systemd 部署可使用 `deploy/systemd/fingerprintd.service` 与
`deploy/systemd/fingerprintd.env.example`；桌面入口发现 8085 上已有 fingerprintd 时会复用它。

### 指纹开锁（lockd）

指纹识别与开锁分成两个进程：`fingerprintd` 只负责识别，`lockd`（`127.0.0.1:8086`）负责
"这个人能不能开、开多久"。识别成功但未登记、分数不够、重复按压都会被拒绝并记入
`data/events.jsonl`，界面会直接显示拒绝原因。

默认指纹策略（都在 `deploy/systemd/lockd.env.example` 里可改）：

| 策略 | 默认 | 说明 |
| --- | --- | --- |
| 只有已登记用户能开门 | 是 | AS608 里可能有别人手工录入的裸模板 |
| 匹配度门槛 | ≥ 60 | 挡住偶发误识 |
| 同人防重复 | 5 s | 手指没抬起会被反复匹配 |
| 连续失败锁定 | 5 次 → 60 s | 防止蹲门口逐个试 |
| 开锁时长 | 5 s | 到时自动回锁 |
| 开锁存证 | 抓拍一张 | 存到 `data/snapshots/YYYYMMDD/*_unlock.jpg` |

锁体驱动（`LOCK_DRIVER`）：

* `mock`：没有锁体时用，界面、策略、事件链都能完整跑通（需显式设置）；
* `gpio`：Pi 直接驱动继电器。**需要先执行 `sudo bash deploy/install_gpio_access.sh`**，
  因为 `/dev/gpiochip*` 默认只有 root 可读，`pi` 会直接 `Permission denied`；
* `mcu`：接 ESP32-S3 执行单元，走 HMAC 签名帧（`src/lock/protocol.py`）。
* `servo`：M2 物理 Pin33 的硬件 PWM 直接控制 SG90；默认 0° 回锁、
  90° 开锁，保持 5 秒后自动回到 0°（当前默认）。

#### SG90 舵机开锁（物理 Pin33）

接线：橙/黄信号线接 **物理 Pin33**，棕线接 GND（如物理 Pin34），红线接
合适的独立稳压 5V 电源；电源 GND 与 M2 共地。不要用 GPIO/3.3V 给舵机供电。
先卸下机械负载校准角度，防止顶住限位、堵转或损坏舵机。

根据 [M2 官方 40-Pin 文档](https://docs.quectel.com/zh/Quectel%20Pi/Quectel%20Pi%20M2/使用指南/40‑Pin扩展/40‑Pin扩展.html)，
Pin33 是 GPIO2_D3 / PWM2_CH3，对应控制器 **`2ade3000.pwm`**。
驱动按控制器地址查找 `pwmchipN/pwm0`，不是 `pwm3`，也不依赖 `pwmchip2` 编号。
SG90 使用 **50Hz**（周期 20,000,000ns），默认 0° 为 500µs、90° 为 1500µs；
官方文档的 1000Hz/50% 占空比是回环测试，不能用来控制舵机。

PWM sysfs 默认只有管理员可写，**首次使用必须执行一次** `sudo bash deploy/install_pwm_access.sh pi`，
安装开机授权服务，只预先导出 Pin33 对应的 `pwm0` 并授权四个控制属性，
不会输出波形，不会开放所有 PWM 或 GPIO。若受沙箱限制，需先由管理员调整沙箱策略。
安装完成后，正常用户直接运行 `.venv/bin/python main.py` 即可（无需以管理员身份运行整个程序）；
也可以使用下面的入口：

```bash
bash deploy/run_servo_doorlock.sh
# 仅运行开锁服务，供已有桌面/指纹服务调用：
bash deploy/run_servo_doorlock.sh --lock-only
```

`main.py` 与此入口都默认使用 `LOCK_DRIVER=servo`；无硬件调试时设置 `LOCK_DRIVER=mock`。
系统服务部署则将 `/etc/doorlock/lockd.env` 的 `LOCK_DRIVER` 改为 `servo`，
其他参数参考 `deploy/systemd/lockd.env.example`。切换驱动前须停止旧的 lockd，
避免桌面入口复用旧的模拟服务。
可通过 `LOCK_SERVO_LOCKED_ANGLE`、`LOCK_SERVO_UNLOCKED_ANGLE`、
`LOCK_SERVO_MIN_US`、`LOCK_SERVO_MAX_US` 和 `LOCK_SERVO_MOVE_S` 校准；
保持时长沿用 `LOCK_PULSE_S`。退出时先回到回锁位置，再停止 PWM。
启动时会先回锁并检查 PWM 写入权限；缺少控制器、权限不足或回锁失败时 lockd 仍保持在线，
每次开锁都会在界面显示具体原因（例如提示执行授权脚本），不会退回模拟模式或报告开锁成功。

指纹开锁仍校验登记、匹配分数、防重复和失败锁定；本机界面两次点击确认开锁
不需要指纹 ID 或匹配分数，但仍受防重复和锁定限制。
`/unlock` 同步等待完整开锁/回锁周期，客户端超时为 15 秒；增加保持时长时也应相应调整客户端超时。
`/health` 的 `driver` 应为 `servo`；这只是指令状态，SG90 没有位置反馈，
没有门磁时软件不能确认门是否实际关闭，掉电/进程崩溃也不能保证机械回锁。

`LOCK_GPIO_ACTIVE_LOW=1` 适配低电平触发的继电器。
接线建议在继电器上串一个 555 单稳限时器：Pi 死机时继电器可能停在吸合状态，
锁体是"断电即锁"的话门会一直开着。
`FINGERPRINT_UNLOCK=0` 可关掉指纹触发开锁，只保留识别和记录。

```bash
.venv/bin/python main.py --kiosk
# 多个摄像头时固定某一个（使用 by-id 稳定路径，不要写 /dev/videoN）
.venv/bin/python main.py --source /dev/v4l/by-id/usb-...-video-index0
.venv/bin/python main.py --backend-only
.venv/bin/python main.py --ui-only --base http://127.0.0.1:8083
.venv/bin/python main.py --audio-only
.venv/bin/python main.py --fingerprint-only
.venv/bin/python main.py --lock-only
# 调试时不拉起任何开锁服务
.venv/bin/python main.py --no-lock
```

启动检查由每端口文件锁串行化，锁文件位于 `data/run/`，不用手动删除。
自动退出前会核对监听端口、进程用户、项目入口路径和进程启动时间，
通过 Linux pidfd 发信号，避免 PID 被重用时误杀其他程序；先发送 SIGTERM，
等待旧桌面清理子进程（最多 30 秒），独立后端最多等待 8 秒，超时才发送 SIGKILL。
其他项目、其他用户、无法确认身份的进程不会被自动终止。
systemd 管理的健康后端会继续复用；异常服务会提示通过服务管理器重启，
避免与 `Restart=always` 反复争抢端口。服务环境配置应使用 `VISION_SOURCE=auto`。
`--ui-only` 只连接界面，不会替换后端。自动恢复需要允许读取本用户的 `/proc` 信息；
无权限或内核不支持 pidfd 时会明确报错，不会退回按端口盲目杀进程。

语音模式仍使用终端输入模拟门铃，暂不自动随桌面启动。SSH 启动 GUI 需要有效的
`DISPLAY` / `XAUTHORITY`；启动器不会修改系统显示授权。可从任意目录使用绝对入口路径：
`/home/pi/smart-door-lock/.venv/bin/python /home/pi/smart-door-lock/main.py`。

依赖统一由 `pyproject.toml` 管理。基础安装包含桌面与视觉功能；NPU 和语音依赖
按需通过 extras 安装。NPU runtime 需匹配板端驱动。

```bash
sudo apt install -y python3-opencv libopenblas-dev \
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

cd /home/pi/smart-door-lock
uv venv .venv
uv pip install --python .venv/bin/python -e '.[npu,audio,fingerprint]'

# 1) 无硬件自测：合成画面跑通录像/缓冲/事件链路
.venv/bin/python -m vision.selftest

# 运行完整单元测试（启动器、检测器与规则）
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v

# 2) 接 USB 摄像头
.venv/bin/python -m vision.app
#    浏览器打开 http://<板子IP>:8081/stream

# 3) 语音：先验证 Coze 连通性
export COZE_API_TOKEN=pat_xxx
export COZE_WORKFLOW_ID=7466xxxx
.venv/bin/python -m audio.coze_smoke_test "东西放门口了"
.venv/bin/python -m audio.app
```

## 目录

```
docs/design.md          完整方案（硬件 BOM、供电、安全清单、里程碑）
main.py                统一启动入口
tests/                 启动器、检测器与规则测试
src/vision/             录像 + 入侵检测 + 快递识别
src/ui/                 桌面界面
src/audio/              门铃对讲 + Coze 自动应答
src/application.py      统一启动编排
device/                 旧 python -m device.* 命令的仓库内兼容包
tests/                  启动器、检测器与规则测试
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
