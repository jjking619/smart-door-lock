# 智能门锁

基于 Quectel Pi M2 智能主控板的智能门锁示例，支持摄像头预览与录像、
人员和包裹检测、指纹开锁及 Coze 语音应答。

## 1. 准备

- Quectel Pi M2（RK3576，ARM64）、Debian 13 桌面系统（系统 Python 3.13）、
  网络、显示器、键鼠及 USB 摄像头；安装需要普通用户的 sudo 权限和足够磁盘空间。
- 指纹开锁还需 AS608 指纹模块；实际开锁需 SG90 舵机及独立稳压 5V 电源；
  语音需麦克风、扬声器和已发布的 Coze 智能体。可以先不接舵机，用模拟锁体验证。
- 确认可访问 GitHub、软件源和 Coze；安装脚本会联网下载依赖及 RKNN Lite。
  RKNN Lite 2.3.2 与系统 `librknnrt` 须兼容。

## 2. 下载并安装

在板子的桌面终端中，以**普通用户**运行（不要对整个脚本使用 sudo）：

```bash
git clone https://github.com/jjking619/smart-door-lock.git
cd smart-door-lock
bash install.sh
```

出现 `Deployment complete` 表示安装完成。脚本创建后端 Python 3.11 的 `.venv`
和界面 Python 3.13 的 `.venv-ui`，不修改系统默认 Python；还会安装系统依赖、
CH340/CH341 串口驱动、Pin33 PWM 开机授权服务，并将当前用户加入 `dialout`。
这些步骤会修改系统，但不会启动门锁或输出舵机波形。**安装后注销并重新登录**，
使串口组权限生效；重新打开终端后进入克隆的项目目录，以下命令均从项目根目录执行。

如需 GPIO 继电器权限，使用 `bash install.sh --gpio`；默认不开放 GPIO。
如果驱动缺失且内核头文件不匹配，按报错安装与当前内核匹配的头文件，或设置
`KERNEL_HEADERS` 后重新运行；内核升级后可运行 `bash deploy/install_ch341_driver.sh`。

仓库包含 RK3576 人员模型 `models/yolov5s-rk3576.rknn` 和包裹模型
`models/parcel-yolov5n-rk3576-fp.rknn`。人员模型缺失时安装脚本尝试从系统镜像
复制，不覆盖已有文件；两个模型都应按下文检查。

## 3. 配置

安装时会从 [`doorlock.env.example`](deploy/systemd/doorlock.env.example)
创建 `deploy/systemd/doorlock.env`，已有文件不会被覆盖。编辑该文件：

```bash
nano deploy/systemd/doorlock.env
```

- 首次验证将 `LOCK_DRIVER` 改为 `mock`，避免实际驱动锁体。
- 语音需要填写 `COZE_API_TOKEN` 和已发布智能体的 `COZE_BOT_ID`，保持
  `COZE_MODE=streaming`；按需填写 `COZE_VOICE_ID`。留空音频设备号时使用系统默认设备，
  可用 `.venv/bin/python -m sounddevice` 查看设备。没有配置语音时可先用 `--no-audio`。
- 默认 `VISION_SOURCE=auto` 自动发现摄像头；多个摄像头时可改为实际的
  `/dev/v4l/by-id/...-video-index0` 路径。指纹串口自动扫描，未找到时将
  `FINGERPRINT_SERIAL` 设为实际串口路径。
- 按摄像头实际画面调整 `VISION_DOORSTEP_POLY`、`VISION_GROUND_POLY`。
  两项数据目录默认均为相对项目目录的 `data`；从其他设备复制配置时检查绝对路径。
- `VISION_TOKEN` 默认留空，而视觉服务可从网络访问；需要远程访问时设置令牌并限制网络
  访问。HTTP 未加密，不应直接暴露到公网。

配置含密钥，不要提交或分享；确保权限为 `chmod 600 deploy/systemd/doorlock.env`。
桌面入口**不会自动加载**该文件，下面的启动命令会显式加载。只加载自己信任、符合
systemd EnvironmentFile 和 shell 语法的配置，不要在配置中使用 shell 命令替换。

## 4. 接线与首次启动

连接摄像头、显示器和所需音频设备。AS608 的串口 TX/RX 使用 3.3V TTL 电平并交叉连接，
确保模块供电符合其规格并与主控共地；USB 转串口设备需要 `dialout` 权限。
SG90 暂不连接机械负载。详细接线及调试参见[使用说明](docs/manual.md)。

在桌面终端中运行：

```bash
ls -lh models/yolov5s-rk3576.rknn models/parcel-yolov5n-rk3576-fp.rknn
set -a
. ./deploy/systemd/doorlock.env
set +a
LOCK_DRIVER=mock .venv/bin/python main.py
```

启动器会启动视觉、音频、指纹和锁体服务及桌面界面；若没有语音配置，在最后一行
追加 `--no-audio`。关闭界面会停止由本次启动器创建的服务。已独立运行的服务可能被复用，
切换 `LOCK_DRIVER` 前先停止旧的 lockd；不要同时用不同方式启动同一服务。
终端退出或重新登录后需重新加载配置。

如果没有包裹模型，可先使用人员模型运行，但包裹识别能力不同。加载配置后执行：

```bash
VISION_PACKAGE_MODEL="" VISION_PACKAGE_CLASSES="suitcase,backpack,handbag" \
  LOCK_DRIVER=mock .venv/bin/python main.py
```

上述类别覆盖是必要的：配置模板中的默认类别对应专用包裹模型。

## 5. 验证

保持界面打开，在另一个终端检查：

```bash
curl -fsS http://127.0.0.1:8083/health
curl -fsS http://127.0.0.1:8084/health  # 使用 --no-audio 时不运行
curl -fsS http://127.0.0.1:8085/health
curl -fsS http://127.0.0.1:8086/health
```

- 视觉健康信息应显示 `detector: "rknn"`；双模型模式下
  `package_detector` 也应为 `"rknn"`，且对应错误字段为空。界面预览应能看到画面；
  默认没有界面、预览或录像时视觉服务会暂停并释放相机，这是正常现象。
- 锁体健康信息中 `driver` 对象的 `driver` 字段应为 `mock`；在界面测试开锁只验证模拟链路，不会开门。
- 指纹服务的健康信息可用于检查模块连接；通过界面录入并识别指纹，检查记录页。
- 语音服务检查 `talk_available` / `talk_error`，再在界面发起对讲，实际验证
  麦克风、Coze 应答和扬声器。健康接口成功不代表云端额度或整个语音链路可用。
- 观察人员和包裹事件、录像与 `data/` 中的记录；检测精度和区域需现场调试。

## 6. 使用实际舵机

先停止模拟运行，再接线并在无机械负载时校准：SG90 信号线接 M2 **物理 Pin33**，
地线接 GND，电源线接独立稳压 5V 电源，电源与 M2 共地；不要用 GPIO/3.3V 给舵机供电。
按实际结构调整 `LOCK_SERVO_LOCKED_ANGLE` 和 `LOCK_SERVO_UNLOCKED_ANGLE`，
确认不会顶住限位或堵转。将 `LOCK_DRIVER` 改为 `servo`，重新加载配置后按第 4 节启动，
检查锁体健康信息及实际开锁、回锁。调试步骤见[舵机说明](docs/manual.md#sg90-舵机开锁物理-pin33)。

SG90 没有位置反馈，断电或进程崩溃时软件无法保证回锁，须保留机械应急开门方式；
不要仅依赖软件保障人身安全。

## 7. 可选：systemd 后台服务

安装脚本只生成服务文件，不会自动安装或启动它们。先退出桌面启动器及同端口的旧服务，
并在配置中确认 `LOCK_DRIVER=mock` 或已经安全校准的 `servo`。从项目根目录执行：

```bash
mkdir -p data
python3 deploy/generate_systemd.py
sudo install -m 644 deploy/systemd/generated/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now visiond audiood lockd fingerprintd
sudo systemctl status visiond audiood lockd fingerprintd
```

只需部分功能时可只启用所需服务。服务读取同一个 `doorlock.env`；
修改后需重启受影响的服务，例如 `sudo systemctl restart audiood`。
查看日志：`sudo journalctl -u visiond -u audiood -u lockd -u fingerprintd -n 100 --no-pager`。
启动后台服务后，桌面终端可加载同一配置，再运行 `.venv/bin/python main.py --ui-only`；
界面关闭不会停止 systemd 服务。移动项目目录或更换运行用户后重新生成并安装服务文件，
避免与用户级同名服务同时启用。详见[部署说明](docs/architecture.md#7-部署)。

## 注意事项

- 如需无界面时持续检测，将 `VISION_PAUSE_WITHOUT_UI=0`，并重启视觉服务。
- 指纹管理无需 PIN；录像和事件保存在 `data/`，请注意访问权限、磁盘空间和备份。
- 本 README 的安装成功不代表真实硬件、网络、模型精度或 Coze 配额已验证。

更多内容：[视觉服务](src/vision/README.md) · [语音服务](src/audio/README.md) ·
[界面运行环境](docs/ui-runtime.md) · [设计方案](docs/design.md)
